"""Row-to-domain mappers for persisted parquet rows.

Persistence mapping is a storage concern: rows from the parquet datasets
are adapted here into domain models, keeping ``app/domain`` free of
storage/file I/O.  All mappers are total functions over well-formed rows
(the same coercions the removed ``from_row`` classmethods applied).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..domain.market.securities import TickerAlias
from ..domain.portfolio import Position


def ticker_alias_from_row(row: Mapping[str, Any]) -> TickerAlias:
    """Rebuild a ticker alias from a persisted entity_aliases row.

    Field mapping only, no identity policy: identity derivation is the
    domain resolver's concern.
    """
    return TickerAlias(
        alias_type=str(row["alias_type"]),
        alias_value=str(row["alias_value"]),
        entity_id=str(row["entity_id"]),
        security_id=row.get("security_id"),
        source=row.get("source"),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        known_at=row.get("known_at"),
        retrieved_at=row.get("retrieved_at"),
    )


def position_from_row(row: Mapping[str, Any], retrieved_at: datetime) -> Position:
    """Rebuild a position from a persisted row.

    ``retrieved_at`` is not persisted in the position schema; it is
    reconstructed as the snapshot's ``created_at`` (the position was
    retrieved during the sync that created the snapshot).
    """
    numeric = {
        key: Decimal(str(row[key])) if row[key] is not None else None
        for key in (
            "quantity",
            "average_cost",
            "market_price",
            "market_value",
            "unrealized_gain",
            "unrealized_gain_pct",
            "portfolio_weight",
        )
    }
    quote_retrieved_at = row.get("quote_retrieved_at")
    asset_type = row.get("asset_type") or "equity"
    return Position(
        position_id=str(row["position_id"]),
        account_id=str(row["account_id"]),
        security_id=str(row["security_id"]) if row.get("security_id") else None,
        entity_id=str(row["entity_id"]) if row.get("entity_id") else None,
        ticker=str(row["ticker"]),
        quantity=numeric["quantity"],
        average_cost=numeric["average_cost"],
        market_price=numeric["market_price"],
        market_value=numeric["market_value"],
        unrealized_gain=numeric["unrealized_gain"],
        unrealized_gain_pct=numeric["unrealized_gain_pct"],
        portfolio_weight=numeric["portfolio_weight"],
        source=str(row["source"]),
        retrieved_at=retrieved_at,
        price_type=str(row["price_type"]) if row.get("price_type") else None,
        quote_retrieved_at=(
            datetime.fromisoformat(quote_retrieved_at.replace("Z", "+00:00"))
            if quote_retrieved_at else None
        ),
        asset_type=asset_type,
    )
