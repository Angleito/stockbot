"""Unit tests for the obligation-aware valuation metrics.

Deterministic and offline: price, consensus, EPS, and obligations inputs
are injected via monkeypatch; nothing touches the network or cache.db.
"""

from unittest.mock import MagicMock

import pytest

from app import valuation


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, ttl=None):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def _estimates():
    return {
        "as_of": "2026-08-26T00:00:00Z",
        "quote": {"price": 213.05, "currency": "USD"},
        "shares_outstanding": 24_221_000_000,
        "forward_estimates": [
            {
                "period": "current_quarter",
                "period_end_date": "2026-07-31",
                "eps_avg": 2.09,
            },
            {
                "period": "next_quarter",
                "period_end_date": "2026-10-31",
                "eps_avg": 2.37,
            },
            {
                "period": "current_fiscal_year",
                "period_end_date": "2027-01-31",
                "eps_avg": 9.02,
            },
            {
                "period": "next_fiscal_year",
                "period_end_date": "2028-01-31",
                "eps_avg": 13.04,
            },
        ],
    }


def _obligations():
    return {
        "obligations": [
            {
                "type": "supply_commitments",
                "amount_billions": 119.0,
                "certainty": "contingent",
                "status": "future_cash_obligation",
                "revenue_matched": True,
                "payment_horizon": {
                    "paid_in_remainder_of_fy": "2027",
                    "paid_in_remainder_billions": 95.0,
                    "paid_after_remainder_billions": 24.0,
                },
            },
            {
                "type": "cloud_commitments",
                "amount_billions": 30.0,
                "certainty": "contingent",
                "status": "future_cash_obligation",
                "revenue_matched": False,
                "payment_horizon": {
                    "schedule": [
                        {"fiscal_year": "2027", "amount_billions": 6.0},
                        {"fiscal_year": "2028", "amount_billions": 7.0},
                        {"fiscal_year": "2029", "amount_billions": 7.0},
                        {"fiscal_year": "2030", "amount_billions": 5.0},
                        {"fiscal_year": "2031", "amount_billions": 3.0},
                        {"fiscal_year": "2032", "amount_billions": 2.0},
                    ]
                },
            },
            {
                "type": "vendor_commitments",
                "amount_billions": 6.0,
                "certainty": "contingent",
                "status": "future_cash_obligation",
                "revenue_matched": False,
            },
            {
                "type": "operating_leases",
                "amount_billions": 5.604,
                "certainty": "contractual",
                "status": "future_cash_obligation",
                "revenue_matched": False,
                "schedule": [
                    {"fiscal_year": "2027", "amount_billions": 0.46},
                    {"fiscal_year": "2028", "amount_billions": 0.626},
                    {"fiscal_year": "2029", "amount_billions": 0.602},
                    {"fiscal_year": "2030", "amount_billions": 0.53},
                    {"fiscal_year": "2031", "amount_billions": 0.462},
                    {"fiscal_year": "2032", "amount_billions": 2.924},
                ],
            },
            {
                "type": "facility_lease_guarantees",
                "amount_billions": 3.5,
                "certainty": "contingent",
                "status": "contingent",
                "revenue_matched": False,
            },
            {
                "type": "8k_guarantees",
                "amount_billions": 105.0,
                "certainty": "contingent",
                "status": "contingent",
                "revenue_matched": False,
            },
        ]
    }


@pytest.fixture
def fake_deps(monkeypatch):
    monkeypatch.setattr(valuation, "cache", FakeCache())
    monkeypatch.setattr(
        valuation, "get_live_price", lambda t: 213.05
    )
    monkeypatch.setattr(
        valuation.analyst_client, "get_analyst_estimates", lambda t: _estimates()
    )
    monkeypatch.setattr(
        valuation.edgar_client, "get_fundamentals", lambda t, m: {
            "ttm_eps_diluted": 6.53,
        }
    )
    monkeypatch.setattr(
        valuation.obligations, "get_obligations", lambda t: _obligations()
    )


def test_trailing_pe_from_live_price(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    assert result["price"]["last"] == 213.05
    assert result["ttm_gaap_eps"] == 6.53
    assert result["trailing_pe"] == pytest.approx(32.6, abs=0.1)


def test_three_eps_figures_never_conflated(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    fe = result["forward_eps"]
    consensus = fe["consensus"]
    adjusted = fe["adjusted"]
    scenario = fe["scenario"]
    with_defaults = fe["scenario_with_defaults"]
    assert consensus["eps"] == 9.02
    assert adjusted["eps_after_contractual"] is not None
    assert scenario["eps_after_all_obligations"] is not None
    assert with_defaults["eps_after_all_obligations"] is not None
    # The OpenAI-default scenario is strictly worse than no-default.
    assert (
        with_defaults["eps_after_all_obligations"]
        < scenario["eps_after_all_obligations"]
    )
    # Labels are distinct and explicit.
    assert "contractual obligations included" in adjusted["label"]
    assert "no counterparty default" in scenario["label"]
    assert "default-triggered" in with_defaults["label"]
    assert scenario["label"] != adjusted["label"]


def test_default_triggered_guarantees_separate_from_contingent(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    fe = result["forward_eps"]
    scenario = fe["scenario"]
    with_defaults = fe["scenario_with_defaults"]
    # 8-K $105B + facility $3.5B = $108.5B / 6y / 24.221B shares.
    delta = scenario["eps_after_all_obligations"] - with_defaults["eps_after_all_obligations"]
    assert delta == pytest.approx(108.5 / 6.0 / 24.221, abs=0.01)


def test_supply_front_loaded_is_revenue_matched_not_drag(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    ob = result["obligations"]
    supply = ob["per_kind"]["supply_commitments"]
    assert supply["total_billions"] == 119.0
    assert supply["certainty"] == "contingent"
    assert supply["revenue_matched"] is True
    assert ob["revenue_matched_annual_billions"] > 100
    assert ob["revenue_matched_implied_revenue_billions"] > 400
    # Scenario EPS must NOT subtract supply spend (double-count).
    fe = result["forward_eps"]
    scenario = fe["scenario"]
    assert scenario["eps_after_all_obligations"] > 8.0


def test_horizon_less_supply_falls_back_flat():
    rows = [
        {"type": "supply_commitments", "amount_billions": 119.0, "certainty": "contingent",
         "status": "future_cash_obligation", "revenue_matched": True},
    ]
    impact = valuation._obligation_annual_impact(rows, years=6)
    assert impact["revenue_matched_annual_billions"] == pytest.approx(119.0 / 6.0, abs=0.01)


def test_next_fy_figures(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    fe = result["forward_eps"]
    assert fe["consensus_next_fy"]["eps"] == 13.04
    assert fe["consensus_next_fy"]["pe"] == pytest.approx(16.3, abs=0.1)
    assert fe["scenario_next_fy"]["eps_after_all_obligations"] is not None


def test_worst_case_tier_includes_revenue_matched_supply(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    fe = result["forward_eps"]
    worst = fe["worst_case"]
    scenario = fe["scenario"]
    # Worst case must be strictly worse than the stress scenario: it adds
    # the revenue-matched supply drag (stranded-cost bear case).
    assert worst["eps_after_all_obligations"] < scenario["eps_after_all_obligations"]
    assert "stranded" in worst["label"]
    assert worst["eps_after_all_obligations"] > 0


def test_projected_prices_matrix(fake_deps):
    result = valuation.get_valuation_metrics("NVDA")
    pp = result["projected_prices"]
    assert pp["current_price"] == 213.05
    assert pp["multiples"] == [15, 20, 25, 30, 35]
    by_tier = {t["tier"]: t for t in pp["tiers"]}
    worst = by_tier["Worst case FY27"]
    # Fixture worst-case FY27 EPS = 9.02 - 0.02 (leases FY27 0.46) - 3.92
    # (supply FY27 95.0 stranded) - 0.29 (cloud FY27 6.0 + vendor 1.0) -
    # 0.75 (default-triggered) = 4.04 (FY-matched; tail 4.8 sits in FY28+).
    assert worst["eps"] == 4.04
    assert worst["prices"]["15x"]["price"] == pytest.approx(60.6, abs=0.1)
    assert worst["prices"]["30x"]["price"] == pytest.approx(121.2, abs=0.1)
    # pct change vs current: 60.6/213.05 - 1 ≈ -71.6%.
    assert worst["prices"]["15x"]["pct_change_vs_current"] == pytest.approx(-71.6, abs=0.2)
    # Consensus FY27 at 25x should exceed current price.
    consensus = by_tier["Consensus FY27"]
    assert consensus["prices"]["25x"]["price"] > 213.05


def test_projected_prices_math_direct():
    pp = valuation._projected_prices(
        {"Worst case FY27": 2.56, "Consensus FY28": 13.04}, price=213.05
    )
    tiers = {t["tier"]: t for t in pp["tiers"]}
    assert tiers["Consensus FY28"]["prices"]["20x"]["price"] == 260.8
    assert tiers["Consensus FY28"]["prices"]["35x"]["pct_change_vs_current"] == pytest.approx(114.2, abs=0.2)


def test_obligation_annual_impact_direct():
    impact = valuation._obligation_annual_impact(
        _obligations()["obligations"], years=6
    )
    assert impact["contractual_annual_billions"] == pytest.approx(0.934, abs=0.01)
    # Contingent (non-default): cloud schedule avg (6+7+7+5+3+2)/6 + vendor 6/6.
    expected_contingent = 30.0 / 6.0 + 6.0 / 6.0
    assert impact["contingent_annual_billions"] == pytest.approx(
        expected_contingent, abs=0.1
    )
    # Default-triggered: facility 3.5/6 + 8-K 105/6.
    expected_default = (3.5 + 105.0) / 6.0
    assert impact["default_triggered_annual_billions"] == pytest.approx(
        expected_default, abs=0.1
    )
    # Revenue-matched: supply front-loaded (95/0.75 + 24/5).
    expected_matched = 95.0 / 0.75 + 24.0 / 5.0
    assert impact["revenue_matched_annual_billions"] == pytest.approx(
        expected_matched, abs=0.1
    )


def test_fy_schedule_separation_no_blended_fallback(monkeypatch):
    """A FY absent from impact_by_fiscal_year must not inherit other years' schedule."""
    scheduled = {
        "type": "vendor_commitments",
        "amount_billions": 30.0,
        "certainty": "contingent",
        "status": "future_cash_obligation",
        "revenue_matched": False,
        "schedule": [
            {"fiscal_year": "2028", "amount_billions": 10.0},
            {"fiscal_year": "2029", "amount_billions": 10.0},
            {"fiscal_year": "2030", "amount_billions": 10.0},
        ],
    }
    flat_vendor = {
        "type": "vendor_commitments",
        "amount_billions": 6.0,
        "certainty": "contingent",
        "status": "future_cash_obligation",
        "revenue_matched": False,
    }

    def _run(rows):
        monkeypatch.setattr(valuation, "cache", FakeCache())
        monkeypatch.setattr(valuation, "get_live_price", lambda t: 213.05)
        monkeypatch.setattr(
            valuation.analyst_client, "get_analyst_estimates", lambda t: _estimates()
        )
        monkeypatch.setattr(
            valuation.edgar_client, "get_fundamentals", lambda t, m: {"ttm_eps_diluted": 6.53}
        )
        monkeypatch.setattr(
            valuation.obligations, "get_obligations", lambda t: {"obligations": rows}
        )
        return valuation.get_valuation_metrics("NVDA")

    shares = 24.221  # 24_221_000_000 from _estimates
    result = _run([scheduled])
    fe = result["forward_eps"]
    assert fe["scenario"].get("contingent_drag_per_share", 0.0) == 0.0
    assert fe["scenario_next_fy"]["contingent_drag_per_share"] == pytest.approx(
        10.0 / shares, abs=0.01
    )
    impact = valuation._obligation_annual_impact([scheduled], years=6)
    assert "2027" not in impact["impact_by_fiscal_year"]
    assert impact["flat_annual_by_bucket"]["contingent"] == 0.0

    result = _run([scheduled, flat_vendor])
    fe = result["forward_eps"]
    assert fe["scenario"]["contingent_drag_per_share"] == pytest.approx(
        1.0 / shares, abs=0.01
    )
    impact = valuation._obligation_annual_impact([scheduled, flat_vendor], years=6)
    assert impact["impact_by_fiscal_year"].get("2027", {}).get("contingent", 0.0) == 0.0
    assert impact["flat_annual_by_bucket"]["contingent"] == pytest.approx(1.0, abs=0.01)