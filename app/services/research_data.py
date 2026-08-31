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

_SEC_THROTTLE_SECONDS = 0.13
_SEC_MAX_ATTEMPTS = 3
_SEC_BACKOFF_BASE = 0.5
_SEC_BACKOFF_CAP = 10.0
_sec_last_request: float = 0.0


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sec_headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("SEC_EDGAR_IDENTITY", "stockbot research contact@example.com"),
        "Accept-Encoding": "gzip, deflate",
    }


def _sec_throttle() -> None:
    """Pace SEC requests to at least one per ``_SEC_THROTTLE_SECONDS``."""
    global _sec_last_request
    now = time.monotonic()
    if now - _sec_last_request < _SEC_THROTTLE_SECONDS:
        time.sleep(_SEC_THROTTLE_SECONDS)
    _sec_last_request = time.monotonic()


def _sec_get(url: str) -> bytes:
    """GET one SEC endpoint with pacing and bounded retry on 429/5xx.

    Backoff is exponential with an optional Retry-After override; the last
    response's status is raised once attempts are exhausted.
    """
    resp = None
    for attempt in range(_SEC_MAX_ATTEMPTS):
        _sec_throttle()
        resp = requests.get(url, headers=_sec_headers(), timeout=60)
        if resp.status_code != 429 and resp.status_code < 500:
            break
        raw = resp.headers.get("Retry-After")
        try:
            retry_after = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
        delay = retry_after if retry_after is not None else _SEC_BACKOFF_BASE * 2 ** attempt
        time.sleep(min(delay, _SEC_BACKOFF_CAP))
    assert resp is not None
    resp.raise_for_status()
    return resp.content


def refresh_sec_tickers(*, data_root: Optional[Path] = None) -> dict:
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    now = _utc_now()
    url = SEC_TICKERS_URL
    payload = _sec_get(url)
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec", "company_tickers", "company_tickers", payload,
        url=url, retrieved_at=now, root=data_root / "raw",
    )
    payload_json = json.loads(payload)
    datasets = normalize_sec_tickers(payload_json, retrieved_at=now, content_hash=content_hash)
    written = sum(
        parquet.write_rows(name, rows, root=data_root / "parquet")
        for name, rows in datasets.items()
    )
    ticker_ciks: dict[str, int] = {}
    items = payload_json.values() if isinstance(payload_json, dict) else (payload_json or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        try:
            cik = int(item.get("cik_str"))
        except (TypeError, ValueError):
            continue
        ticker_ciks[ticker] = cik
    return {
        "source": "sec:company_tickers",
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
        "ticker_ciks": ticker_ciks,
    }


def refresh_sec_company_facts(cik: int, *, data_root: Optional[Path] = None) -> dict:
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    now = _utc_now()
    url = SEC_FACTS_URL.format(cik=cik)
    payload = _sec_get(url)
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


def prepare_short_interest_data(
    settlement_date: str,
    *,
    tickers: Sequence[str] = (),
    ciks: Sequence[int] = (),
    data_root: Optional[Path] = None,
) -> dict:
    """Refresh the SEC ticker universe and the full FINRA snapshot, and
    enrich SEC company facts only for the explicitly requested tickers/CIKs.

    The store accumulates across refreshes (raw-archive write-once dedup +
    Parquet unique-key dedup), so different ``--ticker`` sets grow the facts
    cache; the leaderboard screen itself is always market-wide.  An
    unresolved ticker fetches nothing and is reported in the summary; an
    enrichment failure is reported in ``failed_enrichments`` and never
    blocks the FINRA snapshot.
    """
    requested = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    sec_tickers = refresh_sec_tickers(data_root=data_root)
    ticker_ciks = sec_tickers["ticker_ciks"]
    unresolved = [t for t in requested if t not in ticker_ciks]
    enrich_ciks = list(dict.fromkeys(
        [*ciks, *(ticker_ciks[t] for t in requested if t in ticker_ciks)]
    ))
    finra = refresh_finra_short_interest(settlement_date, data_root=data_root)
    cik_to_ticker = {cik: ticker for ticker, cik in ticker_ciks.items()}
    sec_facts: list[dict] = []
    failed_enrichments: list[dict] = []
    for cik in enrich_ciks:
        try:
            sec_facts.append(refresh_sec_company_facts(cik, data_root=data_root))
        except Exception as exc:
            failed_enrichments.append({
                "ticker": cik_to_ticker.get(cik),
                "cik": cik,
                "error": f"{type(exc).__name__}: {exc}",
            })
    public_tickers = {k: v for k, v in sec_tickers.items() if k != "ticker_ciks"}
    public_tickers["ticker_count"] = len(ticker_ciks)
    return {
        "sec_tickers": public_tickers,
        "sec_facts": sec_facts,
        "finra": finra,
        "unresolved_tickers": unresolved,
        "failed_enrichments": failed_enrichments,
    }
