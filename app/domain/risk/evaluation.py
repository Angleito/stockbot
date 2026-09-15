"""Deterministic mandate evaluation over a portfolio snapshot.

Python calculates; the LLM interprets.  All math is Decimal; a breach
holds exactly when ``NOT actual op threshold``.  Non-evaluable outcomes
are structured ``EvaluationIssue`` codes; renderers turn them into prose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..portfolio import PortfolioSnapshot
from .breaches import RiskBreach
from .mandate import Mandate, RiskLimit

UNKNOWN_SECTOR = "unknown_sector"

_OPS: dict[str, Callable[[Decimal, Decimal], bool]] = {
    "<=": lambda actual, threshold: actual <= threshold,
    ">=": lambda actual, threshold: actual >= threshold,
}


@dataclass(frozen=True)
class EvaluationIssue:
    code: str
    metric: str
    target: str | None = None
    position_id: str | None = None
    ticker: str | None = None


@dataclass(frozen=True)
class RiskEvaluation:
    breaches: tuple[RiskBreach, ...]
    sector_exposures: dict[str, Decimal]   # sector name -> summed weight; includes "unknown_sector"
    issues: tuple[EvaluationIssue, ...]    # structured non-evaluable reasons
    snapshot_id: str
    created_at: datetime                   # = snapshot.created_at (deterministic)


def _excess(operator: str, actual: Decimal, threshold: Decimal) -> Decimal:
    return actual - threshold if operator == "<=" else threshold - actual


def _sector_amounts(limit: RiskLimit, sector_exposures: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """Known/unknown exposure pair (existing boundary)."""
    known = sector_exposures.get(limit.target or "", Decimal(0))
    if limit.target == UNKNOWN_SECTOR:  # the target IS the unknown bucket; measured directly
        return known, Decimal(0)
    return known, sector_exposures.get(UNKNOWN_SECTOR, Decimal(0))


def _sector_breach(limit: RiskLimit, known: Decimal) -> RiskBreach:
    """One sector breach row (existing boundary)."""
    return RiskBreach(metric="sector_exposure", target=limit.target,
                      severity=limit.severity, actual=known,
                      limit=limit.threshold,
                      excess=_excess(limit.operator, known, limit.threshold),
                      unit=limit.unit)


def _evaluate_sector_cap(limit: RiskLimit, known: Decimal, unknown: Decimal,
                         breaches: list[RiskBreach], issues: list[EvaluationIssue]) -> None:
    """The <= sector arm (existing boundary)."""
    if known > limit.threshold:
        breaches.append(_sector_breach(limit, known))
    elif unknown > 0:
        issues.append(EvaluationIssue("unknown_sector_exposure", "sector_exposure",
                                      target=limit.target))


def _evaluate_sector_floor(limit: RiskLimit, known: Decimal, unknown: Decimal,
                           breaches: list[RiskBreach], issues: list[EvaluationIssue]) -> None:
    """The >= sector arm (existing boundary)."""
    if known >= limit.threshold:
        return
    if unknown > 0:
        issues.append(EvaluationIssue("unknown_sector_exposure", "sector_exposure",
                                      target=limit.target))
    else:
        breaches.append(_sector_breach(limit, known))


def _evaluate_sector_limit(limit: RiskLimit, sector_exposures: dict[str, Decimal],
                           breaches: list[RiskBreach], issues: list[EvaluationIssue]) -> None:
    """Sector exposure dispatch (existing boundary)."""
    known, unknown = _sector_amounts(limit, sector_exposures)
    if limit.operator == "<=":
        _evaluate_sector_cap(limit, known, unknown, breaches, issues)
    else:  # ">="
        _evaluate_sector_floor(limit, known, unknown, breaches, issues)


def _evaluate_weight_limit(limit: RiskLimit, snapshot: PortfolioSnapshot,
                           breaches: list[RiskBreach], issues: list[EvaluationIssue]) -> None:
    """Single-position weight arm (existing boundary)."""
    for position in snapshot.positions:
        weight = position.portfolio_weight
        if weight is None:
            issues.append(EvaluationIssue(
                "position_weight_unavailable", "single_position_weight",
                ticker=position.ticker, position_id=position.position_id,
            ))
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
                    unit=limit.unit,
                )
            )


def _cash_actual(limit: RiskLimit, snapshot: PortfolioSnapshot,
                 issues: list[EvaluationIssue]) -> Decimal | None:
    """Cash amount or ratio with non-evaluable guards (existing boundary)."""
    if limit.unit == "dollars":
        return snapshot.cash
    if snapshot.cash is None:
        issues.append(EvaluationIssue("cash_unavailable", "minimum_cash"))
        return None
    if snapshot.total_value is None:
        issues.append(EvaluationIssue("total_value_unavailable", "minimum_cash"))
        return None
    if snapshot.total_value == 0:
        issues.append(EvaluationIssue("total_value_zero", "minimum_cash"))
        return None
    return snapshot.cash / snapshot.total_value


def _evaluate_cash_limit(limit: RiskLimit, snapshot: PortfolioSnapshot,
                         breaches: list[RiskBreach], issues: list[EvaluationIssue]) -> None:
    """Minimum-cash arm (existing boundary)."""
    actual = _cash_actual(limit, snapshot, issues)
    # Dollars-arm None means unavailable cash; ratio-arm None already issued.
    if actual is None:
        if limit.unit == "dollars":
            issues.append(EvaluationIssue("cash_unavailable", "minimum_cash"))
        elif snapshot.cash is not None and snapshot.total_value is not None and snapshot.total_value != 0:
            pass  # unreachable guard: ratio arm returns non-None here
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
                unit=limit.unit,
            )
        )


def _evaluate_limit(
    limit: RiskLimit,
    snapshot: PortfolioSnapshot,
    sector_exposures: dict[str, Decimal],
    breaches: list[RiskBreach],
    issues: list[EvaluationIssue],
) -> None:
    if limit.metric == "sector_exposure":
        _evaluate_sector_limit(limit, sector_exposures, breaches, issues)
    elif limit.metric == "single_position_weight":
        _evaluate_weight_limit(limit, snapshot, breaches, issues)
    elif limit.metric == "minimum_cash":
        _evaluate_cash_limit(limit, snapshot, breaches, issues)


def _sector_exposures(
    snapshot: PortfolioSnapshot, sector_map: dict[str, str], issues: list[EvaluationIssue],
) -> dict[str, Decimal]:
    """Summed sector weights with unknown bucketing (existing boundary)."""
    exposures: dict[str, Decimal] = {}
    for position in snapshot.positions:
        if position.portfolio_weight is None:
            issues.append(EvaluationIssue(
                "position_weight_unavailable", "sector_exposure",
                ticker=position.ticker, position_id=position.position_id,
            ))
            continue
        if position.entity_id is not None and position.entity_id in sector_map:
            sector = sector_map[position.entity_id]
        else:
            sector = UNKNOWN_SECTOR
        exposures[sector] = exposures.get(sector, Decimal(0)) + position.portfolio_weight
    return exposures


def _prohibited_breaches(
    mandate: Mandate, snapshot: PortfolioSnapshot,
) -> list[RiskBreach]:
    """Ticker/entity prohibited-asset matches (existing boundary)."""
    breaches: list[RiskBreach] = []
    for entry in mandate.prohibited_assets:
        for position in snapshot.positions:
            if position.ticker.upper() == entry.upper() or position.entity_id == entry:
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
    return breaches


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
    issues: list[EvaluationIssue] = []
    needs_sector_exposure = any(
        limit.metric == "sector_exposure" for limit in mandate.limits
    )
    sector_exposures = (
        _sector_exposures(snapshot, sector_map, issues) if needs_sector_exposure else {}
    )
    breaches: list[RiskBreach] = []
    for limit in mandate.limits:
        _evaluate_limit(limit, snapshot, sector_exposures, breaches, issues)
    breaches.extend(_prohibited_breaches(mandate, snapshot))
    return RiskEvaluation(
        breaches=tuple(breaches),
        sector_exposures=sector_exposures,
        issues=tuple(dict.fromkeys(issues)),
        snapshot_id=snapshot.snapshot_id,
        created_at=snapshot.created_at,
    )
