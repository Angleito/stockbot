"""Stockbot-owned quote domain model, provider-agnostic by design."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Quote:
    ticker: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    retrieved_at: datetime
    source: str
    security_id: str | None = None

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal("2")
        return None
