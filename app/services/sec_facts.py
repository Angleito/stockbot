"""Store-first SEC fact read path behind get_fundamentals/get_xbrl_facts.

EPS and shares_outstanding read the normalized Parquet store first (gated
``known_at <= as_of``, restatements resolved by the true latest ``filed_at``),
with the live ``edgar_client`` path as fallback; balance_sheet, overview, and
xbrl facts stay always-live.  Every served result is wrapped in a truthfully
labeled envelope: ``data_source`` is ``'store'`` only when the value actually
came from the point-in-time store, ``'live'`` otherwise.

The live EPS payload carries a human label under the key ``source``; the
envelope needs ``source`` for the provider code, so the payload label key is
renamed ``source`` -> ``source_label`` in every envelope (store and live
alike).  All other payload keys stay byte-identical.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from .. import edgar_client
from ..domain.market.identity import resolve_ticker_aliases
from ..edgar_client import (
    _DERIVED_Q4_OFFSET_DAYS,
    _DIVIDEND_ANNUAL_MAX_AGE_DAYS,
    _DIVIDEND_MONTH_MAX_AGE_DAYS,
    _DIVIDEND_SEMI_MAX_AGE_DAYS,
    _DIVIDEND_SOURCE,
    _FY_DAYS,
    _MISSING_QUARTER_GAP_DAYS,
    _MONTH_DAYS,
    _QUARTER_DAYS,
    _SEMI_DAYS,
    _YTD_DAYS,
    _dividend_annual_history,
    _dividend_growth,
    _dividend_valuation,
    _has_contiguous_gaps,
    _has_contiguous_quarters,
    _is_recent_dividend_period,
)
from ..storage import duckdb

DEFAULT_DATA_ROOT = duckdb.DEFAULT_DATA_ROOT

DILUTED_EPS_CONCEPT = "EarningsPerShareDiluted"
BASIC_EPS_CONCEPT = "EarningsPerShareBasic"
SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
DIVIDEND_PER_SHARE_CONCEPT = "CommonStockDividendsPerShareDeclared"

_EPS_CONCEPTS = (DILUTED_EPS_CONCEPT, BASIC_EPS_CONCEPT)
_DIVIDEND_CONCEPTS = (DIVIDEND_PER_SHARE_CONCEPT,)
_METRICS = ("eps", "shares_outstanding", "balance_sheet", "overview", "dividends")


def _today() -> _dt.date:
    return _dt.date.today()


def _validated_as_of(as_of: Optional[str]) -> Optional[_dt.date]:
    """None -> today; otherwise strict YYYY-MM-DD or an error marker."""
    if as_of is None:
        return _today()
    try:
        return _dt.datetime.strptime(str(as_of), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _resolve_entity(ticker: str, as_of: _dt.date, data_root) -> Optional[str]:
    """Resolve a ticker to its entity id through the alias store.

    The alias horizon is end-of-day UTC on the as-of date so an alias
    ingested on the as-of day itself is visible (day-granularity semantics,
    matching the store's ``known_at <= as_of`` facts gate).  Ambiguous or
    unresolved tickers resolve to None: no store path, never a guess.
    """
    horizon = _dt.datetime.combine(as_of, _dt.time.max, tzinfo=_dt.timezone.utc)
    aliases = duckdb.ticker_alias_candidates(ticker, horizon, data_root=data_root)
    if not aliases:
        return None
    resolution = resolve_ticker_aliases(ticker, aliases, as_of=horizon)
    if not resolution.resolved:
        return None
    return resolution.entity_id


def _store_rows(entity_id: str, concepts: tuple[str, ...], as_of: _dt.date, data_root) -> list[dict]:
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    placeholders = ",".join("?" for _ in concepts)
    return duckdb.query(
        "SELECT concept, value, period_start, period_end, fiscal_year, "
        "fiscal_period, filed_at, accession, known_at, source_url "
        "FROM financial_facts "
        f"WHERE entity_id = ? AND concept IN ({placeholders}) AND {clause} "
        "ORDER BY period_end, filed_at, accession",
        params=[entity_id, *concepts, param],
        data_root=data_root,
    )


def _envelope(
    ticker: str,
    metric: str,
    payload: dict[str, Any],
    *,
    data_source: str,
    as_of_date: str,
    requested_as_of: Optional[str] = None,
    row_count: Optional[int] = None,
    returned_count: Optional[int] = None,
    truncated: bool = False,
) -> dict[str, Any]:
    """Wrap a payload in the exact public envelope shape."""
    env: dict[str, Any] = {
        "source": "sec",
        "metric": metric,
        "data_source": data_source,
        "as_of_date": as_of_date,
        "row_count": row_count,
        "returned_count": returned_count if returned_count is not None else row_count,
        "truncated": truncated,
    }
    if requested_as_of is not None and requested_as_of != as_of_date:
        env["requested_as_of"] = requested_as_of
    for key, value in payload.items():
        env["source_label" if key == "source" else key] = value
    return env


# ---------------------------------------------------------------------------
# EPS: store assembly mirroring edgar_client semantics
# ---------------------------------------------------------------------------


def _duration_days(row: dict) -> Optional[int]:
    start, end = row.get("period_start"), row.get("period_end")
    if not start or not end:
        return None
    try:
        start_date = _dt.date.fromisoformat(str(start)[:10])
        end_date = _dt.date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    return (end_date - start_date).days


def _duration_rows(rows: list[dict], concept: str, day_range: tuple[int, int]) -> list[dict]:
    """Facts of a given duration for a concept, restatements resolved
    by the true latest filed_at (accession DESC tie-break) — replacing
    edgar_client._dedup_latest's fiscal-year proxy — newest period first."""
    q = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if duration is not None and day_range[0] <= duration <= day_range[1]:
            q.append(row)
    by_end: dict[str, dict] = {}
    for row in q:
        key = str(row["period_end"])
        prev = by_end.get(key)
        if prev is None or (str(row["filed_at"] or ""), str(row["accession"] or "")) > (
            str(prev["filed_at"] or ""), str(prev["accession"] or "")
        ):
            by_end[key] = row
    return sorted(by_end.values(), key=lambda r: str(r["period_end"]))


def _derive_q4_from_facts(rows: list[dict], concept: str, fy_end: _dt.date) -> Optional[dict]:
    """Q4 = FY_total - YTD_through_Q3 for the fiscal year ending fy_end."""
    fy = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if (
            duration is not None
            and _FY_DAYS[0] <= duration <= _FY_DAYS[1]
            and str(row["period_end"]) == fy_end.isoformat()
        ):
            fy.append(row)
    if not fy:
        return None
    latest_fy = max(fy, key=lambda r: (str(r.get("filed_at") or ""), str(r.get("accession") or "")))
    fy_total = float(latest_fy["value"])
    ytd = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if duration is None or not (_YTD_DAYS[0] <= duration <= _YTD_DAYS[1]):
            continue
        try:
            row_end = _dt.date.fromisoformat(str(row["period_end"])[:10])
        except ValueError:
            continue
        if fy_end - _dt.timedelta(days=_MISSING_QUARTER_GAP_DAYS) <= row_end < fy_end:
            ytd.append(row)
    if not ytd:
        return None
    ytd_q3 = float(sorted(ytd, key=lambda r: str(r["period_end"]))[-1]["value"])
    derived = dict(latest_fy)
    derived["value"] = fy_total - ytd_q3
    derived["period_end"] = fy_end.isoformat()
    derived["fiscal_period"] = "Q4"
    return derived


def _quarters_with_derived_q4(quarter_rows: list[dict], all_rows: list[dict], concept: str) -> list[dict]:
    """Last 4 quarterly facts, deriving a missing final quarter (NVDA reports
    Q4 diluted EPS only as a full-year fact) — mirrors
    edgar_client._quarters_with_derived_q4."""
    quarter = sorted(quarter_rows, key=lambda r: str(r["period_end"]))
    if len(quarter) < 2:
        return quarter[-4:]
    ends = [_dt.date.fromisoformat(str(row["period_end"])[:10]) for row in quarter]
    last_gap = (ends[-1] - ends[-2]).days
    if last_gap > _MISSING_QUARTER_GAP_DAYS:
        missing_end = ends[-1] - _dt.timedelta(days=_DERIVED_Q4_OFFSET_DAYS)
        derived = _derive_q4_from_facts(all_rows, concept, missing_end)
        if derived is not None:
            quarter = sorted([*quarter, derived], key=lambda r: str(r["period_end"]))
    return quarter[-4:]


def _assemble_eps_payload(ticker: str, rows: list[dict]) -> Optional[dict]:
    """Deterministic store assembly over feed rows (pure; no storage)."""
    recent_diluted = _quarters_with_derived_q4(
        _duration_rows(rows, DILUTED_EPS_CONCEPT, _QUARTER_DAYS), rows, DILUTED_EPS_CONCEPT
    )
    if not recent_diluted:
        return None
    recent_basic = None
    basic_quarters = _duration_rows(rows, BASIC_EPS_CONCEPT, _QUARTER_DAYS)
    if basic_quarters:
        recent_basic = _quarters_with_derived_q4(basic_quarters, rows, BASIC_EPS_CONCEPT)

    quarterly_eps: list[dict] = []
    for r in recent_diluted:
        entry: dict[str, Any] = {
            "fiscal_year": str(r["fiscal_year"]) if r.get("fiscal_year") is not None else "",
            "fiscal_period": str(r.get("fiscal_period") or ""),
            "eps_diluted": round(float(r["value"]), 2),
            "period_end": str(r["period_end"]),
        }
        if recent_basic:
            matching = next(
                (b for b in recent_basic if str(b["period_end"]) == entry["period_end"]), None
            )
            if matching is not None:
                entry["eps_basic"] = round(float(matching["value"]), 2)
        quarterly_eps.append(entry)

    result: dict[str, Any] = {
        "ticker": ticker,
        "quarterly_eps": quarterly_eps,
        "source": "SEC EDGAR company facts (Basic & Diluted EPS)",
    }
    if len(recent_diluted) == 4:
        result["ttm_eps_diluted"] = round(sum(float(r["value"]) for r in recent_diluted), 2)
    if recent_basic is not None and len(recent_basic) == 4:
        result["ttm_eps_basic"] = round(sum(float(r["value"]) for r in recent_basic), 2)
    return result


def _assemble_dividend_payload(ticker: str, rows: list[dict], as_of: _dt.date) -> Optional[dict]:
    """Deterministic store assembly over feed rows (pure; no storage)."""
    if not any(r.get("concept") == DIVIDEND_PER_SHARE_CONCEPT for r in rows):
        return None
    quarters = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, _QUARTER_DAYS)
    recent = _quarters_with_derived_q4(quarters, rows, DIVIDEND_PER_SHARE_CONCEPT)
    if len(recent) == 4 and _has_contiguous_quarters([r["period_end"] for r in recent]) and _is_recent_dividend_period(recent[-1]["period_end"], as_of):
        ttm = round(sum(float(r["value"]) for r in recent), 4)
    else:
        ttm = None
        for days, count, cap in ((_MONTH_DAYS, 12, _DIVIDEND_MONTH_MAX_AGE_DAYS), (_SEMI_DAYS, 2, _DIVIDEND_SEMI_MAX_AGE_DAYS)):
            tier = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, days)[-count:]
            if len(tier) == count and _has_contiguous_gaps([r["period_end"] for r in tier], days, count) and _is_recent_dividend_period(tier[-1]["period_end"], as_of, cap):
                ttm = round(sum(float(r["value"]) for r in tier), 4)
                break
        else:
            fy_tier = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, _FY_DAYS)
            if fy_tier and _is_recent_dividend_period(fy_tier[-1]["period_end"], as_of, _DIVIDEND_ANNUAL_MAX_AGE_DAYS):
                ttm = round(float(fy_tier[-1]["value"]), 4)
    fy_rows = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, _FY_DAYS)
    history, annual = _dividend_annual_history(fy_rows)
    return {
        "ticker": ticker,
        "dividend_status": "paying" if ttm is not None else "unknown",
        "ttm_dividend_per_share": ttm,
        **_dividend_growth(annual),
        "annual_history": history,
        "source": _DIVIDEND_SOURCE,
    }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def get_fundamentals(ticker: str, metric: str, as_of: Optional[str] = None) -> dict:
    """Store-first fundamentals with a truthful data_source envelope."""
    requested = _validated_as_of(as_of)
    if requested is None:
        return {
            "error": "as_of must be a date in YYYY-MM-DD format",
            "error_type": "invalid_tool_arguments",
        }
    if metric == "shares_float":
        metric = "shares_outstanding"
    if metric == "eps":
        return _eps_fundamental(ticker, requested)
    if metric == "dividends":
        return _dividend_fundamental(ticker, requested)
    if metric == "shares_outstanding":
        return _shares_outstanding_fundamental(ticker, requested)
    if metric in ("balance_sheet", "overview"):
        return _live_only_fundamental(ticker, metric, requested)
    return {"error": f"Unknown metric '{metric}'", "error_type": "invalid_tool_arguments"}


def _dividend_fundamental(ticker: str, requested: _dt.date) -> dict:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    store_rows = _store_rows(entity_id, _DIVIDEND_CONCEPTS, requested, data_root) if entity_id else []
    payload = _assemble_dividend_payload(ticker, store_rows, requested) if store_rows else None
    current = requested == _today()
    if payload is not None:
        payload = {**payload, **_dividend_valuation(ticker, payload.get("ttm_dividend_per_share"), include_price=current)}
        return _envelope(
            ticker, "dividends", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=len(payload["annual_history"]),
        )
    payload = edgar_client.get_fundamentals(ticker, "dividends", include_dividend_price=current)
    if "error" in payload:
        return payload
    return _envelope(
        ticker, "dividends", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=len(payload.get("annual_history") or []),
    )


def _eps_fundamental(ticker: str, requested: _dt.date) -> dict:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    store_rows = _store_rows(entity_id, _EPS_CONCEPTS, requested, data_root) if entity_id else []
    payload = _assemble_eps_payload(ticker, store_rows) if store_rows else None
    if payload is not None:
        return _envelope(
            ticker, "eps", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=len(payload["quarterly_eps"]),
        )
    payload = edgar_client.get_fundamentals(ticker, "eps")
    if "error" in payload:
        return payload
    return _envelope(
        ticker, "eps", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=len(payload.get("quarterly_eps") or []),
    )


def _shares_outstanding_fundamental(ticker: str, requested: _dt.date) -> dict:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    row: Optional[dict] = None
    if entity_id:
        clause, param = duckdb.as_of_clause(requested.isoformat())
        rows = duckdb.query(
            "SELECT value, period_end, filed_at, accession, known_at, source_url "
            "FROM financial_facts "
            f"WHERE entity_id = ? AND concept = ? AND {clause} "
            "ORDER BY period_end DESC, filed_at DESC, accession DESC LIMIT 1",
            params=[entity_id, SHARES_OUTSTANDING_CONCEPT, param],
            data_root=data_root,
        )
        if rows:
            row = rows[0]
    if row is not None:
        payload = {
            "ticker": ticker,
            "shares_outstanding": float(row["value"]),
            "as_of": str(row["period_end"]),
            "source": "SEC EDGAR company facts",
            "note": "SEC-reported shares outstanding, not public float",
            "filed_at": str(row["filed_at"] or ""),
            "accession": row.get("accession"),
            "source_url": row.get("source_url"),
            "known_at": str(row["known_at"] or ""),
        }
        return _envelope(
            ticker, "shares_outstanding", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=1,
        )
    payload = edgar_client.get_fundamentals(ticker, "shares_outstanding")
    if "error" in payload:
        return payload
    return _envelope(
        ticker, "shares_outstanding", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=1,
    )


def _live_only_fundamental(ticker: str, metric: str, requested: _dt.date) -> dict:
    """balance_sheet/overview are live-only: as_of is accepted but does not
    filter the data; the envelope echoes the requested as-of."""
    payload = edgar_client.get_fundamentals(ticker, metric)
    if "error" in payload:
        return payload
    row_count = 1
    if metric == "balance_sheet":
        sheet = payload.get("balance_sheet")
        if isinstance(sheet, dict):
            row_count = len(sheet)
    return _envelope(
        ticker, metric, payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=row_count,
    )


def get_xbrl_facts(ticker: str, concept: str) -> dict:
    """Always-live XBRL fact search, enveloped (label key source_label)."""
    payload = edgar_client.get_xbrl_facts(ticker, concept)
    if "error" in payload:
        return payload
    matching = payload.get("matching_concepts") or []
    count = payload.get("count") or len(matching)
    return _envelope(
        ticker, "concept", payload,
        data_source="live", as_of_date=_today().isoformat(),
        row_count=count, returned_count=len(matching),
        truncated=count > len(matching),
    )
