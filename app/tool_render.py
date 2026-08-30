"""Render tool results as compact plain text/Markdown for the main model.

Internal results stay structured Python dicts/JSON (caching, validation,
deterministic metrics, the small FINRA analysis model's validated prose).
Only the tool message sent to the main chat model is rendered to compact
text. Rendering enforces a fixed UTF-8 byte budget and reduces the structure
(rows first, then verbose sections, then individual text values) before
rendering — it never slices serialized JSON or rendered text blindly.
"""

from __future__ import annotations

from typing import Any, Optional

MAX_TOOL_MESSAGE_BYTES = 64 * 1024

TRUNCATED_MARKER = "... [Tool output truncated]"

_TEXT_KEYS = ("text", "diff", "summary")


def render_tool_result(
    result: Any, max_bytes: int = MAX_TOOL_MESSAGE_BYTES
) -> str:
    """Render a tool result as compact text within the byte budget.

    Always returns a non-empty string of at most max_bytes UTF-8 bytes.
    """
    if not isinstance(result, dict):
        result = {"result": result}
    if "error" in result:
        return _render_error(result, max_bytes)
    if result.get("result_type") == "market_snapshot":
        text = _render_market_snapshot(result, max_bytes)
    elif result.get("result_type") == "option_chain":
        text = _render_option_chain(result, max_bytes)
    elif result.get("result_type") == "option_analysis":
        text = _render_option_analysis(result, max_bytes)
    elif result.get("result_type") == "option_comparison":
        text = _render_option_comparison(result, max_bytes)
    elif result.get("result_type") == "portfolio_snapshot":
        text = _render_portfolio_snapshot(result, max_bytes)
    elif result.get("result_type") == "scan_specs":
        text = _render_scan_specs(result, max_bytes)
    elif result.get("result_type") == "scan_list":
        text = _render_scan_list(result, max_bytes)
    elif result.get("result_type") == "scan_results":
        text = _render_scan_results(result, max_bytes)
    elif "price_targets" in result and "forward_estimates" in result:
        text = _render_analyst_estimates(result, max_bytes)
    elif "weight_pct" in result and "rank" in result:
        text = _render_sp500_weight(result, max_bytes)
    elif "obligations" in result and "form" in result:
        text = _render_obligations(result, max_bytes)
    elif "forward_eps" in result and "obligations" in result:
        text = _render_valuation_metrics(result, max_bytes)
    elif "entries" in result and "settlement_date" in result:
        text = _render_short_interest_leaderboard(result, max_bytes)
    elif "records" in result and "fields" in result:
        text = _render_datapoints(result, max_bytes)
    elif "coverage" in result and "metrics" in result:
        text = _render_briefing(result, max_bytes)
    elif _is_text_result(result):
        text = _render_text_result(result, max_bytes)
    else:
        text = _render_generic(result, max_bytes)
    if _utf8_size(text) <= max_bytes:
        return text
    return _minimal(result, max_bytes)


def _render_market_snapshot(result: dict, max_bytes: int) -> str:
    lines = [
        f"{result.get('ticker', '?')} market snapshot",
        f"Last: {_cell(result.get('last')) or 'unavailable'}",
        f"Bid: {_cell(result.get('bid')) or 'unavailable'}",
        f"Ask: {_cell(result.get('ask')) or 'unavailable'}",
        f"Retrieved: {_cell(result.get('retrieved_at')) or 'unavailable'}",
        f"Source: {_cell(result.get('source')) or 'robinhood_mcp'}",
    ]
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_option_chain(result: dict, max_bytes: int) -> str:
    fields = ("expiration", "dte", "strike", "bid", "ask", "mark", "mid", "spread", "implied_volatility", "delta", "gamma", "theta", "vega")
    labels = ("Expiration", "DTE", "Strike", "Bid", "Ask", "Mark", "Mid", "Spread", "IV", "Delta", "Gamma", "Theta", "Vega")
    lines = [
        f"{result.get('ticker', '?')} {str(result.get('option_type', '')).upper()} OPTIONS",
        "| " + " | ".join(labels) + " |",
        "|" + "|".join("---" for _ in fields) + "|",
    ]
    for row in result.get("contracts") or []:
        if not isinstance(row, dict):
            continue
        values = []
        for field in fields:
            value = row.get(field)
            if field == "mid" and value is None:
                bid, ask = row.get("bid"), row.get("ask")
                value = (float(bid) + float(ask)) / 2 if bid is not None and ask is not None else None
            if field == "spread" and value is None:
                bid, ask = row.get("bid"), row.get("ask")
                value = float(ask) - float(bid) if bid is not None and ask is not None else None
            values.append(_table_cell(value if value is not None else "unavailable"))
        lines.append("| " + " | ".join(values) + " |")
    lines.append(f"Returned: {result.get('returned', 0)} of {result.get('matched', 0)} matched")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_option_analysis(result: dict, max_bytes: int) -> str:
    fields = ("ticker", "expiration", "dte", "strike", "bid", "ask", "mid", "spread", "implied_volatility", "delta", "gamma", "theta", "vega", "target_price", "target_pnl", "target_return_pct")
    lines = ["Option contract analysis"]
    for field in fields:
        value = result.get(field)
        if value is not None:
            lines.append(f"{field}: {_cell(value)}")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_option_comparison(result: dict, max_bytes: int) -> str:
    lines = [
        f"{result.get('ticker', '?')} option comparison",
        "| Contract | Expiration | Strike | Mid | Spread % | IV | Delta | Theta | Vega | Target P/L |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result.get("contracts") or []:
        if not isinstance(row, dict):
            continue
        lines.append("| " + " | ".join(_table_cell(row.get(field, "unavailable")) for field in (
            "contract_id", "expiration", "strike", "mid", "spread_pct", "implied_volatility", "delta", "theta", "vega", "target_pnl"
        )) + " |")
    lines.append(f"Returned: {result.get('returned', 0)} of {result.get('matched', 0)} matched; ranking: {result.get('ranking', 'unknown')}")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


_PORTFOLIO_SEC_CONCEPTS = (
    "Revenue",
    "NetIncomeLoss",
    "CashAndCashEquivalents",
    "LongTermDebt",
    "EntityCommonStockSharesOutstanding",
)


def _render_portfolio_snapshot(result: dict, max_bytes: int) -> str:
    lines = [
        f"Portfolio snapshot — {_cell(result.get('broker')) or 'robinhood'}",
        f"Created: {_cell(result.get('created_at')) or 'unavailable'}",
        f"Total value: {_cell(result.get('total_value')) or 'unavailable'}",
        f"Cash: {_cell(result.get('cash')) or 'unavailable'}",
        f"Invested: {_cell(result.get('invested_value')) or 'unavailable'}",
        f"Positions: {result.get('position_count', 0)} ({result.get('priced_position_count', 0)} priced, {result.get('unresolved_position_count', 0)} unresolved)",
    ]
    if result.get("concentration"):
        lines.append(f"Concentration: {_cell(result['concentration'])}")
    lines.append("Positions:")
    for row in result.get("positions") or []:
        if isinstance(row, dict):
            lines.append(_portfolio_position_line(row))
    if result.get("omitted_count"):
        lines.append(f"... {result['omitted_count']} smaller positions omitted")
    unresolved = result.get("unresolved") or []
    if unresolved:
        lines.append("Unresolved securities: " + ", ".join(_cell(t) for t in unresolved))
    freshness = result.get("freshness") or {}
    freshness_parts = []
    created = freshness.get("snapshot_created_at") or result.get("created_at")
    if created:
        freshness_parts.append(f"snapshot {created}")
    if freshness.get("sec_latest_filed_at"):
        freshness_parts.append(f"SEC latest filing {freshness['sec_latest_filed_at']}")
    if freshness.get("finra_settlement_date"):
        freshness_parts.append(f"FINRA settlement {freshness['finra_settlement_date']}")
    if freshness_parts:
        lines.append("Research freshness: " + "; ".join(freshness_parts))
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _portfolio_position_line(row: dict) -> str:
    parts = [f"- {_cell(row.get('ticker')) or '?'}"]
    quantity = _cell(row.get("quantity"))
    price = _cell(row.get("market_price")) or "unavailable"
    if quantity:
        parts.append(f"{quantity} x {price}")
    value = _cell(row.get("market_value"))
    if value:
        parts.append(f"value {value}")
    weight = _cell(row.get("portfolio_weight"))
    if weight:
        try:
            weight = f"{float(weight) * 100:.2f}%"
        except (TypeError, ValueError):
            pass
        parts.append(f"weight {weight}")
    gain = _cell(row.get("unrealized_gain"))
    if gain:
        parts.append(f"gain {gain}")
    if not row.get("resolved", True):
        parts.append("[UNRESOLVED]")
    sec = row.get("sec") or {}
    sec_parts = []
    for concept in _PORTFOLIO_SEC_CONCEPTS:
        fact = sec.get(concept)
        if isinstance(fact, dict) and _cell(fact.get("value")):
            label = concept[:3].lower()
            sec_parts.append(f"{label} {_cell(fact['value'])}")
    if sec_parts:
        parts.append("SEC " + " ".join(sec_parts))
    finra = row.get("finra") or {}
    finra_parts = []
    if _cell(finra.get("short_position")):
        finra_parts.append(f"short {_cell(finra['short_position'])}")
    if _cell(finra.get("change_pct")):
        finra_parts.append(f"d {_cell(finra['change_pct'])}")
    if _cell(finra.get("days_to_cover")):
        finra_parts.append(f"dtc {_cell(finra['days_to_cover'])}")
    if finra_parts:
        parts.append("FINRA " + " ".join(finra_parts))
    return "  ".join(parts)


def _render_scan_specs(result: dict, max_bytes: int) -> str:
    specs = result.get("specs") or []
    lines = [
        f"Scanner filter specs ({result.get('count', len(specs))} filter types)",
        "Live data from Robinhood MCP; call this before constructing scan filters.",
    ]
    for spec in specs:
        if not isinstance(spec, dict):
            continue
        name = _cell(spec.get("display_name") or spec.get("filter_type") or spec.get("name") or "?")
        filter_type = _cell(spec.get("filter_type")) or ""
        predicates = _cell(spec.get("supported_predicates")) or ""
        lines.append(f"- {name}{f' ({filter_type})' if filter_type else ''}: {predicates}"[:200])
    if result.get("omitted_count"):
        lines.append(f"... {result['omitted_count']} more filter types omitted")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_scan_list(result: dict, max_bytes: int) -> str:
    scans = result.get("scans") or []
    lines = [f"Saved scanners ({result.get('count', len(scans))})"]
    for scan in scans:
        if not isinstance(scan, dict):
            continue
        scan_id = _cell(scan.get("scan_id") or scan.get("id") or "?")
        title = _cell(scan.get("title") or scan.get("name") or "untitled")
        parts = [f"- {scan_id}: {title}"]
        if scan.get("cortex_managed"):
            parts.append("[Cortex-managed, read-only]")
        if isinstance(scan.get("filters"), list):
            parts.append(f"{len(scan['filters'])} filters")
        if isinstance(scan.get("columns"), list):
            parts.append(f"{len(scan['columns'])} columns")
        lines.append("  ".join(parts))
    if result.get("omitted_count"):
        lines.append(f"... {result['omitted_count']} more scans omitted")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_scan_results(result: dict, max_bytes: int) -> str:
    rows = result.get("rows") or []
    title = _cell(result.get("title")) or "Scan"
    lines = [
        f"{title} — live results ({result.get('total', len(rows))} matches)",
        "Live market data from Robinhood MCP, evaluated at request time.",
    ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _cell(row.get("ticker") or row.get("symbol") or "?")
        values = [
            _cell(row[key]) for key in ("last", "price", "market_cap", "volume", "change", "change_percent")
            if _cell(row.get(key))
        ]
        lines.append(f"- {ticker}" + ("  " + "  ".join(values) if values else ""))
    if result.get("omitted"):
        lines.append(f"... {result['omitted']} more matches omitted")
    if result.get("sort"):
        lines.append(f"Sort: {_cell(result['sort'])}")
    lines.append("Source: " + str(result.get("source", "robinhood_mcp")))
    return _truncate_bytes("\n".join(lines), max_bytes)



def _render_short_interest_leaderboard(result: dict, max_bytes: int) -> str:
    fields = ("rank", "ticker", "short_interest_percent", "short_shares", "shares_outstanding", "sec_shares_as_of", "sec_filed_at")
    labels = ("Rank", "Ticker", "Short %", "Short shares", "Shares outstanding", "SEC shares as of", "SEC filed")
    lines = [
        "Short interest leaderboard — FINRA settlement " + str(result["settlement_date"]),
        "| " + " | ".join(labels) + " |",
        "|" + "|".join("---" for _ in fields) + "|",
    ]
    stale_banner = _datapoints_stale_banner(result)
    if stale_banner:
        lines.append(stale_banner)
    for entry in result.get("entries") or []:
        values = []
        for field in fields:
            value = entry.get(field, "")
            if field == "short_interest_percent" and value != "":
                value = f"{float(value):.2f}%"
            elif field in ("short_shares", "shares_outstanding") and value != "":
                value = f"{float(value):,.0f}"
            values.append(_table_cell(value))
        lines.append("| " + " | ".join(values) + " |")
    coverage = result.get("coverage") or {}
    lines.append("Source: " + str(result.get("source", "FINRA + SEC EDGAR")))
    lines.append("Metric: " + str(result.get("metric", "")))
    lines.append("Coverage: " + f"{coverage.get('eligible_rows', 0)} eligible of {coverage.get('finra_rows', 0)} FINRA rows; exclusions {coverage.get('exclusions', {})}")
    if result.get("as_of_date"):
        lines.append("As of: " + str(result["as_of_date"]) + " (freshness: " + str(result.get("data_freshness") or "unknown") + ")")
    lines.append("Environment: " + str(result.get("environment", "unknown")))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_analyst_estimates(result: dict, max_bytes: int) -> str:
    ticker = result.get("ticker", "?")
    quote = result.get("quote") or {}
    targets = result.get("price_targets") or {}
    valuation = result.get("valuation") or {}
    lines = [
        f"{ticker} analyst consensus (as of {result.get('as_of', '?')})",
        f"Source: {result.get('source', 'Yahoo Finance')}",
    ]
    price = quote.get("price")
    lines.append(
        f"Last: {price if price is not None else 'unavailable'}"
        f"  |  Market cap: {_cell(result.get('market_cap'))}"
        f"  |  Shares out: {_cell(result.get('shares_outstanding'))}"
    )
    lines.append(
        "Targets (12-mo): "
        f"median {_cell(targets.get('median'))}"
        f" | mean {_cell(targets.get('mean'))}"
        f" | high {_cell(targets.get('high'))}"
        f" | low {_cell(targets.get('low'))}"
        f" | {targets.get('num_analysts') or '?'} analysts"
    )
    lines.append(
        f"Rating: {targets.get('recommendation')}"
        f" (mean {_cell(targets.get('recommendation_mean'))})"
        f"  |  P/E trailing {_cell(valuation.get('trailing_pe'))}"
        f" / forward {_cell(valuation.get('forward_pe'))}"
    )
    for row in result.get("forward_estimates") or []:
        eps = row.get("eps_avg")
        rev = row.get("revenue_avg")
        lines.append(
            f"- {row.get('period', '?')} (ends {row.get('period_end_date', '?')}): "
            f"EPS est {_cell(eps)}"
            f" ({_cell(row.get('eps_growth_pct'))}% YoY, n={row.get('eps_analysts')})"
            f" | Revenue est {_cell(rev)}"
            f" ({_cell(row.get('revenue_growth_pct'))}% YoY, n={row.get('revenue_analysts')})"
        )
        rev_trend = row.get("eps_revision") or {}
        if rev_trend.get("current") is not None:
            lines.append(
                f"    EPS revision: now {_cell(rev_trend.get('current'))}"
                f" | 7d ago {_cell(rev_trend.get('days7_ago'))}"
                f" | 30d ago {_cell(rev_trend.get('days30_ago'))}"
                f" | 60d ago {_cell(rev_trend.get('days60_ago'))}"
            )
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_sp500_weight(result: dict, max_bytes: int) -> str:
    ticker = result.get("ticker", "?")
    lines = [
        f"{ticker} S&P 500 index weight (as of {result.get('as_of', '?')})",
        f"Source: {result.get('source', 'Slickcharts')}",
        f"Rank: {_cell(result.get('rank'))}"
        f"  |  Company: {result.get('company', '?')}"
        f"  |  Weight: {_cell(result.get('weight_pct'))}% of index market cap",
    ]
    if result.get("note"):
        lines.append("Note: " + str(result["note"]))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_obligations(result: dict, max_bytes: int) -> str:
    ticker = result.get("ticker", "?")
    lines = [
        f"{ticker} contractual obligations & commitments (filed {result.get('filed', '?')})",
        f"Source: {result.get('source', 'SEC EDGAR notes')}",
    ]
    for row in result.get("obligations") or []:
        amount = row.get("amount_billions")
        amount_s = f"${amount}B" if amount is not None else "(schedule only)"
        matched = " | revenue-matched" if row.get("revenue_matched") else ""
        status = f" | status: {row.get('status', '?')}"
        lines.append(
            f"- {row.get('type', '?')}: {amount_s}"
            f" | certainty: {row.get('certainty', '?')}{status}{matched}"
        )
        horizon = row.get("payment_horizon") or {}
        if horizon.get("paid_in_remainder_billions"):
            lines.append(
                f"    front-loaded: ${horizon['paid_in_remainder_billions']}B"
                f" paid in remainder of FY{horizon.get('paid_in_remainder_of_fy')},"
                f" ${horizon.get('paid_after_remainder_billions')}B after"
            )
        for y in horizon.get("schedule") or []:
            lines.append(
                f"    FY{y.get('fiscal_year')}: ${y.get('amount_billions')}B"
            )
        for y in row.get("schedule") or []:
            lines.append(
                f"    FY{y.get('fiscal_year')}: ${y.get('amount_billions')}B"
            )
    if result.get("note"):
        lines.append("Note: " + str(result["note"]))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _render_valuation_metrics(result: dict, max_bytes: int) -> str:
    ticker = result.get("ticker", "?")
    price = result.get("price") or {}
    fe = result.get("forward_eps") or {}
    ob = result.get("obligations") or {}
    lines = [
        f"{ticker} valuation (live price ${price.get('last')}, as of {result.get('as_of', '?')})",
        f"Source: {result.get('source', '')}",
        f"Trailing P/E (GAAP TTM EPS ${result.get('ttm_gaap_eps')}): "
        f"{result.get('trailing_pe')}x",
        f"Obligation drag per share: contractual ${ob.get('drag_per_share_contractual')}"
        f" | contingent ${ob.get('drag_per_share_contingent')}"
        f" | default-triggered ${ob.get('drag_per_share_default_triggered')}"
        f" (annual: ${ob.get('contractual_annual_billions')}B contractual,"
        f" ${ob.get('contingent_annual_billions')}B contingent,"
        f" ${ob.get('default_triggered_annual_billions')}B default-triggered)",
    ]
    if ob.get("revenue_matched_annual_billions"):
        lines.append(
            f"Revenue-matched (supply) commitments: "
            f"${ob.get('revenue_matched_annual_billions')}B/yr"
            f" | NOT an EPS drag (inventory sold at ~75% GM)"
            f" | implied revenue coverage ~${ob.get('revenue_matched_implied_revenue_billions')}B/yr"
        )

    def _line(label: str, row: Optional[dict], scenario: bool = False) -> None:
        if not row:
            return
        pe = row.get("pe")
        eps = row.get("eps")
        lines.append(
            f"- {label}: EPS ${eps} | P/E {pe}x"
            if not scenario
            else f"- {label}: EPS ${eps} | P/E {pe}x | after obligations"
        )

    _line("Consensus FY27", fe.get("consensus"))
    adj = fe.get("adjusted") or {}
    if adj.get("eps_after_contractual") is not None:
        lines.append(
            f"- Adjusted FY27 (contractual incl.): EPS ${adj['eps_after_contractual']}"
            f" | P/E {adj.get('pe_after_contractual')}x"
            f" | drag ${adj.get('obligation_drag_per_share')}/sh"
        )
    scn = fe.get("scenario") or {}
    if scn.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- Scenario FY27 (+contingent, no default): EPS ${scn['eps_after_all_obligations']}"
            f" | P/E {scn.get('pe_after_all_obligations')}x"
            f" | drag ${scn.get('contingent_drag_per_share')}/sh"
        )
    scn_d = fe.get("scenario_with_defaults") or {}
    if scn_d.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- Scenario FY27 (OpenAI/partner default): EPS ${scn_d['eps_after_all_obligations']}"
            f" | P/E {scn_d.get('pe_after_all_obligations')}x"
            f" | drag ${scn_d.get('contingent_drag_per_share')}/sh"
        )
    _line("Consensus FY28", fe.get("consensus_next_fy"))
    adj2 = fe.get("adjusted_next_fy") or {}
    if adj2.get("eps_after_contractual") is not None:
        lines.append(
            f"- Adjusted FY28 (contractual incl.): EPS ${adj2['eps_after_contractual']}"
            f" | P/E {adj2.get('pe_after_contractual')}x"
        )
    scn2 = fe.get("scenario_next_fy") or {}
    if scn2.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- Scenario FY28 (+contingent, no default): EPS ${scn2['eps_after_all_obligations']}"
            f" | P/E {scn2.get('pe_after_all_obligations')}x"
            f" | drag ${scn2.get('contingent_drag_per_share')}/sh contingent"
        )
    scn_d2 = fe.get("scenario_with_defaults_next_fy") or {}
    if scn_d2.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- Scenario FY28 (OpenAI/partner default): EPS ${scn_d2['eps_after_all_obligations']}"
            f" | P/E {scn_d2.get('pe_after_all_obligations')}x"
        )
    worst = fe.get("worst_case") or {}
    if worst.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- WORST CASE FY27 (all obligations incl. supply stranded):"
            f" EPS ${worst['eps_after_all_obligations']}"
            f" | P/E {worst.get('pe_after_all_obligations')}x"
        )
    worst2 = fe.get("worst_case_next_fy") or {}
    if worst2.get("eps_after_all_obligations") is not None:
        lines.append(
            f"- WORST CASE FY28 (all obligations incl. supply stranded):"
            f" EPS ${worst2['eps_after_all_obligations']}"
            f" | P/E {worst2.get('pe_after_all_obligations')}x"
        )
    projected = result.get("projected_prices") or {}
    tiers = projected.get("tiers") or []
    if tiers:
        lines.append(
            "Projected share price by assumed P/E (vs live "
            f"${projected.get('current_price')}):"
        )
        for t in tiers:
            cells = []
            for m, c in t.get("prices", {}).items():
                cells.append(
                    f"{m} ${c.get('price')} ({c.get('pct_change_vs_current'):+}%)"
                )
            lines.append(
                f"  {t['tier']} (EPS ${t['eps']}): " + " | ".join(cells)
            )
    scenarios = result.get("obligation_eps_scenarios") or {}
    scenario_rows = scenarios.get("scenarios") or []
    if scenario_rows:
        lines.append(
            "Obligation EPS-impact scenarios (after-tax, "
            f"tax rate {scenarios.get('effective_tax_rate')}):"
        )
        for s in scenario_rows:
            lines.append(
                f"  {s['scenario']}: EPS {s['eps_impact']}"
                f" ({'one-time' if s['one_time'] else 'annual'})"
                f" — {s.get('note', '')}"
            )
    if result.get("note"):
        lines.append("Note: " + str(result["note"]))
    return _truncate_bytes("\n".join(lines), max_bytes)


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _truncate_bytes(text: str, max_bytes: int, marker: str = TRUNCATED_MARKER) -> str:
    """Byte-safe prefix truncation with an explicit marker appended."""
    if _utf8_size(text) <= max_bytes:
        return text
    room = max_bytes - _utf8_size(marker)
    if room < 0:
        return marker[: max(0, max_bytes)]
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _utf8_size(text[:mid]) <= room:
            lo = mid
        else:
            hi = mid - 1
    prefix = text[:lo].rstrip()
    return prefix + marker


def _fit_lines(lines: list[str], max_bytes: int) -> tuple[list[str], int]:
    """Keep as many complete lines as fit; returns (kept, omitted)."""
    kept: list[str] = []
    omitted = 0
    used = 0
    for line in lines:
        cost = _utf8_size(line) + 1
        if cost > max_bytes:
            kept.append(_truncate_bytes(line, max_bytes - used))
            return kept, omitted
        if used + cost > max_bytes:
            omitted += 1
            continue
        kept.append(line)
        used += cost
    return kept, omitted


def _cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return s.strip()


_MAX_TABLE_CELL_CHARS = 200


def _table_cell(value: Any) -> str:
    """Single table cell; oversized values are truncated with a marker so
    one huge field cannot balloon the whole table."""
    s = _cell(value)
    if len(s) <= _MAX_TABLE_CELL_CHARS:
        return s
    return s[:_MAX_TABLE_CELL_CHARS].rstrip() + f"... [{len(s)} chars]"


def _minimal(result: dict, max_bytes: int) -> str:
    source = result.get("source") or result.get("dataset_id") or "tool result"
    text = f"Source: {source} | {TRUNCATED_MARKER}"
    return _truncate_bytes(text, max_bytes, marker="")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _render_error(result: dict, max_bytes: int) -> str:
    msg = str(result.get("error") or "Unknown error").strip()
    lines = [f"Error: {msg}"]
    for key in ("dataset", "dataset_id", "source", "request_purpose"):
        value = result.get(key)
        if value:
            lines.append(f"{key}: {value}")
    if result.get("http_status") is not None:
        lines.append(f"http_status: {result['http_status']}")
    if result.get("finra_response"):
        body = _cell(result["finra_response"])
        lines.append(f"finra_response: {body}")
    if result.get("environment"):
        lines.append(f"environment: {result['environment']}")
    return _truncate_bytes("\n".join(lines), max_bytes, marker="")


# ---------------------------------------------------------------------------
# get_finra_datapoints: compact Markdown table of only the selected fields
# ---------------------------------------------------------------------------


def _render_datapoints(result: dict, max_bytes: int) -> str:
    fields = [str(f) for f in (result.get("fields") or [])]
    if not fields:
        return _render_generic(result, max_bytes)
    records = result.get("records") or []

    header = "| " + " | ".join(_table_cell(f) for f in fields) + " |"
    sep = "|" + "|".join("---" for _ in fields) + "|"
    stale_banner = _datapoints_stale_banner(result)
    footer = _datapoints_footer(result)
    if footer:
        footer = "\n" + footer
    reserved = (
        _utf8_size(TRUNCATED_MARKER + "\n" + "Omitted rows: 99999\n" + footer)
        + 32
    )
    if stale_banner:
        reserved += _utf8_size(stale_banner) + 2

    if reserved > max_bytes:
        return _minimal(result, max_bytes)

    used = _utf8_size(header + "\n" + sep + "\n")
    out = [header, sep]
    if stale_banner:
        out.append(stale_banner)
        used += _utf8_size(stale_banner) + 1
    omitted = 0
    for row in records:
        if not isinstance(row, dict):
            line = _table_cell(row)
        else:
            line = "| " + " | ".join(_table_cell(row.get(f)) for f in fields) + " |"
        if used + _utf8_size(line) + 1 + reserved > max_bytes:
            omitted += 1
            continue
        out.append(line)
        used += _utf8_size(line) + 1

    text = "\n".join(out) + footer
    if omitted:
        text = (
            "\n".join(out)
            + f"\n{TRUNCATED_MARKER}\nOmitted rows: {omitted}"
            + footer
        )
    return text


def _datapoints_footer(result: dict) -> str:
    parts = []
    if result.get("source"):
        parts.append(f"Source: {result['source']}")
    returned = result.get("returned_count")
    total = result.get("total_records")
    if total is not None:
        pag = f"{returned if returned is not None else '?'} returned of {total} total"
    else:
        pag = f"{returned if returned is not None else '?'} returned (Record-Total absent)"
    if result.get("pagination_source"):
        pag += f", {result['pagination_source']}"
    if result.get("may_have_more") is not None:
        pag += f", more pages: {'yes' if result['may_have_more'] else 'no'}"
    if result.get("next_offset") is not None:
        pag += f", next_offset {result['next_offset']}"
    parts.append("Pagination: " + pag)
    if result.get("as_of_date"):
        parts.append(
            f"As of: {result['as_of_date']} "
            f"(freshness: {result.get('data_freshness') or 'unknown'})"
        )
    if result.get("environment"):
        parts.append(f"Environment: {result['environment']}")
    warnings = [str(w) for w in (result.get("warnings") or []) if str(w).strip()]
    non_stale = [w for w in warnings if "STALE" not in w]
    if non_stale:
        parts.append("Warnings: " + "; ".join(non_stale))
    return "\n".join(parts)


def _datapoints_stale_banner(result: dict) -> str:
    if result.get("data_freshness") == "stale" and result.get("as_of_date"):
        return (
            f"!! STALE/HISTORICAL DATA !! Newest record is "
            f"{result['as_of_date']} (over 90 days old); this is historical "
            "data, NOT current market data."
        )
    return ""


# ---------------------------------------------------------------------------
# FINRA analysis briefing
# ---------------------------------------------------------------------------


def _render_briefing(result: dict, max_bytes: int) -> str:
    cov = result.get("coverage") or {}
    query = result.get("query") or {}

    name = result.get("name") or result.get("dataset") or result.get("dataset_id")
    ticker = query.get("ticker") or result.get("ticker")
    title = f"FINRA: {name}" + (f" — {ticker}" if ticker else "")

    forced = [title]
    if result.get("source"):
        forced.append(f"Source: {result['source']}")

    cover = []
    if cov.get("rows_analyzed") is not None:
        cover.append(f"{cov['rows_analyzed']} rows analyzed")
    if cov.get("rows_matched") is not None:
        cover.append(f"{cov['rows_matched']} rows returned")
    if cov.get("first_date") and cov.get("last_date"):
        cover.append(f"{cov['first_date']} to {cov['last_date']}")
    statuses = []
    for flag in ("page_complete", "query_complete", "analysis_complete"):
        value = cov.get(flag)
        if value is True:
            status = "yes"
        elif value is False:
            status = "no"
        else:
            status = "unknown"
        statuses.append(f"{flag.replace('_', ' ')}: {status}")
    if statuses:
        cover.append("; ".join(statuses))
    if cover:
        forced.append("Coverage: " + ", ".join(cover))

    query_parts = []
    if ticker:
        query_parts.append(f"ticker {ticker}")
    if query.get("start_date") or query.get("end_date"):
        query_parts.append(
            f"{query.get('start_date') or '…'}..{query.get('end_date') or '…'}"
        )
    if query.get("limit") is not None:
        query_parts.append(f"limit {query['limit']}")
    if query.get("offset"):
        query_parts.append(f"offset {query['offset']}")
    if query_parts:
        forced.append("Query: " + ", ".join(query_parts))

    warnings = _render_warnings(result)
    if warnings:
        forced.append("Warnings:\n" + "\n".join("  - " + w for w in warnings))
    pagination = _render_pagination(result)
    if pagination:
        forced.append("Pagination: " + pagination)

    if result.get("as_of_date"):
        forced.append(
            f"As of: {result['as_of_date']} "
            f"(freshness: {result.get('data_freshness') or 'unknown'})"
        )
    if result.get("environment"):
        forced.append(f"Environment: {result['environment']}")

    used = _utf8_size("\n".join(forced))
    if used > max_bytes:
        return _minimal(result, max_bytes)

    out = list(forced)
    optional: list[tuple[str, list[str]]] = []

    metrics = _render_metrics(result)
    if metrics:
        optional.append(("Key metrics", metrics))
    briefing = _render_briefing_prose(result)
    if briefing:
        optional.append(("Briefing", briefing))
    trends = [str(t) for t in (result.get("trends") or [])]
    if trends:
        optional.append(("Trends", trends))
    categorical = _render_categorical(result)
    if categorical:
        optional.append(("Categorical", categorical))

    for header, lines in optional:
        block = header + "\n" + "\n".join("  - " + l for l in lines)
        cost = _utf8_size(block) + 1
        if used + cost <= max_bytes:
            out.append(block)
            used += cost
            continue
        kept, omitted = _fit_lines(
            ["  - " + l for l in lines], max_bytes - used - _utf8_size(header) - 4
        )
        if kept:
            out.append(header)
            out.extend(kept)
            used += _utf8_size(header) + _utf8_size("\n".join(kept)) + 4
        if omitted:
            out.append(f"{TRUNCATED_MARKER} (Omitted rows: {omitted})")
            break

    return "\n".join(out)


def _render_metrics(result: dict) -> list[str]:
    metrics = result.get("metrics") or {}
    lines: list[str] = []
    for entry in metrics.get("latest_vs_prior") or []:
        if not isinstance(entry, dict):
            continue
        field = entry.get("field", "?")
        latest = entry.get("latest")
        prior = entry.get("prior")
        change = entry.get("change")
        pct = entry.get("change_percent")
        base = f"{field}: latest {latest}"
        if prior is not None:
            base += f" vs prior {prior}"
        if change is not None:
            base += f" (change {change:+,}"
            if pct is not None:
                base += f", {pct:+.2f}%"
            base += ")"
        lines.append(base)
    for name in sorted(metrics.get("fields") or {}):
        stats = metrics["fields"][name]
        if not isinstance(stats, dict):
            continue
        parts = [f"{key} {stats[key]}" for key in ("min", "max", "mean", "median", "sum") if key in stats]
        if "missing" in stats:
            parts.append(f"missing {stats['missing']}")
        lines.append(f"{name}: {', '.join(parts)}")
    return lines


def _render_briefing_prose(result: dict) -> list[str]:
    briefing = result.get("briefing")
    if not isinstance(briefing, dict) or not briefing.get("summary"):
        return []
    lines = [str(briefing["summary"])]
    for finding in briefing.get("key_findings") or []:
        if isinstance(finding, str) and finding.strip():
            lines.append(finding.strip())
    return lines


def _render_categorical(result: dict) -> list[str]:
    breakdowns = (result.get("metrics") or {}).get("categorical") or {}
    lines: list[str] = []
    for field, counts in breakdowns.items():
        if not isinstance(counts, dict):
            continue
        top = ", ".join(f"{k} {v}" for k, v in list(counts.items())[:8])
        lines.append(f"{field}: {top}")
    return lines


def _render_warnings(result: dict) -> list[str]:
    return [str(w) for w in (result.get("warnings") or []) if str(w).strip()]


def _render_pagination(result: dict) -> str:
    total = result.get("total_records")
    source = result.get("pagination_source")
    parts = []
    if total is not None:
        parts.append(f"{total} total records")
    if source:
        parts.append(source)
    if result.get("may_have_more") is not None:
        parts.append(
            f"more pages: {'yes' if result['may_have_more'] else 'no'}"
        )
    if result.get("next_offset") is not None:
        parts.append(f"next_offset {result['next_offset']}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# SEC filing-style results: header + byte-safe truncated text
# ---------------------------------------------------------------------------


def _is_text_result(result: dict) -> bool:
    for key in _TEXT_KEYS:
        value = result.get(key)
        if isinstance(value, str) and len(value) > 400:
            return True
    return False


def _render_text_result(result: dict, max_bytes: int) -> str:
    header_lines = []
    ticker = result.get("ticker")
    if ticker:
        header_lines.append(f"Ticker: {ticker}")
    for key in ("form_type", "item", "statement_type", "concept_searched"):
        if result.get(key):
            header_lines.append(f"{key}: {result[key]}")
    if result.get("filed"):
        header_lines.append(f"Filed: {result['filed']}")
    if result.get("source"):
        header_lines.append(f"Source: {result['source']}")
    header = "\n".join(header_lines)
    header_cost = _utf8_size(header) + 1
    if header_cost > max_bytes:
        return _minimal(result, max_bytes)

    body_keys = [k for k in _TEXT_KEYS if isinstance(result.get(k), str)]
    budget_for_text = max_bytes - header_cost - 2
    if budget_for_text <= 0:
        return header
    body_lines: list[str] = []
    for key in body_keys:
        text = result[key]
        if not text.strip():
            continue
        label = "" if key == "text" and len(body_keys) == 1 else f"{key}: "
        body_lines.append(label + text)
    body = "\n\n".join(body_lines)
    if body:
        body = _truncate_bytes(body, budget_for_text)
    return header + "\n\n" + body if body else header


# ---------------------------------------------------------------------------
# Generic structured results (fundamentals, XBRL facts, catalog, etc.)
# ---------------------------------------------------------------------------


def _render_generic(result: dict, max_bytes: int) -> str:
    lines: list[str] = []
    used = 0
    omitted: list[str] = []

    def _add(text: str) -> bool:
        nonlocal used
        cost = _utf8_size(text) + 1
        if used + cost > max_bytes:
            return False
        lines.append(text)
        used += cost
        return True

    for key in ("ticker", "source", "concept_searched"):
        value = result.get(key)
        if value is not None:
            _add(f"{key}: {_cell(value)}")

    for key, value in result.items():
        if value is None or key in ("ticker", "source", "concept_searched"):
            continue
        if isinstance(value, list):
            kept = 0
            for item in value:
                line = "  - " + _cell(item if not isinstance(item, dict) else _summarize_dict(item))
                if not _add(line):
                    omitted.append(key)
                    break
                kept += 1
            if kept == 0 and not value:
                _add(f"{key}: none")
        elif isinstance(value, dict):
            rendered = ", ".join(f"{k}: {_cell(v)}" for k, v in value.items())
            if not _add(f"{key}: {rendered}"):
                _add(f"{key}: {TRUNCATED_MARKER}")
        else:
            if not _add(f"{key}: {_cell(value)}"):
                _add(f"{key}: {TRUNCATED_MARKER}")

    if omitted:
        _add(f"{TRUNCATED_MARKER} (Omitted rows: {len(omitted)} in {', '.join(omitted)})")
    if not lines:
        return _minimal(result, max_bytes)
    return "\n".join(lines)


def _summarize_dict(item: dict) -> str:
    parts = []
    for key in ("dataset", "group", "name", "description", "concept", "value", "period_end"):
        if key in item and item[key] not in (None, ""):
            parts.append(f"{key} {_cell(item[key])}")
    if not parts:
        return ", ".join(f"{k} {_cell(v)}" for k, v in list(item.items())[:6])
    return ", ".join(parts)
