"""Deterministic, point-in-time screens over the normalized datasets.

The canonical short-interest leaderboard lives here: it reads normalized
FINRA short interest and SEC facts from the versioned Parquet datasets via
DuckDB, enforces ``known_at <= as_of`` on every fact join, classifies
eligible equities, and persists each run (coverage, exclusions, fact
provenance, calculation version) before returning a bounded result to the
agent tool.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .. import finra_client
from ..storage import duckdb, parquet

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"
SCREEN_CALC_VERSION = "short-interest-leaderboard-v2"
SLICE_CALC_VERSION = "short-interest-change-slice-v1"
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
SCREEN_NAME = "short_interest_leaderboard"
SLICE_NAME = "short_interest_change"

_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
_COMMON_EQUITY = "equity-common"


def _resolve_as_of(as_of: Optional[str]) -> str:
    """Knowledge horizon for a screen request.

    When as_of is omitted, the horizon is today's UTC date: the live screen
    sees everything ingested so far.  Historical reproduction must pass an
    explicit as_of, which then gates every FINRA row, ticker alias, security
    classification, and SEC fact via ``known_at <= as_of``.
    """
    if as_of:
        return str(as_of)
    return datetime.now(timezone.utc).date().isoformat()


def _clamp_limit(limit: Optional[int]) -> int:
    try:
        return max(1, min(int(limit if limit is not None else DEFAULT_LIMIT), MAX_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


def latest_settlement_date(as_of: Optional[str] = None, data_root: Optional[Path] = None) -> str:
    """Latest ingested settlement cycle, optionally restricted to cycles
    knowable on or before ``as_of``."""
    if as_of is None:
        rows = duckdb.query(
            "SELECT max(settlement_date) AS latest FROM short_interest",
            data_root=data_root,
        )
    else:
        clause, param = duckdb.as_of_clause(as_of)
        rows = duckdb.query(
            "SELECT max(settlement_date) AS latest FROM short_interest "
            f"WHERE {clause}",
            params=[param],
            data_root=data_root,
        )
    latest = rows[0]["latest"] if rows else None
    if not latest:
        horizon = f" knowable on or before {as_of}" if as_of else ""
        raise ValueError(
            f"No FINRA short interest is ingested{horizon}; run 'python cli.py refresh-data --settlement-date YYYY-MM-DD' first."
        )
    return str(latest)


def _snapshot_rows(settlement_date: str, as_of: str, data_root: Path) -> tuple[list[dict], int]:
    """Short-interest rows for one settlement cycle, point-in-time.

    Only source versions knowable on/before ``as_of`` are visible, and the
    newest such version wins per symbol (corrected snapshots supersede older
    ones exactly when they become knowable).  Same-instant conflicting
    versions (identical known_at/retrieved_at, different material values)
    resolve to unknown: the row is excluded, not arbitrarily picked.
    Returns ``(clean_rows, conflicting_count)``.
    """
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT * EXCLUDE (_rn) FROM ("
        "SELECT *, "
        "row_number() OVER (PARTITION BY symbol_code ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST, content_hash DESC, row_id DESC) AS _rn, "
        "count(DISTINCT list_value(CAST(short_position AS VARCHAR), CAST(prev_position AS VARCHAR), CAST(avg_daily_volume AS VARCHAR), CAST(days_to_cover AS VARCHAR), CAST(issue_name AS VARCHAR))) OVER (PARTITION BY symbol_code, CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
        f"FROM short_interest WHERE settlement_date = ? AND {clause}"
        ") WHERE _rn = 1 ORDER BY symbol_code",
        params=[settlement_date, param],
        data_root=data_root,
    )
    clean = [row for row in rows if row["_variants"] == 1]
    for row in clean:
        del row["_variants"]
    return clean, len(rows) - len(clean)


def _ticker_alias_map(as_of: str, data_root: Path) -> dict[str, list[str]]:
    """ticker -> all entity IDs carrying that ticker alias, restricted to
    aliases knowable on/before ``as_of`` (a mapping acquired later is not
    usable by an earlier screen)."""
    clause, param = duckdb.as_of_clause(as_of)
    aliases: dict[str, list[str]] = {}
    for row in duckdb.query(
        "SELECT alias_value, entity_id FROM entity_aliases "
        f"WHERE alias_type = 'ticker' AND {clause}",
        params=[param],
        data_root=data_root,
    ):
        aliases.setdefault(str(row["alias_value"]), []).append(str(row["entity_id"]))
    return aliases


def _security_type_map(as_of: str, data_root: Path) -> dict[str, str]:
    """entity_id -> security classification, restricted to classifications
    knowable on/before ``as_of``.  The newest classification row known at
    as_of wins per entity (classification revisions are point-in-time);
    same-instant conflicting classifications drop the entity (absent from
    the map -> counted as not classified)."""
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT entity_id, security_type FROM ("
        "SELECT entity_id, security_type, "
        "row_number() OVER (PARTITION BY entity_id ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST, content_hash DESC, security_id DESC) AS _rn, "
        "count(DISTINCT security_type) OVER (PARTITION BY entity_id, CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
        f"FROM securities WHERE {clause}"
        ") WHERE _rn = 1 AND _variants = 1",
        params=[param],
        data_root=data_root,
    )
    return {str(row["entity_id"]): str(row["security_type"]) for row in rows}


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


def _screen_input_fingerprint(settlement_date: str, as_of: str, data_root: Path) -> str:
    """Deterministic hash of the source rows the screen consumed.

    Mirrors the filters of _snapshot_rows/_ticker_alias_map/
    _security_type_map/_facts_by_entity (same WHERE/QUALIFY clauses) so the
    fingerprint changes exactly when the materialized inputs change: new SEC
    enrichment, corrected FINRA snapshot. Stable otherwise.
    """
    clause, param = duckdb.as_of_clause(as_of)
    payload = {
        "short_interest": duckdb.query(
            "SELECT row_id, content_hash, known_at FROM ("
            "SELECT row_id, content_hash, known_at, symbol_code, short_position, prev_position, avg_daily_volume, days_to_cover, issue_name, "
            "row_number() OVER (PARTITION BY symbol_code ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST, content_hash DESC, row_id DESC) AS _rn, "
            "count(DISTINCT list_value(CAST(short_position AS VARCHAR), CAST(prev_position AS VARCHAR), CAST(avg_daily_volume AS VARCHAR), CAST(days_to_cover AS VARCHAR), CAST(issue_name AS VARCHAR))) OVER (PARTITION BY symbol_code, CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
            f"FROM short_interest WHERE settlement_date = ? AND {clause}"
            ") WHERE _rn = 1 AND _variants = 1 ORDER BY symbol_code",
            params=[settlement_date, param],
            data_root=data_root,
        ),
        "entity_aliases": duckdb.query(
            "SELECT alias_type, alias_value, entity_id, source, valid_from, "
            "content_hash, known_at FROM entity_aliases "
            f"WHERE alias_type = 'ticker' AND {clause} "
            "ORDER BY alias_value, entity_id, source, valid_from",
            params=[param],
            data_root=data_root,
        ),
        "securities": duckdb.query(
            "SELECT security_id, content_hash, known_at FROM ("
            "SELECT security_id, content_hash, known_at, entity_id, security_type, "
            "row_number() OVER (PARTITION BY entity_id ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST, content_hash DESC, security_id DESC) AS _rn, "
            "count(DISTINCT security_type) OVER (PARTITION BY entity_id, CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
            f"FROM securities WHERE {clause}"
            ") WHERE _rn = 1 AND _variants = 1 ORDER BY entity_id",
            params=[param],
            data_root=data_root,
        ),
        "financial_facts": duckdb.query(
            "SELECT fact_id, content_hash, known_at FROM financial_facts "
            f"WHERE concept = ? AND {clause} ORDER BY fact_id",
            params=[_SHARES_CONCEPT, param],
            data_root=data_root,
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def materialize_short_interest_screen(
    settlement_date: str,
    as_of: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> dict:
    """Build one complete settlement-date leaderboard from normalized data.

    ``as_of`` is the knowledge horizon: FINRA rows, ticker aliases, security
    classifications, and SEC facts are all restricted to ``known_at <=
    as_of``, and the newest FINRA source version known at as_of wins per
    symbol (same-instant conflicting versions resolve to unknown and are
    excluded).  When omitted it defaults to today (the live screen);
    historical reproduction passes an explicit as_of.

    The ranking is deterministic: same settlement date, same ``as_of``, same
    ingested data -> identical ranking.  The run is persisted with its
    coverage, exclusions, fact provenance, and calculation version before
    any bounded result is returned.
    """
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    as_of = _resolve_as_of(as_of)
    rows, conflicting = _snapshot_rows(settlement_date, as_of, data_root)
    if not rows:
        if conflicting:
            return {
                "error": (
                    f"FINRA short interest exists for settlement date "
                    f"{settlement_date} knowable on or before {as_of}, but all "
                    f"rows for this settlement conflict at the same instant "
                    f"(ambiguous); cannot build an unambiguous leaderboard."
                )
            }
        return {
            "error": (
                f"No normalized FINRA short interest for settlement date "
                f"{settlement_date} is knowable on or before {as_of}; run "
                f"'python cli.py refresh-data --settlement-date {settlement_date}' first (or pass a later as_of)."
            )
        }
    ticker_aliases = _ticker_alias_map(as_of, data_root)
    security_types = _security_type_map(as_of, data_root)
    facts_by_entity = _facts_by_entity(as_of, data_root)
    exclusions = {
        "unmapped_symbol": 0,
        "ambiguous_ticker_mapping": 0,
        "not_classified_common_equity": 0,
        "missing_shares_outstanding": 0,
        "invalid_short_interest": 0,
        "conflicting_versions": 0,
    }
    exclusions["conflicting_versions"] = conflicting
    # Stage counters are cumulative complements of the exclusions: a row
    # excluded at an earlier stage never reached the later checks, so the
    # CLI reports these directly instead of deriving them from exclusions.
    counters = {
        "valid_short_interest_rows": 0,
        "mapped_rows": 0,
        "unambiguous_rows": 0,
        "common_equity_rows": 0,
        "shares_outstanding_rows": 0,
    }
    candidates: list[dict] = []
    for row in rows:
        symbol = str(row["symbol_code"])
        short_shares = row.get("short_position")
        if short_shares is None or float(short_shares) < 0:
            exclusions["invalid_short_interest"] += 1
            continue
        short_shares = float(short_shares)
        counters["valid_short_interest_rows"] += 1
        entity_ids = ticker_aliases.get(symbol)
        if not entity_ids:
            exclusions["unmapped_symbol"] += 1
            continue
        counters["mapped_rows"] += 1
        if len(entity_ids) > 1:
            exclusions["ambiguous_ticker_mapping"] += 1
            continue
        counters["unambiguous_rows"] += 1
        entity_id = entity_ids[0]
        # Eligibility is the stored security classification, not a fact-
        # presence proxy: only entities classified as common equity rank.
        if security_types.get(entity_id) != _COMMON_EQUITY:
            exclusions["not_classified_common_equity"] += 1
            continue
        counters["common_equity_rows"] += 1
        fact = _select_fact_for_period(facts_by_entity.get(entity_id) or [], settlement_date)
        if fact is None:
            # Classified common equity but no shares-outstanding fact
            # knowable on/before as_of with period end <= settlement: a data
            # gap, not proof of non-common-equity.
            exclusions["missing_shares_outstanding"] += 1
            continue
        counters["shares_outstanding_rows"] += 1
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
    run_id = f"{SCREEN_NAME}:{settlement_date}:{as_of}:{SCREEN_CALC_VERSION}:{_screen_input_fingerprint(settlement_date, as_of, data_root)}"
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
            "valid_short_interest_rows": counters["valid_short_interest_rows"],
            "mapped_rows": counters["mapped_rows"],
            "unambiguous_rows": counters["unambiguous_rows"],
            "common_equity_rows": counters["common_equity_rows"],
            "shares_outstanding_rows": counters["shares_outstanding_rows"],
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
    as_of = _resolve_as_of(as_of)
    runs = duckdb.query(
        "SELECT * FROM screen_runs WHERE screen = ? AND settlement_date = ? AND as_of = ? "
        "ORDER BY created_at DESC, run_id DESC LIMIT 1",
        params=[SCREEN_NAME, settlement_date, as_of],
        data_root=data_root,
    )
    if not runs:
        return {"error": f"No published short-interest leaderboard for settlement date {settlement_date}."}
    run = runs[0]
    entries = duckdb.query(
        "SELECT * FROM screen_entries WHERE run_id = ? ORDER BY rank LIMIT ?",
        params=[run["run_id"], limit],
        data_root=data_root,
    )
    try:
        days = (date.today() - date.fromisoformat(settlement_date)).days
        freshness = "stale" if days > finra_client.STALE_AFTER_DAYS else "current"
    except (TypeError, ValueError):
        freshness = "unknown"
    exclusions = json.loads(run["exclusions_json"])
    if run.get("valid_short_interest_rows") is None:
        # Old-schema run (pre stage-counter commit): the sequential pipeline
        # excluded rows in this exact order, so the cumulative counters are
        # reconstructible from the exclusive exclusions.
        valid = run["finra_rows"] - exclusions["invalid_short_interest"]
        mapped = valid - exclusions["unmapped_symbol"]
        unambiguous = mapped - exclusions["ambiguous_ticker_mapping"]
        common_equity = unambiguous - exclusions["not_classified_common_equity"]
        shares_outstanding = common_equity - exclusions["missing_shares_outstanding"]
    else:
        valid = run["valid_short_interest_rows"]
        mapped = run["mapped_rows"]
        unambiguous = run["unambiguous_rows"]
        common_equity = run["common_equity_rows"]
        shares_outstanding = run["shares_outstanding_rows"]
    return {
        "source": "FINRA consolidated short interest + SEC EDGAR company facts (parquet)",
        "metric": "short shares divided by SEC-reported shares outstanding (not public float)",
        "settlement_date": settlement_date,
        "as_of_date": as_of,
        "data_freshness": freshness,
        "calculation_version": run["calc_version"],
        "environment": run["environment"],
        "row_count": run["eligible_rows"],
        "returned_count": len(entries),
        "truncated": len(entries) < run["eligible_rows"],
        "coverage": {
            "finra_rows": run["finra_rows"],
            "eligible_rows": run["eligible_rows"],
            "valid_short_interest_rows": valid,
            "mapped_rows": mapped,
            "unambiguous_rows": unambiguous,
            "common_equity_rows": common_equity,
            "shares_outstanding_rows": shares_outstanding,
            "exclusions": exclusions,
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
    """Return a bounded leaderboard, materializing the requested cycle
    (republishing only when its inputs changed) for the requested ``as_of``.

    ``as_of`` defaults to today; pass an explicit as_of for a historical
    screen (only data knowable on/before as_of is used).
    """
    try:
        as_of = _resolve_as_of(as_of)
        target = settlement_date or latest_settlement_date(as_of, data_root)
        result = materialize_short_interest_screen(target, as_of, data_root=data_root)
        if "error" not in result:
            result = read_short_interest_screen(target, as_of, limit, data_root=data_root)
        return result
    except Exception as exc:
        return {"error": f"Short-interest leaderboard is unavailable: {exc}"}


def _utc_now() -> str:
	"""UTC publication timestamp; microsecond precision so same-second
	materializations still order by publication time."""
	return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# Research slice: short-interest change + shares-outstanding change
# ---------------------------------------------------------------------------


def _cycle_settlement_dates(as_of: str, data_root: Path) -> list[str]:
    """Latest two settlement cycles knowable on or before ``as_of``."""
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT DISTINCT settlement_date FROM short_interest "
        f"WHERE CAST(settlement_date AS DATE) <= CAST(? AS DATE) AND {clause} "
        "ORDER BY settlement_date DESC LIMIT 2",
        params=[as_of, param],
        data_root=data_root,
    )
    return [str(row["settlement_date"]) for row in rows]


def _cycle_entities(
    settlement_date: str,
    as_of: str,
    ticker_aliases: dict[str, list[str]],
    security_types: dict[str, str],
    facts_by_entity: dict[str, list[dict]],
    data_root: Path,
) -> dict[str, dict]:
    """Eligible entities for one settlement cycle: symbol -> row + fact.

    Same point-in-time rules as the leaderboard: only source versions and
    classifications knowable on/before ``as_of`` are used, and only entities
    classified as common equity rank.
    """
    rows, _ = _snapshot_rows(settlement_date, as_of, data_root)
    result: dict[str, dict] = {}
    for row in rows:
        symbol = str(row["symbol_code"])
        short_shares = row.get("short_position")
        if short_shares is None:
            continue
        entity_ids = ticker_aliases.get(symbol)
        if not entity_ids or len(entity_ids) > 1:
            continue
        entity_id = entity_ids[0]
        if security_types.get(entity_id) != _COMMON_EQUITY:
            continue
        fact = _select_fact_for_period(facts_by_entity.get(entity_id) or [], settlement_date)
        if fact is None:
            continue
        result[symbol] = {"row": row, "entity_id": entity_id, "fact": fact}
    return result


def _select_fact_for_period(facts: list[dict], settlement_date: str) -> Optional[dict]:
    """Latest fact whose period end is on/before the settlement date; facts
    are pre-sorted newest first and already restricted by known_at <= as_of."""
    for fact in facts:
        period_end = str(fact.get("period_end") or "")
        if not period_end or period_end > settlement_date:
            continue
        value = fact.get("value")
        if value is None or float(value) <= 0:
            continue
        return fact
    return None


def short_interest_change_screen(
    as_of: str,
    limit: Optional[int] = None,
    data_root: Optional[Path] = None,
) -> dict:
    """Dated research slice: short-interest change + shares-outstanding change
    between the two most recent settlement cycles knowable on/before as_of.

    Every SEC fact is filtered by ``known_at <= as_of``; a later filing can
    never alter a slice computed at an earlier ``as_of``.  Missing prior
    cycles or facts are reported as None, never as zero.
    """
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    limit = _clamp_limit(limit)
    dates = _cycle_settlement_dates(as_of, data_root)
    if not dates:
        return {"error": f"No FINRA short interest cycles knowable on or before {as_of}."}
    current_date, prior_date = dates[0], dates[1] if len(dates) > 1 else None
    ticker_aliases = _ticker_alias_map(as_of, data_root)
    security_types = _security_type_map(as_of, data_root)
    facts_by_entity = _facts_by_entity(as_of, data_root)
    current = _cycle_entities(current_date, as_of, ticker_aliases, security_types, facts_by_entity, data_root)
    prior = _cycle_entities(prior_date, as_of, ticker_aliases, security_types, facts_by_entity, data_root) if prior_date else {}
    entries: list[dict] = []
    for symbol, item in sorted(current.items()):
        row, fact = item["row"], item["fact"]
        short_current = float(row["short_position"])
        si_pct_current = 100 * short_current / float(fact["value"])
        entry: dict = {
            "ticker": symbol,
            "issue_name": row.get("issue_name"),
            "settlement_current": current_date,
            "settlement_prior": prior_date,
            "short_shares_current": short_current,
            "short_interest_percent_current": si_pct_current,
            "shares_outstanding_current": float(fact["value"]),
            "sec_shares_as_of_current": str(fact["period_end"]),
            "sec_filed_at_current": str(fact["filed_at"]),
            "sec_accession_current": fact.get("accession"),
            "sec_source_url_current": fact.get("source_url"),
            "short_shares_prior": None,
            "short_interest_percent_prior": None,
            "shares_outstanding_prior": None,
            "sec_shares_as_of_prior": None,
            "sec_filed_at_prior": None,
            "sec_accession_prior": None,
            "sec_source_url_prior": None,
            "short_change_abs": None,
            "short_change_pct": None,
            "shares_change_abs": None,
            "shares_change_pct": None,
            "si_pp_change": None,
            "finra_source_url": row.get("source_url"),
        }
        prior_item = prior.get(symbol)
        if prior_item is not None:
            prior_row, prior_fact = prior_item["row"], prior_item["fact"]
            short_prior = float(prior_row["short_position"])
            si_pct_prior = 100 * short_prior / float(prior_fact["value"])
            entry.update({
                "short_shares_prior": short_prior,
                "short_interest_percent_prior": si_pct_prior,
                "shares_outstanding_prior": float(prior_fact["value"]),
                "sec_shares_as_of_prior": str(prior_fact["period_end"]),
                "sec_filed_at_prior": str(prior_fact["filed_at"]),
                "sec_accession_prior": prior_fact.get("accession"),
                "sec_source_url_prior": prior_fact.get("source_url"),
                "short_change_abs": short_current - short_prior,
                "short_change_pct": 100 * (short_current - short_prior) / short_prior if short_prior else None,
                "shares_change_abs": float(fact["value"]) - float(prior_fact["value"]),
                "shares_change_pct": 100 * (float(fact["value"]) - float(prior_fact["value"])) / float(prior_fact["value"]),
                "si_pp_change": si_pct_current - si_pct_prior,
            })
        entries.append(entry)
    entries.sort(key=lambda e: (-(e["si_pp_change"] if e["si_pp_change"] is not None else 0.0), e["ticker"]))
    for index, entry in enumerate(entries, 1):
        entry["rank"] = index
    return {
        "source": "FINRA consolidated short interest + SEC EDGAR company facts (parquet)",
        "metric": "cycle-over-cycle short-interest change and shares-outstanding change; short interest is a settlement-date position, not Reg SHO volume",
        "as_of": as_of,
        "settlement_current": current_date,
        "settlement_prior": prior_date,
        "calculation_version": SLICE_CALC_VERSION,
        "coverage": {"current_finra_rows": len(current), "eligible_rows": len(entries)},
        "entries": entries[:limit],
    }