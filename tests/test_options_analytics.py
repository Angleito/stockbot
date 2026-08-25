from datetime import date, datetime, timezone
from decimal import Decimal

from app.analytics.options import analyze_option, compare_options
from app.robinhood.options import OptionQuote, normalize_option_quote


def _quote(**overrides) -> OptionQuote:
    values = {
        "contract_id": "wing-put-80",
        "ticker": "WING",
        "expiration": date(2027, 1, 15),
        "strike": Decimal("80"),
        "option_type": "put",
        "underlying_price": Decimal("116.84"),
        "bid": Decimal("2.00"),
        "ask": Decimal("2.40"),
        "mark": Decimal("2.20"),
        "implied_volatility": Decimal("0.55"),
        "delta": Decimal("-0.12"),
        "gamma": Decimal("0.01"),
        "theta": Decimal("-0.02"),
        "vega": Decimal("0.10"),
        "volume": 10,
        "open_interest": 100,
        "retrieved_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return OptionQuote(**values)


def test_normalize_option_quote_preserves_nullable_provider_values():
    quote = normalize_option_quote(
        {
            "id": "c1",
            "expirationDate": "2027-01-15",
            "strikePrice": "80",
            "optionType": "PUT",
            "bid": "2.00",
            "ask": "2.40",
        },
        ticker="WING",
    )
    assert quote.contract_id == "c1"
    assert quote.strike == Decimal("80")
    assert quote.option_type == "put"
    assert quote.delta is None
    assert quote.mid == Decimal("2.20")


def test_analyze_option_calculates_expiration_metrics_without_replacing_greeks():
    result = analyze_option(_quote(), as_of=date(2026, 8, 25), target_price=80)
    assert result["dte"] == 143
    assert result["mid"] == "2.20"
    assert result["spread"] == "0.40"
    assert result["premium_per_contract"] == "220.00"
    assert result["intrinsic_value"] == "0"
    assert result["extrinsic_value"] == "2.20"
    assert result["breakeven_at_expiration"] == "77.80"
    assert result["target_pnl"] == "-220.00"
    assert result["delta"] == "-0.12"
    assert result["rho"] is None


def test_call_target_payoff_and_comparison_are_deterministic():
    call = _quote(
        contract_id="wing-call-120",
        strike=Decimal("120"),
        option_type="call",
        bid=Decimal("3"),
        ask=Decimal("5"),
        mark=Decimal("4"),
    )
    put = _quote(contract_id="wing-put-70", strike=Decimal("70"), bid=Decimal("1"), ask=Decimal("2"), mark=Decimal("1.5"))
    result = analyze_option(call, as_of=date(2026, 8, 25), target_price=150)
    assert result["target_pnl"] == "2600"
    compared = compare_options([call, put], target_price=150, as_of=date(2026, 8, 25))
    assert compared["ranking"] == "target_pnl_desc"
    assert compared["contracts"][0]["contract_id"] == "wing-call-120"
