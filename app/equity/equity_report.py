"""Equity report section pipeline (FinRobot create_equity_report port).

Deterministic render target over caller-supplied evidence: pure functions,
plain dicts/lists in/out, stdlib only. No I/O, network, or LLM calls.
Evidence comes from existing tools (valuation metrics, SEC fundamentals,
analyst estimates, S&P 500 weight, price series); this module never fetches.
"""

from __future__ import annotations

from typing import TypedDict


class ReportSection(TypedDict):
    """One report section: stable id, display title, markdown body."""

    id: str
    title: str
    body_markdown: str


SECTION_TITLES: dict[str, str] = {
    "overview": "Overview",
    "financial_summary": "Financial Summary",
    "peer_comparison": "Peer Comparison",
    "valuation": "Valuation",
    "charts": "Charts",
}

SECTION_IDS = tuple(SECTION_TITLES)


def _as_dict(value: object) -> dict[str, object]:
    """Narrow untrusted evidence to a mapping ({} when absent/mistyped)."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    """Narrow untrusted evidence to a list ([] when absent/mistyped)."""
    return value if isinstance(value, list) else []


def _cell(value: object) -> str:
    """Compact scalar text ("" for None/dict/list)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _num(value: object) -> float | None:
    """Numeric evidence value (None when missing/non-numeric/bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _fmt(value: object, digits: int = 2) -> str:
    """Formatted number ("n/a" when not numeric)."""
    num = _num(value)
    return f"{num:.{digits}f}" if num is not None else "n/a"


def _not_available(section: str) -> str:
    """Fallback body: always contains the literal 'not available' marker."""
    return f"{section} not available (no evidence supplied)."


def _scalar_lines(data: dict[str, object]) -> list[str]:
    """Bulleted lines for scalar entries (nested values skipped)."""
    lines: list[str] = []
    for key in sorted(data):
        text = _cell(data[key])
        if text:
            lines.append(f"- {key}: {text}")
    return lines


def _overview_body(ticker: str, evidence: dict[str, object]) -> str:
    """Ticker + price + EPS + index-weight bullets (fallback when bare)."""
    valuation = _as_dict(evidence.get("valuation"))
    estimates = _as_dict(evidence.get("estimates"))
    weight = _as_dict(evidence.get("sp500_weight"))
    lines = [f"- Ticker: {ticker or 'unknown'}."]
    price = _as_dict(valuation.get("price")).get("last")
    if _num(price) is not None:
        lines.append(f"- Last price: {_fmt(price)}.")
    for key in ("ttm_gaap_eps", "trailing_pe"):
        if _num(valuation.get(key)) is not None:
            lines.append(f"- {key}: {_fmt(valuation.get(key))}.")
    as_of = estimates.get("as_of")
    if isinstance(as_of, str) and as_of.strip():
        lines.append(f"- Analyst estimates as of {as_of.strip()}.")
    if _num(weight.get("weight_pct")) is not None:
        rank = weight.get("rank")
        suffix = f" (rank {_cell(rank)})" if _cell(rank) else ""
        lines.append(f"- S&P 500 weight: {_fmt(weight.get('weight_pct'))}%{suffix}.")
    if len(lines) == 1:
        return _not_available("Overview")
    return "\n".join(lines)


def _financial_summary_body(ticker: str, evidence: dict[str, object]) -> str:
    """Scalar fundamentals rows (fallback when none)."""
    del ticker
    lines = _scalar_lines(_as_dict(evidence.get("fundamentals")))
    return "\n".join(lines) if lines else _not_available("Financial summary")


def _peer_block(evidence: dict[str, object]) -> dict[str, object] | list[object]:
    """Peer evidence from top level or nested under valuation ({} when absent)."""
    for source in (evidence, _as_dict(evidence.get("valuation"))):
        for key in ("peer_comparison", "peers", "peer_multiples"):
            block = source.get(key)
            if isinstance(block, (dict, list)) and block:
                return block
    return {}


def _peer_comparison_body(ticker: str, evidence: dict[str, object]) -> str:
    """Peer multiple rows (fallback when absent)."""
    del ticker
    block = _peer_block(evidence)
    if isinstance(block, list):
        lines = [f"- {_cell(row)}" for row in block if _cell(row)]
        return "\n".join(lines) if lines else _not_available("Peer comparison")
    lines = _scalar_lines(block)
    return "\n".join(lines) if lines else _not_available("Peer comparison")


def _range_line(synthesis: dict[str, object]) -> str:
    """Target-range line ("" when the range pair is not numeric)."""
    band = synthesis.get("range")
    pair: list[object] = list(band) if isinstance(band, (list, tuple)) else []
    if len(pair) == 2 and all(_num(v) is not None for v in pair):
        return f"- Range: {_fmt(pair[0])} - {_fmt(pair[1])}."
    return ""


def _usable_methods(synthesis: dict[str, object]) -> list[str]:
    """Method names the synthesis actually used ([] when none/degenerate)."""
    return [m for m in (_cell(m) for m in _as_list(synthesis.get("methods_used"))) if m]


def _football_lines(field: dict[str, object], usable: list[str]) -> list[str]:
    """One low/mid/high line per used method with mid > 0 (zero bands skipped)."""
    wanted = set(usable)
    lines: list[str] = []
    for method in sorted(field):
        if method == "current_price" or (wanted and method not in wanted):
            continue
        band = _as_dict(field.get(method))
        mid = _num(band.get("mid"))
        if mid is None or mid <= 0:
            continue
        if all(_num(band.get(k)) is not None for k in ("low", "mid", "high")):
            lines.append(
                f"- {method}: low {_fmt(band.get('low'))} / "
                f"mid {_fmt(band.get('mid'))} / high {_fmt(band.get('high'))}."
            )
    return lines


def _valuation_body(ticker: str, evidence: dict[str, object]) -> str:
    """Synthesis target/range/upside plus football-field bands (fallback when bare)."""
    del ticker
    synthesis = _as_dict(evidence.get("synthesis"))
    field = _as_dict(evidence.get("football_field"))
    valuation = _as_dict(evidence.get("valuation"))
    usable = _usable_methods(synthesis)
    target = _num(synthesis.get("target_price"))
    lines: list[str] = []
    if target is not None and target > 0:
        lines.append(f"- Target price: {_fmt(target)}.")
        range_line = _range_line(synthesis)
        if range_line:
            lines.append(range_line)
        upside = _num(synthesis.get("upside"))
        if upside is not None:
            lines.append(f"- Upside: {_fmt(upside * 100.0)}%.")
    if usable:
        lines.append(f"- Methods: {', '.join(usable)}.")
    lines.extend(_football_lines(field, usable))
    if _num(valuation.get("trailing_pe")) is not None:
        lines.append(f"- Trailing P/E: {_fmt(valuation.get('trailing_pe'), 1)}.")
    if not lines:
        return _not_available("Valuation")
    return "\n".join(lines)


def _charts_body(ticker: str, evidence: dict[str, object]) -> str:
    """Available series inventory (fallback when no series supplied)."""
    del ticker
    series = _as_dict(evidence.get("series"))
    lines: list[str] = []
    for key in sorted(series):
        values = series[key]
        if isinstance(values, list):
            lines.append(f"- {key}: {len(values)} points.")
    return "\n".join(lines) if lines else _not_available("Charts")


_BUILDERS = {
    "overview": _overview_body,
    "financial_summary": _financial_summary_body,
    "peer_comparison": _peer_comparison_body,
    "valuation": _valuation_body,
    "charts": _charts_body,
}


def _build_section(section_id: str, ticker: str, evidence: dict[str, object]) -> ReportSection:
    """One section envelope (fallback body on bad input, never raises)."""
    title = SECTION_TITLES[section_id]
    try:
        body = _BUILDERS[section_id](ticker, evidence)
    except Exception:  # noqa: BLE001 - evidence is untrusted, fallback never aborts
        body = _not_available(title)
    if not isinstance(body, str) or not body.strip():
        body = _not_available(title)
    return {"id": section_id, "title": title, "body_markdown": body.strip()}


def build_equity_report(ticker: str, evidence: dict[str, object] | None) -> dict[str, object]:
    """Assemble the five fixed sections from evidence (never raises)."""
    name = ticker.strip().upper() if isinstance(ticker, str) else ""
    ev = _as_dict(evidence)
    charts = dict(_as_dict(ev.get("charts")))
    charts.setdefault("series", _as_dict(ev.get("series")))
    return {
        "ticker": name,
        "sections": [_build_section(section_id, name, ev) for section_id in SECTION_IDS],
        "football_field": _as_dict(ev.get("football_field")),
        "charts": charts,
        "synthesis": _as_dict(ev.get("synthesis")),
    }


def render_report_markdown(report: object) -> str:
    """Concatenate sections with H2 headers (never raises)."""
    doc = _as_dict(report)
    ticker = doc.get("ticker")
    head = f"# {ticker} Equity Report" if isinstance(ticker, str) and ticker.strip() else "# Equity Report"
    parts = [head]
    for item in _as_list(doc.get("sections")):
        section = _as_dict(item)
        title = section.get("title")
        label = title.strip() if isinstance(title, str) and title.strip() else "Section"
        body = section.get("body_markdown")
        text = body.strip() if isinstance(body, str) and body.strip() else "not available"
        parts.append(f"## {label}\n{text}")
    return "\n\n".join(parts)
