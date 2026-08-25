"""Persistent, point-in-time short-interest screening.

This module deliberately keeps bulk work out of the chat model.  A FINRA
settlement-date snapshot is completely paged, joined to SEC facts, and only
then published as a locally queryable leaderboard.

SEC fetches are throttled to stay under SEC's request limit, and previously
retrieved shares-outstanding facts are reused for up to SEC_FACTS_TTL_SECONDS,
so re-refreshes only crawl new or expired CIKs.  A reused fact is still
filtered point-in-time against the requested settlement date.

A CIK with no SEC company-facts resource (HTTP 404) is counted as a
``no_sec_companyfacts`` exclusion, not a failure; any other SEC service
error still aborts the refresh before publication.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

import requests

from . import cache
from . import finra_client

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(Path(__file__).resolve().parent.parent, "data", "short_interest_screen.db")
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
PARSER_VERSION = "short-interest-screen-v3"
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
# SEC limits automated requests to roughly 10/second; stay well below it.
SEC_REQUEST_INTERVAL_SECONDS = 0.13
SEC_RETRY_DELAY_SECONDS = 5.0
# Reuse a previously fetched SEC shares-outstanding fact for this long before
# refetching it; a refresh then only crawls new or expired CIKs.
SEC_FACTS_TTL_SECONDS = 7 * 86400
SEC_TICKERS_TTL_SECONDS = 86400

_sec_throttle_lock = threading.Lock()
_last_sec_request_at = 0.0


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS finra_snapshot (
          settlement_date TEXT PRIMARY KEY, retrieved_at REAL NOT NULL,
          record_count INTEGER NOT NULL, raw_json TEXT NOT NULL, parser_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS finra_short_interest (
          settlement_date TEXT NOT NULL, symbol TEXT NOT NULL, issue_name TEXT,
          short_shares REAL NOT NULL, PRIMARY KEY (settlement_date, symbol)
        );
        CREATE TABLE IF NOT EXISTS sec_shares_fact (
          cik INTEGER NOT NULL, ticker TEXT NOT NULL, shares_outstanding REAL NOT NULL,
          shares_as_of TEXT NOT NULL, filed_at TEXT NOT NULL, accession TEXT,
          source_url TEXT NOT NULL, retrieved_at REAL NOT NULL, parser_version TEXT NOT NULL,
          PRIMARY KEY (cik, shares_as_of, filed_at, shares_outstanding)
        );
        CREATE TABLE IF NOT EXISTS leaderboard_run (
          settlement_date TEXT PRIMARY KEY, created_at REAL NOT NULL,
          finra_rows INTEGER NOT NULL, eligible_rows INTEGER NOT NULL,
          excluded_json TEXT NOT NULL, parser_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leaderboard_entry (
          settlement_date TEXT NOT NULL, rank INTEGER NOT NULL, ticker TEXT NOT NULL,
          issue_name TEXT, short_shares REAL NOT NULL, shares_outstanding REAL NOT NULL,
          short_interest_percent REAL NOT NULL, sec_shares_as_of TEXT NOT NULL,
          sec_filed_at TEXT NOT NULL, PRIMARY KEY (settlement_date, rank),
          UNIQUE (settlement_date, ticker)
        );
        """
    )
    return conn


def _sec_headers() -> dict[str, str]:
    # SEC requires a descriptive User-Agent.  This avoids depending on the
    # OpenRouter key required by the existing edgartools configuration.
    return {"User-Agent": os.getenv("SEC_EDGAR_IDENTITY", "stockbot research contact@example.com"), "Accept-Encoding": "gzip, deflate"}


def _throttle_sec_request() -> None:
    """Stay under SEC's ~10 requests/second limit across all callers."""
    global _last_sec_request_at
    with _sec_throttle_lock:
        now = time.time()
        wait = SEC_REQUEST_INTERVAL_SECONDS - (now - _last_sec_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_sec_request_at = time.time()


def _sec_get(url: str) -> requests.Response:
    """Throttled SEC GET with one backoff retry on rate-limit responses."""
    _throttle_sec_request()
    response = requests.get(url, headers=_sec_headers(), timeout=60)
    if response.status_code in (403, 429):
        time.sleep(SEC_RETRY_DELAY_SECONDS)
        _throttle_sec_request()
        response = requests.get(url, headers=_sec_headers(), timeout=60)
    response.raise_for_status()
    return response


def _clamp_limit(limit: Optional[int]) -> int:
    try:
        return max(1, min(int(limit if limit is not None else DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def _latest_settlement_date() -> str:
    entry = finra_client._resolve_dataset("otcMarket/consolidatedShortInterest")
    spec = finra_client._get_dataset_spec(entry)
    date_field = finra_client._date_partition_field(spec)
    if date_field is None:
        raise ValueError("FINRA consolidated short interest has no verified date partition; cannot resolve the latest settlement date.")
    index = list(spec.partition_fields).index(date_field)
    partitions = finra_client._get_partitions(spec)
    dates = [str(values[index]) for values in partitions if values and str(values[index])]
    if not dates:
        raise ValueError("FINRA did not publish any consolidated short-interest partitions.")
    return max(dates)


def _fetch_finra_snapshot(settlement_date: str) -> list[dict]:
    """Fetch every page for one exact FINRA partition and prove completeness."""
    entry = finra_client._resolve_dataset("otcMarket/consolidatedShortInterest")
    spec = finra_client._get_dataset_spec(entry)
    if entry.supports_record_offset is False:
        raise ValueError("FINRA consolidated short interest does not support required pagination.")
    fields = [
        "symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity",
    ]
    # Fail loudly on metadata drift instead of silently publishing a screen
    # that is complete but empty (every row excluded as invalid).
    if spec.field_names and not set(fields) <= spec.field_names:
        missing = sorted(set(fields) - spec.field_names)
        raise ValueError(
            "FINRA metadata for consolidated short interest is missing required "
            f"fields: {', '.join(missing)}."
        )
    offset = 0
    all_rows: list[dict] = []
    total: Optional[int] = None
    while True:
        payload = finra_client._build_payload(
            spec, entry, None, settlement_date, settlement_date,
            finra_client.MAX_LIMIT, None, offset=offset, fields=fields,
        )
        rows, headers = finra_client._cached_query(spec, payload)
        raw_total = headers.get("record-total")
        if raw_total is None:
            raise ValueError("FINRA omitted Record-Total; cannot prove the short-interest snapshot is complete.")
        try:
            page_total = int(raw_total)
        except (TypeError, ValueError) as exc:
            raise ValueError("FINRA returned an invalid Record-Total header.") from exc
        if total is None:
            total = page_total
        elif total != page_total:
            raise ValueError("FINRA Record-Total changed while paging the snapshot.")
        page_rows = [row for row in rows if isinstance(row, dict)]
        if rows and not page_rows:
            raise ValueError("FINRA returned malformed rows while paging the short-interest snapshot.")
        all_rows.extend(page_rows)
        if len(all_rows) >= total:
            break
        if not page_rows:
            raise ValueError("FINRA pagination ended before the complete short-interest snapshot was retrieved.")
        offset += len(page_rows)
    if len(all_rows) != total:
        raise ValueError("FINRA returned more rows than its Record-Total; snapshot was not published.")
    return all_rows


class TickerMap:
    """SEC ticker -> CIK mapping with ambiguity preserved.

    A ticker that resolves to more than one CIK cannot be ranked; keeping the
    ambiguity visible (instead of silently dropping it) lets the screen count
    and report every excluded row.
    """

    __slots__ = ("by_ticker", "ambiguous")

    def __init__(self, by_ticker: dict[str, int], ambiguous: dict[str, set[int]]):
        self.by_ticker = by_ticker
        self.ambiguous = ambiguous


def _fetch_sec_tickers() -> TickerMap:
    cache_key = "sec:company_tickers:v1"
    hit = cache.get(cache_key, ttl=SEC_TICKERS_TTL_SECONDS)
    if isinstance(hit, dict) and hit:
        return TickerMap(
            by_ticker=dict(hit.get("by_ticker") or {}),
            ambiguous={str(k): set(v) for k, v in (hit.get("ambiguous") or {}).items()},
        )
    response = _sec_get(SEC_TICKERS_URL)
    raw = response.json()
    seen: dict[str, set[int]] = {}
    for item in raw.values() if isinstance(raw, dict) else raw:
        if not isinstance(item, dict) or not item.get("ticker") or item.get("cik_str") is None:
            continue
        seen.setdefault(str(item["ticker"]).upper(), set()).add(int(item["cik_str"]))
    result = TickerMap(
        by_ticker={ticker: next(iter(ciks)) for ticker, ciks in seen.items() if len(ciks) == 1},
        ambiguous={ticker: ciks for ticker, ciks in seen.items() if len(ciks) > 1},
    )
    cache.set(cache_key, {"by_ticker": result.by_ticker, "ambiguous": {k: sorted(v) for k, v in result.ambiguous.items()}})
    return result


def _fetch_sec_facts(cik: int) -> Optional[tuple[list[dict], dict]]:
    """SEC company facts for one CIK, or None when SEC reports no facts.

    Only a 404 Not Found from the companyfacts endpoint means "this CIK has
    no SEC company facts".  Every other status (401/403/429 after retry,
    5xx), an invalid response, or a network error still raises so the caller
    treats it as a refresh-blocking failure instead of a no-facts exclusion.
    """
    url = SEC_FACTS_URL.format(cik=cik)
    try:
        response = _sec_get(url)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise
    raw = response.json()
    units = (((raw.get("facts") or {}).get("dei") or {}).get("EntityCommonStockSharesOutstanding") or {}).get("units") or {}
    facts = units.get("shares") or []
    return [f for f in facts if isinstance(f, dict)], raw


def _select_fact(facts: list[dict], settlement_date: str) -> Optional[dict]:
    candidates = []
    for fact in facts:
        filed, end, value = str(fact.get("filed") or ""), str(fact.get("end") or ""), fact.get("val")
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 0 or not filed or not end or filed > settlement_date or end > settlement_date:
            continue
        candidates.append((filed, end, value, fact))
    return max(candidates, default=None, key=lambda item: (item[0], item[1]))[3] if candidates else None


def _cached_shares_fact(conn: sqlite3.Connection, cik: int, settlement_date: str) -> Optional[dict]:
    """Reuse the last published SEC fact for a CIK when it is still
    point-in-time valid (filed/end on or before the settlement date) and
    freshly retrieved; otherwise None so the caller refetches. Selection
    order (filed, end) mirrors _select_fact's deterministic max."""
    cutoff = time.time() - SEC_FACTS_TTL_SECONDS
    row = conn.execute(
        "SELECT shares_outstanding, shares_as_of, filed_at, retrieved_at "
        "FROM sec_shares_fact WHERE cik = ? AND filed_at <= ? AND shares_as_of <= ? "
        "ORDER BY filed_at DESC, shares_as_of DESC LIMIT 1",
        (cik, settlement_date, settlement_date),
    ).fetchone()
    if row is None or row["retrieved_at"] < cutoff:
        return None
    return {
        "val": row["shares_outstanding"],
        "end": row["shares_as_of"],
        "filed": row["filed_at"],
    }


def refresh_short_interest_leaderboard(settlement_date: Optional[str] = None) -> dict:
    """Build and atomically publish one complete settlement-date leaderboard."""
    settlement_date = settlement_date or _latest_settlement_date()
    rows = _fetch_finra_snapshot(settlement_date)
    ticker_map = _fetch_sec_tickers()
    exclusions = {
        "unmapped_symbol": 0,
        "ambiguous_ticker_mapping": 0,
        "not_classified_common_equity": 0,
        "no_sec_companyfacts": 0,
        "invalid_short_interest": 0,
    }
    candidates: list[dict] = []
    selected_by_cik: dict[int, Optional[dict]] = {}
    # CIKs for which SEC explicitly has no company-facts resource (HTTP 404);
    # distinct from a CIK whose facts exist but have no usable shares fact.
    no_sec_facts_ciks: set[int] = set()
    conn = _conn()
    try:
        # Preserve the complete source snapshot even when a later SEC request
        # fails; publication of the derived leaderboard remains transactional.
        with conn:
            conn.execute("INSERT OR REPLACE INTO finra_snapshot VALUES (?, ?, ?, ?, ?)", (settlement_date, time.time(), len(rows), json.dumps(rows), PARSER_VERSION))
            conn.execute("DELETE FROM finra_short_interest WHERE settlement_date = ?", (settlement_date,))
            # Rows without a short-position quantity cannot be ranked; store
            # only the ranked-shaped rows and count the rest as invalid below.
            conn.executemany("INSERT INTO finra_short_interest VALUES (?, ?, ?, ?)", [
                (settlement_date, str(r.get("symbolCode") or "").upper(), r.get("issueName"), r.get("currentShortPositionQuantity"))
                for r in rows
                if r.get("currentShortPositionQuantity") is not None and str(r.get("currentShortPositionQuantity")).strip() != ""
            ])
        for row in rows:
            ticker = str(row.get("symbolCode") or "").strip().upper()
            try:
                short_shares = float(row.get("currentShortPositionQuantity"))
            except (TypeError, ValueError):
                exclusions["invalid_short_interest"] += 1
                continue
            if not ticker or short_shares < 0:
                exclusions["invalid_short_interest"] += 1
                continue
            if ticker in ticker_map.ambiguous:
                exclusions["ambiguous_ticker_mapping"] += 1
                continue
            cik = ticker_map.by_ticker.get(ticker)
            if cik is None:
                exclusions["unmapped_symbol"] += 1
                continue
            if cik in no_sec_facts_ciks:
                # SEC has no company-facts resource for this CIK; every row
                # mapping to it is excluded the same way.
                exclusions["no_sec_companyfacts"] += 1
                continue
            if cik not in selected_by_cik:
                fact = _cached_shares_fact(conn, cik, settlement_date)
                if fact is None:
                    fetched = _fetch_sec_facts(cik)
                    if fetched is None:
                        no_sec_facts_ciks.add(cik)
                        selected_by_cik[cik] = None
                        logger.warning(
                            "No SEC company facts for mapped symbol %s (CIK %010d)",
                            ticker, cik,
                        )
                        exclusions["no_sec_companyfacts"] += 1
                        continue
                    fact = _select_fact(fetched[0], settlement_date)
                    if fact is not None:
                        shares = float(fact["val"])
                        source_url = SEC_FACTS_URL.format(cik=cik)
                        conn.execute(
                            "INSERT OR REPLACE INTO sec_shares_fact VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (cik, ticker, shares, str(fact["end"]), str(fact["filed"]), fact.get("accn"), source_url, time.time(), PARSER_VERSION),
                        )
                        # Commit each valid fact as it is retrieved so a later
                        # blocking failure cannot roll it back; a retry then
                        # reuses it instead of restarting every SEC request.
                        conn.commit()
                selected_by_cik[cik] = fact
            fact = selected_by_cik[cik]
            if fact is None:
                # The entity has no shares-outstanding fact known on/before the
                # settlement date, so it is not classified as common equity
                # (could be a fund, ETF, preferred issue, or new listing).
                exclusions["not_classified_common_equity"] += 1
                continue
            shares = float(fact["val"])
            candidates.append({
                "ticker": ticker, "issue_name": row.get("issueName"), "short_shares": short_shares,
                "shares_outstanding": shares, "short_interest_percent": 100 * short_shares / shares,
                "sec_shares_as_of": str(fact["end"]), "sec_filed_at": str(fact["filed"]),
            })
        candidates.sort(key=lambda item: (-item["short_interest_percent"], item["ticker"]))
        # One transaction makes the prior published run remain usable if the refresh fails.
        with conn:
            conn.execute("DELETE FROM leaderboard_entry WHERE settlement_date = ?", (settlement_date,))
            conn.executemany("INSERT INTO leaderboard_entry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [(settlement_date, index, item["ticker"], item["issue_name"], item["short_shares"], item["shares_outstanding"], item["short_interest_percent"], item["sec_shares_as_of"], item["sec_filed_at"]) for index, item in enumerate(candidates, 1)])
            conn.execute("INSERT OR REPLACE INTO leaderboard_run VALUES (?, ?, ?, ?, ?, ?)", (settlement_date, time.time(), len(rows), len(candidates), json.dumps(exclusions, sort_keys=True), PARSER_VERSION))
    finally:
        conn.close()
    return _read_leaderboard(settlement_date, DEFAULT_LIMIT)


def _read_leaderboard(settlement_date: str, limit: int) -> dict:
    conn = _conn()
    try:
        run = conn.execute("SELECT * FROM leaderboard_run WHERE settlement_date = ?", (settlement_date,)).fetchone()
        if run is None:
            return {"error": f"No published short-interest leaderboard for settlement date {settlement_date}."}
        entries = [dict(row) for row in conn.execute("SELECT * FROM leaderboard_entry WHERE settlement_date = ? ORDER BY rank LIMIT ?", (settlement_date, limit))]
    finally:
        conn.close()
    try:
        days = (date.today() - date.fromisoformat(settlement_date)).days
        freshness = "stale" if days > finra_client.STALE_AFTER_DAYS else "current"
    except (TypeError, ValueError):
        freshness = "unknown"
    return {
        "source": "FINRA consolidated short interest + SEC EDGAR company facts",
        "metric": "short shares divided by SEC-reported shares outstanding (not public float)",
        "settlement_date": settlement_date, "entries": entries,
        "coverage": {"finra_rows": run["finra_rows"], "eligible_rows": run["eligible_rows"], "exclusions": json.loads(run["excluded_json"])},
        "environment": finra_client._environment(),
        "as_of_date": settlement_date,
        "data_freshness": freshness,
        "calculation_version": run["parser_version"],
        "source_records": [
            "FINRA otcMarket/consolidatedShortInterest (settlement " + settlement_date + ")",
            SEC_TICKERS_URL,
            SEC_FACTS_URL,
        ],
    }


def get_short_interest_leaderboard(limit: Optional[int] = None, settlement_date: Optional[str] = None) -> dict:
    """Return a published screen, refreshing the requested/latest cycle if absent."""
    limit = _clamp_limit(limit)
    try:
        target = settlement_date or _latest_settlement_date()
        result = _read_leaderboard(target, limit)
        if "error" in result:
            result = refresh_short_interest_leaderboard(target)
            if "error" not in result:
                result = _read_leaderboard(target, limit)
        return result
    except requests.HTTPError as exc:
        return {"error": f"Short-interest leaderboard refresh failed: SEC or FINRA returned HTTP {exc.response.status_code if exc.response is not None else '?'}; no partial leaderboard was published."}
    except Exception as exc:
        return {"error": f"Short-interest leaderboard is unavailable: {exc}"}
