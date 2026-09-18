"""Normalized ``sec_filings`` store with point-in-time queries.

One row per accession, linked to raw archive paths; ``amendment_of``
links an amendment to its prior filing. ``root`` is the DATA root
(parquet rows go to ``root/'parquet'``; defaults to the warehouse root).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .. import normalization
from ..domain.market import ids
from ..storage import duckdb, raw_archive
from .cusip import normalize_cusip, normalize_isin
from .models import (
    BeneficialOwnership,
    EntityCandidate,
    Filing,
    FilingDocument,
    FilingParty,
    InsiderTransaction,
    InstitutionalHolding,
    Offering,
    SearchAttempt,
    SECSearchRequest,
    SECTextHit,
    Transaction,
)

if TYPE_CHECKING:
    from ..domain.evidence.relationships import (
        RelationshipEvidence,
        RelationshipRevision,
    )

PARSER_VERSION = "1"

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_date(value: object, field: str) -> str:
    text = str(value or "")
    if not _AS_OF_RE.match(text):
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{field} must be YYYY-MM-DD, got {value!r}") from None
    return text


def _validate_as_of(as_of: str) -> str:
    return _validate_date(as_of, "as_of")


def _filing_identity(filing: Filing) -> dict[str, object]:
    """Filing identity (accession, form, filer, subject) as parquet columns."""
    return {
        "accession": filing.accession_no,
        "form": filing.form,
        "cik": str(filing.filer_cik),
        "company": filing.filer_name,
        "filer_cik": str(filing.filer_cik),
        "filer_name": filing.filer_name,
        "subject_cik": str(filing.subject_cik) if filing.subject_cik is not None else None,
        "subject_name": filing.subject_name,
        "issuer_cik": str(filing.subject_cik) if filing.subject_cik is not None else None,
    }


def _filing_linkage(
    filing: Filing,
    amendment_of: str | None,
) -> dict[str, object]:
    """Filing dates/documents/amendment linkage as parquet columns."""
    return {
        "filed_at": filing.filed_at,
        "accepted_at": filing.accepted_at,
        "known_at": filing.known_at,
        "report_period": filing.report_period,
        "primary_document": filing.primary_document,
        "is_amendment": filing.is_amendment,
        "amendment_of": amendment_of if amendment_of is not None else filing.amendment_of,
        "source_url": filing.source,
    }


def _filing_provenance(
    canonical: bytes,
    raw_submission_path: Path | str | None,
    raw_primary_path: Path | str | None,
    retrieved_at: str | None,
) -> dict[str, object]:
    """Filing provenance (raw paths, retrieved_at, hash, parser) as columns."""
    return {
        "raw_submission_path": str(raw_submission_path) if raw_submission_path is not None else None,
        "raw_primary_path": str(raw_primary_path) if raw_primary_path is not None else None,
        "retrieved_at": retrieved_at or _utcnow(),
        "content_hash": raw_archive.content_hash(canonical),
        "parser_version": PARSER_VERSION,
    }


def store_filing(
    filing: Filing,
    *,
    amendment_of: str | None = None,
    raw_submission_path: Path | str | None = None,
    raw_primary_path: Path | str | None = None,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one normalized filing row; returns rows written (0 on rerun)."""
    canonical = json.dumps(filing.to_dict(), sort_keys=True).encode("utf-8")
    row: dict[str, object] = {
        **_filing_identity(filing),
        **_filing_linkage(filing, amendment_of),
        **_filing_provenance(canonical, raw_submission_path, raw_primary_path, retrieved_at),
    }
    return duckdb.insert_ignore("sec_filings", [row], data_root=_duckdb_root(root))


def _filings_identity_where(
    cik: int | str | None,
    accession: str | None,
    forms: list[str] | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Filings identity filters (CIK, accession, form list)."""
    if cik is not None:
        where.append("cik = ?")
        params.append(str(cik))
    if accession is not None:
        where.append("accession = ?")
        params.append(accession)
    if forms:
        where.append(f"form IN ({', '.join(['?'] * len(forms))})")
        params.extend(forms)


def _filings_date_where(
    start_date: str | None,
    end_date: str | None,
    as_of: str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Filings date filters (filed window plus PIT as_of)."""
    if start_date is not None:
        where.append("substr(CAST(filed_at AS VARCHAR), 1, 10) >= ?")
        params.append(_validate_date(start_date, "start_date"))
    if end_date is not None:
        where.append("substr(CAST(filed_at AS VARCHAR), 1, 10) <= ?")
        params.append(_validate_date(end_date, "end_date"))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)


def query_filings(
    *,
    cik: int | str | None = None,
    accession: str | None = None,
    forms: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    as_of: str | None = None,
    limit: int | None = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Filings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _filings_identity_where(cik, accession, forms, where, params)
    _filings_date_where(start_date, end_date, as_of, where, params)
    sql = "SELECT * FROM sec_filings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY known_at DESC, retrieved_at DESC, content_hash DESC"
    if limit is not None:
        sql += f" LIMIT {limit}"
    return duckdb.query(sql, params, data_root=_duckdb_root(root))


# --- Phase 4: parties, document text + FTS, search ledgers, coverage, checkpoints ---

FTS_TABLE = "document_text_fts"
FTS_ID = "fts_id"




class _HasToDict(Protocol):
    def to_dict(self) -> Mapping[str, object]: ...


def _duckdb_root(root: Path | str | None) -> Path | None:
    """Warehouse DB root for ``duckdb.query`` (which takes ``Path`` only)."""
    return Path(root) if root is not None else None


def _list_or_none(value: object) -> list[object] | None:
    """``list(value)`` for iterable inputs, ``None`` for ``None``.

    Non-iterable, non-None inputs raise ``TypeError``, matching ``list(value)``.
    """
    if value is None:
        return None
    if not isinstance(value, Iterable):
        raise TypeError(f"expected an iterable or None, got {type(value).__name__}")
    return list(value)


def _party_urls(
    d: dict[str, object],
    source_url: str | None,
    raw_archive_path: Path | str | None,
    document_name: str | None,
) -> dict[str, object]:
    """Filing-party URL/path/name overrides with input fallbacks."""
    return {
        "source_url": source_url or d.get("source_url"),
        "raw_archive_path": str(raw_archive_path) if raw_archive_path is not None else d.get("raw_archive_path"),
        "document_name": document_name or d.get("document_name"),
    }


def _party_hash(d: dict[str, object], now: str) -> dict[str, object]:
    """Filing-party known/retrieved/hash/parser provenance."""
    return {
        "known_at": d.get("known_at") or now,
        "retrieved_at": now,
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _as_dict(value: Mapping[str, object] | _HasToDict) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    return dict(value.to_dict())


def _json(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _pass_through(dataset: str, row: Mapping[str, object], root: Path | str | None = None) -> int:
    """Minimal nullable writer: fills provenance/hash/parser, returns rows written."""
    d: dict[str, object] = dict(row)
    now = d.get("retrieved_at") or _utcnow()
    d["retrieved_at"] = now
    d["known_at"] = d.get("known_at") or now
    d["parser_version"] = d.get("parser_version") or PARSER_VERSION
    if not d.get("content_hash"):
        d["content_hash"] = raw_archive.content_hash(json.dumps(d, sort_keys=True, default=str).encode("utf-8"))
    return duckdb.insert_ignore(dataset, [d], data_root=_duckdb_root(root))


def _party_identity(
    d: dict[str, object],
    filed_at: str | None,
) -> dict[str, object]:
    """Filing-party identity (accession, role, entity/CIK, name, source)."""
    return {
        "accession": d.get("accession_no") or d.get("accession"),
        "role": d.get("role"),
        "entity_id": d.get("entity_id"),
        "cik": str(d["cik"]) if d.get("cik") is not None else None,
        "name": d.get("name"),
        "source": d.get("source"),
        "filed_at": filed_at or d.get("filed_at"),
    }


def _party_provenance(
    d: dict[str, object],
    now: str,
    source_url: str | None,
    raw_archive_path: Path | str | None,
    document_name: str | None,
) -> dict[str, object]:
    """Filing-party provenance (known/retrieved, URLs, hash, parser)."""
    return {
        **_party_urls(d, source_url, raw_archive_path, document_name),
        **_party_hash(d, now),
    }


def _party_row(
    d: dict[str, object],
    now: str,
    source_url: str | None,
    raw_archive_path: Path | str | None,
    document_name: str | None,
    filed_at: str | None,
) -> dict[str, object]:
    """Filing-party input dict to a parquet row (hash filled by caller)."""
    return {
        **_party_identity(d, filed_at),
        **_party_provenance(d, now, source_url, raw_archive_path, document_name),
    }


def _hash_party_row(row: dict[str, object], d: dict[str, object]) -> None:
    """Fill a missing party content hash from the canonical input dict."""
    if not row["content_hash"]:
        row["content_hash"] = raw_archive.content_hash(json.dumps(d, sort_keys=True, default=str).encode("utf-8"))


def store_filing_party(
    party: FilingParty | Mapping[str, object],
    *,
    source_url: str | None = None,
    raw_archive_path: Path | str | None = None,
    document_name: str | None = None,
    filed_at: str | None = None,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one filing-party row (accession + role + entity/CIK key)."""
    d = _as_dict(party)
    now = retrieved_at or _utcnow()
    row = _party_row(d, now, source_url, raw_archive_path, document_name, filed_at)
    _hash_party_row(row, d)
    return duckdb.insert_ignore("filing_parties", [row], data_root=_duckdb_root(root))


def query_parties(
    *,
    accession: str | None = None,
    cik: int | str | None = None,
    role: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Parties newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("accession", accession, where, params)
    _cik_where("cik", cik, where, params)
    _simple_where("role", role, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("filing_parties", where, params, "ORDER BY known_at DESC", limit, root)


def _document_row(
    doc_id: str,
    text: str | bytes,
    now: str,
    accession: str | None,
    document_name: str | None,
    source_url: str | None,
    raw_archive_path: Path | str | None,
    source_content_hash: str | None,
    source_representation: str | None,
    location: str | None,
    file_type: str | None,
    filed_at: str | None,
    known_at: str | None,
) -> tuple[dict[str, object], str, str, str]:
    """Document input to (row, text_value, content_hash, known_at)."""
    payload = text.encode("utf-8") if isinstance(text, str) else text
    text_value: str = payload.decode("utf-8", errors="replace")
    content_value: str = raw_archive.content_hash(payload)
    known_value: str = known_at or filed_at or now
    return (
        {
            "doc_id": doc_id,
            "content_hash": content_value,
            "accession": accession,
            "document_name": document_name,
            "text": text_value,
            "source_url": source_url,
            "raw_archive_path": str(raw_archive_path) if raw_archive_path is not None else None,
            "source_content_hash": source_content_hash,
            "source_representation": source_representation,
            "location": location,
            "file_type": file_type,
            "filed_at": filed_at,
            "known_at": known_value,
            "retrieved_at": now,
            "parser_version": PARSER_VERSION,
        },
        text_value,
        content_value,
        known_value,
    )


def _dividend_filing_cik(
    accession: str,
    root: Path | str | None,
) -> dict[str, object] | None:
    """Filing row (filer_cik, filed_at) for dividend attribution; None when unusable."""
    filings = duckdb.query(
        "SELECT filer_cik, filed_at FROM sec_filings WHERE accession = ? LIMIT 1",
        [accession],
        data_root=_duckdb_root(root),
    )
    if not filings or not filings[0].get("filer_cik"):
        return None
    return filings[0]


def _persist_text_dividends(
    filing: dict[str, object],
    accession: str,
    source_url: str,
    text_value: str,
    content_value: str,
    filed_at: str | None,
    known_value: str,
    root: Path | str | None,
) -> None:
    """Extract dividend events for one filing row and persist them."""
    cik = int(str(filing["filer_cik"]))
    raw_filed: object = filing.get("filed_at") or filed_at
    filed = str(raw_filed) if raw_filed is not None else known_value
    events = normalization._extract_dividend_events_from_text(
        text_value,
        cik=cik,
        entity_id=ids.sec_entity_id(cik),
        security_id=ids.sec_security_id(cik),
        accession=accession,
        filed_at=filed,
        source_url=source_url,
        content_hash=content_value,
    )
    for event in events:
        event["known_at"] = known_value
    if events:
        duckdb.insert_ignore("dividend_events", events, data_root=_duckdb_root(root))


def _extract_text_dividends(
    accession: str | None,
    source_url: str | None,
    text_value: str,
    content_value: str,
    filed_at: str | None,
    known_value: str,
    root: Path | str | None,
) -> None:
    """Best-effort dividend events from stored text; never fails the store."""
    try:
        if not (accession and source_url is not None):
            return
        filing = _dividend_filing_cik(accession, root)
        if filing is None:
            return
        _persist_text_dividends(filing, accession, source_url, text_value, content_value, filed_at, known_value, root)
    except Exception as exc:  # never fail the text store  # noqa: BLE001 - dividend text persistence is best-effort, failure keeps the filing record
        logging.getLogger(__name__).debug("dividend text extraction skipped: %s", exc)


def store_document_text(
    doc_id: str,
    text: str | bytes,
    *,
    accession: str | None = None,
    document_name: str | None = None,
    source_url: str | None = None,
    raw_archive_path: Path | str | None = None,
    source_content_hash: str | None = None,
    source_representation: str | None = None,
    location: str | None = None,
    file_type: str | None = None,
    filed_at: str | None = None,
    known_at: str | None = None,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    now = retrieved_at or _utcnow()
    row, text_value, content_value, known_value = _document_row(
        doc_id,
        text,
        now,
        accession,
        document_name,
        source_url,
        raw_archive_path,
        source_content_hash,
        source_representation,
        location,
        file_type,
        filed_at,
        known_at,
    )
    written = duckdb.insert_ignore("document_text", [row], data_root=_duckdb_root(root))
    _extract_text_dividends(accession, source_url, text_value, content_value, filed_at, known_value, root)
    return written


def query_document_text(
    *,
    doc_id: str | None = None,
    accession: str | None = None,
    document_name: str | None = None,
    as_of: str | None = None,
    limit: int = 50,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Document texts newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("doc_id", doc_id, where, params)
    _simple_where("accession", accession, where, params)
    _simple_where("document_name", document_name, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query(
        "document_text", where, params, "ORDER BY known_at DESC, retrieved_at DESC, content_hash DESC", limit, root
    )


@runtime_checkable
class _DuckDBConnection(Protocol):
    """Structural seam: DuckDB connection or execute/fetch test double."""

    @property
    def description(self) -> object: ...
    def execute(self, query: str, parameters: object = None) -> _DuckDBConnection: ...
    def fetchall(self) -> Sequence[object]: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def close(self) -> object: ...


@runtime_checkable
class _FTSRowsConnection(Protocol):
    """Minimal seam for BM25 row mapping: execute/fetchall/description only."""

    @property
    def description(self) -> object: ...
    def execute(self, query: str, parameters: object = None) -> _FTSRowsConnection: ...
    def fetchall(self) -> Sequence[object]: ...


def _load_fts_extension(conn: object) -> None:
    """LOAD the FTS extension, installing once when missing; else raises."""
    if not isinstance(conn, _DuckDBConnection):
        raise RuntimeError(  # noqa: TRY004 - local-text source failure is always RuntimeError, connection type included
            "local-text source failed: FTS extension unavailable: unsupported connection"
        )
    try:
        conn.execute("LOAD fts")
        return
    except Exception:  # noqa: BLE001, S110 - FTS LOAD falls back to INSTALL+LOAD, the first failure is silent
        pass
    try:
        conn.execute("INSTALL fts")
        conn.execute("LOAD fts")
    except Exception as exc:
        raise RuntimeError(f"local-text source failed: FTS extension unavailable: {exc}") from exc


def _materialize_fts_count(conn: _DuckDBConnection) -> int:
    """Materialize document_text to the FTS table; returns indexed count."""
    conn.execute(
        f"CREATE OR REPLACE TABLE {FTS_TABLE} AS "
        "SELECT (doc_id || '#' || content_hash) AS fts_id, "
        "doc_id, content_hash, text FROM document_text"
    )
    count_row = conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()
    if not isinstance(count_row, (tuple, list)) or not count_row:
        raise RuntimeError("local-text source failed: FTS count unavailable")
    count = count_row[0]
    if isinstance(count, bool) or not isinstance(count, (int, float, str)):
        raise RuntimeError(  # noqa: TRY004 - local-text source failure is always RuntimeError, count shape included
            "local-text source failed: FTS count unavailable"
        )
    conn.execute(
        f"PRAGMA create_fts_index('{FTS_TABLE}', '{FTS_ID}', 'text', "
        "stemmer='porter', stopwords='english', "
        "strip_accents=1, lower=1, overwrite=1)"
    )
    return int(count)


def rebuild_fts(root: Path | str | None = None) -> int:
    """Materialize ``document_text`` into the warehouse + native FTS index.

    Lowercase/accent-strip + Porter stemming; raises RuntimeError (the
    local-text source fails explicitly) when the FTS extension cannot load.
    Returns the number of indexed documents.
    """
    conn = duckdb._connect(Path(root) if root is not None else None)
    try:
        _load_fts_extension(conn)
        return _materialize_fts_count(conn)
    finally:
        conn.close()


def _fts_sql(
    as_of: str | None,
) -> tuple[str, list[str | None]]:
    """BM25 search SQL with optional PIT clause; returns (sql, base params)."""
    where = ["sub.score IS NOT NULL"]
    params: list[str | None] = []
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "d.known_at")
        where.append(clause)
        params.append(param)
    sql = (
        "SELECT d.*, sub.score AS fts_score FROM "
        f"(SELECT *, fts_main_{FTS_TABLE}.match_bm25({FTS_ID}, ?) AS score "
        f"FROM {FTS_TABLE}) sub "
        "JOIN document_text d ON d.doc_id = sub.doc_id "
        "AND d.content_hash = sub.content_hash "
        "WHERE " + " AND ".join(where) + " ORDER BY score DESC LIMIT {limit}"
    )
    return sql, params


def _fts_rows(
    conn: _FTSRowsConnection,
    sql: str,
    params: list[str | None],
    limit: int,
) -> list[dict[str, object]]:
    """Run BM25 SQL and map rows to dicts; raises when unusable."""
    rows = conn.execute(sql.format(limit=limit), params).fetchall()
    description = conn.description
    if not isinstance(description, (tuple, list)):
        raise RuntimeError(  # noqa: TRY004 - local-text source failure is always RuntimeError, description shape included
            "local-text source failed: FTS description unavailable"
        )
    columns = [desc[0] for desc in description if isinstance(desc, (tuple, list)) and desc]
    return [dict(zip(columns, row)) for row in rows if isinstance(row, (tuple, list))]


def _fts_search(
    text: str,
    *,
    limit: int,
    as_of: str | None,
    root: Path | str | None,
) -> list[dict[str, object]]:
    """BM25 token search over the materialized FTS index; raises when unusable."""
    conn = duckdb._connect(Path(root) if root is not None else None)
    try:
        conn.execute("LOAD fts")
        sql, pit_params = _fts_sql(as_of)
        return _fts_rows(conn, sql, [text, *pit_params], limit)
    finally:
        conn.close()


def _validate_search_query(query: str) -> str:
    """Stripped non-empty query; raises on blank input."""
    text = (query or "").strip()
    if not text:
        raise ValueError("query must be a non-empty string")
    return text


def _fallback_text_where(
    text: str,
    literal: bool,
) -> tuple[list[str], list[str | None]]:
    """Substring fallback WHERE: exact phrase when literal, AND-tokens otherwise."""
    if literal:
        return ["lower(text) LIKE '%' || lower(?) || '%'"], [text]
    tokens = [token for token in text.split() if token]
    return (
        ["lower(text) LIKE '%' || lower(?) || '%'" for _ in tokens],
        list(tokens),
    )


def _try_fts_search(
    text: str,
    literal: bool,
    limit: int,
    as_of: str | None,
    root: Path | str | None,
) -> list[dict[str, object]] | None:
    """BM25 token search; None when literal mode or the FTS path fails."""
    if literal:
        return None
    try:
        return _fts_search(text, limit=limit, as_of=as_of, root=root)
    except Exception:  # noqa: BLE001 - FTS search degrades to no local-text results, never raises
        return None


def search_document_text(
    query: str,
    *,
    literal: bool = False,
    limit: int = 50,
    as_of: str | None = None,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Local text search: BM25 token path by default, exact-phrase when literal.

    The token path falls back to AND-of-substrings when the FTS index or
    extension is unavailable (rebuild_fts stays the explicit health signal).
    """
    text = _validate_search_query(query)
    fts_hits = _try_fts_search(text, literal, limit, as_of, root)
    if fts_hits is not None:
        return fts_hits
    where, params = _fallback_text_where(text, literal)
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM document_text WHERE " + " AND ".join(where) + f" LIMIT {limit}"
    return duckdb.query(sql, params, data_root=_duckdb_root(root))


def _attempt_row(
    attempt: SearchAttempt | Mapping[str, object],
    search_id: str,
    now: str,
) -> dict[str, object]:
    """Search-attempt ledger input to a parquet row."""
    d = _as_dict(attempt)
    return {
        "attempt_id": d.get("attempt_id"),
        "search_id": search_id,
        "backend": d.get("backend"),
        "query": d.get("query"),
        "filters_json": _json(d.get("filters")),
        "status": d.get("status"),
        "results_reported": d.get("results_reported") or 0,
        "results_retrieved": d.get("results_retrieved") or 0,
        "pages_retrieved": d.get("pages_retrieved") or 0,
        "truncated": bool(d.get("truncated")),
        "source_limit": d.get("source_limit"),
        "pit_basis": d.get("pit_basis"),
        "error_type": d.get("error_type"),
        "error_message": d.get("error_message"),
        "started_at": d.get("started_at"),
        "completed_at": d.get("completed_at"),
        "retrieved_at": now,
    }


def _ledger_hit_id(search_id: str, d: dict[str, object]) -> str:
    """Deterministic hit ID from search/attempt/query/accession/document."""
    return raw_archive.content_hash(
        f"{search_id}\n{d.get('attempt_id')}\n{d.get('query')}\n"
        f"{d.get('accession_no') or d.get('accession')}\n"
        f"{d.get('matched_document')}".encode()
    )[:16]


def _hit_identity(
    d: dict[str, object],
    search_id: str,
) -> dict[str, object]:
    """Text-hit identity (IDs, query, accession) with deterministic hit ID."""
    return {
        "hit_id": d.get("hit_id") or _ledger_hit_id(search_id, d),
        "search_id": search_id,
        "attempt_id": d.get("attempt_id"),
        "query": d.get("query"),
        "accession": d.get("accession_no") or d.get("accession"),
        "filer_cik": str(d["filer_cik"]) if d.get("filer_cik") is not None else None,
        "filer_name": d.get("filer_name"),
    }


def _relevance_reasons(value: object) -> list[str]:
    """String relevance reasons; None/blank pass as []."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [r for r in value if isinstance(r, str)]
    return []


def _hit_document(d: dict[str, object]) -> dict[str, object]:
    """Text-hit document match (form, filed, document, type, items, geo)."""
    return {
        "form": d.get("form"),
        "filed_at": d.get("filed_at"),
        "matched_document": d.get("matched_document"),
        "file_type": d.get("file_type"),
        "file_description": d.get("file_description"),
        "items_json": _json(_list_or_none(d.get("items"))),
        "sic": d.get("sic"),
        "location": d.get("location"),
        "state": d.get("state"),
        "inc_state": d.get("inc_state"),
        "issuer_cik": (str(d.get("issuer_cik")) if d.get("issuer_cik") is not None else None),
        "relevance_reason_json": _json(_relevance_reasons(d.get("relevance_reason"))),
        "snippet": d.get("snippet"),
        "resource_uri": d.get("resource_uri"),
    }


def _hit_score_page(d: dict[str, object]) -> dict[str, object]:
    """Text-hit score, source, and page with defaults."""
    return {
        "score": d.get("score") or 0.0,
        "source_url": d.get("source_url"),
        "page": d.get("page") or 1,
    }


def _hit_ledger_provenance(d: dict[str, object], now: str) -> dict[str, object]:
    """Text-hit known/retrieved/hash/parser provenance."""
    return {
        "known_at": d.get("known_at") or d.get("filed_at") or now,
        "retrieved_at": now,
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
        "raw_archive_path": d.get("raw_archive_path"),
    }


def _hit_scoring(d: dict[str, object], now: str) -> dict[str, object]:
    """Text-hit score/paging/provenance."""
    return {**_hit_score_page(d), **_hit_ledger_provenance(d, now)}


def _hit_match(d: dict[str, object], now: str) -> dict[str, object]:
    """Text-hit match detail (form, document, score, paging, provenance)."""
    return {**_hit_document(d), **_hit_scoring(d, now)}


def _hit_row(
    hit: SECTextHit | Mapping[str, object],
    search_id: str,
    now: str,
) -> dict[str, object]:
    """Text-hit ledger input to a parquet row (deterministic hit ID)."""
    d = _as_dict(hit)
    return {**_hit_identity(d, search_id), **_hit_match(d, now)}


def store_attempt(
    attempt: SearchAttempt | Mapping[str, object],
    *,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one search-attempt ledger row."""
    d = _as_dict(attempt)
    row = _attempt_row(attempt, str(d.get("search_id") or ""), retrieved_at or _utcnow())
    return duckdb.insert_ignore("sec_search_attempts", [row], data_root=_duckdb_root(root))


def store_hit(
    hit: SECTextHit | Mapping[str, object],
    *,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one text-hit ledger row."""
    d = _as_dict(hit)
    row = _hit_row(hit, str(d.get("search_id") or ""), retrieved_at or _utcnow())
    return duckdb.insert_ignore("sec_text_hits", [row], data_root=_duckdb_root(root))


def _ledger_search_row(
    search_id: str,
    req: dict[str, object],
    now: str,
    coverage_status: str,
    sources_attempted: Iterable[str],
    sources_completed: Iterable[str],
    sources_failed: Iterable[str],
    results_reported: int,
    results_retrieved: int,
    pages: int,
    date_coverage: str | None,
    forms_covered: Iterable[str],
    pending_backfill_jobs: Iterable[str],
    warnings: Iterable[str],
    errors: Iterable[str],
    evidence_packet_ids: Iterable[str],
    counts: dict[str, int],
    pagination_complete: bool,
    source_exhausted: bool,
) -> dict[str, object]:
    """Interactive-search header row with coverage and dedup counts."""
    return {
        "search_id": search_id,
        "request_json": _json(req),
        "coverage_status": coverage_status,
        "sources_attempted_json": _json(list(sources_attempted)),
        "sources_completed_json": _json(list(sources_completed)),
        "sources_failed_json": _json(list(sources_failed)),
        "results_reported": results_reported,
        "results_retrieved": results_retrieved,
        "pages": pages,
        "date_coverage": date_coverage,
        "forms_covered_json": _json(list(forms_covered)),
        "pending_jobs_json": _json(
            _list_or_none(pending_backfill_jobs or req.get("pending_backfill_jobs") or ()) or []
        ),
        "warnings_json": _json(list(warnings)),
        "errors_json": _json(list(errors)),
        "evidence_packet_ids_json": _json(list(evidence_packet_ids)),
        "dedup_counts_json": _json(counts),
        # Retrieval truth, independent of the display-packet bound.
        "pagination_complete": pagination_complete,
        "source_exhausted": source_exhausted,
        "retrieved_at": now,
        "known_at": now,
        "parser_version": PARSER_VERSION,
    }


def persist_search_ledger(
    *,
    search_id: str,
    request: SECSearchRequest | Mapping[str, object],
    entities: Iterable[EntityCandidate | Mapping[str, object]] = (),
    filings: Iterable[Filing | Mapping[str, object]] = (),
    documents: Iterable[FilingDocument | Mapping[str, object]] = (),
    text_hits: Iterable[SECTextHit | Mapping[str, object]] = (),
    attempts: Iterable[SearchAttempt | Mapping[str, object]] = (),
    coverage_status: str = "complete",
    sources_attempted: Iterable[str] = (),
    sources_completed: Iterable[str] = (),
    sources_failed: Iterable[str] = (),
    source_limits: Iterable[str] = (),
    results_reported: int = 0,
    results_retrieved: int = 0,
    warnings: Iterable[str] = (),
    errors: Iterable[str] = (),
    evidence_packet_ids: Iterable[str] = (),
    pending_backfill_jobs: Iterable[str] = (),
    forms_covered: Iterable[str] = (),
    pages: int = 1,
    date_coverage: str | None = None,
    pagination_complete: bool = False,
    source_exhausted: bool = False,
    root: Path | str | None = None,
) -> dict[str, int]:
    """Persist one interactive search: request, attempts, hits, coverage.

    ``pagination_complete``/``source_exhausted`` describe RETRIEVAL, never the
    display packet bound: paging drained every route, and the source itself
    carried no limits. Unknown callers default to the honest "not proven".

    Returns ``{"searches": n, "attempts": n, "hits": n}`` rows written.
    """
    now = _utcnow()
    req = _as_dict(request)
    attempt_list = list(attempts)
    hit_list = list(text_hits)
    search_row = _ledger_search_row(
        search_id,
        req,
        now,
        coverage_status,
        sources_attempted,
        sources_completed,
        sources_failed,
        results_reported,
        results_retrieved,
        pages,
        date_coverage,
        forms_covered,
        pending_backfill_jobs,
        warnings,
        errors,
        evidence_packet_ids,
        {
            "entities": len(tuple(entities)),
            "filings": len(tuple(filings)),
            "documents": len(tuple(documents)),
            "text_hits": len(hit_list),
            "attempts": len(attempt_list),
        },
        pagination_complete,
        source_exhausted,
    )
    attempt_rows = [_attempt_row(attempt, search_id, now) for attempt in attempt_list]
    hit_rows = [_hit_row(hit, search_id, now) for hit in hit_list]
    db_root = _duckdb_root(root)
    return {
        "searches": duckdb.insert_ignore("sec_searches", [search_row], data_root=db_root),
        "attempts": duckdb.insert_ignore("sec_search_attempts", attempt_rows, data_root=db_root),
        "hits": duckdb.insert_ignore("sec_text_hits", hit_rows, data_root=db_root),
    }


def query_search(
    search_id: str,
    *,
    root: Path | str | None = None,
) -> dict[str, object] | None:
    """One persisted search-ledger row, or None."""
    rows = duckdb.query(
        "SELECT * FROM sec_searches WHERE search_id = ? LIMIT 1", [search_id], data_root=_duckdb_root(root)
    )
    return rows[0] if rows else None


def query_attempts(
    search_id: str,
    *,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """All persisted attempt rows for one search, in attempt order."""
    return duckdb.query(
        "SELECT * FROM sec_search_attempts WHERE search_id = ? ORDER BY attempt_id",
        [search_id],
        data_root=_duckdb_root(root),
    )


def _hits_filter(search_id: str, forms: Iterable[str] | None) -> tuple[str, list[object]]:
    """WHERE fragment + params shared by query_hits/query_hits_count."""
    where = "search_id = ?"
    params: list[object] = [search_id]
    wanted = sorted({f.strip().upper() for f in (forms or ()) if isinstance(f, str) and f.strip()})
    if wanted:
        where += f" AND upper(form) IN ({', '.join('?' * len(wanted))})"
        params.extend(wanted)
    return where, params


def query_hits(
    search_id: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    forms: Iterable[str] | None = None,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Persisted text-hit rows for one search, best score first.

    Whole persisted universe by default; ``offset``/``limit`` page it and
    ``forms`` narrows to those form values (case-insensitive). The tiebreaker
    keeps paging stable across equal scores.
    """
    where, params = _hits_filter(search_id, forms)
    sql = f"SELECT * FROM sec_text_hits WHERE {where} ORDER BY score DESC, hit_id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset:
        sql += " OFFSET ?"
        params.append(offset)
    return duckdb.query(sql, params, data_root=_duckdb_root(root))


def query_hits_count(
    search_id: str,
    *,
    forms: Iterable[str] | None = None,
    root: Path | str | None = None,
) -> int:
    """Persisted text-hit rows for one search under the same filter."""
    where, params = _hits_filter(search_id, forms)
    rows = duckdb.query(f"SELECT count(*) AS n FROM sec_text_hits WHERE {where}", params, data_root=_duckdb_root(root))
    count = rows[0].get("n") if rows else None
    return count if isinstance(count, int) else 0


_QUARTER_ENDS = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}


def _quarter_end_for(date_partition: str) -> str | None:
    """Quarter-end date for a ``YYYY-QN`` partition label (None when unparseable)."""
    year, sep, quarter = date_partition.partition("-Q")
    if not sep or not year.isdigit() or len(year) != 4:
        return date_partition[:10] or None
    end = _QUARTER_ENDS.get(quarter)
    return f"{year}-{end}" if end else None


def store_coverage(
    source: str,
    form: str,
    date_partition: str,
    status: str,
    *,
    family: str | None = None,
    coverage_date: str | None = None,
    accession_count: int = 0,
    last_key: str | None = None,
    known_at: str | None = None,
    retrieved_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one ingestion-coverage row (source + form + partition key)."""
    now = retrieved_at or _utcnow()
    row: dict[str, object] = {
        "source": source,
        "form": form,
        "family": family,
        "date_partition": date_partition,
        "coverage_date": coverage_date or _quarter_end_for(date_partition),
        "status": status,
        "accession_count": accession_count or 0,
        "last_key": last_key,
        "parser_version": PARSER_VERSION,
        "known_at": known_at or now,
        "retrieved_at": now,
    }
    return duckdb.insert_ignore("sec_ingestion_coverage", [row], data_root=_duckdb_root(root))


def query_coverage(
    *,
    source: str | None = None,
    form: str | None = None,
    date_partition: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Coverage rows, newest first."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("source", source, where, params)
    _simple_where("form", form, where, params)
    _simple_where("date_partition", date_partition, where, params)
    return _ordered_query("sec_ingestion_coverage", where, params, "ORDER BY coverage_date DESC", limit, root)


def store_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    status: str,
    *,
    payload_hash: str = "",
    record_count: int = 0,
    last_key: str | None = None,
    error: str | None = None,
    totals: Mapping[str, object] | None = None,
    parser_version: str = PARSER_VERSION,
    started_at: str | None = None,
    finished_at: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Append one checkpoint row; reruns with identical keys write nothing."""
    now = _utcnow()
    row: dict[str, object] = {
        "pipeline": pipeline,
        "source": source,
        "key": key,
        "payload_hash": payload_hash or "",
        "status": status,
        "record_count": record_count or 0,
        "started_at": started_at or now,
        "finished_at": finished_at,
        "parser_version": parser_version,
        "last_key": last_key,
        "error": error,
        "totals_json": _json(totals),
    }
    return duckdb.insert_ignore("ingestion_checkpoints", [row], data_root=_duckdb_root(root))


def _checkpoint_order(row: dict[str, object]) -> tuple[str, str]:
    """Newest-finishing checkpoint sorts last (resume keeps the latest)."""
    return (str(row.get("finished_at") or ""), str(row.get("started_at") or ""))


def get_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    *,
    root: Path | str | None = None,
) -> dict[str, object] | None:
    """Latest checkpoint row for a (pipeline, source, key), or None.

    A ``complete`` row means resume skips the partition; ``failed`` (or no
    row) means the accession/partition is retried.
    """
    rows = duckdb.query(
        "SELECT * FROM ingestion_checkpoints WHERE pipeline = ? AND source = ? AND key = ?",
        [pipeline, source, key],
        data_root=_duckdb_root(root),
    )
    if not rows:
        return None
    rows.sort(key=_checkpoint_order)
    return rows[-1]


def advance_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    *,
    last_key: str | None = None,
    record_count: int = 0,
    totals: Mapping[str, object] | None = None,
    payload_hash: str = "",
    parser_version: str = PARSER_VERSION,
    root: Path | str | None = None,
) -> int:
    """Record a partition complete. Call only after immutable archive and all
    normalized writes commit; a rerun after completion writes nothing."""
    now = _utcnow()
    prior = get_checkpoint(pipeline, source, key, root=root)
    started = str((prior or {}).get("started_at") or now)
    return store_checkpoint(
        pipeline,
        source,
        key,
        "complete",
        payload_hash=payload_hash,
        record_count=record_count,
        last_key=last_key,
        totals=totals,
        parser_version=parser_version,
        started_at=started,
        finished_at=now,
        root=root,
    )


def _row_dict(value: Mapping[str, object] | _HasToDict) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        return dict(value.to_dict())
    except Exception:  # noqa: BLE001, S110 - duck-typed row tries to_dict before the mapping path
        pass
    if not value:
        return {}
    raise TypeError(f"cannot build a row dict from {type(value).__name__}")


def _encode_power_part(label: str, value: object) -> str | None:
    """One ``label=int(value)`` arm; None when missing, mistyped, or castable-false."""
    if value is None:
        return None
    if not isinstance(value, (bool, int, float, str)):
        return None
    try:
        return f"{label}={int(value)}"
    except Exception:  # noqa: BLE001 - untrusted label value coerces to None, never raises
        return None


def _beneficial_row(d: dict[str, object]) -> dict[str, object]:
    """Beneficial-ownership input dict to a parquet row (power encodings included)."""
    filer_name = d.get("filer_name") or d.get("reporter_name")
    return {**_beneficial_identity(d, filer_name), **_beneficial_detail(d)}


def _beneficial_economics(d: dict[str, object]) -> dict[str, object]:
    """Beneficial-ownership economics with power/purpose fallbacks."""
    return {
        "shares": d.get("shares"),
        "percent": d.get("percent"),
        "voting_power": d.get("voting_power") or _encode_power(d.get("sole_voting"), d.get("shared_voting")),
        "dispositive_power": d.get("dispositive_power")
        or _encode_power(d.get("sole_dispositive"), d.get("shared_dispositive")),
        "purpose": d.get("purpose") or d.get("purpose_text"),
        "form": d.get("form"),
    }


def _beneficial_provenance(d: dict[str, object]) -> dict[str, object]:
    """Beneficial-ownership dates and provenance."""
    return {
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _beneficial_identity(d: dict[str, object], filer_name: object) -> dict[str, object]:
    """Beneficial-ownership filing identity (accession, subject, filer)."""
    return {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "subject_cik": str(d["subject_cik"]).strip() if d.get("subject_cik") is not None else None,
        "subject_name": d.get("subject_name"),
        "filer_cik": str(d["filer_cik"]).strip() if d.get("filer_cik") is not None else None,
        "filer_name": filer_name,
        "reporter_name": d.get("reporter_name") or filer_name,
    }


def _beneficial_detail(d: dict[str, object]) -> dict[str, object]:
    """Beneficial-ownership economics/provenance with power-encoding fallbacks."""
    return {**_beneficial_economics(d), **_beneficial_provenance(d)}


def _holding_source_row(d: dict[str, object]) -> int:
    """13F 1-based source row; raises unless a positive integer."""
    try:
        source_row = int(str(d.get("source_row")).strip()) if d.get("source_row") is not None else 0
    except Exception:  # noqa: BLE001 - untrusted source_row coerces to 0 then validates positive, never raises
        source_row = 0
    if source_row <= 0:
        raise ValueError("13F holding source_row must be a positive integer")
    return source_row


def _holding_sid(
    cusip_norm: str | None,
    isin_norm: str | None,
) -> str | None:
    """Canonical CUSIP/ISIN security ID; None when neither normalizes."""
    if cusip_norm:
        return f"cusip:{cusip_norm}"
    return f"isin:{isin_norm}" if isin_norm else None


def _holding_filing(
    d: dict[str, object],
    source_row: int,
    canonical_sid: str | None,
) -> dict[str, object]:
    """13F holding filing keys (accession, manager, source row, holding ID)."""
    from .models import institutional_holding_id

    accession = d.get("accession") or d.get("accession_no")
    return {
        "accession": accession,
        "document_name": d.get("document_name"),
        "manager_cik": str(d["manager_cik"]).strip() if d.get("manager_cik") is not None else None,
        "manager_name": d.get("manager_name"),
        "report_period": d.get("report_period"),
        "source_row": source_row,
        "holding_id": institutional_holding_id(str(accession or ""), source_row, canonical_sid),
    }


def _holding_security(
    d: dict[str, object],
    cusip_norm: str | None,
    isin_norm: str | None,
) -> dict[str, object]:
    """13F holding issuer/security keys."""
    return {
        "issuer_name": d.get("issuer_name"),
        "entity_id": d.get("entity_id"),
        "security_id": d.get("security_id"),
        "class_title": d.get("class_title"),
        "cusip": cusip_norm,
        "isin": isin_norm,
    }


def _holding_identity(
    d: dict[str, object],
    cusip_norm: str | None,
    isin_norm: str | None,
    source_row: int,
) -> dict[str, object]:
    """13F holding identity (accession, manager, issuer, security IDs)."""
    return {
        **_holding_filing(d, source_row, _holding_sid(cusip_norm, isin_norm)),
        **_holding_security(d, cusip_norm, isin_norm),
    }


def _holding_detail(d: dict[str, object]) -> dict[str, object]:
    """13F holding position economics and provenance."""
    return {
        "shares": d.get("shares"),
        "value": d.get("value"),
        "put_call": d.get("put_call"),
        "discretion": d.get("discretion"),
        "other_manager": d.get("other_manager"),
        "shares_prn_type": d.get("shares_prn_type"),
        "voting": d.get("voting"),
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _holding_row(
    d: dict[str, object],
    cusip_norm: str | None,
    isin_norm: str | None,
    source_row: int,
) -> dict[str, object]:
    """13F holding input dict to a parquet row (canonical security ID included)."""
    return {
        **_holding_identity(d, cusip_norm, isin_norm, source_row),
        **_holding_detail(d),
    }


def _encode_power(sole: object, shared: object) -> str | None:
    parts = [
        part
        for part in (
            _encode_power_part("sole", sole),
            _encode_power_part("shared", shared),
        )
        if part is not None
    ]
    return " ".join(parts) or None


def store_beneficial_ownership(
    row: BeneficialOwnership | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Typed writer: accepts ``BeneficialOwnership`` or a column row."""
    d = _row_dict(row)
    return _pass_through("sec_beneficial_ownership", _beneficial_row(d), root)


def query_beneficial_ownership(
    *,
    subject_cik: int | str | None = None,
    owner_cik: int | str | None = None,
    filer_cik: int | str | None = None,
    accession: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Typed rows newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    owner = owner_cik if owner_cik is not None else filer_cik
    where: list[str] = []
    params: list[str | None] = []
    _cik_where("subject_cik", subject_cik, where, params)
    _cik_where("filer_cik", owner, where, params)
    _simple_where("accession", accession, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("sec_beneficial_ownership", where, params, "ORDER BY known_at DESC", limit, root)


def store_13f_holding(
    row: InstitutionalHolding | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Typed writer: accepts ``InstitutionalHolding`` or a column row."""
    d = _row_dict(row)
    source_row = _holding_source_row(d)
    return _pass_through(
        "sec_13f_holdings",
        _holding_row(d, normalize_cusip(d.get("cusip")), normalize_isin(d.get("isin")), source_row),
        root,
    )


def _cusip_where(
    cusip: str,
    where: list[str],
    params: list[str | None],
) -> None:
    """CUSIP equality plus dashed-legacy regexp arm; invalid binds no-match."""
    cusip_norm = normalize_cusip(cusip)
    if cusip_norm is None:
        # Binds None, matching nothing; never IS NULL.
        where.append("cusip = ?")
        params.append(None)
        return
    # regexp arm keeps pre-fix dashed rows resolvable (no migration).
    where.append("(cusip = ? OR UPPER(regexp_replace(cusip, '[^A-Za-z0-9]+', '', 'g')) = ?)")
    params.extend([cusip_norm, cusip_norm])


def _cusip_arms(
    cusip_norm: str | None,
    arms: list[str],
    vals: list[str | None],
) -> None:
    """CUSIP equality + legacy-regexp arms when the value normalizes."""
    if cusip_norm is not None:
        arms.append("cusip = ?")
        vals.append(cusip_norm)
        arms.append("UPPER(regexp_replace(cusip, '[^A-Za-z0-9]+', '', 'g')) = ?")
        vals.append(cusip_norm)


def _security_colon_arms(
    raw: str,
    arms: list[str],
    vals: list[str | None],
) -> None:
    """Prefixed ``security`` (CUSIP:/ISIN:) to CUSIP/ISIN/security_id arms."""
    _, _, suffix = raw.partition(":")
    arms.append("UPPER(security_id) = ?")
    vals.append(raw)
    _cusip_arms(normalize_cusip(suffix), arms, vals)
    suffix_isin = normalize_isin(suffix)
    if suffix_isin is not None:
        arms.append("isin = ?")
        vals.append(suffix_isin)


def _security_bare_arms(
    raw: str,
    security: str,
    arms: list[str],
    vals: list[str | None],
) -> None:
    """Bare ``security`` to CUSIP/ISIN arms plus the security_id IN list."""
    sec_cusip = normalize_cusip(security)
    sec_isin = normalize_isin(security)
    _cusip_arms(sec_cusip, arms, vals)
    if sec_isin is not None:
        arms.append("isin = ?")
        vals.append(sec_isin)
    ids = [raw]
    if sec_cusip is not None:
        ids.append(f"CUSIP:{sec_cusip}")
    if sec_isin is not None:
        ids.append(f"ISIN:{sec_isin}")
    arms.append(f"UPPER(security_id) IN ({', '.join(['?'] * len(ids))})")
    vals.extend(ids)


def _security_where(
    security: str,
    where: list[str],
    params: list[str | None],
) -> None:
    """``security`` (CUSIP, ISIN, or security_id) to one OR-group clause."""
    raw = security.strip().upper()
    arms: list[str] = []
    vals: list[str | None] = []
    if ":" in raw:
        _security_colon_arms(raw, arms, vals)
    else:
        _security_bare_arms(raw, security, arms, vals)
    where.append("(" + " OR ".join(arms) + ")")
    params.extend(vals)


def _holdings_identity_where(
    manager_cik: int | str | None,
    security_id: str | None,
    accession: str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Holdings identity filters (manager, security_id, accession)."""
    if manager_cik is not None:
        where.append("manager_cik = ?")
        params.append(str(manager_cik).strip())
    if security_id is not None:
        where.append("security_id = ?")
        params.append(security_id.strip())
    if accession is not None:
        where.append("accession = ?")
        params.append(accession)


def _holdings_security_where(
    cusip: str | None,
    isin: str | None,
    security: str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Holdings security filters (CUSIP, ISIN, security expression)."""
    if cusip is not None:
        _cusip_where(cusip, where, params)
    if isin is not None:
        # Binds None, matching nothing; never IS NULL.
        where.append("isin = ?")
        params.append(normalize_isin(isin))
    if security is not None:
        _security_where(security, where, params)


def query_13f_holdings(
    *,
    manager_cik: int | str | None = None,
    security_id: str | None = None,
    security: str | None = None,
    cusip: str | None = None,
    isin: str | None = None,
    accession: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Holdings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD.

    ``security`` matches CUSIP, ISIN, or ``security_id`` (all uppercased).
    """
    where: list[str] = []
    params: list[str | None] = []
    _holdings_identity_where(manager_cik, security_id, accession, where, params)
    _holdings_security_where(cusip, isin, security, where, params)
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_13f_holdings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {limit}"
    return duckdb.query(sql, params, data_root=_duckdb_root(root))


def _issuer_target(issuer_name: str) -> str:
    """Stripped issuer string; empty when missing or non-stringable."""
    try:
        return (issuer_name or "").strip()
    except Exception:  # noqa: BLE001 - untrusted issuer string coerces to empty, never raises
        return ""


def _trim_period(report_period: str | None) -> str | None:
    """Report period trimmed to YYYY-MM-DD length; None when blank."""
    try:
        return (report_period or "").strip()[:10] or None
    except Exception:  # noqa: BLE001 - untrusted report period coerces to None, never raises
        return None


def _valid_period(period: str | None) -> str | None:
    """Validated YYYY-MM-DD period; None when missing or malformed."""
    if period is None:
        return None
    try:
        date.fromisoformat(period)
    except Exception:  # noqa: BLE001 - malformed period validates to None, never raises
        return None
    return period


def _issuer_period(report_period: str | None) -> str | None:
    """Report period as YYYY-MM-DD; None when missing or malformed."""
    return _valid_period(_trim_period(report_period))


def _query_current_issuer_names(
    target: str,
    root: Path | str | None,
) -> list[dict[str, object]]:
    """Current-name exact matches (trimmed, case-insensitive); [] on failure."""
    try:
        return duckdb.query(
            "SELECT DISTINCT entity_id, name, known_at, retrieved_at FROM entities "
            "WHERE LOWER(TRIM(name)) = LOWER(TRIM(?))",
            [target],
            data_root=_duckdb_root(root),
        )
    except Exception:  # noqa: BLE001 - issuer lookup degrades to no candidates on storage failure
        return []


def _query_former_issuer_names(
    target: str,
    period: str | None,
    root: Path | str | None,
) -> list[dict[str, object]]:
    """Former-name matches valid at the period; [] without period or on failure."""
    if period is None:
        return []
    try:
        return duckdb.query(
            "SELECT DISTINCT e.entity_id, e.name, e.known_at, e.retrieved_at "
            "FROM entity_aliases a JOIN entities e ON e.entity_id = a.entity_id "
            "WHERE a.alias_type = 'former_name' "
            "AND LOWER(TRIM(a.alias_value)) = LOWER(TRIM(?)) "
            "AND (a.valid_from IS NULL OR substr(CAST(a.valid_from AS VARCHAR), 1, 10) <= ?) "
            "AND (a.valid_to IS NULL OR ? < substr(CAST(a.valid_to AS VARCHAR), 1, 10))",
            [target, period, period],
            data_root=_duckdb_root(root),
        )
    except Exception:  # noqa: BLE001 - alias lookup degrades to no candidates on storage failure
        return []


def _issuer_row_eid(row: dict[str, object]) -> str:
    """Row entity_id stripped; empty when missing or non-stringable."""
    try:
        return str(row.get("entity_id") or "").strip()
    except Exception:  # noqa: BLE001 - untrusted row entity_id coerces to empty, never raises
        return ""


def _dedupe_issuer_rows(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """First row per entity_id; skips rows without a usable entity_id."""
    seen: dict[str, dict[str, object]] = {}
    for row in rows:
        eid = _issuer_row_eid(row)
        if eid and eid not in seen:
            seen[eid] = row
    return list(seen.values())


def query_13f_issuer_candidates(
    issuer_name: str, *, report_period: str | None, holding_known_at: str | None, root: Path | str | None = None
) -> list[dict[str, object]]:
    """Exact current/former-name issuer candidates for one 13F issuer string."""
    target = _issuer_target(issuer_name)
    if not target:
        return []
    period = _issuer_period(report_period)
    current = _query_current_issuer_names(target, root)
    former = _query_former_issuer_names(target, period, root)
    return _dedupe_issuer_rows(list(current or []) + list(former or []))


def _issuer_holdings_asof(
    as_of: str | None,
) -> tuple[str | None, str, str]:
    """Validated as_of plus holdings/alias PIT fragments (empty without as_of)."""
    as_of_val = _validate_as_of(as_of) if as_of is not None else None
    if as_of_val is None:
        return None, "", ""
    return (
        as_of_val,
        "AND (h.known_at IS NULL OR substr(CAST(h.known_at AS VARCHAR), 1, 10) <= ?) ",
        "AND (a.known_at IS NULL OR substr(CAST(a.known_at AS VARCHAR), 1, 10) <= ?) ",
    )


def _issuer_holdings_sql(holding_asof: str, alias_asof: str) -> str:
    """Governed issuer-holdings CTE SQL with PIT fragments spliced in."""
    return (
        "WITH holdings AS ("
        " SELECT h.*, CASE WHEN h.security_id IS NOT NULL THEN h.security_id"
        " WHEN h.cusip IS NOT NULL THEN 'cusip:' || UPPER(regexp_replace(h.cusip, '[^A-Za-z0-9]+', '', 'g'))"
        " WHEN h.isin IS NOT NULL THEN 'isin:' || UPPER(h.isin)"
        " ELSE NULL END AS _prov"
        " FROM sec_13f_holdings h WHERE 1=1 " + holding_asof + "), "
        "visible_aliases AS ("
        " SELECT a.security_id AS _skey, a.entity_id AS _eid,"
        " a.valid_from AS _vf, a.valid_to AS _vt"
        " FROM entity_aliases a JOIN entities e ON e.entity_id = a.entity_id"
        " WHERE a.alias_type IN ('cusip', 'isin')"
        " AND a.security_id IS NOT NULL"
        " AND a.entity_id LIKE 'sec:cik:%' " + alias_asof + "), "
        "mapping AS ("
        " SELECT h._prov AS _prov, substr(CAST(h.report_period AS VARCHAR), 1, 10) AS _period,"
        " COUNT(DISTINCT a._eid) AS _n, MAX(a._eid) AS _sole"
        " FROM holdings h JOIN visible_aliases a ON a._skey = h._prov"
        " WHERE h._prov IS NOT NULL AND h.report_period IS NOT NULL"
        " AND (a._vf IS NULL OR substr(CAST(a._vf AS VARCHAR), 1, 10) <= substr(CAST(h.report_period AS VARCHAR), 1, 10))"
        " AND (a._vt IS NULL OR substr(CAST(h.report_period AS VARCHAR), 1, 10) < substr(CAST(a._vt AS VARCHAR), 1, 10))"
        " GROUP BY h._prov, substr(CAST(h.report_period AS VARCHAR), 1, 10)), "
        "sole AS (SELECT _prov, _period, _sole FROM mapping WHERE _n = 1 AND _sole = ?) "
        "SELECT * EXCLUDE (_rn) FROM ("
        " SELECT h.*, f.form AS filing_form, f.is_amendment AS is_amendment,"
        " f.amendment_of AS amendment_of, f.accepted_at AS accepted_at,"
        " ROW_NUMBER() OVER ("
        " PARTITION BY h.accession, h.document_name, h.manager_cik,"
        " h.report_period, h._prov, h.class_title, h.shares, h.value,"
        " h.put_call, h.discretion, h.voting"
        " ORDER BY CASE WHEN h.cusip = UPPER(regexp_replace("
        " h.cusip, '[^A-Za-z0-9]+', '', 'g'))"
        " THEN 0 ELSE 1 END,"
        " h.cusip) AS _rn"
        " FROM holdings h JOIN sole s ON s._prov = h._prov"
        " AND s._period = substr(CAST(h.report_period AS VARCHAR), 1, 10)"
        " LEFT JOIN sec_filings f ON f.accession = h.accession"
        " ) WHERE _rn = 1 ORDER BY known_at DESC"
    )


def _issuer_holdings_params(
    as_of_val: str | None,
    eid: str,
    limit: int | None,
    sql: str,
) -> tuple[str, list[str | None]]:
    """Bind PIT/entity params and append the limit clause."""
    params: list[str | None] = []
    if as_of_val is not None:
        params.extend([as_of_val])
        params.extend([as_of_val])
    params.append(eid)
    if limit is not None:
        sql += f" LIMIT {limit}"
    return sql, params


def query_13f_holdings_for_issuer(
    entity_id: str, *, as_of: str | None = None, limit: int = 200, root: Path | str | None = None
) -> list[dict[str, object]]:
    """Governed issuer -> holdings via exact CUSIP/ISIN alias mapping (PIT)."""
    eid = (entity_id or "").strip()
    if not eid:
        return []
    as_of_val, holding_asof, alias_asof = _issuer_holdings_asof(as_of)
    sql, params = _issuer_holdings_params(as_of_val, eid, limit, _issuer_holdings_sql(holding_asof, alias_asof))
    rows = duckdb.query(sql, params, data_root=_duckdb_root(root))
    for row in rows:
        try:
            row["entity_id"] = eid
        except Exception:  # noqa: BLE001, S110 - best-effort entity_id stamp skips immutable rows
            pass
    return rows


def _insider_filing(d: dict[str, object]) -> dict[str, object]:
    """Insider filing keys (accession, document, form, issuer)."""
    return {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form"),
        "issuer_cik": str(d["issuer_cik"]).strip() if d.get("issuer_cik") is not None else None,
        "issuer_name": d.get("issuer_name") or d.get("issuer"),
    }


def _insider_person(d: dict[str, object]) -> dict[str, object]:
    """Insider person keys (owner CIK/name) with insider_* fallbacks."""
    return {
        "owner_cik": str(d.get("owner_cik") or d.get("insider_cik") or "").strip() or None,
        "owner_name": d.get("owner_name") or d.get("insider_name"),
    }


def _insider_identity(d: dict[str, object]) -> dict[str, object]:
    """Insider filing identity (accession, issuer, owner) with fallbacks."""
    return {**_insider_filing(d), **_insider_person(d)}


def _insider_roles(d: dict[str, object]) -> dict[str, object]:
    """Insider role flags and titles."""
    return {
        "is_director": d.get("is_director"),
        "is_officer": d.get("is_officer"),
        "is_ten_percent": d.get("is_ten_percent"),
        "is_other": d.get("is_other"),
        "role_title": d.get("role_title"),
    }


def _insider_economics(d: dict[str, object]) -> dict[str, object]:
    """Insider security economics with title/holdings fallbacks."""
    return {
        "security_title": d.get("security_title") or d.get("security"),
        "transaction_code": d.get("transaction_code"),
        "transaction_date": d.get("transaction_date"),
        "shares": d.get("shares"),
        "price": d.get("price"),
        "holdings": d.get("holdings") if d.get("holdings") is not None else d.get("holdings_after"),
    }


def _insider_provenance(d: dict[str, object]) -> dict[str, object]:
    """Insider dates and provenance."""
    return {
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _insider_detail(d: dict[str, object]) -> dict[str, object]:
    """Insider role/security/economics with holdings and provenance fallbacks."""
    return {**_insider_roles(d), **_insider_economics(d), **_insider_provenance(d)}


def _insider_row(d: dict[str, object]) -> dict[str, object]:
    """Insider-transaction input dict to a parquet row (owner fallbacks included)."""
    return {**_insider_identity(d), **_insider_detail(d)}


def _offering_identity(d: dict[str, object]) -> dict[str, object]:
    """Offering filing identity (accession, filer, registrant) with fallbacks."""
    return {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form"),
        "filer_cik": str(d["filer_cik"]).strip() if d.get("filer_cik") is not None else None,
        "filer_name": d.get("filer_name"),
        "registrant_cik": str(d["registrant_cik"]).strip() if d.get("registrant_cik") is not None else None,
        "registrant_name": d.get("registrant_name") or d.get("issuer"),
    }


def _offering_economics(d: dict[str, object]) -> dict[str, object]:
    """Offering security economics with title/amount fallbacks."""
    return {
        "security_title": d.get("security_title") or d.get("offering_type"),
        "amount": d.get("amount") if d.get("amount") is not None else d.get("gross_proceeds"),
    }


def _offering_provenance(d: dict[str, object]) -> dict[str, object]:
    """Offering dates and provenance."""
    return {
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _offering_detail(d: dict[str, object]) -> dict[str, object]:
    """Offering security economics and provenance with amount fallbacks."""
    return {**_offering_economics(d), **_offering_provenance(d)}


def _offering_row(d: dict[str, object]) -> dict[str, object]:
    """Offering input dict to a parquet row (registrant/amount fallbacks included)."""
    return {**_offering_identity(d), **_offering_detail(d)}


def store_insider_transaction(
    row: InsiderTransaction | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Typed writer: accepts ``InsiderTransaction`` or a column row."""
    d = _row_dict(row)
    return _pass_through("sec_insider_transactions", _insider_row(d), root)


def query_insider_transactions(
    *,
    issuer_cik: int | str | None = None,
    owner_cik: int | str | None = None,
    accession: str | None = None,
    form: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Insider rows newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _cik_where("issuer_cik", issuer_cik, where, params)
    _cik_where("owner_cik", owner_cik, where, params)
    _simple_where("accession", accession, where, params)
    _simple_where("form", form, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("sec_insider_transactions", where, params, "ORDER BY known_at DESC", limit, root)


def store_offering(
    row: Offering | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Typed writer: accepts ``Offering`` or a column row.

    The registrant defaults to the issuer, never the reverse; amounts stay
    proposed/registered upstream, never issuance.
    """
    d = _row_dict(row)
    return _pass_through("sec_offerings", _offering_row(d), root)


def _registrant_cik_where(
    registrant_cik: int | str,
    where: list[str],
    params: list[str | None],
) -> None:
    """Registrant CIK clause: canonical bare plus 10-digit padded legacy form."""
    # ponytail: canonical bare CIK plus SEC 10-digit padding match legacy rows
    try:
        _canon = str(int(str(registrant_cik).strip()))
    except TypeError, ValueError, AttributeError:
        _canon = None
    if _canon is not None:
        where.append("(registrant_cik = ? OR registrant_cik = ?)")
        params.extend([_canon, f"{int(_canon):010d}"])
        return
    where.append("registrant_cik = ?")
    params.append(str(registrant_cik).strip())


def _transaction_filer(d: dict[str, object]) -> dict[str, object]:
    """Transaction filing identity (accession, document, form, filer)."""
    return {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form") or d.get("deal_type"),
        "filer_cik": _strip_cik(d.get("filer_cik")),
        "filer_name": d.get("filer_name"),
    }


def _transaction_subject(d: dict[str, object]) -> dict[str, object]:
    """Transaction subject/target identity with subject fallback."""
    return {
        "subject_cik": _strip_cik(d.get("subject_cik")),
        "subject_name": d.get("subject_name"),
        "target_cik": _transaction_target_cik(d),
        "target_name": d.get("target_name") or d.get("target") or d.get("subject_name"),
    }


def _transaction_acquirer(d: dict[str, object]) -> dict[str, object]:
    """Transaction acquirer identity with buyer/offeror fallbacks."""
    return {
        "acquirer_cik": _strip_cik(d.get("acquirer_cik")),
        "acquirer_name": d.get("acquirer_name") or d.get("buyer") or d.get("offeror"),
    }


def _strip_cik(value: object) -> str | None:
    """CIK-ish value to stripped text; None when missing."""
    return str(value).strip() if value is not None else None


def _transaction_target_cik(d: dict[str, object]) -> str | None:
    """Explicit target CIK, else the subject CIK; None when neither exists."""
    if d.get("target_cik") is not None:
        return str(d["target_cik"]).strip()
    if d.get("subject_cik") is not None:
        return str(d["subject_cik"]).strip()
    return None


def _transaction_row(d: dict[str, object]) -> dict[str, object]:
    """Transaction input dict to a parquet row (identity fallbacks included)."""
    return {**_transaction_parties(d), **_transaction_detail(d)}


def _transaction_parties(d: dict[str, object]) -> dict[str, object]:
    """Transaction filing parties (filer, subject/target, acquirer) with fallbacks."""
    return {
        **_transaction_filer(d),
        **_transaction_subject(d),
        **_transaction_acquirer(d),
    }


def _transaction_dates(d: dict[str, object]) -> dict[str, object]:
    """Transaction status/dates with announced_at and unknown fallbacks."""
    return {
        "status": d.get("status") or "unknown",
        "filed_at": d.get("filed_at") or d.get("announced_at"),
        "known_at": d.get("known_at") or d.get("filed_at") or d.get("announced_at"),
    }


def _transaction_provenance(d: dict[str, object]) -> dict[str, object]:
    """Transaction provenance (retrieved, source, hash, parser)."""
    return {
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }


def _transaction_detail(d: dict[str, object]) -> dict[str, object]:
    """Transaction status/dates/provenance with announced_at fallbacks."""
    return {**_transaction_dates(d), **_transaction_provenance(d)}


def _simple_where(
    field: str,
    value: str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Single equality filter when the value is present."""
    if value is not None:
        where.append(f"{field} = ?")
        params.append(value)


def _cik_where(
    field: str,
    value: int | str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Stripped CIK equality filter when the value is present."""
    if value is not None:
        where.append(f"{field} = ?")
        params.append(str(value).strip())


def _asof_where(
    as_of: str | None,
    column: str,
    where: list[str],
    params: list[str | None],
) -> None:
    """Strict PIT filter when as_of is present."""
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), column)
        where.append(clause)
        params.append(param)


def _ordered_query(
    table: str,
    where: list[str],
    params: list[str | None],
    order: str,
    limit: int,
    root: Path | str | None,
) -> list[dict[str, object]]:
    """Assemble WHERE + ORDER + LIMIT and run the query."""
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" {order} LIMIT {limit}"
    return duckdb.query(sql, params, data_root=_duckdb_root(root))


def query_offerings(
    *,
    registrant: str | None = None,
    registrant_cik: int | str | None = None,
    filer_cik: int | str | None = None,
    accession: str | None = None,
    form: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Offerings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("registrant_name", registrant, where, params)
    if registrant_cik is not None:
        _registrant_cik_where(registrant_cik, where, params)
    _cik_where("filer_cik", filer_cik, where, params)
    _simple_where("accession", accession, where, params)
    _simple_where("form", form, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("sec_offerings", where, params, "ORDER BY known_at DESC", limit, root)


def store_transaction(
    row: Transaction | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Typed writer: accepts ``Transaction`` or a column row.

    Subject/target come only from structured/explicit evidence upstream;
    status stays ``unknown`` without closing evidence.
    """
    return _pass_through("sec_transactions", _transaction_row(_row_dict(row)), root)


def query_transactions(
    *,
    target: str | None = None,
    acquirer: str | None = None,
    subject_cik: int | str | None = None,
    filer_cik: int | str | None = None,
    accession: str | None = None,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Transactions newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("target_name", target, where, params)
    _simple_where("acquirer_name", acquirer, where, params)
    _cik_where("subject_cik", subject_cik, where, params)
    _cik_where("filer_cik", filer_cik, where, params)
    _simple_where("accession", accession, where, params)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("sec_transactions", where, params, "ORDER BY known_at DESC", limit, root)


def store_relationship_evidence(
    row: RelationshipEvidence | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Workflow writer: accepts ``RelationshipEvidence`` or a column row."""
    d = _row_dict(row)
    mapped = {
        "evidence_id": d.get("evidence_id"),
        "relationship_id": d.get("relationship_id"),
        "relationship_type": d.get("relationship_type"),
        "from_entity_id": d.get("from_entity_id"),
        "to_entity_id": d.get("to_entity_id"),
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name") or d.get("matched_document"),
        "source_span": d.get("source_span") or d.get("span"),
        "extraction_method": d.get("extraction_method"),
        "confidence": d.get("confidence"),
        "is_counterevidence": bool(d.get("is_counterevidence")),
        "known_at": d.get("known_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("relationship_evidence", mapped, root)


def store_relationship_revision(
    row: RelationshipRevision | Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Workflow writer: accepts ``RelationshipRevision`` or a column row."""
    d = _row_dict(row)
    mapped = {
        "revision_id": d.get("revision_id"),
        "relationship_id": d.get("relationship_id"),
        "previous_status": d.get("previous_status"),
        "new_status": d.get("new_status") or d.get("status"),
        "actor": d.get("actor"),
        "reason": d.get("reason"),
        "recorded_at": d.get("recorded_at"),
        "superseded_revision_id": d.get("superseded_revision_id"),
        "known_at": d.get("known_at") or d.get("recorded_at"),
        "retrieved_at": d.get("retrieved_at"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("relationship_revisions", mapped, root)


def _require_eval_field(d: dict[str, object], field: str) -> None:
    """Required evaluation field; raises naming the missing field."""
    if not str(d.get(field) or "").strip():
        raise ValueError(f"evaluation requires {field}")


def store_relationship_type_evaluation(
    row: Mapping[str, object],
    *,
    root: Path | str | None = None,
) -> int:
    """Append one walk-forward type-evaluation row (idempotent on rerun).

    Requires ``evaluation_id`` + ``relationship_type``; fills ``known_at``,
    ``retrieved_at``, ``content_hash``, and ``parser_version`` like every
    other workflow writer. Deterministic ``evaluation_id`` values make
    re-evaluation over identical inputs write nothing.
    """
    d: dict[str, object] = dict(row) if row else {}
    _require_eval_field(d, "evaluation_id")
    _require_eval_field(d, "relationship_type")
    return _pass_through("relationship_type_evaluations", d, root)


def query_relationship_type_evaluations(
    relationship_type: str | None = None,
    *,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Evaluation rows newest ``window_end`` first; full history is retained."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("relationship_type", relationship_type, where, params)
    return _ordered_query(
        "relationship_type_evaluations", where, params, "ORDER BY window_end DESC, retrieved_at DESC", limit, root
    )


def _evaluation_order(row: dict[str, object]) -> tuple[str, str, str]:
    """Newest evaluation (retrieved, window, id) sorts last."""
    return (str(row.get("retrieved_at") or ""), str(row.get("window_end") or ""), str(row.get("evaluation_id") or ""))


def latest_type_state(
    relationship_type: str,
    *,
    root: Path | str | None = None,
) -> tuple[str, dict[str, object] | None]:
    """Latest ``(state, row)`` for one type; ``unevaluated`` when no history."""
    rows = query_relationship_type_evaluations(relationship_type, limit=500, root=root)
    if not rows:
        return "unevaluated", None
    rows = sorted(rows, key=_evaluation_order)
    latest = rows[-1]
    return str(latest.get("new_state") or "unevaluated"), latest


def _evidence_entity_where(
    entity_id: str | None,
    where: list[str],
    params: list[str | None],
) -> None:
    """Evidence entity filter (either endpoint)."""
    if entity_id is not None:
        where.append("(from_entity_id = ? OR to_entity_id = ?)")
        params.extend([entity_id, entity_id])


def _evidence_counter_where(
    include_counterevidence: bool,
    where: list[str],
) -> None:
    """Exclude counterevidence rows unless explicitly included."""
    if not include_counterevidence:
        where.append("(is_counterevidence IS NULL OR is_counterevidence = FALSE)")


def query_relationship_evidence(
    relationship_id: str | None = None,
    *,
    entity_id: str | None = None,
    relationship_type: str | None = None,
    as_of: str | None = None,
    include_counterevidence: bool = True,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Evidence rows oldest first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("relationship_id", relationship_id, where, params)
    _evidence_entity_where(entity_id, where, params)
    _simple_where("relationship_type", relationship_type, where, params)
    _evidence_counter_where(include_counterevidence, where)
    _asof_where(as_of, "known_at", where, params)
    return _ordered_query("relationship_evidence", where, params, "ORDER BY known_at ASC", limit, root)


def query_relationship_revisions(
    relationship_id: str | None = None,
    *,
    as_of: str | None = None,
    limit: int = 200,
    root: Path | str | None = None,
) -> list[dict[str, object]]:
    """Revision rows oldest first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list[str | None] = []
    _simple_where("relationship_id", relationship_id, where, params)
    _asof_where(as_of, "recorded_at", where, params)
    return _ordered_query("relationship_revisions", where, params, "ORDER BY recorded_at ASC", limit, root)


# --- Phase 5: durable backfill queue (mutable SQLite; history stays Parquet) ---

_JOBS_LOCK = threading.Lock()
_JOBS_TABLE = "sec_backfill_jobs"


def _jobs_db_path(root: Path | str | None = None) -> Path:
    """Jobs DB lives at ``<data_root>/sec_backfill.sqlite``."""
    if root is None:
        return duckdb.DEFAULT_DATA_ROOT / "sec_backfill.sqlite"
    base = Path(root)
    if base.name == "parquet":
        base = base.parent
    return base / "sec_backfill.sqlite"


def ensure_jobs_table(root: Path | str | None = None) -> Path:
    """Create the jobs table if missing; returns the SQLite path.

    Never touches job states: interrupted ``running`` leases are recovered
    explicitly via :func:`recover_stale_jobs` at worker/drain start, so a
    live lease is never stolen mid-flight.
    """
    path = _jobs_db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_JOBS_TABLE} ("
                "id TEXT PRIMARY KEY, source TEXT NOT NULL, form TEXT NOT NULL, "
                "family TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL, "
                "parser_version TEXT NOT NULL, status TEXT NOT NULL, "
                "batch_size INTEGER NOT NULL DEFAULT 50, "
                "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
                "last_key TEXT, error TEXT)"
            )
            conn.commit()
        finally:
            conn.close()
    return path


def _backfill_job_id(
    source: str, form: str, start_date: str, end_date: str, parser_version: str, family: str | None = None
) -> str:
    digest = hashlib.sha256(
        f"{source}\n{form}\n{family or ''}\n{start_date}\n{end_date}\n{parser_version}".encode()
    ).hexdigest()[:16]
    return f"job:{digest}"


def _backfill_alias_values(
    form: str | None,
    start_date: str | None,
    end_date: str | None,
    aliases: dict[str, object],
) -> tuple[object, object, object]:
    """Explicit args with legacy alias fallbacks for form/start/end."""
    form_value: object = form if form is not None else aliases.get("form_")
    start_value: object = start_date
    if start_value is None:
        start_value = aliases.get("from_date", aliases.get("from_", aliases.get("start", aliases.get("from"))))
    end_value: object = end_date
    if end_value is None:
        end_value = aliases.get("to_date", aliases.get("to_", aliases.get("end", aliases.get("to"))))
    return form_value, start_value, end_value


def _require_backfill_source(source: str) -> None:
    """Required queue source; raises when blank."""
    if not source or not source.strip():
        raise ValueError("source is required (e.g. sec-global)")


def _require_backfill_form(form_value: object) -> None:
    """Required filing form; raises when blank."""
    if not form_value or not str(form_value).strip():
        raise ValueError("form is required (e.g. 10-K)")


def _require_backfill_dates(start_value: object, end_value: object) -> None:
    """Required start/end dates; raises when either is missing."""
    if start_value is None or end_value is None:
        raise ValueError("start/end dates are required (YYYY-MM-DD); no all-history default")


def _require_backfill_fields(
    source: str,
    form_value: object,
    start_value: object,
    end_value: object,
) -> None:
    """Required source/form/dates; raises naming the missing field."""
    _require_backfill_source(source)
    _require_backfill_form(form_value)
    _require_backfill_dates(start_value, end_value)


def _validate_backfill_range(
    source: str,
    form_value: object,
    start_value: object,
    end_value: object,
) -> tuple[str, str]:
    """Required source/form/dates to a validated (start, end) date pair."""
    _require_backfill_fields(source, form_value, start_value, end_value)
    start = _validate_date(start_value, "start_date")
    end = _validate_date(end_value, "end_date")
    if start > end:
        raise ValueError(f"invalid date range: {start!r}..{end!r}")
    return start, end


def _insert_backfill_job(
    job_id: str,
    source: str,
    form_value: object,
    family: str | None,
    start: str,
    end: str,
    parser_version: str,
    batch_size: int,
    root: Path | str | None,
) -> None:
    """Idempotent queued insert for one deterministic job ID."""
    path = ensure_jobs_table(root)
    now = _utcnow()
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            conn.execute(
                f"INSERT OR IGNORE INTO {_JOBS_TABLE} "
                "(id, source, form, family, start_date, end_date, "
                "parser_version, status, batch_size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (
                    job_id,
                    source,
                    str(form_value),
                    family if family is not None else None,
                    start,
                    end,
                    parser_version,
                    batch_size or 50,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def enqueue_backfill_job(
    source: str,
    form: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    parser_version: str = PARSER_VERSION,
    *,
    family: str | None = None,
    batch_size: int = 50,
    root: Path | str | None = None,
    **aliases: object,
) -> str:
    """Idempotent queue insert; reruns return the same deterministic ID."""
    form_value, start_value, end_value = _backfill_alias_values(form, start_date, end_date, aliases)
    if parser_version == PARSER_VERSION and aliases.get("parser") is not None:
        parser_version = str(aliases["parser"])
    start, end = _validate_backfill_range(source, form_value, start_value, end_value)
    job_id = _backfill_job_id(source, str(form_value), start, end, parser_version, family)
    _insert_backfill_job(job_id, source, form_value, family, start, end, parser_version, batch_size, root)
    return job_id


def _row_to_job(row: tuple[object, ...]) -> dict[str, object]:
    keys = (
        "id",
        "source",
        "form",
        "family",
        "start_date",
        "end_date",
        "parser_version",
        "status",
        "batch_size",
        "created_at",
        "started_at",
        "finished_at",
        "last_key",
        "error",
    )
    return dict(zip(keys, row))


def get_job(job_id: str, *, root: Path | str | None = None) -> dict[str, object] | None:
    """One job row by ID, or None."""
    path = ensure_jobs_table(root)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        cur = conn.execute(
            f"SELECT id, source, form, family, start_date, end_date, "
            f"parser_version, status, batch_size, created_at, started_at, "
            f"finished_at, last_key, error FROM {_JOBS_TABLE} WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_job(row) if row else None


def list_jobs(
    *, status: str | None = None, root: Path | str | None = None, limit: int = 200
) -> list[dict[str, object]]:
    """Jobs oldest first, optionally filtered by status."""
    path = ensure_jobs_table(root)
    sql = (
        f"SELECT id, source, form, family, start_date, end_date, "
        f"parser_version, status, batch_size, created_at, started_at, "
        f"finished_at, last_key, error FROM {_JOBS_TABLE}"
    )
    params: list[str | None] = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(status)
    sql += f" ORDER BY created_at ASC LIMIT {limit}"
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_job(r) for r in rows]


def claim_job(job_id: str | None = None, *, root: Path | str | None = None) -> dict[str, object] | None:
    """Lease one job as ``running``.

    Auto-claim takes the oldest ``queued`` job only: a ``failed`` job stays
    an explicit coverage failure until resumed (see :func:`requeue_job`),
    which also keeps queue drains from re-failing forever. An explicit
    ``job_id`` may lease a ``queued`` or ``failed`` job for targeted retry.
    """
    path = ensure_jobs_table(root)
    now = _utcnow()
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            if job_id is not None:
                cur = conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='running', "
                    "started_at=?, finished_at=NULL, error=NULL WHERE id = ? "
                    "AND status IN ('queued', 'failed')",
                    (now, job_id),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
            else:
                cur = conn.execute(
                    f"SELECT id FROM {_JOBS_TABLE} WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
                )
                found = cur.fetchone()
                if not found:
                    return None
                cur = conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='running', "
                    "started_at=?, finished_at=NULL, error=NULL WHERE id = ? "
                    "AND status = 'queued'",
                    (now, found[0]),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
                job_id = found[0]
        finally:
            conn.close()
    return get_job(str(job_id), root=root)


def complete_job(
    job_id: str, *, last_key: str | None = None, root: Path | str | None = None
) -> dict[str, object] | None:
    """Mark a leased job ``complete``."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            if last_key is not None:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='complete', finished_at=?, last_key=? WHERE id = ?",
                    (_utcnow(), last_key, job_id),
                )
            else:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='complete', finished_at=? WHERE id = ?", (_utcnow(), job_id)
                )
            conn.commit()
        finally:
            conn.close()
    return get_job(job_id, root=root)


def fail_job(
    job_id: str, error: object = "", *, last_key: str | None = None, root: Path | str | None = None
) -> dict[str, object] | None:
    """Mark a leased job ``failed``; the accession/partition is retried."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            if last_key is not None:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='failed', finished_at=?, last_key=?, error=? WHERE id = ?",
                    (_utcnow(), last_key, str(error), job_id),
                )
            else:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='failed', finished_at=?, error=? WHERE id = ?",
                    (_utcnow(), str(error), job_id),
                )
            conn.commit()
        finally:
            conn.close()
    return get_job(job_id, root=root)


def requeue_job(job_id: str, *, root: Path | str | None = None) -> dict[str, object] | None:
    """Return a ``failed``/``complete`` job to ``queued`` for resume."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            conn.execute(
                f"UPDATE {_JOBS_TABLE} SET status='queued', started_at=NULL, finished_at=NULL, error=NULL WHERE id = ?",
                (job_id,),
            )
            conn.commit()
        finally:
            conn.close()
    return get_job(job_id, root=root)


def recover_stale_jobs(*, root: Path | str | None = None) -> int:
    """Recover interrupted ``running`` leases to ``queued``; returns count."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            cur = conn.execute(f"UPDATE {_JOBS_TABLE} SET status='queued', started_at=NULL WHERE status='running'")
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def is_partition_covered(source: str, form: str, date_partition: str, *, root: Path | str | None = None) -> bool:
    """True when a ``complete`` coverage row exists for the partition."""
    try:
        rows = query_coverage(source=source, form=form, date_partition=date_partition, root=root)
    except Exception:  # noqa: BLE001 - coverage probe returns False on storage failure
        return False
    return any(str(r.get("status") or "").lower() == "complete" for r in rows)
