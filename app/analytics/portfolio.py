"""Deterministic calculations for portfolio positions and valuations."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from ..domain.portfolio.valuation import ZERO


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
