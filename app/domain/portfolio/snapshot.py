"""Deterministic, immutable portfolio snapshot assembly."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from .models import PortfolioSnapshot, Position
from .valuation import portfolio_market_value, position_weight


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
    cash_complete = (
        bool(cash_balances)
        and len(cash_balances) == len(account_ids)
        and set(cash_balances) == set(account_ids)
        and all(c is not None for c in cash_balances.values())
    )
    cash = sum(cash_balances.values()) if cash_complete else None
    invested_value = (
        Decimal("0")
        if not positions and cash is not None
        else portfolio_market_value([position.market_value for position in positions])[0]
    )
    total_value = invested_value + cash if invested_value is not None and cash is not None else None
    valuation_complete = all(
        position.market_value is not None or position.quantity == 0
        for position in positions
    )
    if not valuation_complete:
        total_value = None

    snapshot_id = f"portfolio:{broker}:{created_at.isoformat()}"
    local_ids = tuple(account_ids)
    built_positions: list[Position] = []
    for position in positions:
        account_id = position.account_id
        built_positions.append(
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
    built = tuple(built_positions)
    return PortfolioSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        broker=broker,
        account_ids=local_ids,
        cash=cash,
        invested_value=invested_value,
        total_value=total_value,
        positions=built,
    )
