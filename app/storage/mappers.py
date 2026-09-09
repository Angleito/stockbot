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

from ..domain.market.securities import TickerAlias
from ..domain.portfolio import Position


def ticker_alias_from_row(row: Mapping[str, object]) -> TickerAlias:
    """Rebuild a ticker alias from a persisted entity_aliases row.

    Field mapping only, no identity policy: identity derivation is the
    domain resolver's concern.
    """
    return TickerAlias(
        alias_type=str(row["alias_type"]),
        alias_value=str(row["alias_value"]),
        entity_id=str(row["entity_id"]),
        security_id=str(row["security_id"]) if row.get("security_id") else None,
        source=str(row["source"]),
        valid_from=str(row["valid_from"]) if row.get("valid_from") else None,
        valid_to=str(row["valid_to"]) if row.get("valid_to") else None,
        known_at=str(row["known_at"]) if row.get("known_at") else None,
        retrieved_at=str(row["retrieved_at"]) if row.get("retrieved_at") else None,
    )


def canonical_decimal(value: Decimal | None) -> Decimal | None:
    """Strip storage-scale artifacts: Decimal('1168.40000000000000') -> Decimal('1168.4'),
    Decimal('10.00000000') -> Decimal('10').  Value-preserving."""
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return normalized.quantize(Decimal(1))
    return normalized


def _required_quantity(value: Decimal | None) -> Decimal:
    """Fail closed on a persisted position without a quantity (never default money)."""
    if value is None:
        raise ValueError("Position row is missing or has a malformed quantity")
    return value


def position_from_row(row: Mapping[str, object], retrieved_at: datetime) -> Position:
    """Rebuild a position from a persisted row.

    ``retrieved_at`` is not persisted in the position schema; it is
    reconstructed as the snapshot's ``created_at`` (the position was
    retrieved during the sync that created the snapshot).
    """
    numeric = {
        key: canonical_decimal(
            Decimal(str(row[key])) if row[key] is not None else None
        )
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
    asset_type_value = row.get("asset_type")
    asset_type = asset_type_value if isinstance(asset_type_value, str) and asset_type_value else "equity"
    return Position(
        position_id=str(row["position_id"]),
        account_id=str(row["account_id"]),
        security_id=str(row["security_id"]) if row.get("security_id") else None,
        entity_id=str(row["entity_id"]) if row.get("entity_id") else None,
        ticker=str(row["ticker"]),
        quantity=_required_quantity(numeric["quantity"]),
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
            if isinstance(quote_retrieved_at, str) and quote_retrieved_at else None
        ),
        asset_type=asset_type,
    )
