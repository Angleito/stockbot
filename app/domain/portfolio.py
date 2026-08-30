"""Stockbot-owned portfolio domain models, provider-agnostic by design."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Position:
    position_id: str
    account_id: str
    security_id: str | None
    entity_id: str | None
    ticker: str
    quantity: Decimal
    average_cost: Decimal | None
    market_price: Decimal | None
    market_value: Decimal | None
    unrealized_gain: Decimal | None
    unrealized_gain_pct: Decimal | None
    portfolio_weight: Decimal | None
    source: str
    retrieved_at: datetime
    price_type: str | None = None
    quote_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    snapshot_id: str
    created_at: datetime
    broker: str
    account_ids: tuple[str, ...]
    cash: Decimal | None
    invested_value: Decimal | None
    total_value: Decimal | None
    positions: tuple[Position, ...]