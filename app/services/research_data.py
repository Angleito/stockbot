"""Minimal research data refresh service: fetch -> archive -> normalize -> Parquet.

The short-interest leaderboard screen (app/analytics/screens.py) is fed by
these datasets; `python cli.py refresh-data` drives this module.  This is a
deliberately narrow path — no ingestion framework, no checkpoints: reruns of
identical payloads are no-ops via the raw-archive write-once dedup and the
Parquet unique-key dedup.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Optional, Sequence

try:
    import fcntl  # Linux-only; short-interest locking is explicitly Linux-only.
except ImportError:  # pragma: no cover
    fcntl: ModuleType | None = None

import requests

from .. import finra_client
from ..config import finra_use_mock, get_data_root
from ..normalization import (
    SHORT_INTEREST_PARSER_VERSION,
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)
from ..storage import parquet, raw_archive

DEFAULT_DATA_ROOT = get_data_root()

_LEGACY_SHORT_INTEREST_PARSER_VERSION = "finra-short-interest-v1"


@contextlib.contextmanager
def _finra_short_interest_lock(parquet_root: Path) -> Iterator[None]:
    """Hold one blocking ``fcntl.flock`` across short_interest parquet mutations."""
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("finra short-interest lock requires fcntl.flock")
    lock_path = parquet_root / "short_interest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _is_legacy_settlement_stamped(row: dict[str, object]) -> bool:
    settlement = str(row.get("settlement_date") or "")
    known = str(row.get("known_at") or "")
    retrieved = str(row.get("retrieved_at") or "")
    if not (settlement and known and retrieved and known == settlement):
        return False
    return str(row.get("parser_version") or "") == _LEGACY_SHORT_INTEREST_PARSER_VERSION

def _short_interest_has_legacy_v1(parquet_root: Path) -> bool:
    try:
        table = parquet.read_table("short_interest", root=parquet_root, columns=["parser_version"])
    except Exception:
        return True
    try:
        for batch in table.to_batches():
            try:
                values = batch.column("parser_version").to_pylist()
            except Exception:
                return True
            for v in values:
                if str(v or "") == _LEGACY_SHORT_INTEREST_PARSER_VERSION:
                    return True
        return False
    except Exception:
        return True

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


def refresh_sec_tickers(*, data_root: Optional[Path] = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
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
        cik_raw = item.get("cik_str")
        if cik_raw is None:
            continue
        try:
            cik = int(cik_raw)
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


def _normalize_and_write_company_facts(
    cik: int, payload: bytes, *, retrieved_at: str, url: str, data_root: Path,
) -> int:
    """Shared normalize -> Parquet step behind refresh and archive replay."""
    content_hash = raw_archive.content_hash(payload)
    datasets = normalize_sec_company_facts(
        json.loads(payload), retrieved_at=retrieved_at, content_hash=content_hash,
        source_url=url, source_record_id=f"cik{cik:010d}",
    )
    return sum(
        parquet.write_rows(name, rows, root=data_root / "parquet")
        for name, rows in datasets.items()
    )


def refresh_sec_company_facts(cik: int, *, data_root: Optional[Path] = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
    now = _utc_now()
    url = SEC_FACTS_URL.format(cik=cik)
    payload = _sec_get(url)
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec", f"cik{cik:010d}", "companyfacts", payload,
        url=url, retrieved_at=now, root=data_root / "raw",
    )
    written = _normalize_and_write_company_facts(
        cik, payload, retrieved_at=now, url=url, data_root=data_root,
    )
    return {
        "source": "sec:companyfacts",
        "cik": cik,
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
    }


def replay_sec_facts_from_archive(*, data_root: Optional[Path] = None) -> dict[str, object]:
    """Replay archived SEC companyfacts payloads through normalize -> Parquet.

    Offline: already-enriched CIKs gain rows (e.g. EPS) without re-downloading.
    Uses each manifest's ``retrieved_at`` (not the wall clock) so replayed
    rows are deterministic; existing rows dedup to zero writes.  Failures are
    isolated per payload and reported, never raised mid-iteration.
    """
    data_root = Path(data_root) if data_root else get_data_root()
    raw_root = data_root / "raw"
    archived_payloads = 0
    written_rows = 0
    failed: list[dict[str, str]] = []
    sec_dir = raw_root / "sec"
    if sec_dir.is_dir():
        for cik_dir in sorted(p for p in sec_dir.iterdir() if p.is_dir()):
            if not (cik_dir / "companyfacts").is_dir():
                continue
            for record in raw_archive.iter_archive("sec", cik_dir.name, "companyfacts", root=raw_root):
                archived_payloads += 1
                try:
                    cik = int(cik_dir.name.removeprefix("cik"))
                    written_rows += _normalize_and_write_company_facts(
                        cik, record.payload_path.read_bytes(),
                        retrieved_at=record.retrieved_at, url=record.url,
                        data_root=data_root,
                    )
                except Exception as exc:
                    failed.append({
                        "cik": cik_dir.name,
                        "sha256": record.sha256,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
    return {
        "source": "sec",
        "kind": "companyfacts",
        "archived_payloads": archived_payloads,
        "written_rows": written_rows,
        "failed": failed,
    }


def refresh_finra_short_interest(settlement_date: str, *, data_root: Optional[Path] = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
    name = "consolidatedShortInterest" + ("Mock" if finra_use_mock() else "")
    url = f"{finra_client.FINRA_API_BASE}/data/group/otcMarket/name/{name}"
    fields = (
        "symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity",
        "previousShortPositionQuantity", "averageDailyVolumeQuantity", "daysToCoverQuantity",
    )
    all_rows: list[dict[str, object]] = []
    total: Optional[int] = None
    offset = 0
    while True:
        time.sleep(0.2)  # politeness pacing, same interval as the pre-cut pipeline
        payload: dict[str, object] = {
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
        page_total = int(str(raw_total))
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
    retrieved_at = _utc_now()
    with _finra_short_interest_lock(data_root / "parquet"):
        datasets = normalize_finra_short_interest(
            all_rows, settlement_date=settlement_date, retrieved_at=retrieved_at,
            content_hash=snapshot_hash, source_url=url,
            source_record_id=f"otcMarket/consolidatedShortInterest:{settlement_date}",
        )
        written = sum(
            parquet.write_rows(name, rows, root=data_root / "parquet")
            for name, rows in datasets.items()
        )
        backfilled = _backfill_finra_known_at_locked(data_root)["rewritten"]
    return {
        "source": "finra:consolidatedShortInterest",
        "settlement_date": settlement_date,
        "rows": len(all_rows),
        "written": written,
        "backfilled": backfilled,
        "content_hash": snapshot_hash,
        "retrieved_at": retrieved_at,
    }


def prepare_short_interest_data(
    settlement_date: str,
    *,
    tickers: Sequence[str] = (),
    ciks: Sequence[int] = (),
    data_root: Optional[Path] = None,
) -> dict[str, object]:
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
    ticker_ciks_raw = sec_tickers["ticker_ciks"]
    ticker_ciks: dict[str, int] = {}
    if isinstance(ticker_ciks_raw, dict):
        for k, v in ticker_ciks_raw.items():
            if isinstance(k, str) and isinstance(v, int):
                ticker_ciks[k] = v
    unresolved = [t for t in requested if t not in ticker_ciks]
    enrich_ciks = list(dict.fromkeys(
        [*ciks, *(ticker_ciks[t] for t in requested if t in ticker_ciks)]
    ))
    finra = refresh_finra_short_interest(settlement_date, data_root=data_root)
    cik_to_ticker = {cik: ticker for ticker, cik in ticker_ciks.items()}
    sec_facts: list[dict[str, object]] = []
    failed_enrichments: list[dict[str, object]] = []
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

def backfill_finra_known_at(*, data_root: Optional[Path] = None) -> dict[str, object]:
    """Rewrite legacy v1 settlement-stamped FINRA ``known_at`` to ``retrieved_at``.

    Only legacy v1 settlement-stamped rows gain their own ``retrieved_at`` and
    the v2 stamp; reruns return 0.
    """
    root = Path(data_root) if data_root else get_data_root()
    with _finra_short_interest_lock(root / "parquet"):
        return _backfill_finra_known_at_locked(root)


def _backfill_finra_known_at_locked(data_root: Path) -> dict[str, object]:
    root = data_root
    parquet_root = root / "parquet"
    dataset_dir = parquet_root / "short_interest"
    backup_dir = parquet_root / "short_interest-backfill-bak"
    if backup_dir.exists() and not dataset_dir.exists():
        backup_dir.rename(dataset_dir)
    elif backup_dir.exists():
        shutil.rmtree(backup_dir)
    if not _short_interest_has_legacy_v1(parquet_root):
        return {"rewritten": 0}
    table = parquet.read_table("short_interest", root=parquet_root)
    rows = table.to_pylist()
    fixed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _is_legacy_settlement_stamped(row):
            row = dict(row)
            row["known_at"] = str(row.get("retrieved_at") or "")
            row["parser_version"] = SHORT_INTEREST_PARSER_VERSION
            fixed.append(row)
    if not fixed:
        return {"rewritten": 0}
    by_id = {str(r.get("row_id")): r for r in rows if isinstance(r, dict)}
    for row in fixed:
        by_id[str(row.get("row_id"))] = row
    corrected = list(by_id.values())
    staging = Path(tempfile.mkdtemp(prefix="short_interest-backfill-", dir=parquet_root))
    try:
        parquet.write_rows("short_interest", corrected, root=staging)
        staged = parquet.read_table("short_interest", root=staging).to_pylist()
        if len(staged) != len(corrected):
            raise RuntimeError(
                f"backfill validation failed: staged row count {len(staged)} != {len(corrected)}"
            )
        staged_by_id = {str(r.get("row_id")): r for r in staged if isinstance(r, dict)}
        for row in fixed:
            row_id = str(row.get("row_id"))
            staged_row = staged_by_id.get(row_id)
            if staged_row is None:
                raise RuntimeError(f"backfill validation failed: fixed row {row_id} missing from staged copy")
            if str(staged_row.get("known_at")) != str(staged_row.get("retrieved_at")):
                raise RuntimeError(f"backfill validation failed: fixed row {row_id} known_at != retrieved_at")
            if str(staged_row.get("parser_version") or "") != SHORT_INTEREST_PARSER_VERSION:
                raise RuntimeError(f"backfill validation failed: fixed row {row_id} missing v2 stamp")
        for staged_row in staged:
            if not isinstance(staged_row, dict):
                continue
            if _is_legacy_settlement_stamped(staged_row):
                raise RuntimeError(
                    f"backfill validation failed: staged row {staged_row.get('row_id')} still settlement-stamped"
                )
        # short_interest parquet mutations hold _finra_short_interest_lock; network fetch stays outside
        if dataset_dir.exists():
            os.replace(dataset_dir, backup_dir)
        os.replace(staging / "short_interest", dataset_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"rewritten": len(fixed)}
