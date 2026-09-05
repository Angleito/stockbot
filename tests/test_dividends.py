"""Offline dividend fundamentals tests (app/services/sec_facts.py metric "dividends").

Seed a tmp parquet store through the real normalizers, then exercise the
store-first path: mixed quarterly/YTD/FY/restated fixtures, exact-gap
growth/CAGR, TTM yield off a stubbed live price, null-instead-of-error
behavior for non-payers, dispatch, and rendering. Live paths are
monkeypatched at the edgar_client / valuation seams.
"""

import pytest

import app.edgar_client as edgar_client
import app.valuation as valuation
from app.normalization import (
    COMPANY_FACTS_PARSER_VERSION,
    DIVIDEND_PER_SHARE_CONCEPT,
    normalize_sec_company_facts,
    normalize_sec_tickers,
)
from app.policy import LOCAL_CONTEXT
from app.services import sec_facts
from app.storage import parquet
from app.tools import TOOLS, execute_tool
from app.tool_render import render_tool_result

KO_CIK = 21344
RETRIEVED_AT = "2026-08-01T00:00:00Z"
AS_OF = "2026-08-10"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated data root for every service query."""
    monkeypatch.setattr(sec_facts, "DEFAULT_DATA_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def priced(monkeypatch):
    """Stub the live-price seam shared by the store and live paths."""
    monkeypatch.setattr(valuation, "get_live_price", lambda ticker: 65.0)
    return valuation


def _seed_ticker(tmp_path, cik, ticker):
    datasets = normalize_sec_tickers(
        {"0": {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}},
        retrieved_at=RETRIEVED_AT, content_hash=f"tickers-{cik}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _div_fact(val, start, end, fy, fp, filed, accn):
    return {"start": start, "end": end, "val": val, "accn": accn,
            "fy": fy, "fp": fp, "filed": filed}


def _seed_dividends(tmp_path, cik, facts, distractors=()):
    units = {"USD/shares": list(facts)}
    for unit, extra in distractors:
        units.setdefault(unit, []).extend(extra)
    payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {
        "us-gaap": {DIVIDEND_PER_SHARE_CONCEPT: {"units": units}},
    }}
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash=f"div-facts-{cik}",
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        source_record_id=f"cik{cik:010d}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


# KO-style calendar fixture: restated Q1, YTD distractors, explicit Q4,
# wrong-unit and paid-concept distractors, one next-year quarter.
_KO_FACTS = [
    _div_fact(0.50, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-29", "k1"),
    _div_fact(0.51, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-05-06", "k2"),
    _div_fact(1.02, "2025-01-01", "2025-06-30", 2025, "Q2", "2025-07-29", "k3"),  # 6-mo YTD
    _div_fact(0.51, "2025-04-01", "2025-06-30", 2025, "Q2", "2025-07-29", "k4"),
    _div_fact(1.53, "2025-01-01", "2025-09-30", 2025, "Q3", "2025-10-28", "k5"),  # 9-mo YTD
    _div_fact(0.51, "2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-28", "k6"),
    _div_fact(0.51, "2025-10-01", "2025-12-31", 2025, "Q4", "2026-02-10", "k7"),
    _div_fact(0.54, "2026-01-01", "2026-03-31", 2026, "Q1", "2026-04-28", "k8"),
]
_KO_DISTRACTORS = [
    ("USD", [_div_fact(999.0, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-29", "x1")]),
]
_KO_PAID = {"cik": KO_CIK, "entityName": "KO", "facts": {"us-gaap": {
    "CommonStockDividendsPaid": {"units": {"USD": [
        _div_fact(999.0, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-29", "x2"),
    ]}},
}}}


def _seed_ko(tmp_path):
    _seed_ticker(tmp_path, KO_CIK, "KO")
    _seed_dividends(tmp_path, KO_CIK, _KO_FACTS, _KO_DISTRACTORS)


# NVDA-style dates: Q4 exists only as an FY total, so the trailing quarter
# must be derived as FY_total - YTD_through_Q3 (mirrors test_sec_facts NVDA).
_NVDA_DIV_FACTS = [
    _div_fact(0.50, "2025-01-27", "2025-04-27", 2026, "Q1", "2025-05-28", "d1"),
    _div_fact(0.51, "2025-01-27", "2025-04-27", 2026, "Q1", "2025-05-28", "d2"),
    _div_fact(1.02, "2025-01-27", "2025-07-27", 2026, "Q2", "2025-08-27", "d3"),  # 6-mo YTD
    _div_fact(0.51, "2025-04-28", "2025-07-27", 2026, "Q2", "2025-08-27", "d4"),
    _div_fact(1.53, "2025-01-27", "2025-10-26", 2026, "Q3", "2025-11-19", "d5"),  # 9-mo YTD
    _div_fact(0.51, "2025-07-28", "2025-10-26", 2026, "Q3", "2025-11-19", "d6"),
    _div_fact(2.04, "2025-01-27", "2026-01-25", 2026, "FY", "2026-02-25", "d7"),  # Q4 only as FY
    _div_fact(0.54, "2026-01-26", "2026-04-26", 2027, "Q1", "2026-05-27", "d8"),
]


def _annual_facts(year, total):
    """Four ex-dividend quarters splitting an annual total."""
    ends = [(f"{year}-01-01", f"{year}-03-31", "Q1"),
            (f"{year}-04-01", f"{year}-06-30", "Q2"),
            (f"{year}-07-01", f"{year}-09-30", "Q3"),
            (f"{year}-10-01", f"{year}-12-31", "Q4")]
    return [_div_fact(round(total / 4, 4), start, end, year, fp,
                      f"{year}-12-15", f"y{year}{fp}")
            for start, end, fp in ends]


_ANNUAL_TOTALS = {2015: 1.00, 2016: 1.05, 2017: 1.10, 2018: 1.16, 2019: 1.22,
                  2020: 1.25, 2021: 1.32, 2022: 1.50, 2023: 1.70, 2024: 1.90,
                  2025: 2.00}


def _seed_growth(tmp_path, skip_years=()):
    _seed_ticker(tmp_path, KO_CIK, "KO")
    facts = [f for year, total in _ANNUAL_TOTALS.items() if year not in skip_years
             for f in _annual_facts(year, total)]
    _seed_dividends(tmp_path, KO_CIK, facts)


def test_concept_parity_and_parser_bump():
    assert DIVIDEND_PER_SHARE_CONCEPT == "CommonStockDividendsPerShareDeclared"
    assert sec_facts.DIVIDEND_PER_SHARE_CONCEPT == DIVIDEND_PER_SHARE_CONCEPT
    assert edgar_client._DIVIDEND_CONCEPT == DIVIDEND_PER_SHARE_CONCEPT
    assert COMPANY_FACTS_PARSER_VERSION == "sec-companyfacts-v4"


def test_wrong_unit_and_paid_concept_rejected():
    out = normalize_sec_company_facts(
        _KO_PAID, retrieved_at=RETRIEVED_AT, content_hash="paid",
        source_url="u", source_record_id="r")
    assert out["financial_facts"] == []


def test_mixed_durations_restatement_no_double_count(store, priced):
    _seed_ko(store)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["data_source"] == "store"
    # Restated Q1 wins; YTD/FY rows never double-count the TTM sum.
    assert result["ttm_dividend_per_share"] == 2.07
    assert result["ttm_dividend_yield"] == 0.0318
    assert result["annual_history"] == [{"fiscal_year": 2025, "dividend_per_share": 2.04}]


def test_derived_q4_uses_fy_minus_ytd(store, priced):
    _seed_ticker(store, KO_CIK, "KO")
    _seed_dividends(store, KO_CIK, _NVDA_DIV_FACTS)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["ttm_dividend_per_share"] == 2.07
    assert result["annual_history"] == [{"fiscal_year": 2026, "dividend_per_share": 2.04}]


def test_exact_gap_growth_and_cagr(store, priced):
    _seed_growth(store)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["ttm_dividend_per_share"] == 2.00
    assert result["ttm_dividend_yield"] == 0.0308
    assert result["growth_1y"] == 0.0526
    assert result["growth_3y_cagr"] == 0.1006
    assert result["growth_5y_cagr"] == 0.0986
    assert result["growth_10y_cagr"] == 0.0718
    years = [row["fiscal_year"] for row in result["annual_history"]]
    assert years == sorted(years, reverse=True) == list(range(2025, 2014, -1))


def test_missing_comparison_year_yields_null(store, priced):
    _seed_growth(store, skip_years=(2022,))
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["growth_1y"] == 0.0526
    assert result["growth_3y_cagr"] is None
    assert result["growth_5y_cagr"] == 0.0986
    assert result["growth_10y_cagr"] == 0.0718


@pytest.mark.parametrize("price", [None, 0, -3.0])
def test_unusable_price_yields_null_with_history_intact(store, monkeypatch, price):
    _seed_ko(store)
    monkeypatch.setattr(valuation, "get_live_price", lambda ticker: price)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["ttm_dividend_per_share"] == 2.07
    assert result["ttm_dividend_yield"] is None
    assert result["annual_history"] == [{"fiscal_year": 2025, "dividend_per_share": 2.04}]


def test_live_fallback_when_store_empty(store, monkeypatch):
    _seed_ticker(store, KO_CIK, "KO")  # resolved entity, no dividend facts
    calls = []

    def _live(ticker, metric):
        calls.append((ticker, metric))
        return {"ticker": ticker, "ttm_dividend_per_share": 2.07,
                "ttm_dividend_yield": 0.0318, "growth_1y": None,
                "growth_3y_cagr": None, "growth_5y_cagr": None,
                "growth_10y_cagr": None,
                "annual_history": [{"fiscal_year": 2025, "dividend_per_share": 2.04}],
                "source": edgar_client._DIVIDEND_SOURCE}

    monkeypatch.setattr(sec_facts.edgar_client, "get_fundamentals", _live)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["data_source"] == "live"
    assert result["requested_as_of"] == AS_OF
    assert result["ttm_dividend_per_share"] == 2.07
    assert calls == [("KO", "dividends")]


def test_nonpayer_reports_nulls_not_error(store, monkeypatch):
    _seed_ticker(store, KO_CIK, "KO")
    monkeypatch.setattr(sec_facts.edgar_client, "get_fundamentals",
                        lambda ticker, metric: edgar_client._null_dividend_payload(ticker))
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["data_source"] == "live"
    assert result["ttm_dividend_per_share"] is None
    assert result["ttm_dividend_yield"] is None
    assert result["growth_1y"] is None
    assert result["annual_history"] == []


def test_unknown_metric_still_errors(store):
    result = sec_facts.get_fundamentals("KO", "bogus", as_of=AS_OF)
    assert result["error"] == "Unknown metric 'bogus'"


def test_tool_schema_and_dispatch(store, priced):
    schema = next(item for item in TOOLS if item["function"]["name"] == "get_fundamentals")
    assert "dividends" in schema["function"]["parameters"]["properties"]["metric"]["enum"]
    _seed_ko(store)
    result = execute_tool(
        "get_fundamentals", {"ticker": "KO", "metric": "dividends", "as_of": AS_OF},
        model="test", context=LOCAL_CONTEXT,
    )
    assert result["source"] == "sec"
    assert result["metric"] == "dividends"
    assert result["data_source"] == "store"
    assert result["ticker"] == "KO"
    assert result["ttm_dividend_per_share"] == 2.07
    assert result["ttm_dividend_yield"] == 0.0318
    assert set(result) >= {"ticker", "ttm_dividend_per_share", "ttm_dividend_yield",
                           "growth_1y", "growth_3y_cagr", "growth_5y_cagr",
                           "growth_10y_cagr", "annual_history"}


def test_render_annual_history(store, priced):
    _seed_ko(store)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    text = render_tool_result(result)
    assert text.startswith("KO dividends [store] as of 2026-08-10")
    assert "ttm_dividend_per_share: 2.07" in text
    assert "- 2025: dividend 2.04" in text
