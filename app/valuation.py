"""Valuation metrics computed from live price, SEC EPS, consensus estimates,
and filing-disclosed obligations.

The user-visible numbers are price-anchored to the latest quote at query
time (short TTL), never a stale cached price. Three EPS figures are
reported, never conflated:

* ``consensus_forward_eps`` — Yahoo sell-side consensus (FY current/next).
* ``adjusted_forward_eps`` — consensus minus contractual (non-cancelable,
  firm) obligations annualized per the filing's own schedule. This is the
  "adjusted" number: only absolute, contractually obligated items go in.
* ``forward_eps_incl_contingent`` — consensus minus ALL disclosed
  obligations (contractual + contingent). A stress scenario, explicitly
  NOT called "adjusted".

Buybacks, dividends, and unquantifiable indemnities are excluded from both
adjusted numbers and reported separately.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from . import analyst_client
from . import cache
from . import edgar_client
from . import obligations
from .obligations import DEFAULT_TRIGGERED_TYPES
from .services import sec_facts

logger = logging.getLogger(__name__)

PRICE_CACHE_TTL_SECONDS = 300  # 5 minutes: numbers must be as-of-now.
VALUATION_CACHE_TTL_SECONDS = 900


def _no_data(ticker: str, what: str) -> dict:
    return {"error": f"No valuation data for {ticker}: {what}"}


def get_live_price(ticker: str) -> Optional[float]:
    """Latest tradeable quote as of now (5-minute TTL)."""
    key = f"live_price:{ticker}"
    hit = cache.get(key, ttl=PRICE_CACHE_TTL_SECONDS)
    if hit is not None:
        return hit.get("price")
    estimates = analyst_client.get_analyst_estimates(ticker)
    price = (estimates.get("quote") or {}).get("price")
    if price is None:
        return None
    cache.set(key, {"price": price})
    return price


def _annualized(amount_billions: float, years: float) -> float:
    if years <= 0:
        return 0.0
    return amount_billions / years


def _obligation_annual_impact(obligations_rows: list[dict], years: int) -> dict:
    """Per-kind annualized $B impact split by EPS treatment.

    Three buckets, never conflated:

    * ``contractual`` — non-cancelable/firm obligations (pure expense:
      leases). These reduce EPS; they are the only items folded into
      "adjusted forward EPS".
    * ``contingent`` — cancellable/reducible/terminable/default-triggered
      PURE EXPENSE obligations (cloud R&D, vendor, guarantees). Shown in
      the stress scenario, never in "adjusted".
    * ``revenue_matched`` — spend that buys inventory/COGS for products
      sold at gross margin (supply commitments). This is NOT a separate
      EPS drag: consensus revenue and COGS already embed it (the spend
      buys inventory sold at a margin; counting it again would
      double-count the cost). Reported separately with implied revenue
      coverage at the company's own gross margin.

    Annualization honors the filing's disclosed payment horizon when
    present: a front-loaded commitment ($95B paid in the remainder of the
    current fiscal year, tail over ~4 years) is annualized accordingly
    rather than spread flat.
    """
    contractual_b = 0.0
    contingent_b = 0.0
    default_triggered_b = 0.0
    revenue_matched_b = 0.0
    per_kind: dict[str, dict] = {}
    impact_by_fy: dict[str, dict[str, float]] = {}
    flat_annual_by_bucket: dict[str, float] = {
        "contractual": 0.0,
        "contingent": 0.0,
        "default_triggered": 0.0,
        "revenue_matched": 0.0,
    }

    def _add_fy(year: str, bucket: str, amount: float) -> None:
        year = str(year or "").strip()
        if not year:
            return
        entry = impact_by_fy.setdefault(
            year, {"contractual": 0.0, "contingent": 0.0, "default_triggered": 0.0, "revenue_matched": 0.0}
        )
        entry[bucket] = entry.get(bucket, 0.0) + float(amount or 0.0)

    for row in obligations_rows:
        if row.get("schedule_component"):
            continue
        amount_b = row.get("amount_billions")
        if not amount_b:
            continue
        kind = row.get("type", "other")
        schedule = row.get("schedule") or []
        total = sum(y.get("amount_billions", 0.0) for y in schedule)
        if schedule and total > 0:
            annual = total / max(1, len(schedule))
        else:
            horizon = row.get("payment_horizon") or {}
            # ponytail: front-loaded remainder (~0.75yr) + tail spread; flat fallback otherwise
            cloud_schedule = horizon.get("schedule") or []
            if cloud_schedule:
                annual = sum(
                    y.get("amount_billions", 0.0) for y in cloud_schedule
                ) / max(1, len(cloud_schedule))
            else:
                near_b = horizon.get("paid_in_remainder_billions")
                if near_b:
                    tail_b = horizon.get("paid_after_remainder_billions", 0.0)
                    tail_years = max(1, years - 1)
                    annual = near_b / 0.75 + tail_b / tail_years
                else:
                    if row.get("status") == "off_balance_sheet" and "lease" in kind:
                        annual = _annualized(amount_b, 10)
                    else:
                        annual = _annualized(amount_b, years)
        is_contractual = row.get("certainty") == "contractual"
        is_revenue_matched = bool(row.get("revenue_matched"))
        is_default_triggered = bool(row.get("default_triggered")) or row.get(
            "type"
        ) in DEFAULT_TRIGGERED_TYPES
        status = row.get("status")
        is_on_balance_sheet = status == "on_balance_sheet"
        per_kind[kind] = {
            "total_billions": round(amount_b, 3),
            "annualized_billions": round(annual, 3),
            "certainty": row.get("certainty"),
            "status": status,
            "revenue_matched": is_revenue_matched,
            "default_triggered": is_default_triggered,
            "payment_horizon": row.get("payment_horizon") or {},
        }
        if is_revenue_matched:
            revenue_matched_b += annual
            bucket = "revenue_matched"
        elif is_on_balance_sheet:
            # Already accrued/expensed (leases, debt, deferred revenue):
            # informational only, never a future EPS drag.
            continue
        elif is_contractual:
            contractual_b += annual
            bucket = "contractual"
        elif is_default_triggered:
            default_triggered_b += annual
            bucket = "default_triggered"
        else:
            contingent_b += annual
            bucket = "contingent"
        if schedule and total > 0:
            for y in schedule:
                _add_fy(str(y.get("fiscal_year") or ""), bucket, y.get("amount_billions", 0.0))
            continue
        horizon = row.get("payment_horizon") or {}
        cloud_schedule = horizon.get("schedule") or []
        if cloud_schedule:
            for y in cloud_schedule:
                _add_fy(str(y.get("fiscal_year") or ""), bucket, y.get("amount_billions", 0.0))
            continue
        near_b = horizon.get("paid_in_remainder_billions")
        remainder_year = str(horizon.get("paid_in_remainder_of_fy") or "").strip()
        if near_b and remainder_year:
            _add_fy(remainder_year, bucket, near_b)
            tail_b = horizon.get("paid_after_remainder_billions", 0.0) or 0.0
            tail_per = tail_b / max(1, years - 1)
            if tail_per:
                try:
                    base = int(remainder_year[:4])
                except ValueError:
                    base = None
                if base is not None:
                    for i in range(1, years):
                        _add_fy(str(base + i), bucket, tail_per)
            continue
        flat_annual_by_bucket[bucket] += annual
    return {
        "contractual_annual_billions": round(contractual_b, 3),
        "contingent_annual_billions": round(contingent_b, 3),
        "default_triggered_annual_billions": round(default_triggered_b, 3),
        "revenue_matched_annual_billions": round(revenue_matched_b, 3),
        "per_kind": per_kind,
        "flat_annual_by_bucket": {k: round(v, 3) for k, v in flat_annual_by_bucket.items()},
        "impact_by_fiscal_year": {
            year: {k: round(v, 3) for k, v in buckets.items()}
            for year, buckets in impact_by_fy.items()
        },
    }


# P/E multiples used for scenario share-price projections. 15x is a deep
# value/bear multiple, 25x a stable-growth tech multiple, 30x a premium
# growth multiple, and 35x a momentum/peak multiple.
PROJECTION_MULTIPLES = (15, 20, 25, 30, 35)


def _projected_prices(eps_by_tier: dict[str, Optional[float]], price: Optional[float]) -> dict:
    """Share price per scenario EPS under a set of assumed P/E multiples.

    projected_price = scenario EPS x assumed P/E multiple. Each cell also
    carries the % change vs the current live price, so a user can see how
    far the stock must fall (or rise) if a scenario's EPS materializes at a
    given multiple.
    """
    tiers: list[dict] = []
    for tier, eps in eps_by_tier.items():
        if eps is None:
            continue
        cells: dict[str, dict] = {}
        for multiple in PROJECTION_MULTIPLES:
            projected = round(eps * multiple, 2)
            pct = round((projected / price - 1) * 100, 1) if price is not None else None
            cells[f"{multiple}x"] = {"price": projected, "pct_change_vs_current": pct}
        tiers.append({"tier": tier, "eps": round(eps, 2), "prices": cells})
    return {
        "assumption": (
            "projected price = scenario EPS x assumed P/E multiple; "
            "pct_change is vs the current live price"
        ),
        "multiples": list(PROJECTION_MULTIPLES),
        "current_price": round(price, 2) if price is not None else None,
        "tiers": tiers,
    }


def _effective_tax_rate(ob_rows: dict) -> Optional[float]:
    """Company's own effective tax rate from its 10-K (income tax expense /
    pre-tax income), falling back to XBRL annual facts, else None. Rates
    outside a sane band (5%-35%) are rejected."""
    candidates: list[Optional[float]] = []

    try:
        filings = edgar_client.get_latest_report(ob_rows.get("ticker", ""), "10-K")
        if filings is not None:
            _filing, doc = filings
            notes = getattr(doc, "notes", None)
            if notes is not None:
                for note in notes.search("tax")[:3]:
                    md = note.to_markdown()
                    match = re.search(
                        r"(?:income tax expense|provision for income taxes)"
                        r"[^|]{0,80}\|\s*\$?([\d,]+)",
                        md, re.I,
                    )
                    pre = re.search(
                        r"income before income tax(?:es)?[^|]{0,80}\|\s*\$?([\d,]+)",
                        md, re.I,
                    )
                    if match and pre:
                        tax = float(match.group(1).replace(",", ""))
                        pretax = float(pre.group(1).replace(",", ""))
                        if pretax > 0:
                            candidates.append(tax / pretax)
    except Exception:
        pass

    try:
        df = edgar_client.get_company(ob_rows.get("ticker", "")).get_facts().to_dataframe()
        tax = df[
            df["concept"].str.contains("IncomeTaxExpenseBenefit", case=False)
            & (df["fiscal_period"] == "FY")
        ].sort_values("period_end")
        pre = df[
            df["concept"].str.contains(
                "IncomeLossFromContinuingOperationsBeforeIncomeTaxes", case=False
            )
            & (df["fiscal_period"] == "FY")
        ].sort_values("period_end")
        if not tax.empty and not pre.empty:
            tax_v = float(tax.iloc[-1]["value"])
            pre_v = float(pre.iloc[-1]["value"])
            if pre_v != 0:
                candidates.append(abs(tax_v / pre_v))
    except Exception:
        pass

    for rate in candidates:
        if rate is not None and 0.05 <= rate <= 0.35:
            return round(rate, 3)
    return None


def _revenue_matched_margin(ticker: str) -> tuple[float | None, str]:
    """Company's own gross margin from latest FY XBRL facts (GrossProfit /
    Revenue), else None with a reason (never an invented default).

    Mirrors _effective_tax_rate: same company-facts frame, same defensive
    pattern (any failure -> None, never an exception). The source tag
    ("company_facts" vs "unavailable: ...") rides along so coverage can
    caveat it.
    """
    try:
        df = edgar_client.get_company(ticker).get_facts().to_dataframe()
        gp = df[
            df["concept"].str.fullmatch(r"(us-gaap:)?GrossProfit", case=False)
            & (df["fiscal_period"] == "FY")
        ].sort_values("period_end")
        rev = df.iloc[0:0]
        for concept in (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ):
            hit = df[
                df["concept"].str.fullmatch(rf"(us-gaap:)?{concept}", case=False)
                & (df["fiscal_period"] == "FY")
            ].sort_values("period_end")
            if not hit.empty:
                rev = hit
                break
        if not gp.empty and not rev.empty:
            gross = float(gp.iloc[-1]["value"])
            revenue = float(rev.iloc[-1]["value"])
            if revenue:
                margin = gross / revenue
                if 0.05 <= margin <= 0.95:
                    return round(margin, 3), "company_facts"
    except Exception:
        pass
    return None, "unavailable: gross margin fact missing"


def _obligation_eps_scenarios(
    ob_rows: dict, shares_out: Optional[int], tax_rate: Optional[float]
) -> dict:
    """Translate disclosed obligations into after-tax EPS-impact scenarios.

    Scenario math (Burry-style): each obligation's exposure is converted to
    an after-tax EPS hit using the company's own effective tax rate and
    diluted shares. One-time items (write-downs, settlements, guarantee
    calls) are shown as one-time hits; recurring items are annualized.
    No invented inputs: an unknown tax rate yields after_tax None, unknown
    shares yield eps_impact None, each with a reason (never 0.15/1-share).
    """
    rows: list[dict] = []
    snapshot = ob_rows.get("current_snapshot", ob_rows.get("obligations", [])) or []

    def _hit(name, pretax_billions, one_time, note=""):
        if tax_rate is None:
            rows.append(
                {
                    "scenario": name,
                    "pretax_billions": round(pretax_billions, 3),
                    "after_tax_billions": None,
                    "eps_impact": None,
                    "one_time": one_time,
                    "tax_rate": None,
                    "reason": "effective tax rate unavailable",
                    "note": note,
                }
            )
            return
        after_tax = pretax_billions * (1 - tax_rate)
        if not shares_out:
            rows.append(
                {
                    "scenario": name,
                    "pretax_billions": round(pretax_billions, 3),
                    "after_tax_billions": round(after_tax, 3),
                    "eps_impact": None,
                    "one_time": one_time,
                    "tax_rate": round(tax_rate, 3),
                    "reason": "diluted shares unavailable",
                    "note": note,
                }
            )
            return
        eps = after_tax / (shares_out / 1e9)
        rows.append(
            {
                "scenario": name,
                "pretax_billions": round(pretax_billions, 3),
                "after_tax_billions": round(after_tax, 3),
                "eps_impact": round(-eps, 3),
                "one_time": one_time,
                "tax_rate": round(tax_rate, 3),
                "reason": None,
                "note": note,
            }
        )

    purchase = None
    for row in snapshot:
        if row.get("type") in ("purchase_commitments", "supply_commitments"):
            purchase = row.get("amount_billions")
            break
    if purchase:
        for pct in (0.05, 0.10, 0.20):
            _hit(
                f"purchase_commitments_write_down_{int(pct*100)}pct",
                purchase * pct,
                True,
                f"{int(pct*100)}% of ${purchase}B purchase commitments written down",
            )

    total_commitments = sum(
        row.get("amount_billions") or 0
        for row in snapshot
        if row.get("status") in ("future_cash_obligation", "off_balance_sheet")
        and row.get("type") != "supply_commitments"
    )
    if total_commitments:
        _hit(
            "all_future_commitments_annualized",
            total_commitments / 6.0,
            False,
            "disclosed future-cash and off-balance-sheet commitments spread over 6 years",
        )

    for row in snapshot:
        if row.get("default_triggered"):
            _hit(
                f"{row.get('type')}_call",
                row.get("amount_billions") or 0,
                True,
                "default-triggered guarantee called",
            )
        elif row.get("type") == "unrecognized_tax_benefits":
            _hit(
                "tax_settlement",
                row.get("amount_billions") or 0,
                True,
                "full adverse tax settlement",
            )

    return {
        "assumption": (
            "after-tax EPS impact = pretax exposure x (1 - effective tax "
            f"rate {round(tax_rate, 3) if tax_rate is not None else 'unavailable; no rate used'})"
            " / diluted shares; one-time items are "
            "shown per event, recurring items annualized"
        ),
        "effective_tax_rate": round(tax_rate, 3) if tax_rate is not None else None,
        "scenarios": rows,
    }


def get_valuation_metrics(ticker: str) -> dict:
    """Price-anchored valuation with obligation-aware forward EPS (cached 15m)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    key = f"valuation:{ticker}"
    hit = cache.get(key, ttl=VALUATION_CACHE_TTL_SECONDS)
    if hit is not None:
        return hit

    price = get_live_price(ticker)
    price_gap = (
        f"No live price for {ticker}: price-anchored multiples (trailing "
        "P/E, forward P/E) and projected-price moves vs current are "
        "unavailable; EPS and obligation figures below carry no price, "
        "and no price is estimated."
        if price is None
        else None
    )

    estimates = analyst_client.get_analyst_estimates(ticker)
    if "error" in estimates:
        return _no_data(ticker, estimates["error"])
    estimates_by_period = {
        r["period"]: r for r in estimates.get("forward_estimates", [])
    }

    eps = sec_facts.get_fundamentals(ticker, "eps")
    if "error" in eps:
        return _no_data(ticker, eps["error"])
    ttm_eps_diluted = eps.get("ttm_eps_diluted")  # envelope keeps payload keys
    shares_out = estimates.get("shares_outstanding")

    ob_rows = obligations.get_obligations(ticker)
    if "error" in ob_rows:
        return _no_data(ticker, ob_rows["error"])
    impact = _obligation_annual_impact(ob_rows.get("current_snapshot", ob_rows.get("obligations", [])), years=6)
    tax_rate = _effective_tax_rate(ob_rows)
    eps_scenarios = _obligation_eps_scenarios(ob_rows, shares_out, tax_rate)

    fy_current = estimates_by_period.get("current_fiscal_year") or {}
    fy_next = estimates_by_period.get("next_fiscal_year") or {}
    eps_current = fy_current.get("eps_avg")
    eps_next = fy_next.get("eps_avg")

    def _eps_line(eps_value, contractual, contingent, label, scenario=False):
        if eps_value is None:
            return None
        line = {
            "eps": round(eps_value, 2),
            "price": round(price, 2) if price is not None else None,
            "pe": round(price / eps_value, 1)
            if (price is not None and eps_value)
            else None,
            "label": label,
        }
        if contractual:
            line["obligation_drag_per_share"] = round(contractual, 2)
            line["eps_after_contractual"] = round(eps_value - contractual, 2)
            line["pe_after_contractual"] = (
                round(price / max(0.01, eps_value - contractual), 1)
                if (price is not None and (eps_value - contractual) > 0)
                else None
            )
        if contingent:
            line["contingent_drag_per_share"] = round(contingent, 2)
            line["eps_after_all_obligations"] = round(
                eps_value - contractual - contingent, 2
            )
            line["pe_after_all_obligations"] = (
                round(price / max(0.01, eps_value - contractual - contingent), 1)
                if (price is not None and (eps_value - contractual - contingent) > 0)
                else None
            )
        return line

    contractual_ps = (
        impact["contractual_annual_billions"] / (shares_out / 1e9)
        if shares_out
        else None
    )
    contingent_ps = (
        impact["contingent_annual_billions"] / (shares_out / 1e9)
        if shares_out
        else None
    )
    default_triggered_ps = (
        impact["default_triggered_annual_billions"] / (shares_out / 1e9)
        if shares_out
        else None
    )
    # Revenue-matched supply spend is NOT an EPS drag: it buys inventory
    # sold at gross margin and is already embedded in consensus revenue
    # and COGS. Shown separately with implied revenue coverage.
    revenue_matched_ps = (
        impact["revenue_matched_annual_billions"] / (shares_out / 1e9)
        if shares_out
        else None
    )
    gross_margin, margin_source = _revenue_matched_margin(ticker)
    implied_coverage_b = (
        impact["revenue_matched_annual_billions"] / (1 - gross_margin)
        if (impact["revenue_matched_annual_billions"] and gross_margin is not None)
        else None
    )
    impact_by_fy = impact.get("impact_by_fiscal_year") or {}
    flat_map = impact.get("flat_annual_by_bucket") or {}

    def _fy_year(period: dict) -> str | None:
        year = str((period or {}).get("period_end_date") or "")[:4]
        return year or None

    def _fy_ps(year: str | None, key: str) -> float | None:
        if shares_out and year:
            return (impact_by_fy.get(year, {}).get(key, 0.0) + flat_map.get(key, 0.0)) / (shares_out / 1e9)
        return None

    year_cur = _fy_year(fy_current)
    year_next = _fy_year(fy_next)
    contractual_cur = _fy_ps(year_cur, "contractual")
    contingent_cur = _fy_ps(year_cur, "contingent")
    default_cur = _fy_ps(year_cur, "default_triggered")
    revenue_cur = _fy_ps(year_cur, "revenue_matched")
    contractual_next = _fy_ps(year_next, "contractual")
    contingent_next = _fy_ps(year_next, "contingent")
    default_next = _fy_ps(year_next, "default_triggered")
    revenue_next = _fy_ps(year_next, "revenue_matched")
    forward_eps = {
            "consensus": _eps_line(eps_current, None, None, "consensus"),
            "adjusted": _eps_line(
                eps_current, contractual_cur, None,
                "adjusted forward EPS (contractual obligations included)",
            ),
            "scenario": _eps_line(
                eps_current, contractual_cur, contingent_cur,
                "forward EPS incl. contingent obligations (stress scenario, no counterparty default)",
            ),
            "scenario_with_defaults": _eps_line(
                eps_current,
                contractual_cur,
                (contingent_cur or 0.0) + (default_cur or 0.0),
                "forward EPS incl. contingent obligations AND counterparty-default-triggered guarantees (pay only on counterparty default)",
            ),
            "worst_case": _eps_line(
                eps_current,
                (contractual_cur or 0.0) + (revenue_cur or 0.0),
                (contingent_cur or 0.0) + (default_cur or 0.0),
                "worst-case EPS (all disclosed obligations incl. revenue-matched supply stranded AND counterparty defaults)",
            ),
            "consensus_next_fy": _eps_line(eps_next, None, None, "consensus"),
            "adjusted_next_fy": _eps_line(
                eps_next, contractual_next, None,
                "adjusted forward EPS (contractual obligations included)",
            ),
            "scenario_next_fy": _eps_line(
                eps_next, contractual_next, contingent_next,
                "forward EPS incl. contingent obligations (stress scenario, no counterparty default)",
            ),
            "scenario_with_defaults_next_fy": _eps_line(
                eps_next,
                contractual_next,
                (contingent_next or 0.0) + (default_next or 0.0),
                "forward EPS incl. contingent obligations AND counterparty-default-triggered guarantees (pay only on counterparty default)",
            ),
            "worst_case_next_fy": _eps_line(
                eps_next,
                (contractual_next or 0.0) + (revenue_next or 0.0),
                (contingent_next or 0.0) + (default_next or 0.0),
                "worst-case EPS (all disclosed obligations incl. revenue-matched supply stranded AND counterparty defaults)",
            ),
        }

    rows = ob_rows.get("obligations", [])
    ob_coverage = ob_rows.get("coverage") or {}
    manifest = ob_coverage.get("scan_manifest") or []
    coverage = {
        "scan_manifest": manifest,
        "filings_examined": ob_rows.get("filings_examined")
        or sorted({str(m.get("filing_date")) for m in manifest if m.get("filing_date")})
        or sorted({str(r.get("filed")) for r in rows if r.get("filed")}),
        "sections_examined": ob_rows.get("sections_examined")
        or sorted({str(s) for m in manifest for s in (m.get("sections_examined") or []) if s})
        or sorted({str(r.get("source")) for r in rows if r.get("source")}),
        "quantified_count": ob_coverage.get("quantified_count", len(rows)),
        "unquantified_count": ob_coverage.get(
            "unquantified_count",
            len(
                ob_rows.get("unquantified_exposures")
                or ob_rows.get("unquantified")
                or ob_rows.get("unquantified_items")
                or []
            ),
        ),
        "warnings": list(ob_coverage.get("warnings") or ob_rows.get("warnings") or []),
    }
    if coverage["unquantified_count"] and not rows:
        coverage["warnings"].append(
            f"{coverage['unquantified_count']} unquantified exposure(s) disclosed "
            "without dollar amounts; excluded from quantified obligations."
        )
    if price_gap is not None:
        coverage["warnings"].append(price_gap)

    value = {
        "ticker": ticker,
        "as_of": estimates.get("as_of"),
        "fiscal_year_current": year_cur,
        "fiscal_year_next": year_next,
        "source": "live price (Yahoo Finance quote) + SEC EDGAR EPS + Yahoo consensus + SEC 10-Q/10-K notes",
        "price": {
            "last": round(price, 2) if price is not None else None,
            "retrieved_as_of": estimates.get("as_of"),
        },
        "price_gap": price_gap,
        "coverage": coverage,
        "shares_outstanding": shares_out,
        "ttm_gaap_eps": ttm_eps_diluted,
        "trailing_pe": (
            round(price / ttm_eps_diluted, 1)
            if (price is not None and ttm_eps_diluted)
            else None
        ),
        "obligations": {
            "contractual_annual_billions": impact["contractual_annual_billions"],
            "contingent_annual_billions": impact["contingent_annual_billions"],
            "default_triggered_annual_billions": impact["default_triggered_annual_billions"],
            "revenue_matched_annual_billions": impact["revenue_matched_annual_billions"],
            "drag_per_share_contractual": contractual_ps,
            "drag_per_share_contingent": contingent_ps,
            "drag_per_share_default_triggered": default_triggered_ps,
            "revenue_matched_per_share": revenue_matched_ps,
            "revenue_matched_implied_revenue_billions": round(implied_coverage_b, 1)
            if implied_coverage_b is not None
            else None,
            "revenue_matched_gross_margin": gross_margin,
            "revenue_matched_margin_source": margin_source,
            "per_kind": impact["per_kind"],
        },
        "obligation_eps_scenarios": eps_scenarios,
        "forward_eps": forward_eps,
        "projected_prices": _projected_prices(
            {
                f"Consensus FY{year_cur}" if year_cur else "Consensus (current FY)": (forward_eps["consensus"] or {}).get("eps"),
                f"Adjusted FY{year_cur}" if year_cur else "Adjusted (current FY)": (forward_eps["adjusted"] or {}).get(
                    "eps_after_contractual"
                ),
                f"Scenario FY{year_cur} (no default)" if year_cur else "Scenario (current FY, no default)": (forward_eps["scenario"] or {}).get(
                    "eps_after_all_obligations"
                ),
                f"Scenario FY{year_cur} (counterparty default)" if year_cur else "Scenario (current FY, counterparty default)": (forward_eps["scenario_with_defaults"] or {}).get(
                    "eps_after_all_obligations"
                ),
                f"Worst case FY{year_cur}" if year_cur else "Worst case (current FY)": (forward_eps["worst_case"] or {}).get(
                    "eps_after_all_obligations"
                ),
                f"Consensus FY{year_next}" if year_next else "Consensus (next FY)": (forward_eps["consensus_next_fy"] or {}).get(
                    "eps"
                ),
                f"Scenario FY{year_next} (no default)" if year_next else "Scenario (next FY, no default)": (forward_eps["scenario_next_fy"] or {}).get(
                    "eps_after_all_obligations"
                ),
                f"Scenario FY{year_next} (counterparty default)" if year_next else "Scenario (next FY, counterparty default)": (forward_eps["scenario_with_defaults_next_fy"] or {}).get(
                    "eps_after_all_obligations"
                ),
                f"Worst case FY{year_next}" if year_next else "Worst case (next FY)": (forward_eps["worst_case_next_fy"] or {}).get(
                    "eps_after_all_obligations"
                ),
            },
            price,
        ),
        "note": (
            "'Adjusted' includes only contractual (non-cancelable/firm) "
            "obligations from the latest 10-Q/10-K notes, annualized. "
            "'Scenario' adds contingent obligations that are cancellable / "
            "reducible / terminable per the filing (cloud, vendor, supply "
            "commitments) — no counterparty default assumed. "
            "'Scenario (counterparty default)' also adds default-triggered "
            "guarantees, which pay only if a named counterparty defaults "
            "or becomes insolvent. 'Worst case' additionally treats "
            "revenue-matched supply commitments as stranded costs (demand "
            "fails). Revenue-matched spend is NOT subtracted from consensus "
            "EPS (already embedded); it is reported separately with implied "
            "revenue coverage at the company's own gross margin (None when "
            "the filed gross-margin fact is missing). Buybacks, dividends, "
            "and unquantifiable indemnities are excluded."
        ),
    }
    if price_gap is None:  # never cache a quote gap; retry fresh next call
        cache.set(key, value)
    return value