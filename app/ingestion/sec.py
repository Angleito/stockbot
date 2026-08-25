"""SEC EDGAR ingestion pipeline.

Sources: company_tickers.json (entity/alias universe) and XBRL company
facts (financial_facts for the common-share outstanding concept; the full
payload is archived for later complete parsing).

SEC compliance: requests are serialized and paced at ~8/sec (0.13s
interval), and retry with exponential backoff on 429/5xx.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import requests

from ..normalization.sec import (
    normalize_company_facts,
    normalize_company_tickers,
)
from ..storage import duckdb, ids, parquet, raw_archive
from .base import (
    Checkpointer,
    Connector,
    FetchResult,
    Pacing,
    retry_policy,
    summarize,
    utc_now,
)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
TICKERS_PIPELINE = "sec_company_tickers"
FACTS_PIPELINE = "sec_company_facts"
# Reuse a recent facts checkpoint before refetching (mirrors the screen TTL).
FACTS_TTL_SECONDS = 7 * 86400


def _sec_headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("SEC_EDGAR_IDENTITY", "stockbot research contact@example.com"),
        "Accept-Encoding": "gzip, deflate",
    }


class SecConnector(Connector):
    """Fetches raw SEC payloads with pacing and retry/backoff."""

    source = "sec"

    def __init__(self, pacing: Optional[Pacing] = None):
        self.pacing = pacing or Pacing(min_interval_seconds=0.13)

    def _get(self, url: str) -> requests.Response:
        self.pacing.wait()
        return requests.get(url, headers=_sec_headers(), timeout=60)

    def _get_with_retry(self, url: str) -> requests.Response:
        return retry_policy(self._get, url)

    def fetch_tickers(self) -> FetchResult:
        response = self._get_with_retry(SEC_TICKERS_URL)
        return FetchResult(
            key="company_tickers",
            payload=response.content,
            url=SEC_TICKERS_URL,
            kind="company_tickers",
            metadata={"retrieved_at": utc_now(), "status": response.status_code},
        )

    def fetch_company_facts(self, cik: int) -> FetchResult:
        url = SEC_FACTS_URL.format(cik=cik)
        try:
            response = self._get_with_retry(url)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                # SEC has no company-facts resource for this CIK (foreign
                # private issuers, funds, some shells).  Explicit no-facts
                # result, not an error: the caller records it as a negative
                # instead of aborting the whole batch.
                return FetchResult(
                    key=f"cik{cik:010d}",
                    payload=b"",
                    url=url,
                    kind="companyfacts",
                    metadata={
                        "retrieved_at": utc_now(),
                        "cik": cik,
                        "status": 404,
                        "no_companyfacts": True,
                    },
                )
            raise
        return FetchResult(
            key=f"cik{cik:010d}",
            payload=response.content,
            url=url,
            kind="companyfacts",
            metadata={"retrieved_at": utc_now(), "cik": cik, "status": response.status_code},
        )


def _write_datasets(datasets: dict[str, list[dict]], data_root: Path) -> int:
    written = 0
    for name, rows in datasets.items():
        written += parquet.write_rows(name, rows, root=data_root / "parquet")
    return written


def ingest_company_tickers(
    data_root: Path,
    archive_root: Optional[Path] = None,
    pacing: Optional[Pacing] = None,
    connector: Optional[SecConnector] = None,
) -> dict:
    """Fetch, archive, normalize, and checkpoint company_tickers.json."""
    archive_root = archive_root or data_root / "raw"
    connector = connector or SecConnector(pacing)
    checkpointer = Checkpointer(data_root)
    started_at = utc_now()
    if checkpointer.is_fresh_for_key(TICKERS_PIPELINE, "sec", "company_tickers", FACTS_TTL_SECONDS):
        return summarize("complete", skipped=1, written=0, total=1)
    result = connector.fetch_tickers()
    record = raw_archive.archive(
        "sec", "company_tickers", result.key, result.payload,
        url=result.url, retrieved_at=result.metadata.get("retrieved_at"),
        root=archive_root,
    )
    if checkpointer.is_complete(TICKERS_PIPELINE, "sec", result.key, record.sha256):
        return summarize("complete", skipped=1, written=0, total=1)
    raw = json.loads(record.payload_path.read_text())
    datasets = normalize_company_tickers(
        raw, retrieved_at=record.retrieved_at, content_hash=record.sha256,
    )
    written = _write_datasets(datasets, data_root)
    checkpointer.complete(
        TICKERS_PIPELINE, "sec", result.key, record.sha256,
        record_count=written, started_at=started_at,
    )
    return summarize("complete", skipped=0, written=1, total=1)


def ingest_company_facts(
    cik: int,
    data_root: Path,
    archive_root: Optional[Path] = None,
    pacing: Optional[Pacing] = None,
    connector: Optional[SecConnector] = None,
) -> dict:
    """Fetch, archive, normalize, and checkpoint one CIK's company facts."""
    archive_root = archive_root or data_root / "raw"
    connector = connector or SecConnector(pacing)
    checkpointer = Checkpointer(data_root)
    started_at = utc_now()
    key = f"cik{cik:010d}"
    if checkpointer.is_fresh_for_key(FACTS_PIPELINE, "sec", key, FACTS_TTL_SECONDS):
        return summarize("complete", skipped=1, written=0, total=1)
    result = connector.fetch_company_facts(cik)
    if result.metadata.get("no_companyfacts"):
        # SEC reports no company-facts resource for this CIK.  Record the
        # negative as a complete checkpoint (empty payload hash) so reruns
        # within the TTL skip it without re-probing SEC; nothing is archived
        # or normalized for a CIK that has no facts.
        checkpointer.complete(
            FACTS_PIPELINE, "sec", key, raw_archive.content_hash(result.payload),
            record_count=0, started_at=started_at,
        )
        summary = summarize("complete", skipped=0, written=0, total=1)
        summary["no_companyfacts"] = True
        return summary
    record = raw_archive.archive(
        "sec", "companyfacts", key, result.payload,
        url=result.url, retrieved_at=result.metadata.get("retrieved_at"),
        metadata={"cik": cik}, root=archive_root,
    )
    if checkpointer.is_complete(FACTS_PIPELINE, "sec", key, record.sha256):
        return summarize("complete", skipped=1, written=0, total=1)
    raw = json.loads(record.payload_path.read_text())
    datasets = normalize_company_facts(
        raw,
        retrieved_at=record.retrieved_at,
        content_hash=record.sha256,
        source_url=record.url,
        source_record_id=key,
    )
    written = _write_datasets(datasets, data_root)
    checkpointer.complete(
        FACTS_PIPELINE, "sec", key, record.sha256,
        record_count=written, started_at=started_at,
    )
    return summarize("complete", skipped=0, written=1, total=1)


def ingest_shares_facts_for_tickers(
    tickers: list[str],
    data_root: Path,
    archive_root: Optional[Path] = None,
    pacing: Optional[Pacing] = None,
) -> dict:
    """Ingest company facts for every ticker that resolves to a CIK.

    Resolution uses the normalized entity_aliases dataset, so the ticker
    universe must be ingested first (see ingest_company_tickers).
    """
    aliases = duckdb.query(
        "SELECT alias_value, entity_id FROM entity_aliases "
        "WHERE alias_type = 'ticker' AND entity_id LIKE 'sec:cik:%'",
        data_root=data_root,
    )
    wanted = {t.upper() for t in tickers}
    ciks = {
        int(alias["entity_id"].rsplit(":", 1)[1])
        for alias in aliases
        if alias["alias_value"] in wanted
    }
    results = [
        ingest_company_facts(cik, data_root, archive_root=archive_root, pacing=pacing)
        for cik in sorted(ciks)
    ]
    return {
        "status": "complete",
        "ciks_requested": len(ciks),
        "ciks_skipped": sum(1 for r in results if r["payloads_skipped"]),
        "ciks_written": sum(1 for r in results if r["payloads_written"]),
        "ciks_no_companyfacts": sum(1 for r in results if r.get("no_companyfacts")),
        "summary": results,
    }


def resolve_entity_id(ticker: str, data_root: Path) -> Optional[str]:
    """Resolve a ticker alias to its stable SEC entity ID, if any."""
    rows = duckdb.query(
        "SELECT entity_id FROM entity_aliases "
        "WHERE alias_type = 'ticker' AND alias_value = ? LIMIT 1",
        params=[ticker.upper()],
        data_root=data_root,
    )
    return rows[0]["entity_id"] if rows else None


def resolve_security_id(ticker: str, data_root: Path) -> Optional[str]:
    """The common-equity security ID for a ticker's entity, when mapped."""
    entity_id = resolve_entity_id(ticker, data_root)
    if not entity_id or not entity_id.startswith("sec:cik:"):
        return None
    return ids.sec_security_id(int(entity_id.rsplit(":", 1)[1]))