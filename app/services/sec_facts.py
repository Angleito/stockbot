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
    _DIVIDEND_SOURCE,
    _FY_DAYS,
    _MISSING_QUARTER_GAP_DAYS,
    _QUARTER_DAYS,
    _YTD_DAYS,
    _dividend_annual_history,
    _dividend_growth,
    _dividend_valuation,
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
# Dividend events: past / present / future-declared (no projections)
# ---------------------------------------------------------------------------


def _store_dividend_events(entity_id: str, as_of: _dt.date, data_root) -> list[dict]:
    """PIT-gated dividend events, deduped by id keeping max known_at.

    Amended filings produce different ids (amount is part of the id) so both
    revisions stay visible; duplicate 8-K/10-Q disclosures share an id and
    collapse here.
    """
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    rows = duckdb.query(
        "SELECT dividend_event_id, entity_id, security_id, ticker, amount_per_share, "
        "currency, dividend_type, declaration_date, record_date, payment_date, "
        "ex_dividend_date, ex_dividend_date_source, status, source_form, accession, "
        "filed_at, known_at, source_url, source_concept, source_type, evidence_excerpt, "
        "content_hash, parser_version "
        "FROM dividend_events "
        f"WHERE entity_id = ? AND {clause} "
        "ORDER BY known_at, dividend_event_id",
        params=[entity_id, param],
        data_root=data_root,
    )
    by_id: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("dividend_event_id"))
        prev = by_id.get(key)
        if prev is None or str(row.get("known_at") or "") >= str(prev.get("known_at") or ""):
            by_id[key] = row
    return list(by_id.values())


def _classify_dividend_event(row: dict, as_of: _dt.date) -> str:
    """payment_date < as_of -> paid; >= as_of -> upcoming; null -> unknown."""
    pay = row.get("payment_date")
    if not pay:
        return "unknown"
    return "paid" if str(pay)[:10] < as_of.isoformat() else "upcoming"


def _extreme_event(cands: list[dict], *, earliest: bool) -> Optional[dict]:
    """Edge payment date wins; same-date revisions prefer latest known_at."""
    edge = (min if earliest else max)(str(r["payment_date"]) for r in cands)
    tied = [r for r in cands if str(r["payment_date"]) == edge]
    return max(tied, key=lambda r: str(r.get("known_at") or ""))


def _dividend_event_payload(events: list[dict], as_of: _dt.date) -> dict[str, Any]:
    """last/next/past/coverage over classified events (pure; no storage)."""
    upcoming = [
        r for r in events
        if _classify_dividend_event(r, as_of) == "upcoming"
        and r.get("amount_per_share") is not None and r.get("payment_date")
    ]
    paid = [
        r for r in events
        if _classify_dividend_event(r, as_of) == "paid"
        and r.get("amount_per_share") is not None and r.get("payment_date")
    ]
    nxt = _extreme_event(upcoming, earliest=True) if upcoming else None
    last = _extreme_event(paid, earliest=False) if paid else None
    dated = sorted(
        (r for r in events if r.get("payment_date")),
        key=lambda r: str(r["payment_date"]), reverse=True,
    )
    undated = [r for r in events if not r.get("payment_date")]
    past = [
        {
            "amount_per_share": r.get("amount_per_share"),
            "payment_date": r.get("payment_date"),
            "declaration_date": r.get("declaration_date"),
            "record_date": r.get("record_date"),
            "dividend_type": r.get("dividend_type"),
            "status": _classify_dividend_event(r, as_of),
            "accession": r.get("accession"),
            "source_url": r.get("source_url"),
        }
        for r in (dated + undated)[:12]
    ]
    return {
        "last_dividend": (
            {"amount_per_share": last["amount_per_share"],
             "payment_date": last["payment_date"], "type": last.get("dividend_type")}
            if last is not None else None
        ),
        "next_declared_dividend": (
            {
                "amount_per_share": nxt["amount_per_share"],
                "declaration_date": nxt.get("declaration_date"),
                "record_date": nxt.get("record_date"),
                "payment_date": nxt["payment_date"],
                "status": "upcoming",
                "source_url": nxt.get("source_url"),
                "accession": nxt.get("accession"),
            }
            if nxt is not None else None
        ),
        "past_events": past,
        "events_coverage": (
            "structured_and_text"
            if any(r.get("source_type") == "structured_xbrl" for r in events)
            else "no_structured_events"
        ),
    }


# ---------------------------------------------------------------------------
# Dividend safety: FCF-based coverage on SEC inputs (no new providers)
# ---------------------------------------------------------------------------

_SAFETY_METHODOLOGY = "common-stock EPS/FCF basis; not AFFO/FFO"

_OCF_CONCEPT = "OperatingCashFlow"
_CAPEX_CONCEPT = "CapEx"
_DIV_PAID_CONCEPT = "DividendsPaid"
_CASH_CONCEPT = "CashAndCashEquivalents"
_DEBT_CONCEPT = "LongTermDebt"
_NET_INCOME_CONCEPT = "NetIncomeLoss"

_SAFETY_CONCEPTS = (
    _OCF_CONCEPT, _CAPEX_CONCEPT, _DIV_PAID_CONCEPT, _CASH_CONCEPT,
    _DEBT_CONCEPT, _NET_INCOME_CONCEPT,
    DILUTED_EPS_CONCEPT, BASIC_EPS_CONCEPT,
)


def _ttm_cash_total(rows: list[dict], concept: str, *, outflow: bool = False) -> Optional[float]:
    """Trailing-4-quarter total via the shared quarterly + derived-Q4 machinery.

    Outflow concepts (CapEx, dividends paid) are cash-flow debits, usually
    filed as negative values; coverage math needs their magnitude.
    """
    quarters = _quarters_with_derived_q4(
        _duration_rows(rows, concept, _QUARTER_DAYS), rows, concept
    )
    if len(quarters) != 4 or not _has_contiguous_quarters([r["period_end"] for r in quarters]):
        return None
    values = [abs(float(r["value"])) if outflow else float(r["value"]) for r in quarters]
    return round(sum(values), 2)


def _fy_annual_totals(rows: list[dict], concept: str, *, outflow: bool = False) -> dict[int, float]:
    """FY-duration facts keyed by calendar year of period_end."""
    annual: dict[int, float] = {}
    for r in _duration_rows(rows, concept, _FY_DAYS):
        try:
            year = _dt.date.fromisoformat(str(r["period_end"])[:10]).year
            val = abs(float(r["value"])) if outflow else float(r["value"])
        except (TypeError, ValueError):
            continue
        annual[year] = round(val, 2)
    return annual


def _latest_concept_value(rows: list[dict], concept: str) -> Optional[dict]:
    """Latest row for a concept by (period_end, filed_at, accession)."""
    cands = [r for r in rows if r.get("concept") == concept and r.get("period_end")]
    if not cands:
        return None
    return max(cands, key=lambda r: (str(r["period_end"]), str(r.get("filed_at") or ""),
                                     str(r.get("accession") or "")))


def _debt_up_yoy(rows: list[dict]) -> Optional[bool]:
    """Latest LongTermDebt vs the most recent row at least ~10 months older."""
    latest = _latest_concept_value(rows, _DEBT_CONCEPT)
    if latest is None:
        return None
    try:
        end = _dt.date.fromisoformat(str(latest["period_end"])[:10])
        now_val = float(latest["value"])
    except (TypeError, ValueError):
        return None
    cutoff = (end - _dt.timedelta(days=300)).isoformat()
    older = [r for r in rows
             if r.get("concept") == _DEBT_CONCEPT and str(r.get("period_end") or "") <= cutoff]
    if not older:
        return None
    base = _latest_concept_value(older, _DEBT_CONCEPT)
    try:
        then_val = float(base["value"])  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    return now_val > then_val


def _assemble_dividend_safety(
    rows: list[dict],
    dividend: dict,
    *,
    ttm_eps_diluted: Optional[float] = None,
    ttm_yield: Optional[float] = None,
) -> dict[str, Any]:
    """FCF/EPS safety on SEC inputs only (pure; no storage).

    Absent concepts yield nulls with reasons, never zero-filled or borrowed.
    """
    ttm_dps = dividend.get("ttm_dividend_per_share")
    ttm_ocf = _ttm_cash_total(rows, _OCF_CONCEPT)
    ttm_capx = _ttm_cash_total(rows, _CAPEX_CONCEPT, outflow=True)
    ttm_div_paid = _ttm_cash_total(rows, _DIV_PAID_CONCEPT, outflow=True)
    ttm_fcf = round(ttm_ocf - ttm_capx, 2) if ttm_ocf is not None and ttm_capx is not None else None

    safety: dict[str, Any] = {
        "ttm_fcf": ttm_fcf,
        "ttm_dividends_paid": ttm_div_paid,
        "methodology": _SAFETY_METHODOLOGY,
    }

    def _set(key: str, value: Any, reason: Optional[str] = None) -> None:
        safety[key] = value
        if value is None and reason:
            safety[f"{key}_reason"] = reason

    flags: list[dict] = []

    def _flag(flag: str, status: Optional[bool], basis: str) -> None:
        flags.append({"flag": flag, "status": status, "basis": basis})

    if ttm_eps_diluted is not None and ttm_eps_diluted <= 0:
        _set("earnings_payout_ratio", None, f"ttm_eps_diluted {ttm_eps_diluted} <= 0")
        _flag("negative_eps", True, f"ttm_eps_diluted {ttm_eps_diluted} <= 0; payout meaningless")
    elif ttm_dps is None or ttm_eps_diluted is None:
        _set("earnings_payout_ratio", None, "missing ttm dps or diluted eps")
    else:
        _set("earnings_payout_ratio", round(float(ttm_dps) / float(ttm_eps_diluted), 4))

    fcf_nonpositive = ttm_fcf is not None and ttm_fcf <= 0
    if fcf_nonpositive:
        _set("fcf_payout_ratio", None, f"ttm_fcf {ttm_fcf} <= 0")
        _set("fcf_coverage", None, f"ttm_fcf {ttm_fcf} <= 0")
        _flag("negative_or_zero_fcf", True, f"ttm_fcf {ttm_fcf} <= 0; payout/coverage meaningless")
    elif ttm_div_paid is None or ttm_fcf is None:
        _set("fcf_payout_ratio", None, "missing ttm dividends-paid or fcf")
        _set("fcf_coverage", None, "missing ttm dividends-paid or fcf")
    elif ttm_div_paid == 0:
        _set("fcf_payout_ratio", 0.0)
        _set("fcf_coverage", None, "zero ttm dividends-paid")
        _flag("zero_dividend", True, "ttm_dividends_paid is zero; coverage undefined")
    else:
        _set("fcf_payout_ratio", round(ttm_div_paid / ttm_fcf, 4))
        _set("fcf_coverage", round(ttm_fcf / ttm_div_paid, 4))

    cash_row = _latest_concept_value(rows, _CASH_CONCEPT)
    try:
        cash = float(cash_row["value"]) if cash_row is not None else None
    except (TypeError, ValueError):
        cash = None
    if cash is None or ttm_div_paid is None:
        _set("cash_to_annual_dividend", None, "missing cash balance or ttm dividends-paid")
    elif ttm_div_paid == 0:
        _set("cash_to_annual_dividend", None, "zero ttm dividends-paid")
    else:
        _set("cash_to_annual_dividend", round(cash / ttm_div_paid, 4))

    _set("interest_coverage", None, "operating-income/interest-expense concepts not in store")

    debt_up = _debt_up_yoy(rows)
    safety["debt_up_yoy"] = debt_up
    if debt_up is None:
        safety["debt_up_yoy_reason"] = "missing current or year-ago long-term debt"

    ocf_ann = _fy_annual_totals(rows, _OCF_CONCEPT)
    capx_ann = _fy_annual_totals(rows, _CAPEX_CONCEPT, outflow=True)
    fcf_ann = {y: round(ocf_ann[y] - capx_ann[y], 2) for y in ocf_ann if y in capx_ann}
    paid_ann = _fy_annual_totals(rows, _DIV_PAID_CONCEPT, outflow=True)
    div_cagr = dividend.get("growth_5y_cagr")
    fcf_cagr = _dividend_growth(fcf_ann)["growth_5y_cagr"]
    if div_cagr is None or fcf_cagr is None:
        verdict = "insufficient_data"
    elif div_cagr - fcf_cagr > 0.02:
        verdict = "payout_expanding"
    else:
        verdict = "runway_supported"
    safety["dividend_vs_fcf_growth_5y"] = {
        "dividend_cagr": div_cagr, "fcf_cagr": fcf_cagr, "verdict": verdict,
    }

    if ttm_yield is None:
        _flag("high_absolute_yield", None, "no current ttm yield (historical as_of or missing price)")
    else:
        _flag("high_absolute_yield", ttm_yield >= 0.06, f"ttm_dividend_yield {ttm_yield}")
    if fcf_ann and max(fcf_ann) - 1 in fcf_ann:
        latest_y = max(fcf_ann)
        _flag("fcf_declined_yoy", fcf_ann[latest_y] < fcf_ann[latest_y - 1],
              f"annual fcf {latest_y - 1} {fcf_ann[latest_y - 1]} -> {latest_y} {fcf_ann[latest_y]}")
    else:
        _flag("fcf_declined_yoy", None, "missing consecutive annual fcf totals")
    if fcf_ann and paid_ann and max(fcf_ann) - 1 in fcf_ann and max(fcf_ann) - 1 in paid_ann:
        latest_y = max(fcf_ann)
        prior_y = latest_y - 1
        if fcf_ann[latest_y] > 0 and fcf_ann[prior_y] > 0:
            cur, prev = paid_ann[latest_y] / fcf_ann[latest_y], paid_ann[prior_y] / fcf_ann[prior_y]
            _flag("fcf_payout_expanded", cur - prev > 0.10,
                  f"annual fcf payout {prev:.4f} ({prior_y}) -> {cur:.4f} ({latest_y})")
        else:
            _flag("fcf_payout_expanded", None, "non-positive annual fcf base")
    else:
        _flag("fcf_payout_expanded", None, "missing consecutive annual payout bases")
    eps_ann = _fy_annual_totals(rows, DILUTED_EPS_CONCEPT)
    eps_basis_name = "diluted eps"
    if not eps_ann:
        eps_ann = _fy_annual_totals(rows, _NET_INCOME_CONCEPT)
        eps_basis_name = "net income"
    if eps_ann and max(eps_ann) - 1 in eps_ann:
        latest_y = max(eps_ann)
        _flag("eps_declined_yoy", eps_ann[latest_y] < eps_ann[latest_y - 1],
              f"annual {eps_basis_name} {latest_y - 1} {eps_ann[latest_y - 1]} -> "
              f"{latest_y} {eps_ann[latest_y]}")
    else:
        _flag("eps_declined_yoy", None, "missing consecutive annual earnings totals")
    if debt_up is None:
        _flag("leverage_rising", None, "missing current or year-ago long-term debt")
    else:
        _flag("leverage_rising", debt_up, f"long-term debt {'up' if debt_up else 'not up'} year-over-year")
    g1, g5 = dividend.get("growth_1y"), dividend.get("growth_5y_cagr")
    if g1 is None or g5 is None:
        _flag("growth_decelerating", None, "missing growth_1y or growth_5y_cagr")
    else:
        _flag("growth_decelerating", g1 < g5, f"growth_1y {g1} vs growth_5y_cagr {g5}")

    safety["risk_flags"] = flags
    return safety


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
    store_rows = _store_rows(entity_id, _DIVIDEND_CONCEPTS + _SAFETY_CONCEPTS, requested, data_root) if entity_id else []
    payload = _assemble_dividend_payload(ticker, store_rows, requested) if store_rows else None
    current = requested == _today()
    if payload is not None:
        events = _store_dividend_events(entity_id, requested, data_root) if entity_id else []
        valuation = _dividend_valuation(ticker, payload.get("ttm_dividend_per_share"), include_price=current)
        eps_payload = _assemble_eps_payload(ticker, store_rows)
        safety = _assemble_dividend_safety(
            store_rows, payload,
            ttm_eps_diluted=(eps_payload or {}).get("ttm_eps_diluted"),
            ttm_yield=valuation.get("ttm_dividend_yield"),
        )
        payload = {**payload, **_dividend_event_payload(events, requested),
                   **valuation, "safety": safety}
        return _envelope(
            ticker, "dividends", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=len(payload["annual_history"]),
        )
    payload = edgar_client.get_fundamentals(ticker, "dividends", include_dividend_price=current)
    if "error" in payload:
        return payload
    payload = {**payload, **_dividend_event_payload([], requested), "safety": None}
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
