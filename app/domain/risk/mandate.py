"""Mandate configuration: limits loaded from a JSON mandate file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

SUPPORTED_METRICS = ("single_position_weight", "minimum_cash", "sector_exposure")
SUPPORTED_OPERATORS = ("<=", ">=")
SUPPORTED_SEVERITIES = ("warning", "critical")
SUPPORTED_UNITS = ("ratio", "dollars")


@dataclass(frozen=True)
class RiskLimit:
    metric: str
    operator: str
    threshold: Decimal
    target: str | None = None       # required for sector_exposure (sector name)
    severity: str = "warning"       # "warning" | "critical"
    unit: str = "ratio"             # "ratio" | "dollars" (minimum_cash)


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


def load_mandate(path: Path) -> Mandate:
    """Load and validate a JSON mandate file.

    Raises ``ValueError`` with a clear message on any malformed or
    unsupported configuration; ``FileNotFoundError`` when the file is
    missing.  Unknown extra keys are ignored.
    """
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("mandate: root must be a JSON object")
    raw_limits = data.get("limits")
    if raw_limits is None:
        raise ValueError("mandate: 'limits' is required and must be a list")
    if not isinstance(raw_limits, list):
        raise ValueError("mandate: 'limits' must be a list")
    limits: list[RiskLimit] = []
    for index, entry in enumerate(raw_limits):
        if not isinstance(entry, dict):
            raise ValueError(f"mandate limit {index}: must be an object")
        metric = entry.get("metric")
        if metric not in SUPPORTED_METRICS:
            raise ValueError(
                f"mandate limit {index}: unknown metric {metric!r} "
                f"(supported: {', '.join(SUPPORTED_METRICS)})"
            )
        operator = entry.get("operator")
        if operator not in SUPPORTED_OPERATORS:
            raise ValueError(
                f"mandate limit {index}: unknown operator {operator!r} "
                f"(supported: {', '.join(SUPPORTED_OPERATORS)})"
            )
        unit = entry.get("unit", "ratio")
        if unit not in SUPPORTED_UNITS:
            raise ValueError(
                f"mandate limit {index}: unknown unit {unit!r} "
                f"(supported: {', '.join(SUPPORTED_UNITS)})"
            )
        if "threshold" not in entry:
            raise ValueError(f"mandate limit {index}: missing threshold")
        threshold = _decimal_threshold(entry["threshold"], index=index)
        if metric in ("single_position_weight", "sector_exposure") and unit != "ratio":
            raise ValueError(f"mandate limit {index}: {metric} requires unit 'ratio'")
        if unit == "ratio" and threshold > 1:
            raise ValueError(f"mandate limit {index}: threshold must be at most 1 for ratio units")
        severity = entry.get("severity", "warning")
        if severity not in SUPPORTED_SEVERITIES:
            raise ValueError(
                f"mandate limit {index}: unknown severity {severity!r} "
                f"(supported: {', '.join(SUPPORTED_SEVERITIES)})"
            )
        target = entry.get("target")
        if metric == "sector_exposure" and not (isinstance(target, str) and target.strip()):
            raise ValueError(
                f"mandate limit {index}: sector_exposure requires a non-empty 'target' sector"
            )
        limits.append(
            RiskLimit(
                metric=metric,
                operator=operator,
                threshold=threshold,
                target=target,
                severity=severity,
                unit=unit,
            )
        )
    prohibited = data.get("prohibited_assets", [])
    if not isinstance(prohibited, list) or not all(
        isinstance(item, str) and item.strip() for item in prohibited
    ):
        raise ValueError("mandate: 'prohibited_assets' must be a list of non-empty strings")
    return Mandate(limits=tuple(limits), prohibited_assets=tuple(prohibited))
