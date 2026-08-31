"""Map Robinhood provider models to Stockbot domain models at the boundary."""

from __future__ import annotations

from ..domain.market.quotes import Quote
from ..domain.portfolio import BrokeragePositionInput
from .account import BrokeragePosition
from .options import MarketSnapshot


def to_quote(snapshot: MarketSnapshot) -> Quote:
    """Provider snapshot -> domain Quote. security_id is unknown at quote
    time (resolution happens per-position); it stays None here."""
    return Quote(
        ticker=snapshot.ticker,
        last=snapshot.last,
        bid=snapshot.bid,
        ask=snapshot.ask,
        retrieved_at=snapshot.retrieved_at,
        source=snapshot.source,
    )


def to_position_input(raw: BrokeragePosition) -> BrokeragePositionInput:
    return BrokeragePositionInput(
        position_id=raw.position_id,
        account_id=raw.account_id,
        ticker=raw.ticker,
        provider_instrument_id=raw.provider_instrument_id,
        quantity=raw.quantity,
        average_cost=raw.average_cost,
        retrieved_at=raw.retrieved_at,
        source=raw.source,
        asset_type="equity",
    )
