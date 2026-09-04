"""Normalized ``sec_filings`` store with point-in-time queries.

One row per accession, linked to raw archive paths; ``amendment_of``
links an amendment to its prior filing. ``root`` is the DATA root
(parquet rows go to ``root/'parquet'``; defaults to the warehouse root).
"""

from __future__ import annotations

import json
import re
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
        "cik": str(filing.cik),
        "company": filing.company,
        "filed_at": filing.filed_at,
        "accepted_at": filing.accepted_at,
        "known_at": filing.known_at,
        "report_period": filing.report_period,
        "primary_document": filing.primary_document,
        "is_amendment": filing.is_amendment,
        "amendment_of": amendment_of if amendment_of is not None else filing.amendment_of,
        "issuer_cik": str(filing.issuer_cik),
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
