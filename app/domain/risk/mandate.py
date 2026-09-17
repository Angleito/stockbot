"""Mandate configuration: risk limits and prohibited assets.

Validation lives here in the domain, free of file I/O; the storage glue
loads JSON payloads and hands them to :func:`parse_mandate`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SUPPORTED_METRICS = ("single_position_weight", "minimum_cash", "sector_exposure")
SUPPORTED_OPERATORS = ("<=", ">=")
SUPPORTED_SEVERITIES = ("warning", "critical")
SUPPORTED_UNITS = ("ratio", "dollars")


@dataclass(frozen=True)
class RiskLimit:
    metric: str
    operator: str
    threshold: Decimal
    target: str | None = None  # required for sector_exposure (sector name)
    severity: str = "warning"  # "warning" | "critical"
    unit: str = "ratio"  # "ratio" | "dollars" (minimum_cash)


@dataclass(frozen=True)
class Mandate:
    limits: tuple[RiskLimit, ...]
    prohibited_assets: tuple[str, ...]


def _decimal_threshold(value: object, *, index: int) -> Decimal:
    try:
        threshold = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"mandate limit {index}: threshold must be a positive number") from exc
    if threshold <= 0:
        raise ValueError(f"mandate limit {index}: threshold must be a positive number")
    return threshold


def _limit_root(data: object) -> list[object]:
    """Narrow the limits list (existing boundary)."""
    if not isinstance(data, dict):
        raise ValueError("mandate: root must be a JSON object")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    raw_limits = data.get("limits")
    if raw_limits is None:
        raise ValueError("mandate: 'limits' is required and must be a list")
    if not isinstance(raw_limits, list):
        raise ValueError("mandate: 'limits' must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return raw_limits


def _limit_vocab(entry: dict[str, object], index: int) -> tuple[str, str, str, str]:
    """Metric/operator/unit/severity vocab checks (existing boundary)."""
    metric = entry.get("metric")
    if metric not in SUPPORTED_METRICS:
        raise ValueError(
            f"mandate limit {index}: unknown metric {metric!r} (supported: {', '.join(SUPPORTED_METRICS)})"
        )
    operator = entry.get("operator")
    if operator not in SUPPORTED_OPERATORS:
        raise ValueError(
            f"mandate limit {index}: unknown operator {operator!r} (supported: {', '.join(SUPPORTED_OPERATORS)})"
        )
    unit = entry.get("unit", "ratio")
    if unit not in SUPPORTED_UNITS:
        raise ValueError(f"mandate limit {index}: unknown unit {unit!r} (supported: {', '.join(SUPPORTED_UNITS)})")
    severity = entry.get("severity", "warning")
    if severity not in SUPPORTED_SEVERITIES:
        raise ValueError(
            f"mandate limit {index}: unknown severity {severity!r} (supported: {', '.join(SUPPORTED_SEVERITIES)})"
        )
    assert isinstance(metric, str) and isinstance(operator, str)
    assert isinstance(unit, str) and isinstance(severity, str)
    return metric, operator, unit, severity


def _limit_threshold(entry: dict[str, object], metric: str, unit: str, index: int) -> Decimal:
    """Threshold presence/range checks (existing boundary)."""
    if "threshold" not in entry:
        raise ValueError(f"mandate limit {index}: missing threshold")
    threshold = _decimal_threshold(entry["threshold"], index=index)
    if metric in ("single_position_weight", "sector_exposure") and unit != "ratio":
        raise ValueError(f"mandate limit {index}: {metric} requires unit 'ratio'")
    if unit == "ratio" and threshold > 1:
        raise ValueError(f"mandate limit {index}: threshold must be at most 1 for ratio units")
    return threshold


def _limit_target(entry: dict[str, object], metric: str, index: int) -> str | None:
    """Sector target check (existing boundary)."""
    target = entry.get("target")
    if isinstance(target, str):
        target = target.strip()
    if metric == "sector_exposure" and not (isinstance(target, str) and target.strip()):
        raise ValueError(f"mandate limit {index}: sector_exposure requires a non-empty 'target' sector")
    assert target is None or isinstance(target, str)
    return target


def _parse_limit(entry: object, index: int) -> RiskLimit:
    """One limit entry (existing boundary)."""
    if not isinstance(entry, dict):
        raise ValueError(f"mandate limit {index}: must be an object")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    metric, operator, unit, severity = _limit_vocab(entry, index)
    threshold = _limit_threshold(entry, metric, unit, index)
    return RiskLimit(
        metric=metric,
        operator=operator,
        threshold=threshold,
        target=_limit_target(entry, metric, index),
        severity=severity,
        unit=unit,
    )


def _prohibited_assets(data: dict[str, object]) -> tuple[str, ...]:
    """Prohibited-assets list check (existing boundary)."""
    prohibited = data.get("prohibited_assets", [])
    if not isinstance(prohibited, list) or not all(isinstance(item, str) and item.strip() for item in prohibited):
        raise ValueError("mandate: 'prohibited_assets' must be a list of non-empty strings")
    return tuple(item.strip() for item in prohibited)


def parse_mandate(data: Mapping[str, object]) -> Mandate:
    """Validate and build a mandate from a parsed JSON payload.

    Raises ``ValueError`` with a clear message on any malformed or
    unsupported configuration.  Unknown extra keys are ignored.
    """
    raw_limits = _limit_root(data)
    assert isinstance(data, dict)
    return Mandate(
        limits=tuple(_parse_limit(entry, index) for index, entry in enumerate(raw_limits)),
        prohibited_assets=_prohibited_assets(data),
    )
