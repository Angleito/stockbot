from datetime import datetime, timezone
from decimal import Decimal

from app.analytics.portfolio import largest_positions, portfolio_concentration
from app.domain.market.quotes import Quote
from app.domain.portfolio.valuation import (
    portfolio_market_value,
    position_market_value,
    position_weight,
    unrealized_gain,
    unrealized_gain_pct,
    valuation_price,
)


def _snapshot(**overrides) -> Quote:
    values = {
        "ticker": "AMD",
        "last": Decimal("100"),
        "bid": Decimal("99.5"),
        "ask": Decimal("100.5"),
        "retrieved_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "source": "robinhood_mcp",
    }
    values.update(overrides)
    return Quote(**values)


def test_valuation_price_prefers_last():
    result = valuation_price(_snapshot())
    assert result == {"price": "100", "price_type": "last"}


def test_valuation_price_uses_mid_when_last_missing():
    result = valuation_price(_snapshot(last=None))
    assert result == {"price": "100.0", "price_type": "mid"}


def test_valuation_price_last_wins_over_bid_ask():
    result = valuation_price(_snapshot(last=Decimal("98"), bid=Decimal("99"), ask=Decimal("101")))
    assert result == {"price": "98", "price_type": "last"}


def test_valuation_price_unavailable_when_all_missing():
    result = valuation_price(_snapshot(last=None, bid=None, ask=None))
    assert result == {"price": None, "price_type": None}


def test_valuation_price_none_quote():
    assert valuation_price(None) == {"price": None, "price_type": None}


def test_position_market_value():
    assert position_market_value(Decimal("10"), Decimal("100")) == Decimal("1000")


def test_position_market_value_fractional_shares():
    assert position_market_value(Decimal("0.5"), Decimal("200")) == Decimal("100")


def test_position_market_value_zero_quantity_is_known_zero():
    assert position_market_value(Decimal("0"), Decimal("100")) == Decimal("0")
    assert position_market_value(Decimal("0"), None) == Decimal("0")


def test_position_market_value_missing_price_is_none():
    assert position_market_value(Decimal("10"), None) is None
    assert position_market_value(None, Decimal("100")) is None


def test_weights_amd_nvda():
    total = portfolio_market_value([Decimal("600"), Decimal("400")])
    assert total == (Decimal("1000"), 2, 2)
    assert position_weight(Decimal("600"), Decimal("1000")) == Decimal("0.6")
    assert position_weight(Decimal("400"), Decimal("1000")) == Decimal("0.4")


def test_cash_and_positions():
    assert portfolio_market_value([Decimal("900"), Decimal("100")]) == (Decimal("1000"), 2, 2)


def test_portfolio_market_value_missing_price_degrades_completeness():
    assert portfolio_market_value([None, Decimal("100")]) == (Decimal("100"), 1, 2)
    assert portfolio_market_value([None, None, None]) == (None, 0, 3)
    assert portfolio_market_value([]) == (None, 0, 0)


def test_weight_with_missing_or_zero_total():
    assert position_weight(None, Decimal("1000")) is None
    assert position_weight(Decimal("100"), None) is None
    assert position_weight(Decimal("100"), Decimal("0")) is None
    assert position_weight(Decimal("0"), Decimal("0")) is None


def test_unrealized_gain_and_pct():
    gain = unrealized_gain(Decimal("1200"), Decimal("100"), Decimal("10"))
    assert gain == Decimal("200")
    assert unrealized_gain_pct(gain, Decimal("1000")) == Decimal("0.2")
    assert unrealized_gain_pct(Decimal("-500"), Decimal("1000")) == Decimal("-0.5")
    assert unrealized_gain(None, Decimal("100"), Decimal("10")) is None
    assert unrealized_gain(Decimal("1200"), None, Decimal("10")) is None
    assert unrealized_gain(Decimal("1200"), Decimal("100"), None) is None
    assert unrealized_gain_pct(gain, None) is None
    assert unrealized_gain_pct(gain, Decimal("0")) is None
    assert unrealized_gain_pct(None, Decimal("1000")) is None


def test_largest_positions_ordering_none_last_and_limit_clamping():
    items = [
        ("NVDA", Decimal("400")),
        ("AMD", Decimal("600")),
        ("MSFT", None),
    ]
    assert largest_positions(items) == [("AMD", Decimal("600")), ("NVDA", Decimal("400")), ("MSFT", None)]
    assert largest_positions(items, limit=2) == [("AMD", Decimal("600")), ("NVDA", Decimal("400"))]
    assert len(largest_positions(items, limit=0)) == 1
    assert len(largest_positions(items, limit=500)) == 3


def test_largest_positions_ticker_tiebreaker_ascending():
    items = [("NVDA", Decimal("400")), ("AMD", Decimal("400"))]
    assert largest_positions(items) == [("AMD", Decimal("400")), ("NVDA", Decimal("400"))]


def test_portfolio_concentration():
    assert portfolio_concentration([Decimal("0.5"), Decimal("0.5")]) == Decimal("0.5")
    assert portfolio_concentration([Decimal("1.0")]) == Decimal("1.0")
    assert portfolio_concentration([None, None]) is None


def test_quote_mid():
    assert _snapshot().mid == Decimal("100")
    assert _snapshot(bid=None).mid is None
    assert _snapshot(ask=None).mid is None