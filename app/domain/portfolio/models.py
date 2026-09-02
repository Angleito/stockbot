"""Stockbot-owned portfolio domain models, provider-agnostic by design."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


def local_account_id(account_id: str) -> str:
    """Stable local opaque identifier for a broker account.

    The raw broker account id must never reach persisted data; this
    one-way deterministic mapping keeps per-account grouping and
    round-trip identity in the analytical tables without exposing the
    account to anyone who reads data/.
    """
    digest = hashlib.sha256(
        f"stockbot:local-account:v1:{account_id}".encode("utf-8")
    ).hexdigest()
    return f"local:{digest[:16]}"


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
    asset_type: str = "equity"


@dataclass(frozen=True)
class BrokeragePositionInput:
    position_id: str
    account_id: str
    ticker: str
    provider_instrument_id: str | None
    quantity: Decimal
    average_cost: Decimal | None
    retrieved_at: datetime
    source: str
    asset_type: str = "equity"


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
