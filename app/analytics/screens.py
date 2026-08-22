"""Deterministic, point-in-time screens over the normalized datasets.

The canonical short-interest leaderboard lives here: it reads normalized
FINRA short interest and SEC facts from the versioned Parquet datasets via
DuckDB, enforces ``known_at <= as_of`` on every fact join, classifies
eligible equities, and persists each run (coverage, exclusions, fact
provenance, calculation version) before returning a bounded result to the
agent tool.

``app/short_interest_screen.py`` remains the interim SQLite product; this
module is the foundation-backed implementation that the agent tool uses.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from .. import finra_client
from ..storage import duckdb, parquet

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
SCREEN_CALC_VERSION = "short-interest-leaderboard-v2"
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
SCREEN_NAME = "short_interest_leaderboard"

_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"


def _clamp_limit(limit: Optional[int]) -> int:
    try:
        return max(1, min(int(limit if limit is not None else DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def latest_settlement_date(data_root: Optional[Path] = None) -> str:
    rows = duckdb.query(
        "SELECT max(settlement_date) AS latest FROM short_interest",
        data_root=data_root,
    )
    latest = rows[0]["latest"] if rows else None
    if not latest:
        raise ValueError(
            "No FINRA short interest is ingested; run the FINRA snapshot pipeline first."
        )
    return str(latest)


def _snapshot_rows(settlement_date: str, data_root: Path) -> list[dict]:
    return duckdb.query(
        "SELECT * FROM short_interest WHERE settlement_date = ? ORDER BY symbol_code",
        params=[settlement_date],
        data_root=data_root,
    )


def _ticker_alias_map(data_root: Path) -> dict[str, list[str]]:
    """ticker -> all entity IDs carrying that ticker alias."""
    aliases: dict[str, list[str]] = {}
    for row in duckdb.query(
        "SELECT alias_value, entity_id FROM entity_aliases WHERE alias_type = 'ticker'",
        data_root=data_root,
    ):
        aliases.setdefault(str(row["alias_value"]), []).append(str(row["entity_id"]))
    return aliases


def _facts_by_entity(as_of: str, data_root: Path) -> dict[str, list[dict]]:
    """Shares-outstanding facts per entity, newest filed first.

    The as-of clause is mandatory: a fact filed after ``as_of`` is never
    visible to the screen.  Ties are broken deterministically by (filed,
    period end, accession).
    """
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT entity_id, value, period_end, filed_at, accession, source_url "
        f"FROM financial_facts WHERE concept = ? AND {clause} "
        "ORDER BY filed_at DESC, period_end DESC, accession DESC",
        params=[_SHARES_CONCEPT, param],
        data_root=data_root,
    )
    by_entity: dict[str, list[dict]] = {}
    for row in rows:
        by_entity.setdefault(str(row["entity_id"]), []).append(row)
    return by_entity


def _select_fact(facts: list[dict]) -> Optional[dict]:
    """Latest fact for an entity; the list is already sorted newest-first."""
    for fact in facts:
        value = fact.get("value")
        if value is None or float(value) <= 0:
            continue
        return fact
    return None


def materialize_short_interest_screen(
    settlement_date: str,
    as_of: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> dict:
    """Build one complete settlement-date leaderboard from normalized data.

    The ranking is deterministic: same settlement date, same ``as_of``, same
    ingested facts -> identical ranking.  The run is persisted with its
    coverage, exclusions, fact provenance, and calculation version before
    any bounded result is returned.
    """
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    as_of = as_of or settlement_date
    rows = _snapshot_rows(settlement_date, data_root)
    if not rows:
        return {
            "error": (
                f"No normalized FINRA short interest exists for settlement "
                f"date {settlement_date}; run the FINRA snapshot pipeline first."
            )
        }
    ticker_aliases = _ticker_alias_map(data_root)
    facts_by_entity = _facts_by_entity(as_of, data_root)
    exclusions = {
        "unmapped_symbol": 0,
        "ambiguous_ticker_mapping": 0,
        "not_classified_common_equity": 0,
        "invalid_short_interest": 0,
    }
    candidates: list[dict] = []
    for row in rows:
        symbol = str(row["symbol_code"])
        short_shares = row.get("short_position")
        if short_shares is None or float(short_shares) < 0:
            exclusions["invalid_short_interest"] += 1
            continue
        short_shares = float(short_shares)
        entity_ids = ticker_aliases.get(symbol)
        if not entity_ids:
            exclusions["unmapped_symbol"] += 1
            continue
        if len(entity_ids) > 1:
            exclusions["ambiguous_ticker_mapping"] += 1
            continue
        entity_id = entity_ids[0]
        fact = _select_fact(facts_by_entity.get(entity_id) or [])
        if fact is None:
            # No shares-outstanding fact known on/before as_of: not classified
            # as common equity (fund, ETF, preferred issue, or new listing).
            exclusions["not_classified_common_equity"] += 1
            continue
        shares = float(fact["value"])
        candidates.append({
            "entity_id": entity_id,
            "security_id": f"sec:equity:{entity_id.rsplit(':', 1)[1]}",
            "ticker": symbol,
            "issue_name": row.get("issue_name"),
            "short_shares": short_shares,
            "shares_outstanding": shares,
            "short_interest_percent": 100 * short_shares / shares,
            "sec_shares_as_of": str(fact["period_end"]),
            "sec_filed_at": str(fact["filed_at"]),
            "sec_accession": fact.get("accession"),
            "sec_source_url": fact.get("source_url"),
        })
    candidates.sort(key=lambda item: (-item["short_interest_percent"], item["ticker"]))
    run_id = f"{SCREEN_NAME}:{settlement_date}:{as_of}"
    created_at = _utc_now()
    parquet.write_rows(
        "screen_runs",
        [{
            "run_id": run_id,
            "screen": SCREEN_NAME,
            "settlement_date": settlement_date,
            "as_of": as_of,
            "created_at": created_at,
            "calc_version": SCREEN_CALC_VERSION,
            "finra_rows": len(rows),
            "eligible_rows": len(candidates),
            "exclusions_json": json.dumps(exclusions, sort_keys=True),
            "environment": finra_client._environment(),
            "parser_version": SCREEN_CALC_VERSION,
        }],
        root=data_root / "parquet",
    )
    parquet.write_rows(
        "screen_entries",
        [
            {
                "run_id": run_id,
                "rank": index,
                "entity_id": item["entity_id"],
                "security_id": item["security_id"],
                "ticker": item["ticker"],
                "issue_name": item["issue_name"],
                "short_shares": item["short_shares"],
                "shares_outstanding": item["shares_outstanding"],
                "short_interest_percent": item["short_interest_percent"],
                "sec_shares_as_of": item["sec_shares_as_of"],
                "sec_filed_at": item["sec_filed_at"],
                "sec_accession": item["sec_accession"],
                "sec_source_url": item["sec_source_url"],
            }
            for index, item in enumerate(candidates, 1)
        ],
        root=data_root / "parquet",
    )
    return read_short_interest_screen(settlement_date, as_of, DEFAULT_LIMIT, data_root=data_root)


def read_short_interest_screen(
    settlement_date: str,
    as_of: Optional[str] = None,
    limit: Optional[int] = None,
    data_root: Optional[Path] = None,
) -> dict:
    """Read a published screen run, bounded to ``limit`` entries."""
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    limit = _clamp_limit(limit)
    as_of = as_of or settlement_date
    run_id = f"{SCREEN_NAME}:{settlement_date}:{as_of}"
    runs = duckdb.query(
        "SELECT * FROM screen_runs WHERE run_id = ?", params=[run_id], data_root=data_root
    )
    if not runs:
        return {"error": f"No published short-interest leaderboard for settlement date {settlement_date}."}
    run = runs[0]
    entries = duckdb.query(
        "SELECT * FROM screen_entries WHERE run_id = ? ORDER BY rank LIMIT ?",
        params=[run_id, limit],
        data_root=data_root,
    )
    try:
        days = (date.today() - date.fromisoformat(settlement_date)).days
        freshness = "stale" if days > finra_client.STALE_AFTER_DAYS else "current"
    except (TypeError, ValueError):
        freshness = "unknown"
    return {
        "source": "FINRA consolidated short interest + SEC EDGAR company facts (parquet)",
        "metric": "short shares divided by SEC-reported shares outstanding (not public float)",
        "settlement_date": settlement_date,
        "as_of_date": as_of,
        "data_freshness": freshness,
        "calculation_version": run["calc_version"],
        "environment": run["environment"],
        "coverage": {
            "finra_rows": run["finra_rows"],
            "eligible_rows": run["eligible_rows"],
            "exclusions": json.loads(run["exclusions_json"]),
        },
        "source_records": [
            f"FINRA otcMarket/consolidatedShortInterest (settlement {settlement_date})",
            "SEC company_tickers.json",
            "SEC companyfacts (EntityCommonStockSharesOutstanding)",
        ],
        "entries": [
            {
                "rank": entry["rank"],
                "ticker": entry["ticker"],
                "issue_name": entry["issue_name"],
                "short_shares": entry["short_shares"],
                "shares_outstanding": entry["shares_outstanding"],
                "short_interest_percent": entry["short_interest_percent"],
                "sec_shares_as_of": entry["sec_shares_as_of"],
                "sec_filed_at": entry["sec_filed_at"],
                "sec_accession": entry["sec_accession"],
                "sec_source_url": entry["sec_source_url"],
            }
            for entry in entries
        ],
    }


def get_short_interest_leaderboard(
    limit: Optional[int] = None,
    settlement_date: Optional[str] = None,
    as_of: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> dict:
    """Return a bounded leaderboard, materializing the requested cycle if it
    has not been published for the requested ``as_of``."""
    try:
        target = settlement_date or latest_settlement_date(data_root)
        result = read_short_interest_screen(target, as_of, limit, data_root=data_root)
        if "error" in result:
            result = materialize_short_interest_screen(target, as_of, data_root=data_root)
            if "error" not in result:
                result = read_short_interest_screen(target, as_of, limit, data_root=data_root)
        return result
    except Exception as exc:
        return {"error": f"Short-interest leaderboard is unavailable: {exc}"}


def _utc_now() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())