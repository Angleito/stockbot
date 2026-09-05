"""Normalized ``sec_filings`` store with point-in-time queries.

One row per accession, linked to raw archive paths; ``amendment_of``
links an amendment to its prior filing. ``root`` is the DATA root
(parquet rows go to ``root/'parquet'``; defaults to the warehouse root).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..storage import duckdb, parquet, raw_archive
from .models import Filing

PARSER_VERSION = "1"

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_as_of(as_of: str) -> str:
    if not _AS_OF_RE.match(str(as_of)):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    try:
        datetime.strptime(str(as_of), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}") from None
    return str(as_of)


def store_filing(
    filing: Filing,
    *,
    amendment_of: Optional[str] = None,
    raw_submission_path: Optional[Path | str] = None,
    raw_primary_path: Optional[Path | str] = None,
    retrieved_at: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> int:
    """Append one normalized filing row; returns rows written (0 on rerun)."""
    canonical = json.dumps(filing.to_dict(), sort_keys=True).encode("utf-8")
    row = {
        "accession": filing.accession_no,
        "form": filing.form,
        "cik": str(filing.filer_cik),
        "company": filing.filer_name,
        "filer_cik": str(filing.filer_cik),
        "filer_name": filing.filer_name,
        "subject_cik": str(filing.subject_cik) if filing.subject_cik is not None else None,
        "subject_name": filing.subject_name,
        "filed_at": filing.filed_at,
        "accepted_at": filing.accepted_at,
        "known_at": filing.known_at,
        "report_period": filing.report_period,
        "primary_document": filing.primary_document,
        "is_amendment": filing.is_amendment,
        "amendment_of": amendment_of if amendment_of is not None else filing.amendment_of,
        "issuer_cik": str(filing.subject_cik) if filing.subject_cik is not None else None,
        "source_url": filing.source,
        "raw_submission_path": str(raw_submission_path) if raw_submission_path is not None else None,
        "raw_primary_path": str(raw_primary_path) if raw_primary_path is not None else None,
        "retrieved_at": retrieved_at or _utcnow(),
        "content_hash": raw_archive.content_hash(canonical),
        "parser_version": PARSER_VERSION,
    }
    parquet_root = Path(root) / "parquet" if root is not None else None
    return parquet.write_rows("sec_filings", [row], root=parquet_root)


def query_filings(
    *,
    cik: Optional[int | str] = None,
    forms: Optional[list[str]] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Filings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if cik is not None:
        where.append("cik = ?")
        params.append(str(cik))
    if forms:
        where.append(f"form IN ({', '.join(['?'] * len(forms))})")
        params.extend(forms)
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_filings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


# --- Phase 4: parties, document text + FTS, search ledgers, coverage, checkpoints ---

FTS_TABLE = "document_text_fts"
FTS_ID = "fts_id"


def _parquet_root(root: Optional[Path | str] = None) -> Optional[Path]:
    return Path(root) / "parquet" if root is not None else None


def _as_dict(value) -> dict:
    return value.to_dict() if hasattr(value, "to_dict") else dict(value or {})


def _json(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def _pass_through(dataset: str, row: dict, root) -> int:
    """Minimal nullable writer: fills provenance/hash/parser, returns rows written."""
    d = dict(row)
    now = d.get("retrieved_at") or _utcnow()
    d.setdefault("retrieved_at", now)
    d["known_at"] = d.get("known_at") or now
    d["parser_version"] = d.get("parser_version") or PARSER_VERSION
    if not d.get("content_hash"):
        d["content_hash"] = raw_archive.content_hash(
            json.dumps(d, sort_keys=True, default=str).encode("utf-8"))
    return parquet.write_rows(dataset, [d], root=_parquet_root(root))


def store_filing_party(
    party,
    *,
    source_url: Optional[str] = None,
    raw_archive_path: Optional[Path | str] = None,
    document_name: Optional[str] = None,
    filed_at: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> int:
    """Append one filing-party row (accession + role + entity/CIK key)."""
    d = _as_dict(party)
    now = retrieved_at or _utcnow()
    row = {
        "accession": d.get("accession_no") or d.get("accession"),
        "role": d.get("role"),
        "entity_id": d.get("entity_id"),
        "cik": str(d["cik"]) if d.get("cik") is not None else None,
        "name": d.get("name"),
        "source": d.get("source"),
        "filed_at": filed_at or d.get("filed_at"),
        "known_at": d.get("known_at") or now,
        "retrieved_at": now,
        "source_url": source_url or d.get("source_url"),
        "raw_archive_path": str(raw_archive_path)
        if raw_archive_path is not None else d.get("raw_archive_path"),
        "document_name": document_name or d.get("document_name"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    if not row["content_hash"]:
        row["content_hash"] = raw_archive.content_hash(
            json.dumps(d, sort_keys=True, default=str).encode("utf-8"))
    return parquet.write_rows("filing_parties", [row], root=_parquet_root(root))


def query_parties(
    *,
    accession: Optional[str] = None,
    cik: Optional[int | str] = None,
    role: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Parties newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if cik is not None:
        where.append("cik = ?")
        params.append(str(cik))
    if role is not None:
        where.append("role = ?")
        params.append(str(role))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM filing_parties"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_document_text(
    doc_id: str,
    text: str | bytes,
    *,
    accession: Optional[str] = None,
    document_name: Optional[str] = None,
    source_url: Optional[str] = None,
    raw_archive_path: Optional[Path | str] = None,
    location: Optional[str] = None,
    file_type: Optional[str] = None,
    filed_at: Optional[str] = None,
    known_at: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> int:
    """Append one archived normalized document text, keyed by doc ID + hash."""
    now = retrieved_at or _utcnow()
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    row = {
        "doc_id": str(doc_id),
        "content_hash": raw_archive.content_hash(payload),
        "accession": accession,
        "document_name": document_name,
        "text": payload.decode("utf-8", errors="replace"),
        "source_url": source_url,
        "raw_archive_path": str(raw_archive_path)
        if raw_archive_path is not None else None,
        "location": location,
        "file_type": file_type,
        "filed_at": filed_at,
        "known_at": known_at or filed_at or now,
        "retrieved_at": now,
        "parser_version": PARSER_VERSION,
    }
    return parquet.write_rows("document_text", [row], root=_parquet_root(root))


def query_document_text(
    *,
    doc_id: Optional[str] = None,
    accession: Optional[str] = None,
    document_name: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 50,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Document texts newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if doc_id is not None:
        where.append("doc_id = ?")
        params.append(str(doc_id))
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if document_name is not None:
        where.append("document_name = ?")
        params.append(str(document_name))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM document_text"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def rebuild_fts(root: Optional[Path | str] = None) -> int:
    """Materialize ``document_text`` into the warehouse + native FTS index.

    Lowercase/accent-strip + Porter stemming; raises RuntimeError (the
    local-text source fails explicitly) when the FTS extension cannot load.
    Returns the number of indexed documents.
    """
    # ponytail: _connect already registers union_by_name parquet views; no
    # second view layer, just a base table + FTS index on top.
    conn = duckdb._connect(Path(root) if root is not None else None)
    try:
        try:
            conn.execute("LOAD fts")
        except Exception:
            try:
                conn.execute("INSTALL fts")
                conn.execute("LOAD fts")
            except Exception as exc:
                raise RuntimeError(
                    f"local-text source failed: FTS extension unavailable: {exc}"
                ) from exc
        conn.execute(
            f"CREATE OR REPLACE TABLE {FTS_TABLE} AS "
            "SELECT (doc_id || '#' || content_hash) AS fts_id, "
            "doc_id, content_hash, text FROM document_text"
        )
        count = conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0]
        conn.execute(
            f"PRAGMA create_fts_index('{FTS_TABLE}', '{FTS_ID}', 'text', "
            "stemmer='porter', stopwords='english', "
            "strip_accents=1, lower=1, overwrite=1)"
        )
        return int(count)
    finally:
        conn.close()


def _fts_search(
    text: str, *, limit: int, as_of: Optional[str], root,
) -> list[dict]:
    """BM25 token search over the materialized FTS index; raises when unusable."""
    conn = duckdb._connect(Path(root) if root is not None else None)
    try:
        conn.execute("LOAD fts")
        where = ["sub.score IS NOT NULL"]
        params: list = [text]
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
            "WHERE " + " AND ".join(where) +
            f" ORDER BY score DESC LIMIT {int(limit)}"
        )
        rows = conn.execute(sql, params).fetchall()
        columns = [desc[0] for desc in conn.description]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def search_document_text(
    query: str,
    *,
    literal: bool = False,
    limit: int = 50,
    as_of: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Local text search: BM25 token path by default, exact-phrase when literal.

    The token path falls back to AND-of-substrings when the FTS index or
    extension is unavailable (rebuild_fts stays the explicit health signal).
    """
    text = str(query or "").strip()
    if not text:
        raise ValueError("query must be a non-empty string")
    if not literal:
        try:
            return _fts_search(text, limit=limit, as_of=as_of, root=root)
        except Exception:
            pass
    if literal:
        where = ["lower(text) LIKE '%' || lower(?) || '%'"]
        params: list = [text]
    else:
        tokens = [token for token in text.split() if token]
        where = ["lower(text) LIKE '%' || lower(?) || '%'" for _ in tokens]
        params = list(tokens)
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = ("SELECT * FROM document_text WHERE " + " AND ".join(where) +
           f" LIMIT {int(limit)}")
    return duckdb.query(sql, params, data_root=root)


def store_attempt(attempt, *, retrieved_at: Optional[str] = None,
                  root: Optional[Path | str] = None) -> int:
    """Append one search-attempt ledger row."""
    d = _as_dict(attempt)
    row = {
        "attempt_id": d.get("attempt_id"),
        "search_id": d.get("search_id"),
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
        "retrieved_at": retrieved_at or _utcnow(),
    }
    return parquet.write_rows(
        "sec_search_attempts", [row], root=_parquet_root(root))


def store_hit(hit, *, retrieved_at: Optional[str] = None,
               root: Optional[Path | str] = None) -> int:
    """Append one text-hit ledger row."""
    d = _as_dict(hit)
    now = retrieved_at or _utcnow()
    search_id = d.get("search_id")
    query = d.get("query")
    accession = d.get("accession_no") or d.get("accession")
    matched = d.get("matched_document")
    hit_id = d.get("hit_id") or raw_archive.content_hash(
        f"{search_id}\n{d.get('attempt_id')}\n{query}\n{accession}\n{matched}"
        .encode("utf-8"))[:16]
    items = d.get("items")
    row = {
        "hit_id": hit_id,
        "search_id": search_id,
        "attempt_id": d.get("attempt_id"),
        "query": query,
        "accession": accession,
        "filer_cik": str(d["filer_cik"]) if d.get("filer_cik") is not None else None,
        "filer_name": d.get("filer_name"),
        "form": d.get("form"),
        "filed_at": d.get("filed_at"),
        "matched_document": matched,
        "file_type": d.get("file_type"),
        "file_description": d.get("file_description"),
        "items_json": _json(list(items) if items is not None else None),
        "sic": d.get("sic"),
        "location": d.get("location"),
        "state": d.get("state"),
        "inc_state": d.get("inc_state"),
        "score": d.get("score") or 0.0,
        "source_url": d.get("source_url"),
        "page": d.get("page") or 1,
        "known_at": d.get("known_at") or d.get("filed_at") or now,
        "retrieved_at": now,
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
        "raw_archive_path": d.get("raw_archive_path"),
    }
    return parquet.write_rows("sec_text_hits", [row], root=_parquet_root(root))


def persist_search_ledger(
    *,
    search_id: str,
    request,
    entities=(),
    filings=(),
    documents=(),
    text_hits=(),
    attempts=(),
    coverage_status: str = "complete",
    sources_attempted=(),
    sources_completed=(),
    sources_failed=(),
    source_limits=(),
    results_reported: int = 0,
    results_retrieved: int = 0,
    warnings=(),
    errors=(),
    evidence_packet_ids=(),
    pending_backfill_jobs=(),
    forms_covered=(),
    pages=1,
    date_coverage=None,
    root: Optional[Path | str] = None,
) -> dict:
    """Persist one interactive search: request, attempts, hits, coverage.

    Returns ``{"searches": n, "attempts": n, "hits": n}`` rows written.
    """
    now = _utcnow()
    req = _as_dict(request)
    search_row = {
        "search_id": str(search_id),
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
        "pending_jobs_json": _json(list(
            pending_backfill_jobs or req.get("pending_backfill_jobs") or [])),
        "warnings_json": _json(list(warnings)),
        "errors_json": _json(list(errors)),
        "evidence_packet_ids_json": _json(list(evidence_packet_ids)),
        "dedup_counts_json": _json({
            "entities": len(tuple(entities)),
            "filings": len(tuple(filings)),
            "documents": len(tuple(documents)),
            "text_hits": len(tuple(text_hits)),
            "attempts": len(tuple(attempts)),
        }),
        "retrieved_at": now,
        "known_at": now,
        "parser_version": PARSER_VERSION,
    }
    attempt_rows = []
    for attempt in attempts:
        d = _as_dict(attempt)
        attempt_rows.append({
            "attempt_id": d.get("attempt_id"),
            "search_id": str(search_id),
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
        })
    hit_rows = []
    for hit in text_hits:
        d = _as_dict(hit)
        accession = d.get("accession_no") or d.get("accession")
        matched = d.get("matched_document")
        items = d.get("items")
        hit_rows.append({
            "hit_id": raw_archive.content_hash(
                f"{search_id}\n{d.get('attempt_id')}\n{d.get('query')}\n"
                f"{accession}\n{matched}".encode("utf-8"))[:16],
            "search_id": str(search_id),
            "attempt_id": d.get("attempt_id"),
            "query": d.get("query"),
            "accession": accession,
            "filer_cik": str(d["filer_cik"])
            if d.get("filer_cik") is not None else None,
            "filer_name": d.get("filer_name"),
            "form": d.get("form"),
            "filed_at": d.get("filed_at"),
            "matched_document": matched,
            "file_type": d.get("file_type"),
            "file_description": d.get("file_description"),
            "items_json": _json(list(items) if items is not None else None),
            "sic": d.get("sic"),
            "location": d.get("location"),
            "state": d.get("state"),
            "inc_state": d.get("inc_state"),
            "score": d.get("score") or 0.0,
            "source_url": d.get("source_url"),
            "page": d.get("page") or 1,
            "known_at": d.get("known_at") or d.get("filed_at") or now,
            "retrieved_at": now,
            "content_hash": d.get("content_hash"),
            "parser_version": d.get("parser_version") or PARSER_VERSION,
            "raw_archive_path": d.get("raw_archive_path"),
        })
    proot = _parquet_root(root)
    return {
        "searches": parquet.write_rows("sec_searches", [search_row], root=proot),
        "attempts": parquet.write_rows(
            "sec_search_attempts", attempt_rows, root=proot),
        "hits": parquet.write_rows("sec_text_hits", hit_rows, root=proot),
    }


def query_search(
    search_id: str, *, root: Optional[Path | str] = None,
) -> Optional[dict]:
    """One persisted search-ledger row, or None."""
    rows = duckdb.query("SELECT * FROM sec_searches WHERE search_id = ? LIMIT 1",
                        [str(search_id)], data_root=root)
    return rows[0] if rows else None


def query_attempts(
    search_id: str, *, root: Optional[Path | str] = None,
) -> list[dict]:
    """All persisted attempt rows for one search, in attempt order."""
    return duckdb.query(
        "SELECT * FROM sec_search_attempts WHERE search_id = ? ORDER BY attempt_id",
        [str(search_id)], data_root=root)


def query_hits(
    search_id: str, *, root: Optional[Path | str] = None,
) -> list[dict]:
    """All persisted text-hit rows for one search, best score first."""
    return duckdb.query(
        "SELECT * FROM sec_text_hits WHERE search_id = ? ORDER BY score DESC",
        [str(search_id)], data_root=root)


def store_coverage(
    source: str,
    form: str,
    date_partition: str,
    status: str,
    *,
    family: Optional[str] = None,
    coverage_date: Optional[str] = None,
    accession_count: int = 0,
    last_key: Optional[str] = None,
    known_at: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> int:
    """Append one ingestion-coverage row (source + form + partition key)."""
    now = retrieved_at or _utcnow()
    row = {
        "source": str(source),
        "form": str(form),
        "family": family,
        "date_partition": str(date_partition),
        "coverage_date": coverage_date or str(date_partition)[:10],
        "status": str(status),
        "accession_count": accession_count or 0,
        "last_key": last_key,
        "parser_version": PARSER_VERSION,
        "known_at": known_at or now,
        "retrieved_at": now,
    }
    return parquet.write_rows(
        "sec_ingestion_coverage", [row], root=_parquet_root(root))


def query_coverage(
    *,
    source: Optional[str] = None,
    form: Optional[str] = None,
    date_partition: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Coverage rows, newest first."""
    where: list[str] = []
    params: list = []
    if source is not None:
        where.append("source = ?")
        params.append(str(source))
    if form is not None:
        where.append("form = ?")
        params.append(str(form))
    if date_partition is not None:
        where.append("date_partition = ?")
        params.append(str(date_partition))
    sql = "SELECT * FROM sec_ingestion_coverage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY coverage_date DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    status: str,
    *,
    payload_hash: str = "",
    record_count: int = 0,
    last_key: Optional[str] = None,
    error: Optional[str] = None,
    totals: Optional[dict] = None,
    parser_version: str = PARSER_VERSION,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    root: Optional[Path | str] = None,
) -> int:
    """Append one checkpoint row; reruns with identical keys write nothing."""
    now = _utcnow()
    row = {
        "pipeline": str(pipeline),
        "source": str(source),
        "key": str(key),
        "payload_hash": str(payload_hash or ""),
        "status": str(status),
        "record_count": record_count or 0,
        "started_at": started_at or now,
        "finished_at": finished_at,
        "parser_version": parser_version,
        "last_key": last_key,
        "error": error,
        "totals_json": _json(totals),
    }
    return parquet.write_rows(
        "ingestion_checkpoints", [row], root=_parquet_root(root))


def get_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    *,
    root: Optional[Path | str] = None,
) -> Optional[dict]:
    """Latest checkpoint row for a (pipeline, source, key), or None.

    A ``complete`` row means resume skips the partition; ``failed`` (or no
    row) means the accession/partition is retried.
    """
    rows = duckdb.query(
        "SELECT * FROM ingestion_checkpoints "
        "WHERE pipeline = ? AND source = ? AND key = ?",
        [str(pipeline), str(source), str(key)], data_root=root)
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("finished_at") or "",
                             r.get("started_at") or ""))
    return rows[-1]


def advance_checkpoint(
    pipeline: str,
    source: str,
    key: str,
    *,
    last_key: Optional[str] = None,
    record_count: int = 0,
    totals: Optional[dict] = None,
    payload_hash: str = "",
    parser_version: str = PARSER_VERSION,
    root: Optional[Path | str] = None,
) -> int:
    """Record a partition complete. Call only after immutable archive and all
    normalized writes commit; a rerun after completion writes nothing."""
    now = _utcnow()
    prior = get_checkpoint(pipeline, source, key, root=root)
    started = (prior or {}).get("started_at") or now
    return store_checkpoint(
        pipeline, source, key, "complete",
        payload_hash=payload_hash, record_count=record_count,
        last_key=last_key, totals=totals, parser_version=parser_version,
        started_at=started, finished_at=now, root=root)


def _row_dict(value) -> dict:
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict())
        except Exception:
            pass
    return dict(value or {})


def _encode_power(sole, shared) -> "Optional[str]":
    parts = []
    for label, value in (("sole", sole), ("shared", shared)):
        if value is None:
            continue
        try:
            parts.append(f"{label}={int(value)}")
        except Exception:
            continue
    return " ".join(parts) or None


def store_beneficial_ownership(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Typed writer: accepts ``BeneficialOwnership`` or a column row."""
    d = _row_dict(row)
    filer_name = d.get("filer_name") or d.get("reporter_name")
    mapped = {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "subject_cik": str(d["subject_cik"]).strip() if d.get("subject_cik") is not None else None,
        "subject_name": d.get("subject_name"),
        "filer_cik": str(d["filer_cik"]).strip() if d.get("filer_cik") is not None else None,
        "filer_name": filer_name,
        "reporter_name": d.get("reporter_name") or filer_name,
        "shares": d.get("shares"),
        "percent": d.get("percent"),
        "voting_power": d.get("voting_power") or _encode_power(
            d.get("sole_voting"), d.get("shared_voting")),
        "dispositive_power": d.get("dispositive_power") or _encode_power(
            d.get("sole_dispositive"), d.get("shared_dispositive")),
        "purpose": d.get("purpose") or d.get("purpose_text"),
        "form": d.get("form"),
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("sec_beneficial_ownership", mapped, root)


def query_beneficial_ownership(
    *,
    subject_cik: "Optional[int | str]" = None,
    owner_cik: "Optional[int | str]" = None,
    filer_cik: "Optional[int | str]" = None,
    accession: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Typed rows newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    owner = owner_cik if owner_cik is not None else filer_cik
    where: list[str] = []
    params: list = []
    if subject_cik is not None:
        where.append("subject_cik = ?")
        params.append(str(subject_cik).strip())
    if owner is not None:
        where.append("filer_cik = ?")
        params.append(str(owner).strip())
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_beneficial_ownership"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_13f_holding(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Typed writer: accepts ``InstitutionalHolding`` or a column row."""
    d = _row_dict(row)
    mapped = {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "manager_cik": str(d["manager_cik"]).strip() if d.get("manager_cik") is not None else None,
        "manager_name": d.get("manager_name"),
        "report_period": d.get("report_period"),
        "issuer_name": d.get("issuer_name"),
        "entity_id": d.get("entity_id"),
        "security_id": d.get("security_id"),
        "class_title": d.get("class_title"),
        "cusip": str(d["cusip"]).strip().upper() if d.get("cusip") is not None else None,
        "isin": str(d["isin"]).strip().upper() if d.get("isin") is not None else None,
        "shares": d.get("shares"),
        "value": d.get("value"),
        "put_call": d.get("put_call"),
        "discretion": d.get("discretion"),
        "voting": d.get("voting"),
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("sec_13f_holdings", mapped, root)


def query_13f_holdings(
    *,
    manager_cik: "Optional[int | str]" = None,
    entity_id: Optional[str] = None,
    security_id: Optional[str] = None,
    security: Optional[str] = None,
    cusip: Optional[str] = None,
    isin: Optional[str] = None,
    accession: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Holdings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD.

    ``security`` matches CUSIP, ISIN, or ``security_id`` (all uppercased).
    """
    where: list[str] = []
    params: list = []
    if manager_cik is not None:
        where.append("manager_cik = ?")
        params.append(str(manager_cik).strip())
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(str(entity_id).strip())
    if security_id is not None:
        where.append("security_id = ?")
        params.append(str(security_id).strip())
    key = security if security is not None else cusip if cusip is not None else isin
    if key is not None:
        normalized = str(key).strip().upper()
        where.append("(cusip = ? OR isin = ? OR security_id = ?)")
        params.extend([normalized, normalized, normalized])
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_13f_holdings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_insider_transaction(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Typed writer: accepts ``InsiderTransaction`` or a column row."""
    d = _row_dict(row)
    mapped = {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form"),
        "issuer_cik": str(d["issuer_cik"]).strip() if d.get("issuer_cik") is not None else None,
        "issuer_name": d.get("issuer_name") or d.get("issuer"),
        "owner_cik": str(d.get("owner_cik") or d.get("insider_cik") or "").strip() or None,
        "owner_name": d.get("owner_name") or d.get("insider_name"),
        "is_director": d.get("is_director"),
        "is_officer": d.get("is_officer"),
        "is_ten_percent": d.get("is_ten_percent"),
        "is_other": d.get("is_other"),
        "role_title": d.get("role_title"),
        "security_title": d.get("security_title") or d.get("security"),
        "transaction_code": d.get("transaction_code"),
        "transaction_date": d.get("transaction_date"),
        "shares": d.get("shares"),
        "price": d.get("price"),
        "holdings": d.get("holdings") if d.get("holdings") is not None else d.get("holdings_after"),
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("sec_insider_transactions", mapped, root)


def query_insider_transactions(
    *,
    issuer_cik: "Optional[int | str]" = None,
    owner_cik: "Optional[int | str]" = None,
    accession: Optional[str] = None,
    form: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Insider rows newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if issuer_cik is not None:
        where.append("issuer_cik = ?")
        params.append(str(issuer_cik).strip())
    if owner_cik is not None:
        where.append("owner_cik = ?")
        params.append(str(owner_cik).strip())
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if form is not None:
        where.append("form = ?")
        params.append(str(form))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_insider_transactions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_offering(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Typed writer: accepts ``Offering`` or a column row.

    The registrant defaults to the issuer, never the reverse; amounts stay
    proposed/registered upstream, never issuance.
    """
    d = _row_dict(row)
    issuer = d.get("issuer")
    mapped = {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form"),
        "filer_cik": str(d["filer_cik"]).strip() if d.get("filer_cik") is not None else None,
        "filer_name": d.get("filer_name"),
        "registrant_cik": str(d["registrant_cik"]).strip() if d.get("registrant_cik") is not None else None,
        "registrant_name": d.get("registrant_name") or issuer,
        "security_title": d.get("security_title") or d.get("offering_type"),
        "amount": d.get("amount") if d.get("amount") is not None else d.get("gross_proceeds"),
        "filed_at": d.get("filed_at"),
        "known_at": d.get("known_at") or d.get("filed_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("sec_offerings", mapped, root)


def query_offerings(
    *,
    registrant: Optional[str] = None,
    registrant_cik: "Optional[int | str]" = None,
    filer_cik: "Optional[int | str]" = None,
    accession: Optional[str] = None,
    form: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Offerings newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if registrant is not None:
        where.append("registrant_name = ?")
        params.append(str(registrant))
    if registrant_cik is not None:
        # ponytail: canonical bare CIK plus SEC 10-digit padding match legacy rows
        try:
            _canon = str(int(str(registrant_cik).strip()))
        except (TypeError, ValueError, AttributeError):
            _canon = None
        if _canon is not None:
            where.append("(registrant_cik = ? OR registrant_cik = ?)")
            params.extend([_canon, f"{int(_canon):010d}"])
        else:
            where.append("registrant_cik = ?")
            params.append(str(registrant_cik).strip())
    if filer_cik is not None:
        where.append("filer_cik = ?")
        params.append(str(filer_cik).strip())
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if form is not None:
        where.append("form = ?")
        params.append(str(form))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_offerings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_transaction(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Typed writer: accepts ``Transaction`` or a column row.

    Subject/target come only from structured/explicit evidence upstream;
    status stays ``unknown`` without closing evidence.
    """
    d = _row_dict(row)
    mapped = {
        "accession": d.get("accession") or d.get("accession_no"),
        "document_name": d.get("document_name"),
        "form": d.get("form") or d.get("deal_type"),
        "filer_cik": str(d["filer_cik"]).strip() if d.get("filer_cik") is not None else None,
        "filer_name": d.get("filer_name"),
        "subject_cik": str(d["subject_cik"]).strip() if d.get("subject_cik") is not None else None,
        "subject_name": d.get("subject_name"),
        "target_cik": str(d["target_cik"]).strip() if d.get("target_cik") is not None else (
            str(d["subject_cik"]).strip() if d.get("subject_cik") is not None else None),
        "target_name": d.get("target_name") or d.get("target") or d.get("subject_name"),
        "acquirer_cik": str(d["acquirer_cik"]).strip() if d.get("acquirer_cik") is not None else None,
        "acquirer_name": d.get("acquirer_name") or d.get("buyer") or d.get("offeror"),
        "status": d.get("status") or "unknown",
        "filed_at": d.get("filed_at") or d.get("announced_at"),
        "known_at": d.get("known_at") or d.get("filed_at") or d.get("announced_at"),
        "retrieved_at": d.get("retrieved_at"),
        "source_url": d.get("source_url") or d.get("source"),
        "raw_archive_path": d.get("raw_archive_path"),
        "content_hash": d.get("content_hash"),
        "parser_version": d.get("parser_version") or PARSER_VERSION,
    }
    return _pass_through("sec_transactions", mapped, root)


def query_transactions(
    *,
    target: Optional[str] = None,
    acquirer: Optional[str] = None,
    subject_cik: "Optional[int | str]" = None,
    filer_cik: "Optional[int | str]" = None,
    accession: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Transactions newest ``known_at`` first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if target is not None:
        where.append("target_name = ?")
        params.append(str(target))
    if acquirer is not None:
        where.append("acquirer_name = ?")
        params.append(str(acquirer))
    if subject_cik is not None:
        where.append("subject_cik = ?")
        params.append(str(subject_cik).strip())
    if filer_cik is not None:
        where.append("filer_cik = ?")
        params.append(str(filer_cik).strip())
    if accession is not None:
        where.append("accession = ?")
        params.append(str(accession))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM sec_transactions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def store_relationship_evidence(
    row: dict, *, root: Optional[Path | str] = None,
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
    row: dict, *, root: Optional[Path | str] = None,
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


def store_relationship_type_evaluation(
    row: dict, *, root: Optional[Path | str] = None,
) -> int:
    """Append one walk-forward type-evaluation row (idempotent on rerun).

    Requires ``evaluation_id`` + ``relationship_type``; fills ``known_at``,
    ``retrieved_at``, ``content_hash``, and ``parser_version`` like every
    other workflow writer. Deterministic ``evaluation_id`` values make
    re-evaluation over identical inputs write nothing.
    """
    d = dict(row or {})
    if not str(d.get("evaluation_id") or "").strip():
        raise ValueError("evaluation requires evaluation_id")
    if not str(d.get("relationship_type") or "").strip():
        raise ValueError("evaluation requires relationship_type")
    return _pass_through("relationship_type_evaluations", d, root)


def query_relationship_type_evaluations(
    relationship_type: str | None = None, *,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Evaluation rows newest ``window_end`` first; full history is retained."""
    where: list[str] = []
    params: list = []
    if relationship_type is not None:
        where.append("relationship_type = ?")
        params.append(str(relationship_type))
    sql = "SELECT * FROM relationship_type_evaluations"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY window_end DESC, retrieved_at DESC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def latest_type_state(
    relationship_type: str, *,
    root: Optional[Path | str] = None,
) -> tuple[str, Optional[dict]]:
    """Latest ``(state, row)`` for one type; ``unevaluated`` when no history."""
    rows = query_relationship_type_evaluations(
        relationship_type, limit=500, root=root)
    if not rows:
        return "unevaluated", None
    rows = sorted(rows, key=lambda r: (
        str(r.get("retrieved_at") or ""), str(r.get("window_end") or ""),
        str(r.get("evaluation_id") or "")))
    latest = rows[-1]
    return str(latest.get("new_state") or "unevaluated"), latest


def query_relationship_evidence(
    relationship_id: str | None = None, *,
    entity_id: str | None = None,
    relationship_type: str | None = None,
    as_of: Optional[str] = None,
    include_counterevidence: bool = True,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Evidence rows oldest first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if relationship_id is not None:
        where.append("relationship_id = ?")
        params.append(str(relationship_id))
    if entity_id is not None:
        where.append("(from_entity_id = ? OR to_entity_id = ?)")
        params.extend([str(entity_id), str(entity_id)])
    if relationship_type is not None:
        where.append("relationship_type = ?")
        params.append(str(relationship_type))
    if not include_counterevidence:
        where.append("(is_counterevidence IS NULL OR is_counterevidence = FALSE)")
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "known_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM relationship_evidence"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY known_at ASC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


def query_relationship_revisions(
    relationship_id: str | None = None, *,
    as_of: Optional[str] = None,
    limit: int = 200,
    root: Optional[Path | str] = None,
) -> list[dict]:
    """Revision rows oldest first; ``as_of`` is strict YYYY-MM-DD."""
    where: list[str] = []
    params: list = []
    if relationship_id is not None:
        where.append("relationship_id = ?")
        params.append(str(relationship_id))
    if as_of is not None:
        clause, param = duckdb.as_of_clause(_validate_as_of(as_of), "recorded_at")
        where.append(clause)
        params.append(param)
    sql = "SELECT * FROM relationship_revisions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY recorded_at ASC LIMIT {int(limit)}"
    return duckdb.query(sql, params, data_root=root)


# --- Phase 5: durable backfill queue (mutable SQLite; history stays Parquet) ---

_JOBS_LOCK = threading.Lock()
_JOBS_TABLE = "sec_backfill_jobs"


def _jobs_db_path(root: Optional[Path | str] = None) -> Path:
    """Jobs DB lives at ``<data_root>/sec_backfill.sqlite``."""
    if root is None:
        return duckdb.DEFAULT_DATA_ROOT / "sec_backfill.sqlite"
    base = Path(root)
    if base.name == "parquet":
        base = base.parent
    return base / "sec_backfill.sqlite"


def _validate_date(value: object, label: str) -> str:
    text = str(value or "")
    if not _AS_OF_RE.match(text):
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}") from None
    return text

def ensure_jobs_table(root: Optional[Path | str] = None) -> Path:
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


def _backfill_job_id(source: str, form: str, start_date: str,
                     end_date: str, parser_version: str,
                     family: Optional[str] = None) -> str:
    digest = hashlib.sha256(
        f"{source}\n{form}\n{family or ''}\n{start_date}\n{end_date}\n"
        f"{parser_version}".encode("utf-8")).hexdigest()[:16]
    return f"job:{digest}"


def enqueue_backfill_job(source: str, form: Optional[str] = None,
                         start_date: Optional[str] = None,
                         end_date: Optional[str] = None,
                         parser_version: str = PARSER_VERSION, *,
                         family: Optional[str] = None,
                         batch_size: int = 50,
                         root: Optional[Path | str] = None,
                         **aliases) -> str:
    """Idempotent queue insert; reruns return the same deterministic ID."""
    form = form if form is not None else aliases.get("form_")
    if start_date is None:
        start_date = aliases.get("from_date", aliases.get("from_",
                                 aliases.get("start", aliases.get("from"))))
    if end_date is None:
        end_date = aliases.get("to_date", aliases.get("to_",
                               aliases.get("end", aliases.get("to"))))
    if parser_version == PARSER_VERSION and aliases.get("parser") is not None:
        parser_version = aliases["parser"]
    if not source or not str(source).strip():
        raise ValueError("source is required (e.g. sec-global)")
    if not form or not str(form).strip():
        raise ValueError("form is required (e.g. 10-K)")
    if start_date is None or end_date is None:
        raise ValueError("start/end dates are required (YYYY-MM-DD); no all-history default")
    start = _validate_date(start_date, "start_date")
    end = _validate_date(end_date, "end_date")
    if start > end:
        raise ValueError(f"invalid date range: {start!r}..{end!r}")
    job_id = _backfill_job_id(
        str(source), str(form), start, end, str(parser_version), family)
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
                (job_id, str(source), str(form),
                 str(family) if family is not None else None,
                 start, end, str(parser_version), int(batch_size or 50), now),
            )
            conn.commit()
        finally:
            conn.close()
    return job_id


def _row_to_job(row: tuple) -> dict:
    keys = ("id", "source", "form", "family", "start_date", "end_date",
            "parser_version", "status", "batch_size", "created_at",
            "started_at", "finished_at", "last_key", "error")
    return dict(zip(keys, row))


def get_job(job_id: str, *,
            root: Optional[Path | str] = None) -> Optional[dict]:
    """One job row by ID, or None."""
    path = ensure_jobs_table(root)
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        cur = conn.execute(
            f"SELECT id, source, form, family, start_date, end_date, "
            f"parser_version, status, batch_size, created_at, started_at, "
            f"finished_at, last_key, error FROM {_JOBS_TABLE} WHERE id = ?",
            (str(job_id),))
        row = cur.fetchone()
    finally:
        conn.close()
    return _row_to_job(row) if row else None


def list_jobs(*, status: Optional[str] = None,
              root: Optional[Path | str] = None,
              limit: int = 200) -> list[dict]:
    """Jobs oldest first, optionally filtered by status."""
    path = ensure_jobs_table(root)
    sql = (f"SELECT id, source, form, family, start_date, end_date, "
           f"parser_version, status, batch_size, created_at, started_at, "
           f"finished_at, last_key, error FROM {_JOBS_TABLE}")
    params: list = []
    if status is not None:
        sql += " WHERE status = ?"
        params.append(str(status))
    sql += f" ORDER BY created_at ASC LIMIT {int(limit)}"
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_job(r) for r in rows]


def claim_job(job_id: Optional[str] = None, *,
              root: Optional[Path | str] = None) -> Optional[dict]:
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
                    (now, str(job_id)))
                conn.commit()
                if cur.rowcount == 0:
                    return None
            else:
                cur = conn.execute(
                    f"SELECT id FROM {_JOBS_TABLE} WHERE status = 'queued' "
                    "ORDER BY created_at ASC LIMIT 1")
                found = cur.fetchone()
                if not found:
                    return None
                cur = conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='running', "
                    "started_at=?, finished_at=NULL, error=NULL WHERE id = ? "
                    "AND status = 'queued'",
                    (now, found[0]))
                conn.commit()
                if cur.rowcount == 0:
                    return None
                job_id = found[0]
        finally:
            conn.close()
    return get_job(str(job_id), root=root)

def complete_job(job_id: str, *, last_key: Optional[str] = None,
                 root: Optional[Path | str] = None) -> Optional[dict]:
    """Mark a leased job ``complete``."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            if last_key is not None:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='complete', "
                    "finished_at=?, last_key=? WHERE id = ?",
                    (_utcnow(), str(last_key), str(job_id)))
            else:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='complete', "
                    "finished_at=? WHERE id = ?",
                    (_utcnow(), str(job_id)))
            conn.commit()
        finally:
            conn.close()
    return get_job(str(job_id), root=root)


def fail_job(job_id: str, error: object = "", *,
             last_key: Optional[str] = None,
             root: Optional[Path | str] = None) -> Optional[dict]:
    """Mark a leased job ``failed``; the accession/partition is retried."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            if last_key is not None:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='failed', "
                    "finished_at=?, last_key=?, error=? WHERE id = ?",
                    (_utcnow(), str(last_key), str(error), str(job_id)))
            else:
                conn.execute(
                    f"UPDATE {_JOBS_TABLE} SET status='failed', "
                    "finished_at=?, error=? WHERE id = ?",
                    (_utcnow(), str(error), str(job_id)))
            conn.commit()
        finally:
            conn.close()
    return get_job(str(job_id), root=root)


def requeue_job(job_id: str, *,
                root: Optional[Path | str] = None) -> Optional[dict]:
    """Return a ``failed``/``complete`` job to ``queued`` for resume."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            conn.execute(
                f"UPDATE {_JOBS_TABLE} SET status='queued', started_at=NULL, "
                "finished_at=NULL, error=NULL WHERE id = ?",
                (str(job_id),))
            conn.commit()
        finally:
            conn.close()
    return get_job(str(job_id), root=root)


def recover_stale_jobs(*,
                       root: Optional[Path | str] = None) -> int:
    """Recover interrupted ``running`` leases to ``queued``; returns count."""
    path = ensure_jobs_table(root)
    with _JOBS_LOCK:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            cur = conn.execute(
                f"UPDATE {_JOBS_TABLE} SET status='queued', started_at=NULL "
                "WHERE status='running'")
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()


def is_partition_covered(source: str, form: str, date_partition: str, *,
                         root: Optional[Path | str] = None) -> bool:
    """True when a ``complete`` coverage row exists for the partition."""
    try:
        rows = query_coverage(source=str(source), form=str(form),
                              date_partition=str(date_partition), root=root)
    except Exception:
        return False
    return any((r.get("status") or "").lower() == "complete" for r in rows)
