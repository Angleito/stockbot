"""Deterministic calculations for portfolio positions and valuations."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from ..domain.market.quotes import Quote

ZERO = Decimal("0")


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, ZERO):
        return None
    return numerator / denominator


def valuation_price(quote: Quote | None) -> dict[str, Any]:
    """Select the single deterministic valuation price for a quote.

    Precedence: (1) ``last`` when present; (2) ``mid`` (the bid/ask midpoint
    when both are present); (3) otherwise unavailable. Returns
    ``{"price": str(Decimal) | None, "price_type": "last" | "mid" | None}``
    with the price serialized as ``str(Decimal)`` per repo convention.
    """
    if quote is None:
        return {"price": None, "price_type": None}
    if quote.last is not None:
        return {"price": str(quote.last), "price_type": "last"}
    if quote.mid is not None:
        return {"price": str(quote.mid), "price_type": "mid"}
    return {"price": None, "price_type": None}


def position_market_value(
    quantity: Decimal | None, price: Decimal | None
) -> Decimal | None:
    """Return ``quantity * price`` when both are present, else None."""
    if quantity is None or price is None:
        return None
    return quantity * price


def unrealized_gain(
    market_value: Decimal | None,
    average_cost: Decimal | None,
    quantity: Decimal | None,
) -> Decimal | None:
    """Return ``market_value - average_cost * quantity`` when all are present."""
    if market_value is None or average_cost is None or quantity is None:
        return None
    return market_value - average_cost * quantity


def unrealized_gain_pct(
    gain: Decimal | None, cost_basis: Decimal | None
) -> Decimal | None:
    """Return gain as a ratio of cost basis, None when either is missing or basis is zero."""
    return _ratio(gain, cost_basis)


def portfolio_market_value(
    values: Sequence[Decimal | None],
) -> tuple[Decimal | None, int, int]:
    """Sum priced values.

    Returns ``(total, priced_count, total_count)``. A missing price degrades
    completeness: priced values are summed, but the total is only meaningful
    when ``priced_count == total_count > 0`` (callers decide how to surface
    incompleteness). With no priced values, total is None.
    """
    priced = [value for value in values if value is not None]
    total = sum(priced) if priced else None
    return total, len(priced), len(values)


def position_weight(
    market_value: Decimal | None, portfolio_total: Decimal | None
) -> Decimal | None:
    """Return market value as a fraction of the portfolio total, else None."""
    return _ratio(market_value, portfolio_total)


def largest_positions(
    items: Sequence[tuple[str, Decimal | None]],
    limit: int | None = None,
) -> list[tuple[str, Decimal | None]]:
    """Rank positions by market value descending, None values last, ticker ascending."""
    ordered = sorted(
        items,
        key=lambda item: (
            item[1] is None,
            -item[1] if item[1] is not None else ZERO,
            item[0],
        ),
    )
    if limit is None:
        return ordered
    bounded = max(1, min(int(limit), 100))
    return ordered[:bounded]


def portfolio_concentration(
    weights: Sequence[Decimal | None],
) -> Decimal | None:
    """Return a Herfindahl-style concentration on a 0..1 scale.

    Sum of squared non-None weights; closer to 1 means more concentrated.
    None when no weight is non-None.
    """
    present = [weight for weight in weights if weight is not None]
    if not present:
        return None
    return sum(weight * weight for weight in present)