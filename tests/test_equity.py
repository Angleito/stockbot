"""Equity render regressions: upside percent units + pairwise SP500 alignment."""

import pytest

from app.equity import (
    build_equity_report,
    financial_data_from_evidence,
    from_evidence,
    render_report_markdown,
    sp500_relative_changes,
)


def _valuation_body(ticker: str, synthesis: dict[str, object]) -> str:
    report = build_equity_report(ticker, {"synthesis": synthesis})
    sections = report.get("sections")
    assert isinstance(sections, list)
    section = next(s for s in sections if isinstance(s, dict) and s.get("id") == "valuation")
    assert isinstance(section, dict)
    body = section.get("body_markdown")
    assert isinstance(body, str)
    return body


def test_upside_ratio_renders_as_percent() -> None:
    body = _valuation_body("AAPL", {"target_price": 185.7, "upside": 0.857})
    assert "- Upside: 85.70%." in body
    assert "0.86%" not in body


def test_sp500_skips_bad_tick_pairwise() -> None:
    result = sp500_relative_changes([100.0, "bad", 110.0], [100.0, 105.0, 115.0])
    ticker_pct = result.get("ticker_pct")
    index_pct = result.get("sp500_pct")
    assert isinstance(ticker_pct, list) and isinstance(index_pct, list)
    assert len(ticker_pct) == 2
    assert ticker_pct[1] == pytest.approx(10.0)
    assert index_pct[1] == pytest.approx(15.0)


def test_from_evidence_maps_snapshot_keys() -> None:
    engine = from_evidence(
        {"price": {"last": 213.05}, "shares_outstanding": 1000, "ttm_gaap_eps": 5.0},
        peer_multiples={"AAA": 10.0, "BAD": -3.0},
        ev_ebitda_history=[8.0, 10.0],
    )
    assert engine.financial_data["current_price"] == 213.05
    assert engine.financial_data["shares_outstanding"] == 1000
    assert engine.financial_data["ev_ebitda_history"] == [8.0, 10.0]
    assert set(engine.peer_data) == {"AAA"}
    assert from_evidence(None).financial_data["current_price"] == 0.0


def test_financial_data_from_evidence_maps_fcf() -> None:
    data = financial_data_from_evidence(
        {"price": {"last": 213.05}, "shares_outstanding": 1000},
        {"safety": {"ttm_fcf": 3200.0}},
    )
    assert data["current_price"] == 213.05
    assert data["shares_outstanding"] == 1000
    assert data["free_cash_flow"] == 3200.0
    assert data["ebitda"] is None


def test_explicit_ebitda_fcf_overrides_win() -> None:
    engine = from_evidence(
        {"price": {"last": 100.0}, "shares_outstanding": 1000},
        dividend_fundamentals={"safety": {"ttm_fcf": 3200.0}},
        ebitda=500.0,
        free_cash_flow=100.0,
        ev_ebitda_history=[10.0],
    )
    assert engine.financial_data["ebitda"] == 500.0
    assert engine.financial_data["free_cash_flow"] == 100.0
    assert engine.synthesize_valuation()["methods_used"] != []


def test_zero_confidence_bands_render_not_available() -> None:
    engine = from_evidence({"price": {"last": 100.0}, "shares_outstanding": 1000})
    synthesis = engine.synthesize_valuation()
    assert synthesis["methods_used"] == []
    report = build_equity_report(
        "AAPL", {"synthesis": synthesis, "football_field": engine.generate_football_field_data()}
    )
    sections = report.get("sections")
    assert isinstance(sections, list)
    section = next(s for s in sections if isinstance(s, dict) and s.get("id") == "valuation")
    assert isinstance(section, dict)
    assert section.get("body_markdown") == "Valuation not available (no evidence supplied)."


def test_report_markdown_carries_upside_percent() -> None:
    report = build_equity_report("AAPL", {"synthesis": {"target_price": 185.7, "upside": 0.857}})
    assert "85.70%" in render_report_markdown(report)
