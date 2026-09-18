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
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from .. import finra_client
from ..config import finra_use_mock, get_data_root
from ..storage import duckdb

DEFAULT_DATA_ROOT = get_data_root()
SCREEN_CALC_VERSION = "short-interest-leaderboard-v2"
SLICE_CALC_VERSION = "short-interest-change-slice-v1"
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
SCREEN_NAME = "short_interest_leaderboard"
SLICE_NAME = "short_interest_change"

_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"
_COMMON_EQUITY = "equity-common"


def _resolve_as_of(as_of: str | None) -> str:
    """Knowledge horizon for a screen request.

    When as_of is omitted, the horizon is today's UTC date: the live screen
    sees everything ingested so far.  Historical reproduction must pass an
    explicit as_of, which then gates every FINRA row, ticker alias, security
    classification, and SEC fact via ``known_at <= as_of``.
    """
    if as_of:
        return as_of
    return datetime.now(UTC).date().isoformat()


def _date_str(value: object) -> str:
    """ISO date string for TEXT or TIMESTAMPTZ column values (existing boundary)."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def _clamp_limit(limit: int | None) -> int:
    try:
        return max(1, min((limit if limit is not None else DEFAULT_LIMIT), MAX_LIMIT))
    except TypeError, ValueError:
        return DEFAULT_LIMIT


def latest_settlement_date(as_of: str | None = None, data_root: Path | None = None) -> str:
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
            f"SELECT max(settlement_date) AS latest FROM short_interest WHERE {clause}",
            params=[param],
            data_root=data_root,
        )
    latest = rows[0]["latest"] if rows else None
    if not latest:
        horizon = f" knowable on or before {as_of}" if as_of else ""
        raise ValueError(
            f"No FINRA short interest is ingested{horizon}; run 'python cli.py refresh-data --settlement-date YYYY-MM-DD' first."
        )
    return _date_str(latest)


def _snapshot_rows(settlement_date: str, as_of: str, data_root: Path) -> tuple[list[dict[str, object]], int]:
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
        row.pop("_dedup", None)
        row.pop("_tsraw", None)
    return clean, len(rows) - len(clean)


def _ticker_alias_map(as_of: str, data_root: Path) -> dict[str, list[str]]:
    """ticker -> all entity IDs carrying that ticker alias, restricted to
    aliases knowable on/before ``as_of`` (a mapping acquired later is not
    usable by an earlier screen)."""
    clause, param = duckdb.as_of_clause(as_of)
    aliases: dict[str, list[str]] = {}
    for row in duckdb.query(
        f"SELECT alias_value, entity_id FROM entity_aliases WHERE alias_type = 'ticker' AND {clause}",
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


def _facts_by_entity(as_of: str, data_root: Path) -> dict[str, list[dict[str, object]]]:
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
    by_entity: dict[str, list[dict[str, object]]] = {}
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
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:16]


def _leaderboard_key(item: dict[str, object]) -> tuple[float, str]:
    """Short-interest percent descending, ticker ascending."""
    return (-float(str(item["short_interest_percent"])), str(item["ticker"]))


class _ScreenInputs:
    """Resolved point-in-time maps for one screen build (existing boundary)."""

    def __init__(
        self,
        ticker_aliases: dict[str, list[str]],
        security_types: dict[str, str],
        facts_by_entity: dict[str, list[dict[str, object]]],
    ) -> None:
        self.ticker_aliases = ticker_aliases
        self.security_types = security_types
        self.facts_by_entity = facts_by_entity


class _ScreenAccum:
    """Exclusions, stage counters, and candidates (existing boundary)."""

    def __init__(self, conflicting: int) -> None:
        self.exclusions = {
            "unmapped_symbol": 0,
            "ambiguous_ticker_mapping": 0,
            "not_classified_common_equity": 0,
            "missing_shares_outstanding": 0,
            "invalid_short_interest": 0,
            "conflicting_versions": conflicting,
        }
        # Stage counters are cumulative complements of the exclusions: a row
        # excluded at an earlier stage never reached the later checks, so the
        # CLI reports these directly instead of deriving them from exclusions.
        self.counters = {
            "valid_short_interest_rows": 0,
            "mapped_rows": 0,
            "unambiguous_rows": 0,
            "common_equity_rows": 0,
            "shares_outstanding_rows": 0,
        }
        self.candidates: list[dict[str, object]] = []


def _empty_screen_error(settlement_date: str, as_of: str, conflicting: int) -> dict[str, object] | None:
    """Empty-snapshot error envelope, None when rows exist (existing boundary)."""
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


def _screen_inputs(as_of: str, data_root: Path) -> _ScreenInputs:
    """Point-in-time alias/classification/fact maps (existing boundary)."""
    return _ScreenInputs(
        _ticker_alias_map(as_of, data_root),
        _security_type_map(as_of, data_root),
        _facts_by_entity(as_of, data_root),
    )


def _short_shares(row: dict[str, object], accum: _ScreenAccum) -> float | None:
    """Validated short shares, None when invalid (existing boundary)."""
    short_shares_raw = row.get("short_position")
    if short_shares_raw is None or float(str(short_shares_raw)) < 0:
        accum.exclusions["invalid_short_interest"] += 1
        return None
    accum.counters["valid_short_interest_rows"] += 1
    return float(str(short_shares_raw))


def _screen_entity(symbol: str, inputs: _ScreenInputs, accum: _ScreenAccum) -> str | None:
    """Mapped unambiguous entity, None when excluded (existing boundary)."""
    entity_ids = inputs.ticker_aliases.get(symbol)
    if not entity_ids:
        accum.exclusions["unmapped_symbol"] += 1
        return None
    accum.counters["mapped_rows"] += 1
    if len(entity_ids) > 1:
        accum.exclusions["ambiguous_ticker_mapping"] += 1
        return None
    accum.counters["unambiguous_rows"] += 1
    return entity_ids[0]


def _screen_fact(
    entity_id: str, settlement_date: str, inputs: _ScreenInputs, accum: _ScreenAccum
) -> dict[str, object] | None:
    """Eligible shares-outstanding fact, None when excluded (existing boundary)."""
    # Eligibility is the stored security classification, not a fact-
    # presence proxy: only entities classified as common equity rank.
    if inputs.security_types.get(entity_id) != _COMMON_EQUITY:
        accum.exclusions["not_classified_common_equity"] += 1
        return None
    accum.counters["common_equity_rows"] += 1
    fact = _select_fact_for_period(inputs.facts_by_entity.get(entity_id) or [], settlement_date)
    if fact is None:
        # Classified common equity but no shares-outstanding fact
        # knowable on/before as_of with period end <= settlement: a data
        # gap, not proof of non-common-equity.
        accum.exclusions["missing_shares_outstanding"] += 1
        return None
    accum.counters["shares_outstanding_rows"] += 1
    return fact


def _screen_candidate(
    symbol: str, row: dict[str, object], entity_id: str, short_shares: float, fact: dict[str, object]
) -> dict[str, object]:
    """One ranked candidate row (existing boundary)."""
    shares = float(str(fact["value"]))
    return {
        "entity_id": entity_id,
        "security_id": f"sec:equity:{entity_id.rsplit(':', 1)[1]}",
        "ticker": symbol,
        "issue_name": row.get("issue_name"),
        "short_shares": short_shares,
        "shares_outstanding": shares,
        "short_interest_percent": 100 * short_shares / shares,
        "sec_shares_as_of": _date_str(fact["period_end"]),
        "sec_filed_at": _date_str(fact["filed_at"]),
        "sec_accession": fact.get("accession"),
        "sec_source_url": fact.get("source_url"),
    }


def _accumulate_screen_row(
    row: dict[str, object], settlement_date: str, inputs: _ScreenInputs, accum: _ScreenAccum
) -> None:
    """One snapshot row through the eligibility pipeline (existing boundary)."""
    symbol = str(row["symbol_code"])
    short_shares = _short_shares(row, accum)
    if short_shares is None:
        return
    entity_id = _screen_entity(symbol, inputs, accum)
    if entity_id is None:
        return
    fact = _screen_fact(entity_id, settlement_date, inputs, accum)
    if fact is None:
        return
    accum.candidates.append(_screen_candidate(symbol, row, entity_id, short_shares, fact))


def _persist_screen_run(
    settlement_date: str, as_of: str, data_root: Path, rows: list[dict[str, object]], accum: _ScreenAccum
) -> None:
    """Persist run + entries rows (existing boundary)."""
    accum.candidates.sort(key=_leaderboard_key)
    run_id = f"{SCREEN_NAME}:{settlement_date}:{as_of}:{SCREEN_CALC_VERSION}:{_screen_input_fingerprint(settlement_date, as_of, data_root)}"
    created_at = _utc_now()
    duckdb.insert_ignore(
        "screen_runs",
        [
            {
                "run_id": run_id,
                "screen": SCREEN_NAME,
                "settlement_date": settlement_date,
                "as_of": as_of,
                "created_at": created_at,
                "calc_version": SCREEN_CALC_VERSION,
                "finra_rows": len(rows),
                "eligible_rows": len(accum.candidates),
                "valid_short_interest_rows": accum.counters["valid_short_interest_rows"],
                "mapped_rows": accum.counters["mapped_rows"],
                "unambiguous_rows": accum.counters["unambiguous_rows"],
                "common_equity_rows": accum.counters["common_equity_rows"],
                "shares_outstanding_rows": accum.counters["shares_outstanding_rows"],
                "exclusions_json": json.dumps(accum.exclusions, sort_keys=True),
                "environment": finra_client._environment(),
                "parser_version": SCREEN_CALC_VERSION,
            }
        ],
        data_root=data_root,
    )
    duckdb.insert_ignore(
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
            for index, item in enumerate(accum.candidates, 1)
        ],
        data_root=data_root,
    )


def materialize_short_interest_screen(
    settlement_date: str,
    as_of: str | None = None,
    data_root: Path | None = None,
) -> dict[str, object]:
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
    data_root = Path(data_root) if data_root else get_data_root()
    as_of = _resolve_as_of(as_of)
    rows, conflicting = _snapshot_rows(settlement_date, as_of, data_root)
    if not rows:
        err = _empty_screen_error(settlement_date, as_of, conflicting)
        assert err is not None
        return err
    inputs = _screen_inputs(as_of, data_root)
    accum = _ScreenAccum(conflicting)
    for row in rows:
        _accumulate_screen_row(row, settlement_date, inputs, accum)
    _persist_screen_run(settlement_date, as_of, data_root, rows, accum)
    return read_short_interest_screen(settlement_date, as_of, DEFAULT_LIMIT, data_root=data_root)


def read_short_interest_screen(
    settlement_date: str,
    as_of: str | None = None,
    limit: int | None = None,
    data_root: Path | None = None,
) -> dict[str, object]:
    """Read a published screen run, bounded to ``limit`` entries."""
    data_root = Path(data_root) if data_root else get_data_root()
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
        days = (date.today() - date.fromisoformat(settlement_date)).days  # noqa: DTZ011 - trading-calendar local date has no tz meaning
        freshness = "stale" if days > finra_client.STALE_AFTER_DAYS else "current"
    except TypeError, ValueError:
        freshness = "unknown"
    exclusions_raw = run["exclusions_json"]
    exclusions = json.loads(exclusions_raw) if isinstance(exclusions_raw, str) else {}
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
    run_eligible = run["eligible_rows"]
    eligible_count = run_eligible if isinstance(run_eligible, int) else 0
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
        "truncated": len(entries) < eligible_count,
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


_FETCH_DISCOVERY_CYCLES = 6


def _raw_cycle_dates(year: int, month: int) -> list[date]:
    """Month-end then mid-month raw settlement dates (existing boundary)."""
    return [date(year, month, monthrange(year, month)[1]), date(year, month, 15)]


def _shift_candidates(raw: date, today: date, candidates: list[str]) -> None:
    """Up-to-3 preceding weekdays for one raw date (existing boundary)."""
    for offset in range(4):
        candidate = raw - timedelta(days=offset)
        if offset and candidate.weekday() >= 5:
            continue
        if candidate <= today and str(candidate) not in candidates:
            candidates.append(str(candidate))


def _prev_month(year: int, month: int) -> tuple[int, int]:
    """One month back with year rollover (existing boundary)."""
    month -= 1
    if month == 0:
        return year - 1, 12
    return year, month


def _candidate_settlement_dates(today: date, count: int = _FETCH_DISCOVERY_CYCLES) -> list[str]:
    """Newest-first FINRA settlement calendar dates on/before ``today``.

    FINRA publishes mid-month (15th) and month-end cycles, shifted to a
    business day when the calendar date hits a weekend or holiday. The
    shift is resolved by the 1-row probe in
    ``_discover_latest_published_settlement_date``, not by weekday
    arithmetic here: each raw date is emitted with up to 3 preceding
    weekdays (covers weekend + single-holiday shifts), newest-first
    deduped, and the first candidate with published rows wins.
    """
    candidates: list[str] = []
    year, month = today.year, today.month
    while len(candidates) < count:
        for raw in _raw_cycle_dates(year, month):
            _shift_candidates(raw, today, candidates)
            if len(candidates) >= count:
                break
        year, month = _prev_month(year, month)
    return candidates


def _probe_published_rows(candidate: str) -> int:
    """1-row FINRA probe: Record-Total tells whether ``candidate`` published."""
    name = "consolidatedShortInterest" + ("Mock" if finra_use_mock() else "")
    _, _, headers = finra_client.ingestion_post_query(
        "otcMarket",
        name,
        {
            "limit": 1,
            "offset": 0,
            "fields": ["settlementDate"],
            "compareFilters": [
                {
                    "compareType": "EQUAL",
                    "fieldName": "settlementDate",
                    "fieldValue": candidate,
                }
            ],
        },
    )
    try:
        return int(str(headers.get("record-total", 0)))
    except TypeError, ValueError:
        return 0


def _discover_latest_published_settlement_date(today: date) -> str | None:
    """Newest FINRA-published settlement date, newest candidate first.

    Probes are best-effort: a failed probe skips that candidate.  Returns
    None when no candidate has published rows.
    """
    for candidate in _candidate_settlement_dates(today):
        try:
            if _probe_published_rows(candidate) > 0:
                return candidate
        except Exception:  # noqa: BLE001, S112 - intentional best-effort boundary, never aborts
            continue
    return None


def _fetch_live_cycle(settlement_date: str, data_root: Path) -> None:
    """Full-fetch one settlement cycle into the store (live screens only)."""
    from ..services import research_data

    research_data.refresh_finra_short_interest(settlement_date, data_root=data_root)


def _explicit_cycle_target(resolved: str, root: Path, settlement_date: str, live: bool) -> str:
    """Explicit-date target with live fetch-on-empty (existing boundary)."""
    if live:
        rows, conflicting = _snapshot_rows(settlement_date, resolved, root)
        if not rows and not conflicting:
            _fetch_live_cycle(settlement_date, root)
    return settlement_date


def _discover_and_fetch(resolved: str, root: Path) -> str:
    """Newest published cycle, fetched (existing boundary)."""
    discovered = _discover_latest_published_settlement_date(date.fromisoformat(resolved))
    if discovered is None:
        raise ValueError("no published settlement cycle")
    _fetch_live_cycle(discovered, root)
    return discovered


def _refresh_newer_cycle(resolved: str, root: Path, stored: str) -> str:
    """Fetch the newer published cycle, stored when fetch fails (existing boundary)."""
    newer = _newer_published_cycle(resolved, stored)
    if newer is None:
        return stored
    return _refresh_published_cycle(newer, root) or stored


def _stored_cycle_target(resolved: str, root: Path, live: bool) -> str:
    """Stored target, refreshed when a newer cycle published (existing boundary)."""
    stored = latest_settlement_date(resolved, root)
    if not live:
        return stored
    return _refresh_newer_cycle(resolved, root, stored)


def _resolve_leaderboard_target(resolved: str, root: Path, settlement_date: str | None, live: bool) -> str:
    """Settlement target with live fetch-on-empty (existing boundary)."""
    if settlement_date is not None:
        return _explicit_cycle_target(resolved, root, settlement_date, live)
    try:
        return _stored_cycle_target(resolved, root, live)
    except ValueError:
        if not live:
            raise
        return _discover_and_fetch(resolved, root)


def _published_cycle(resolved: str) -> str | None:
    """Newest published cycle, None on any probe failure (existing boundary)."""
    try:
        return _discover_latest_published_settlement_date(date.fromisoformat(resolved))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _newer_published_cycle(resolved: str, target: str) -> str | None:
    """Newest published cycle when newer than the store, else None (existing boundary)."""
    published = _published_cycle(resolved)
    if published is None or not published > target:
        return None
    return published


def _read_leaderboard_cycle(target: str, resolved: str, limit: int | None, root: Path) -> dict[str, object]:
    """Materialize then read one cycle (existing boundary)."""
    result = materialize_short_interest_screen(target, resolved, data_root=root)
    if "error" not in result:
        result = read_short_interest_screen(target, resolved, limit, data_root=root)
    return result


def _refresh_published_cycle(published: str, root: Path) -> str | None:
    """Fetch one published cycle; None when the fetch fails (existing boundary)."""
    try:
        _fetch_live_cycle(published, root)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return published


def get_short_interest_leaderboard(
    limit: int | None = None,
    settlement_date: str | None = None,
    as_of: str | None = None,
    data_root: Path | None = None,
) -> dict[str, object]:
    """Return a bounded leaderboard, materializing the requested cycle
    (republishing only when its inputs changed) for the requested ``as_of``.

    ``as_of`` defaults to today; pass an explicit as_of for a historical
    screen (only data knowable on/before as_of is used).

    A live screen (no ``as_of``) fetches from FINRA when the store has no
    usable cycle: with no ``settlement_date`` it discovers the newest
    published cycle and fetches it; with an explicit ``settlement_date``
    missing from the store it fetches exactly that date.  A historical
    screen (explicit ``as_of``) never fetches. Fetched rows carry ``retrieved_at``
    as ``known_at`` (publication unknown), so a later ``as_of >=`` retrieval
    sees them; a past ``as_of`` still sees nothing new. Fetch failures
    surface as ``{"error": ...}``, never raise.
    """
    live = not as_of
    try:
        resolved = _resolve_as_of(as_of)
        root = Path(data_root) if data_root else get_data_root()
        target = _resolve_leaderboard_target(resolved, root, settlement_date, live)
        return _read_leaderboard_cycle(target, resolved, limit, root)
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return {"error": f"Short-interest leaderboard is unavailable: {exc}"}


def _utc_now() -> str:
    """UTC publication timestamp; microsecond precision so same-second
    materializations still order by publication time."""
    return datetime.now(UTC).isoformat(timespec="microseconds")


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
    return [_date_str(row["settlement_date"]) for row in rows]


class _CycleItem(TypedDict):
    row: dict[str, object]
    entity_id: str
    fact: dict[str, object]


def _cycle_entities(
    settlement_date: str,
    as_of: str,
    ticker_aliases: dict[str, list[str]],
    security_types: dict[str, str],
    facts_by_entity: dict[str, list[dict[str, object]]],
    data_root: Path,
) -> dict[str, _CycleItem]:
    """Eligible entities for one settlement cycle: symbol -> row + fact.

    Same point-in-time rules as the leaderboard: only source versions and
    classifications knowable on/before ``as_of`` are used, and only entities
    classified as common equity rank.
    """
    rows, _ = _snapshot_rows(settlement_date, as_of, data_root)
    result: dict[str, _CycleItem] = {}
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


def _select_fact_for_period(facts: list[dict[str, object]], settlement_date: str) -> dict[str, object] | None:
    """Latest fact whose period end is on/before the settlement date; facts
    are pre-sorted newest first and already restricted by known_at <= as_of."""
    for fact in facts:
        period_end = _date_str(fact.get("period_end") or "")
        if not period_end or period_end[:10] > settlement_date:
            continue
        value = fact.get("value")
        if value is None or float(str(value)) <= 0:
            continue
        return fact
    return None


def _change_key(e: dict[str, object]) -> tuple[float, str]:
    """Largest percentage-point change first, ticker ascending."""
    change = e["si_pp_change"]
    return (-(float(str(change)) if change is not None else 0.0), str(e["ticker"]))


def short_interest_change_screen(
    as_of: str,
    limit: int | None = None,
    data_root: Path | None = None,
) -> dict[str, object]:
    """Dated research slice: short-interest change + shares-outstanding change
    between the two most recent settlement cycles knowable on/before as_of.

    Every SEC fact is filtered by ``known_at <= as_of``; a later filing can
    never alter a slice computed at an earlier ``as_of``.  Missing prior
    cycles or facts are reported as None, never as zero.
    """
    data_root = Path(data_root) if data_root else get_data_root()
    limit = _clamp_limit(limit)
    dates = _cycle_settlement_dates(as_of, data_root)
    if not dates:
        return {"error": f"No FINRA short interest cycles knowable on or before {as_of}."}
    current_date, prior_date = dates[0], dates[1] if len(dates) > 1 else None
    ticker_aliases = _ticker_alias_map(as_of, data_root)
    security_types = _security_type_map(as_of, data_root)
    facts_by_entity = _facts_by_entity(as_of, data_root)
    current = _cycle_entities(current_date, as_of, ticker_aliases, security_types, facts_by_entity, data_root)
    prior: dict[str, _CycleItem] = (
        _cycle_entities(prior_date, as_of, ticker_aliases, security_types, facts_by_entity, data_root)
        if prior_date
        else {}
    )
    entries: list[dict[str, object]] = []
    for symbol, item in sorted(current.items()):
        row, fact = item["row"], item["fact"]
        short_current = float(str(row["short_position"]))
        si_pct_current = 100 * short_current / float(str(fact["value"]))
        entry: dict[str, object] = {
            "ticker": symbol,
            "issue_name": row.get("issue_name"),
            "settlement_current": current_date,
            "settlement_prior": prior_date,
            "short_shares_current": short_current,
            "short_interest_percent_current": si_pct_current,
            "shares_outstanding_current": float(str(fact["value"])),
            "sec_shares_as_of_current": _date_str(fact["period_end"]),
            "sec_filed_at_current": _date_str(fact["filed_at"]),
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
            short_prior = float(str(prior_row["short_position"]))
            si_pct_prior = 100 * short_prior / float(str(prior_fact["value"]))
            entry.update(
                {
                    "short_shares_prior": short_prior,
                    "short_interest_percent_prior": si_pct_prior,
                    "shares_outstanding_prior": float(str(prior_fact["value"])),
                    "sec_shares_as_of_prior": _date_str(prior_fact["period_end"]),
                    "sec_filed_at_prior": _date_str(prior_fact["filed_at"]),
                    "sec_accession_prior": prior_fact.get("accession"),
                    "sec_source_url_prior": prior_fact.get("source_url"),
                    "short_change_abs": short_current - short_prior,
                    "short_change_pct": 100 * (short_current - short_prior) / short_prior if short_prior else None,
                    "shares_change_abs": float(str(fact["value"])) - float(str(prior_fact["value"])),
                    "shares_change_pct": 100
                    * (float(str(fact["value"])) - float(str(prior_fact["value"])))
                    / float(str(prior_fact["value"])),
                    "si_pp_change": si_pct_current - si_pct_prior,
                }
            )
        entries.append(entry)
    entries.sort(key=_change_key)
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
