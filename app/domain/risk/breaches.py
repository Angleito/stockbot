"""Risk breach model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskBreach:
    metric: str
    target: str | None
    severity: str
    actual: Decimal | str | None
    limit: Decimal | str | None
    excess: Decimal | None
    note: str | None = None
