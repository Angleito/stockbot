"""Equity bounded context: valuation engine, charting, equity report (stdlib only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .equity_report import build_equity_report, render_report_markdown

if TYPE_CHECKING:
    from .charting import pe_eps_points, render_markdown_table, render_svg_series, sp500_relative_changes
    from .valuation_engine import ValuationEngine, ValuationResult, financial_data_from_evidence, from_evidence

__all__ = [
    "ValuationEngine",
    "ValuationResult",
    "build_equity_report",
    "financial_data_from_evidence",
    "from_evidence",
    "pe_eps_points",
    "render_markdown_table",
    "render_report_markdown",
    "render_svg_series",
    "sp500_relative_changes",
]

_LAZY: dict[str, tuple[str, str]] = {
    "ValuationEngine": (".valuation_engine", "ValuationEngine"),
    "ValuationResult": (".valuation_engine", "ValuationResult"),
    "financial_data_from_evidence": (".valuation_engine", "financial_data_from_evidence"),
    "from_evidence": (".valuation_engine", "from_evidence"),
    "sp500_relative_changes": (".charting", "sp500_relative_changes"),
    "pe_eps_points": (".charting", "pe_eps_points"),
    "render_svg_series": (".charting", "render_svg_series"),
    "render_markdown_table": (".charting", "render_markdown_table"),
}


def __getattr__(name: str) -> object:
    """Lazy sibling re-export (sibling modules land independently; no cycle)."""
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    import importlib

    value: object = getattr(importlib.import_module(module_name, __name__), attr)
    globals()[name] = value
    return value
