"""Dividend safety tests (Phase 5): FCF-based coverage on SEC inputs only."""

import pytest

from app.normalization import normalize_sec_company_facts, normalize_sec_tickers
from app.services import sec_facts
from app.services.sec_facts import _assemble_dividend_safety
from app.storage import parquet
import app.valuation as valuation

KO_CIK = 21344
RETRIEVED_AT = "2026-08-01T00:00:00Z"
AS_OF = "2026-08-10"

DPS_TAG = "CommonStockDividendsPerShareDeclared"
EPS_TAG = "EarningsPerShareDiluted"
OCF_TAG = "NetCashProvidedByUsedInOperatingActivities"
CAPX_TAG = "PaymentsToAcquirePropertyPlantAndEquipment"
PAID_TAG = "PaymentsOfDividendsCommonStock"
CASH_TAG = "CashAndCashEquivalentsAtCarryingValue"
DEBT_TAG = "LongTermDebtCurrentAndNoncurrent"
INCOME_TAG = "NetIncomeLoss"

# Four contiguous recent quarters (Q3'25-Q2'26); latest end 2026-06-30.
QUARTERS = [
    ("2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-28"),
    ("2025-10-01", "2025-12-31", 2025, "Q4", "2026-02-10"),
    ("2026-01-01", "2026-03-31", 2026, "Q1", "2026-04-28"),
    ("2026-04-01", "2026-06-30", 2026, "Q2", "2026-07-28"),
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(sec_facts, "DEFAULT_DATA_ROOT", tmp_path)
    return tmp_path


def _seed_ticker(tmp_path, cik, ticker):
    datasets = normalize_sec_tickers(
        {"0": {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}},
        retrieved_at=RETRIEVED_AT, content_hash=f"tickers-{cik}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _qfact(val, start, end, fy, fp, filed, accn):
    return {"start": start, "end": end, "val": val, "accn": accn,
            "fy": fy, "fp": fp, "filed": filed}


def _seed_concepts(tmp_path, cik, concepts, suffix):
    """Seed canonical + per-share facts through normalization (unit-aware)."""
    payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {
        "us-gaap": {tag: {"units": units} for tag, units in concepts.items()},
    }}
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash=f"safety-{suffix}-{cik}",
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        source_record_id=f"safety-{suffix}-cik{cik:010d}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _quarters(tag_values, prefix):
    """tag -> unit -> quarterly facts zipped over QUARTERS."""
    out = {}
    for tag, unit, values in tag_values:
        out[tag] = {unit: [
            _qfact(v, s, e, fy, fp, filed, f"{prefix}-{tag}-{fp}{fy}")
            for v, (s, e, fy, fp, filed) in zip(values, QUARTERS)
        ]}
    return out


def _seed_healthy(tmp_path, *, dps=(0.51,) * 4, eps=(0.80,) * 4,
                  ocf=(1000.0,) * 4, capx=(-200.0,) * 4, paid=(-260.0,) * 4):
    _seed_ticker(tmp_path, KO_CIK, "KO")
    _seed_concepts(tmp_path, KO_CIK,
                   _quarters([(DPS_TAG, "USD/shares", dps), (EPS_TAG, "USD/shares", eps),
                              (OCF_TAG, "USD", ocf), (CAPX_TAG, "USD", capx),
                              (PAID_TAG, "USD", paid)], "q"),
                   "quarters")
    fy = {
        OCF_TAG: {"USD": [
            _qfact(3500.0, "2024-01-01", "2024-12-31", 2024, "FY", "2025-02-10", "fy-ocf-2024"),
            _qfact(3800.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "fy-ocf-2025"),
        ]},
        CAPX_TAG: {"USD": [
            _qfact(-700.0, "2024-01-01", "2024-12-31", 2024, "FY", "2025-02-10", "fy-capx-2024"),
            _qfact(-750.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "fy-capx-2025"),
        ]},
        PAID_TAG: {"USD": [
            _qfact(-900.0, "2024-01-01", "2024-12-31", 2024, "FY", "2025-02-10", "fy-paid-2024"),
            _qfact(-1000.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "fy-paid-2025"),
        ]},
        INCOME_TAG: {"USD": [
            _qfact(2500.0, "2024-01-01", "2024-12-31", 2024, "FY", "2025-02-10", "fy-ni-2024"),
            _qfact(2800.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "fy-ni-2025"),
        ]},
    }
    _seed_concepts(tmp_path, KO_CIK, fy, "fy")
    _seed_concepts(tmp_path, KO_CIK, {
        DPS_TAG: {"USD/shares": [
            _qfact(1.90, "2024-01-01", "2024-12-31", 2024, "FY", "2025-02-10", "fy-dps-2024"),
            _qfact(2.00, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "fy-dps-2025"),
        ]},
        CASH_TAG: {"USD": [
            {"end": "2026-06-30", "val": 5000.0, "accn": "cash-2026",
             "fy": 2026, "fp": "Q2", "filed": "2026-07-28"},
        ]},
        DEBT_TAG: {"USD": [
            {"end": "2025-06-30", "val": 8000.0, "accn": "debt-2025",
             "fy": 2025, "fp": "Q2", "filed": "2025-07-28"},
            {"end": "2026-06-30", "val": 9000.0, "accn": "debt-2026",
             "fy": 2026, "fp": "Q2", "filed": "2026-07-28"},
        ]},
    }, "fy-div-cash-debt")


def _fail_on_price(monkeypatch):
    def _boom(ticker):
        raise AssertionError("historical dividend query must not call Yahoo")
    monkeypatch.setattr(valuation, "get_live_quote", _boom)


def _flags(safety):
    return {f["flag"]: f for f in safety["risk_flags"]}


def test_healthy_safety_ratios(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_healthy(store)
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["data_source"] == "store"
    safety = result["safety"]
    assert safety["methodology"] == "common-stock EPS/FCF basis; not AFFO/FFO"
    assert safety["ttm_fcf"] == 3200.0
    assert safety["ttm_dividends_paid"] == 1040.0
    assert safety["earnings_payout_ratio"] == 0.6375
    assert safety["fcf_payout_ratio"] == 0.325
    assert safety["fcf_coverage"] == 3.0769
    assert safety["cash_to_annual_dividend"] == 4.8077
    assert safety["interest_coverage"] is None
    assert safety["interest_coverage_reason"]
    assert safety["debt_up_yoy"] is True
    flags = _flags(safety)
    assert flags["fcf_declined_yoy"]["status"] is False
    assert flags["fcf_payout_expanded"]["status"] is False
    assert flags["eps_declined_yoy"]["status"] is False
    assert flags["leverage_rising"]["status"] is True
    assert flags["high_absolute_yield"]["status"] is None
    assert flags["growth_decelerating"]["status"] is None
    assert safety["dividend_vs_fcf_growth_5y"]["verdict"] == "insufficient_data"


def test_negative_eps_nulls_payout_with_flag(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_healthy(store, eps=(-0.50,) * 4)
    safety = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)["safety"]
    assert safety["earnings_payout_ratio"] is None
    assert safety["earnings_payout_ratio_reason"]
    assert _flags(safety)["negative_eps"]["status"] is True
    # FCF leg is unaffected.
    assert safety["fcf_payout_ratio"] == 0.325
    assert safety["fcf_coverage"] == 3.0769


def test_negative_fcf_nulls_payout_and_coverage_with_flag(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_healthy(store, ocf=(100.0,) * 4, capx=(-500.0,) * 4)
    safety = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)["safety"]
    assert safety["ttm_fcf"] == -1600.0
    assert safety["fcf_payout_ratio"] is None
    assert safety["fcf_coverage"] is None
    assert _flags(safety)["negative_or_zero_fcf"]["status"] is True
    assert safety["earnings_payout_ratio"] == 0.6375


def test_zero_dividend_nulls_coverage_with_flag(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_healthy(store, dps=(0.0,) * 4, paid=(0.0,) * 4)
    safety = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)["safety"]
    assert safety["ttm_dividends_paid"] == 0.0
    assert safety["fcf_payout_ratio"] == 0.0
    assert safety["fcf_coverage"] is None
    assert _flags(safety)["zero_dividend"]["status"] is True
    assert safety["cash_to_annual_dividend"] is None


def test_payout_expanding_verdict(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_ticker(store, KO_CIK, "KO")
    _seed_concepts(store, KO_CIK, {
        DPS_TAG: {"USD/shares": [
            _qfact(1.00, "2020-01-01", "2020-12-31", 2020, "FY", "2021-02-10", "v-dps-2020"),
            _qfact(2.00, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "v-dps-2025"),
        ]},
        OCF_TAG: {"USD": [
            _qfact(3500.0, "2020-01-01", "2020-12-31", 2020, "FY", "2021-02-10", "v-ocf-2020"),
            _qfact(3800.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "v-ocf-2025"),
        ]},
        CAPX_TAG: {"USD": [
            _qfact(-500.0, "2020-01-01", "2020-12-31", 2020, "FY", "2021-02-10", "v-capx-2020"),
            _qfact(-750.0, "2025-01-01", "2025-12-31", 2025, "FY", "2026-02-10", "v-capx-2025"),
        ]},
    }, "verdict")
    safety = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)["safety"]
    comp = safety["dividend_vs_fcf_growth_5y"]
    assert comp["dividend_cagr"] == 0.1487
    assert comp["fcf_cagr"] == 0.0033
    assert comp["verdict"] == "payout_expanding"


def test_high_absolute_yield_flag_unit():
    base = {"ttm_dividend_per_share": 2.0, "growth_1y": 0.05, "growth_5y_cagr": 0.10}
    assert _flags(_assemble_dividend_safety([], base, ttm_yield=0.068))["high_absolute_yield"]["status"] is True
    assert _flags(_assemble_dividend_safety([], base, ttm_yield=0.03))["high_absolute_yield"]["status"] is False
    assert _flags(_assemble_dividend_safety([], base))["high_absolute_yield"]["status"] is None
