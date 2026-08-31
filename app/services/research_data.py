"""Minimal research data refresh service: fetch -> archive -> normalize -> Parquet.

The short-interest leaderboard screen (app/analytics/screens.py) is fed by
these datasets; `python cli.py refresh-data` drives this module.  This is a
deliberately narrow path — no ingestion framework, no checkpoints: reruns of
identical payloads are no-ops via the raw-archive write-once dedup and the
Parquet unique-key dedup.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional, Sequence

import requests

from .. import finra_client
from ..config import finra_use_mock
from ..normalization import (
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)
from ..storage import parquet, raw_archive

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sec_headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("SEC_EDGAR_IDENTITY", "stockbot research contact@example.com"),
        "Accept-Encoding": "gzip, deflate",
    }


def refresh_sec_tickers(*, data_root: Optional[Path] = None) -> dict:
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    now = _utc_now()
    url = SEC_TICKERS_URL
    resp = requests.get(url, headers=_sec_headers(), timeout=60)
    resp.raise_for_status()
    payload = resp.content
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec", "company_tickers", "company_tickers", payload,
        url=url, retrieved_at=now, root=data_root / "raw",
    )
    datasets = normalize_sec_tickers(json.loads(payload), retrieved_at=now, content_hash=content_hash)
    written = sum(
        parquet.write_rows(name, rows, root=data_root / "parquet")
        for name, rows in datasets.items()
    )
    return {
        "source": "sec:company_tickers",
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
    }


def refresh_sec_company_facts(cik: int, *, data_root: Optional[Path] = None) -> dict:
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    now = _utc_now()
    url = SEC_FACTS_URL.format(cik=cik)
    resp = requests.get(url, headers=_sec_headers(), timeout=60)
    resp.raise_for_status()
    payload = resp.content
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec", f"cik{cik:010d}", "companyfacts", payload,
        url=url, retrieved_at=now, root=data_root / "raw",
    )
    datasets = normalize_sec_company_facts(
        json.loads(payload), retrieved_at=now, content_hash=content_hash,
        source_url=url, source_record_id=f"cik{cik:010d}",
    )
    written = sum(
        parquet.write_rows(name, rows, root=data_root / "parquet")
        for name, rows in datasets.items()
    )
    return {
        "source": "sec:companyfacts",
        "cik": cik,
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
    }


def refresh_finra_short_interest(settlement_date: str, *, data_root: Optional[Path] = None) -> dict:
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    name = "consolidatedShortInterest" + ("Mock" if finra_use_mock() else "")
    url = f"{finra_client.FINRA_API_BASE}/data/group/otcMarket/name/{name}"
    fields = (
        "symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity",
        "previousShortPositionQuantity", "averageDailyVolumeQuantity", "daysToCoverQuantity",
    )
    all_rows: list[dict] = []
    total: Optional[int] = None
    offset = 0
    while True:
        time.sleep(0.2)  # politeness pacing, same interval as the pre-cut pipeline
        payload = {
            "limit": finra_client.MAX_LIMIT,
            "offset": offset,
            "fields": list(fields),
            "compareFilters": [{
                "compareType": "EQUAL",
                "fieldName": "settlementDate",
                "fieldValue": settlement_date,
            }],
        }
        content, rows, headers = finra_client.ingestion_post_query("otcMarket", name, payload)
        raw_archive.archive(
            "finra", "data_page", f"otcMarket/consolidatedShortInterest:{settlement_date}:offset{offset}",
            content, url=url, metadata={"payload": payload, "headers": headers},
            root=data_root / "raw",
        )
        raw_total = headers.get("record-total")
        if raw_total is None:
            raise ValueError("FINRA omitted Record-Total; cannot prove the short-interest snapshot is complete.")
        page_total = int(raw_total)
        if total is None:
            total = page_total
        elif page_total != total:
            raise ValueError("FINRA Record-Total changed while paging the snapshot.")
        page_rows = [row for row in rows if isinstance(row, dict)]
        if not page_rows and len(all_rows) < total:
            raise ValueError("FINRA pagination ended before the complete short-interest snapshot was retrieved.")
        all_rows.extend(page_rows)
        offset += len(page_rows)
        if len(all_rows) >= total:
            break
    if len(all_rows) != total:
        raise ValueError("FINRA pagination returned an incomplete short-interest snapshot.")
    snapshot_hash = hashlib.sha256(
        json.dumps(all_rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    known_at = retrieved_at = _utc_now()
    datasets = normalize_finra_short_interest(
        all_rows, settlement_date=settlement_date, known_at=known_at, retrieved_at=retrieved_at,
        content_hash=snapshot_hash, source_url=url,
        source_record_id=f"otcMarket/consolidatedShortInterest:{settlement_date}",
    )
    written = sum(
        parquet.write_rows(name, rows, root=data_root / "parquet")
        for name, rows in datasets.items()
    )
    return {
        "source": "finra:consolidatedShortInterest",
        "settlement_date": settlement_date,
        "rows": len(all_rows),
        "written": written,
        "content_hash": snapshot_hash,
        "retrieved_at": retrieved_at,
    }


def prepare_short_interest_data(settlement_date: str, *, ciks: Sequence[int] = (320193,), data_root: Optional[Path] = None) -> dict:
    summary = {
        "sec_tickers": refresh_sec_tickers(data_root=data_root),
        "sec_facts": [refresh_sec_company_facts(cik, data_root=data_root) for cik in ciks],
        "finra": refresh_finra_short_interest(settlement_date, data_root=data_root),
    }
    return summary
