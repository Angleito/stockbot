"""Deterministic mandate evaluation over a portfolio snapshot.

Python calculates; the LLM interprets.  All math is Decimal; a breach
holds exactly when ``NOT (actual op threshold)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..portfolio import PortfolioSnapshot
from .breaches import RiskBreach
from .mandate import Mandate, RiskLimit

UNKNOWN_SECTOR = "unknown_sector"

_OPS = {
    "<=": lambda actual, threshold: actual <= threshold,
    ">=": lambda actual, threshold: actual >= threshold,
}


@dataclass(frozen=True)
class RiskEvaluation:
    breaches: tuple[RiskBreach, ...]
    sector_exposures: dict[str, Decimal]   # sector name -> summed weight; includes "unknown_sector"
    not_evaluable: tuple[str, ...]         # human-readable reasons
    snapshot_id: str
    created_at: datetime                   # = snapshot.created_at (deterministic)


def _excess(operator: str, actual: Decimal, threshold: Decimal) -> Decimal:
    return actual - threshold if operator == "<=" else threshold - actual


def _evaluate_limit(
    limit: RiskLimit,
    snapshot: PortfolioSnapshot,
    sector_exposures: dict[str, Decimal],
    breaches: list[RiskBreach],
    not_evaluable: list[str],
) -> None:
    if limit.metric == "sector_exposure":
        actual = sector_exposures.get(limit.target or "", Decimal("0"))
        if not _OPS[limit.operator](actual, limit.threshold):
            breaches.append(
                RiskBreach(
                    metric="sector_exposure",
                    target=limit.target,
                    severity=limit.severity,
                    actual=actual,
                    limit=limit.threshold,
                    excess=_excess(limit.operator, actual, limit.threshold),
                )
            )
    elif limit.metric == "single_position_weight":
        for position in snapshot.positions:
            weight = position.portfolio_weight
            if weight is None:
                not_evaluable.append(
                    f"single_position_weight: {position.ticker} (no weight)"
                )
                continue
            if not _OPS[limit.operator](weight, limit.threshold):
                breaches.append(
                    RiskBreach(
                        metric="single_position_weight",
                        target=None,
                        severity=limit.severity,
                        actual=weight,
                        limit=limit.threshold,
                        excess=_excess(limit.operator, weight, limit.threshold),
                        note=f"{position.ticker} ({position.position_id})",
                    )
                )
    elif limit.metric == "minimum_cash":
        if limit.unit == "dollars":
            actual = snapshot.cash
        else:
            actual = (
                snapshot.cash / snapshot.total_value
                if snapshot.cash is not None and snapshot.total_value is not None
                else None
            )
        if actual is None:
            not_evaluable.append("minimum_cash: cash unavailable")
            return
        if not _OPS[limit.operator](actual, limit.threshold):
            breaches.append(
                RiskBreach(
                    metric="minimum_cash",
                    target=None,
                    severity=limit.severity,
                    actual=actual,
                    limit=limit.threshold,
                    excess=_excess(limit.operator, actual, limit.threshold),
                )
            )


def evaluate_mandate(
    snapshot: PortfolioSnapshot,
    mandate: Mandate,
    sector_map: dict[str, str] | None = None,
) -> RiskEvaluation:
    """Evaluate a mandate against a snapshot.

    ``sector_map`` maps entity_id -> sector (newest-wins).  Positions whose
    entity is unknown or unmapped bucket to ``UNKNOWN_SECTOR``.
    """
    sector_map = sector_map or {}
    sector_exposures: dict[str, Decimal] = {}
    for position in snapshot.positions:
        if position.portfolio_weight is None:
            continue
        if position.entity_id is not None and position.entity_id in sector_map:
            sector = sector_map[position.entity_id]
        else:
            sector = UNKNOWN_SECTOR
        sector_exposures[sector] = (
            sector_exposures.get(sector, Decimal("0")) + position.portfolio_weight
        )

    breaches: list[RiskBreach] = []
    not_evaluable: list[str] = []
    for limit in mandate.limits:
        _evaluate_limit(limit, snapshot, sector_exposures, breaches, not_evaluable)
    for entry in mandate.prohibited_assets:
        for position in snapshot.positions:
            if (
                position.ticker.upper() == entry.upper()
                or position.entity_id == entry
            ):
                breaches.append(
                    RiskBreach(
                        metric="prohibited_assets",
                        target=entry,
                        severity="warning",
                        actual=position.ticker,
                        limit=entry,
                        excess=None,
                        note=f"position {position.ticker} ({position.position_id})",
                    )
                )
    return RiskEvaluation(
        breaches=tuple(breaches),
        sector_exposures=sector_exposures,
        not_evaluable=tuple(dict.fromkeys(not_evaluable)),
        snapshot_id=snapshot.snapshot_id,
        created_at=snapshot.created_at,
    )
