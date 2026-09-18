"""Minimal research data refresh service: fetch -> archive -> normalize -> DuckDB.

The short-interest leaderboard screen (app/analytics/screens.py) is fed by
these datasets; `python cli.py refresh-data` drives this module.  This is a
deliberately narrow path — no ingestion framework, no checkpoints: reruns of
identical payloads are no-ops via the raw-archive write-once dedup and the
DuckDB primary-key dedup.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .. import finra_client
from ..config import finra_use_mock, get_data_root
from ..normalization import (
    SHORT_INTEREST_PARSER_VERSION,
    normalize_finra_short_interest,
    normalize_sec_company_facts,
    normalize_sec_tickers,
)
from ..sec.client import ensure_identity
from ..storage import duckdb, raw_archive

DEFAULT_DATA_ROOT = get_data_root()

_LEGACY_SHORT_INTEREST_PARSER_VERSION = "finra-short-interest-v1"


def _iso_stamp(value: object) -> str:
    """Warehouse datetime (or ISO string) back to ISO-8601 text."""
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value or "")


def _is_legacy_settlement_stamped(row: dict[str, object]) -> bool:
    settlement = str(row.get("settlement_date") or "")
    known = str(row.get("known_at") or "")
    retrieved = str(row.get("retrieved_at") or "")
    if not (settlement and known and retrieved and known == settlement):
        return False
    return str(row.get("parser_version") or "") == _LEGACY_SHORT_INTEREST_PARSER_VERSION


def _parser_version_values(data_root: Path) -> list[object] | None:
    """All parser_version values, or None when the table is unreadable."""
    try:
        rows = duckdb.query("SELECT parser_version FROM short_interest", data_root=data_root)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return [row.get("parser_version") for row in rows]


def _short_interest_has_legacy_v1(data_root: Path) -> bool:
    values = _parser_version_values(data_root)
    if values is None:
        return True
    return any(str(v or "") == _LEGACY_SHORT_INTEREST_PARSER_VERSION for v in values)


def _edgar_get(url: str) -> bytes:
    """Fetch one SEC URL via EdgarTools (owns identity, throttle, retry)."""
    ensure_identity()
    from edgar.httprequests import get_with_retry, inspect_response

    resp = get_with_retry(url)
    inspect_response(resp)
    return bytes(resp.content)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_ticker_ciks(payload_json: object) -> dict[str, int]:
    """ticker -> CIK from the SEC universe payload (skips malformed rows)."""
    ticker_ciks: dict[str, int] = {}
    items = payload_json.values() if isinstance(payload_json, dict) else []
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
        except TypeError, ValueError:
            continue
        ticker_ciks[ticker] = cik
    return ticker_ciks


def refresh_sec_tickers(*, data_root: Path | None = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
    now = _utc_now()
    from edgar.urls import build_company_tickers_url

    url = build_company_tickers_url()
    payload = _edgar_get(url)
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec",
        "company_tickers",
        "company_tickers",
        payload,
        url=url,
        retrieved_at=now,
        root=data_root / "raw",
    )
    payload_json = json.loads(payload)
    datasets = normalize_sec_tickers(payload_json, retrieved_at=now, content_hash=content_hash)
    written = sum(duckdb.insert_ignore(name, rows, data_root=data_root) for name, rows in datasets.items())
    ticker_ciks = _parse_ticker_ciks(payload_json)
    return {
        "source": "sec:company_tickers",
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
        "ticker_ciks": ticker_ciks,
    }


def _normalize_and_write_company_facts(
    cik: int,
    payload: bytes,
    *,
    retrieved_at: str,
    url: str,
    data_root: Path,
) -> int:
    """Shared normalize -> DuckDB step behind refresh and archive replay."""
    content_hash = raw_archive.content_hash(payload)
    datasets = normalize_sec_company_facts(
        json.loads(payload),
        retrieved_at=retrieved_at,
        content_hash=content_hash,
        source_url=url,
        source_record_id=f"cik{cik:010d}",
    )
    return sum(duckdb.insert_ignore(name, rows, data_root=data_root) for name, rows in datasets.items())


def refresh_sec_company_facts(cik: int, *, data_root: Path | None = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
    now = _utc_now()
    from edgar.urls import build_company_facts_url

    url = build_company_facts_url(cik)
    payload = _edgar_get(url)
    content_hash = raw_archive.content_hash(payload)
    raw_archive.archive(
        "sec",
        f"cik{cik:010d}",
        "companyfacts",
        payload,
        url=url,
        retrieved_at=now,
        root=data_root / "raw",
    )
    written = _normalize_and_write_company_facts(
        cik,
        payload,
        retrieved_at=now,
        url=url,
        data_root=data_root,
    )
    return {
        "source": "sec:companyfacts",
        "cik": cik,
        "written": written,
        "content_hash": content_hash,
        "retrieved_at": now,
    }


def replay_sec_facts_from_archive(*, data_root: Path | None = None) -> dict[str, object]:
    """Replay archived SEC companyfacts payloads through normalize -> DuckDB.

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
                        cik,
                        record.payload_path.read_bytes(),
                        retrieved_at=record.retrieved_at,
                        url=record.url,
                        data_root=data_root,
                    )
                except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                    failed.append(
                        {
                            "cik": cik_dir.name,
                            "sha256": record.sha256,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
    return {
        "source": "sec",
        "kind": "companyfacts",
        "archived_payloads": archived_payloads,
        "written_rows": written_rows,
        "failed": failed,
    }


def _finra_page_request(settlement_date: str, offset: int, fields: tuple[str, ...]) -> dict[str, object]:
    """One FINRA consolidatedShortInterest page request body."""
    return {
        "limit": finra_client.MAX_LIMIT,
        "offset": offset,
        "fields": list(fields),
        "compareFilters": [
            {
                "compareType": "EQUAL",
                "fieldName": "settlementDate",
                "fieldValue": settlement_date,
            }
        ],
    }


def _finra_check_page_total(headers: Mapping[str, object], total: int | None) -> int:
    """Prove snapshot completeness: Record-Total present and stable across pages."""
    raw_total = headers.get("record-total")
    if raw_total is None:
        raise ValueError("FINRA omitted Record-Total; cannot prove the short-interest snapshot is complete.")
    page_total = int(str(raw_total))
    if total is not None and page_total != total:
        raise ValueError("FINRA Record-Total changed while paging the snapshot.")
    return page_total


def _finra_extend_snapshot(all_rows: list[dict[str, object]], rows: object, total: int) -> int:
    """Append one page of dict rows; returns the page size for the offset."""
    page_rows: list[dict[str, object]] = (
        [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    )
    if not page_rows and len(all_rows) < total:
        raise ValueError("FINRA pagination ended before the complete short-interest snapshot was retrieved.")
    all_rows.extend(page_rows)
    return len(page_rows)


def _fetch_finra_snapshot(
    settlement_date: str, name: str, url: str, fields: tuple[str, ...], data_root: Path
) -> list[dict[str, object]]:
    """Page the full FINRA snapshot, archiving every raw page (write-once dedup)."""
    all_rows: list[dict[str, object]] = []
    total: int | None = None
    offset = 0
    while True:
        time.sleep(0.2)  # politeness pacing, same interval as the pre-cut pipeline
        payload = _finra_page_request(settlement_date, offset, fields)
        content, rows, headers = finra_client.ingestion_post_query("otcMarket", name, payload)
        raw_archive.archive(
            "finra",
            "data_page",
            f"otcMarket/consolidatedShortInterest:{settlement_date}:offset{offset}",
            content,
            url=url,
            metadata={"payload": payload, "headers": headers},
            root=data_root / "raw",
        )
        total = _finra_check_page_total(headers, total)
        offset += _finra_extend_snapshot(all_rows, rows, total)
        if len(all_rows) >= total:
            break
    if len(all_rows) != total:
        raise ValueError("FINRA pagination returned an incomplete short-interest snapshot.")
    return all_rows


def _write_finra_snapshot(
    all_rows: list[dict[str, object]], settlement_date: str, snapshot_hash: str, url: str, data_root: Path
) -> tuple[int, int, str]:
    """Normalize + write one snapshot; backfill legacy known_at."""
    retrieved_at = _utc_now()
    datasets = normalize_finra_short_interest(
        all_rows,
        settlement_date=settlement_date,
        retrieved_at=retrieved_at,
        content_hash=snapshot_hash,
        source_url=url,
        source_record_id=f"otcMarket/consolidatedShortInterest:{settlement_date}",
    )
    written = sum(duckdb.insert_ignore(name, rows, data_root=data_root) for name, rows in datasets.items())
    backfilled_raw = _backfill_finra_known_at_locked(data_root)["rewritten"]
    backfilled = backfilled_raw if isinstance(backfilled_raw, int) else 0
    return written, backfilled, retrieved_at


def refresh_finra_short_interest(settlement_date: str, *, data_root: Path | None = None) -> dict[str, object]:
    data_root = Path(data_root) if data_root else get_data_root()
    name = "consolidatedShortInterest" + ("Mock" if finra_use_mock() else "")
    url = f"{finra_client.FINRA_API_BASE}/data/group/otcMarket/name/{name}"
    fields = (
        "symbolCode",
        "issueName",
        "settlementDate",
        "currentShortPositionQuantity",
        "previousShortPositionQuantity",
        "averageDailyVolumeQuantity",
        "daysToCoverQuantity",
    )
    all_rows = _fetch_finra_snapshot(settlement_date, name, url, fields, data_root)
    snapshot_hash = hashlib.sha256(json.dumps(all_rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    written, backfilled, retrieved_at = _write_finra_snapshot(all_rows, settlement_date, snapshot_hash, url, data_root)
    return {
        "source": "finra:consolidatedShortInterest",
        "settlement_date": settlement_date,
        "rows": len(all_rows),
        "written": written,
        "backfilled": backfilled,
        "content_hash": snapshot_hash,
        "retrieved_at": retrieved_at,
    }


def _normalize_ticker_ciks(ticker_ciks_raw: object) -> dict[str, int]:
    """ticker -> CIK keeping only well-typed entries."""
    ticker_ciks: dict[str, int] = {}
    if isinstance(ticker_ciks_raw, dict):
        for k, v in ticker_ciks_raw.items():
            if isinstance(k, str) and isinstance(v, int):
                ticker_ciks[k] = v
    return ticker_ciks


def _resolve_enrichment_ciks(
    tickers: Sequence[str],
    ciks: Sequence[int],
    ticker_ciks_raw: object,
) -> tuple[list[str], dict[str, int], list[str], list[int]]:
    """(requested tickers, ticker->cik, unresolved, enrich ciks)."""
    requested = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))
    ticker_ciks = _normalize_ticker_ciks(ticker_ciks_raw)
    unresolved = [t for t in requested if t not in ticker_ciks]
    enrich_ciks = list(dict.fromkeys([*ciks, *(ticker_ciks[t] for t in requested if t in ticker_ciks)]))
    return requested, ticker_ciks, unresolved, enrich_ciks


def _enrich_cik_facts(
    enrich_ciks: Sequence[int], cik_to_ticker: dict[int, str], data_root: Path
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """SEC facts per CIK; failures reported per CIK, never blocking siblings."""
    sec_facts: list[dict[str, object]] = []
    failed_enrichments: list[dict[str, object]] = []
    for cik in enrich_ciks:
        try:
            sec_facts.append(refresh_sec_company_facts(cik, data_root=data_root))
        except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            failed_enrichments.append(
                {
                    "ticker": cik_to_ticker.get(cik),
                    "cik": cik,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return sec_facts, failed_enrichments


def prepare_short_interest_data(
    settlement_date: str,
    *,
    tickers: Sequence[str] = (),
    ciks: Sequence[int] = (),
    data_root: Path | None = None,
) -> dict[str, object]:
    """Refresh the SEC ticker universe and the full FINRA snapshot, and
    enrich SEC company facts only for the explicitly requested tickers/CIKs.

    The store accumulates across refreshes (raw-archive write-once dedup +
    DuckDB primary-key dedup), so different ``--ticker`` sets grow the facts
    cache; the leaderboard screen itself is always market-wide.  An
    unresolved ticker fetches nothing and is reported in the summary; an
    enrichment failure is reported in ``failed_enrichments`` and never
    blocks the FINRA snapshot.
    """
    data_root = Path(data_root) if data_root else get_data_root()
    sec_tickers = refresh_sec_tickers(data_root=data_root)
    _, ticker_ciks, unresolved, enrich_ciks = _resolve_enrichment_ciks(tickers, ciks, sec_tickers["ticker_ciks"])
    finra = refresh_finra_short_interest(settlement_date, data_root=data_root)
    cik_to_ticker = {cik: ticker for ticker, cik in ticker_ciks.items()}
    sec_facts, failed_enrichments = _enrich_cik_facts(enrich_ciks, cik_to_ticker, data_root)
    public_tickers = {k: v for k, v in sec_tickers.items() if k != "ticker_ciks"}
    public_tickers["ticker_count"] = len(ticker_ciks)
    return {
        "sec_tickers": public_tickers,
        "sec_facts": sec_facts,
        "finra": finra,
        "unresolved_tickers": unresolved,
        "failed_enrichments": failed_enrichments,
    }


def backfill_finra_known_at(*, data_root: Path | None = None) -> dict[str, object]:
    """Rewrite legacy v1 settlement-stamped FINRA ``known_at`` to ``retrieved_at``.

    Only legacy v1 settlement-stamped rows gain their own ``retrieved_at`` and
    the v2 stamp; reruns return 0.
    """
    root = Path(data_root) if data_root else get_data_root()
    return _backfill_finra_known_at_locked(root)


def _legacy_stamped_fixes(rows: Sequence[object]) -> list[dict[str, object]]:
    """Legacy v1 settlement-stamped rows with known_at -> retrieved_at + v2 stamp."""
    fixed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _is_legacy_settlement_stamped(row):
            row = dict(row)
            row["known_at"] = _iso_stamp(row.get("retrieved_at"))
            row["retrieved_at"] = _iso_stamp(row.get("retrieved_at"))
            row["parser_version"] = SHORT_INTEREST_PARSER_VERSION
            fixed.append(row)
    return fixed


def _backfill_finra_known_at_locked(data_root: Path) -> dict[str, object]:
    root = data_root
    if not _short_interest_has_legacy_v1(root):
        return {"rewritten": 0}
    rows = duckdb.query("SELECT * FROM short_interest", data_root=root)
    fixed = _legacy_stamped_fixes(rows)
    if not fixed:
        return {"rewritten": 0}
    conn = duckdb._connect(root)
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            for row in fixed:
                conn.execute(
                    "UPDATE short_interest SET known_at = ?, parser_version = ?, _tsraw = ? WHERE row_id = ?",
                    [
                        str(row.get("known_at")),
                        str(row.get("parser_version")),
                        json.dumps(
                            {"known_at": str(row.get("known_at")), "retrieved_at": str(row.get("retrieved_at"))},
                            sort_keys=True,
                        ),
                        str(row.get("row_id")),
                    ],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"rewritten": len(fixed)}
