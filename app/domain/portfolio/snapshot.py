"""Deterministic, immutable portfolio snapshot assembly."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .models import PortfolioSnapshot, Position
from .valuation import portfolio_market_value, position_weight


def _cash_total(
    account_ids: Sequence[str], cash_balances: Mapping[str, Decimal | None],
) -> Decimal | None:
    """All-or-nothing cash sum (existing boundary)."""
    cash_complete = (
        bool(cash_balances)
        and len(cash_balances) == len(account_ids)
        and set(cash_balances) == set(account_ids)
        and all(c is not None for c in cash_balances.values())
    )
    if not cash_complete:
        return None
    return sum((c for c in cash_balances.values() if c is not None), Decimal(0))


def _total_value(
    positions: Sequence[Position],
    invested_value: Decimal | None,
    cash: Decimal | None,
) -> Decimal | None:
    """Denominator with valuation-completeness gate (existing boundary)."""
    total = invested_value + cash if invested_value is not None and cash is not None else None
    valuation_complete = all(
        position.market_value is not None or position.quantity == 0
        for position in positions
    )
    if not valuation_complete:
        return None
    return total


def _weighted_positions(
    snapshot_id: str, positions: Sequence[Position], total_value: Decimal | None,
) -> tuple[Position, ...]:
    """Rebuild positions with deterministic ids and weights (existing boundary)."""
    built: list[Position] = []
    for position in positions:
        account_id = position.account_id
        built.append(
            Position(
                position_id=f"{snapshot_id}:{account_id}:{position.ticker}",
                account_id=account_id,
                security_id=position.security_id,
                entity_id=position.entity_id,
                ticker=position.ticker,
                quantity=position.quantity,
                average_cost=position.average_cost,
                market_price=position.market_price,
                market_value=position.market_value,
                unrealized_gain=position.unrealized_gain,
                unrealized_gain_pct=position.unrealized_gain_pct,
                portfolio_weight=position_weight(position.market_value, total_value),
                source=position.source,
                retrieved_at=position.retrieved_at,
                price_type=position.price_type,
                quote_retrieved_at=position.quote_retrieved_at,
                asset_type=position.asset_type,
            )
        )
    return tuple(built)


def build_portfolio_snapshot(
    *,
    broker: str,
    account_ids: Sequence[str],
    positions: Sequence[Position],
    cash_balances: Mapping[str, Decimal | None],
    created_at: datetime,
) -> PortfolioSnapshot:
    """Assemble the deterministic, immutable portfolio snapshot.

    ``account_ids`` and ``position.account_id`` must already be local
    opaque identifiers (raw broker ids never enter the domain builder);
    anonymization happens at the services boundary.

    Position weights use ``total_value`` as the denominator (cash included);
    weights are ``None`` when total value is unknown.  Weights are ``None``
    whenever any non-zero position lacks a valuation (total_value is then
    ``None``; zero-quantity positions never block completeness).  Cash is the
    sum of all balances, and only when every account has a non-None balance
    (all-or-nothing completeness); partial or missing balances yield
    ``None``, never an invented partial sum.
    """
    cash = _cash_total(account_ids, cash_balances)
    if not positions and cash is not None:
        invested_value: Decimal | None = Decimal(0)
    else:
        invested_value = portfolio_market_value(
            [position.market_value for position in positions])[0]
    total_value = _total_value(positions, invested_value, cash)

    snapshot_id = f"portfolio:{broker}:{created_at.isoformat()}"
    return PortfolioSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        broker=broker,
        account_ids=tuple(account_ids),
        cash=cash,
        invested_value=invested_value,
        total_value=total_value,
        positions=_weighted_positions(snapshot_id, positions, total_value),
    )
