"""Shared live-evaluation case/result contracts and normalized quality scoring.

First production slice only: seven judge dimensions with fixed weights,
strict stdlib validation, JSON-compatible ``as_dict`` boundaries.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

DIMENSION_WEIGHTS: dict[str, int] = {
    "factual_correctness": 15,
    "evidence_entailment": 10,
    "task_coverage": 6,
    "causal_reasoning": 9,
    "uncertainty": 7,
    "question_fidelity": 5,
    "decision_usefulness": 5,
}

ALL_DIMENSIONS: tuple[str, ...] = (
    "factual_correctness",
    "evidence_entailment",
    "task_coverage",
    "causal_reasoning",
    "uncertainty",
    "question_fidelity",
    "decision_usefulness",
)

_VALID_DIMENSIONS = frozenset(ALL_DIMENSIONS)

_REQUIRED_FIELDS = frozenset(
    {
        "id",
        "question",
        "category",
        "as_of",
        "requires_research",
        "requires_counterevidence",
        "requires_point_in_time",
        "expected_branches",
        "explicit_tasks",
        "applicable_dimensions",
    }
)

_ALLOWED_FIELDS = frozenset({*_REQUIRED_FIELDS, "prompt_injection_markers", "out_of_scope"})


@dataclass(frozen=True)
class LiveEvalCase:
    """One live-evaluation case with strict validated fields."""

    id: str
    question: str
    category: str
    as_of: str | None
    requires_research: bool
    requires_counterevidence: bool
    requires_point_in_time: bool
    expected_branches: tuple[str, ...]
    explicit_tasks: tuple[str, ...]
    applicable_dimensions: tuple[str, ...]
    prompt_injection_markers: tuple[str, ...] = ()
    out_of_scope: bool = False

    def as_dict(self) -> dict[str, object]:
        """JSON-compatible mapping with the contract field names."""
        return {
            "id": self.id,
            "question": self.question,
            "category": self.category,
            "as_of": self.as_of,
            "requires_research": self.requires_research,
            "requires_counterevidence": self.requires_counterevidence,
            "requires_point_in_time": self.requires_point_in_time,
            "expected_branches": list(self.expected_branches),
            "explicit_tasks": list(self.explicit_tasks),
            "applicable_dimensions": list(self.applicable_dimensions),
            "prompt_injection_markers": list(self.prompt_injection_markers),
            "out_of_scope": self.out_of_scope,
        }


def _field_str(d: Mapping[str, object], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where}.{key}: must be a non-empty string, got {v!r}")
    return v


def _field_bool(d: Mapping[str, object], key: str, where: str) -> bool:
    v = d.get(key)
    if type(v) is not bool:
        raise ValueError(f"{where}.{key}: must be a boolean, got {v!r}")
    return v


def _field_list_str(d: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    v = d.get(key)
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError(f"{where}.{key}: must be a list of strings, got {v!r}")
    if any(not x.strip() for x in v):
        raise ValueError(f"{where}.{key}: must contain non-empty strings, got {v!r}")
    return tuple(v)


def _field_as_of(d: Mapping[str, object], key: str, where: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where}.{key}: must be an ISO-8601 date/datetime, 'unbounded', or null, got {v!r}")
    s = v.strip()
    if s.lower() == "unbounded":
        return s
    try:
        datetime.fromisoformat(s)
        return s
    except ValueError:
        pass
    try:
        date.fromisoformat(s)
        return s
    except ValueError:
        raise ValueError(f"{where}.{key}: must be an ISO-8601 date/datetime, 'unbounded', or null, got {v!r}") from None


def _field_dimensions(d: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    v = d.get(key)
    if not isinstance(v, list) or not v or any(not isinstance(x, str) for x in v):
        raise ValueError(f"{where}.{key}: must be a non-empty list of strings, got {v!r}")
    for x in v:
        if x not in _VALID_DIMENSIONS:
            raise ValueError(f"{where}.{key}: unknown dimension {x!r} (valid: {sorted(_VALID_DIMENSIONS)})")
    if len(set(v)) != len(v):
        raise ValueError(f"{where}.{key}: duplicate dimensions, got {v!r}")
    return tuple(v)


def _opt_markers(d: Mapping[str, object], key: str, where: str) -> tuple[str, ...]:
    if key not in d:
        return ()
    v = d.get(key)
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError(f"{where}.{key}: must be a list of strings, got {v!r}")
    if any(not x.strip() for x in v):
        raise ValueError(f"{where}.{key}: must contain non-empty strings, got {v!r}")
    return tuple(v)


def _opt_out_of_scope(d: Mapping[str, object], key: str, where: str) -> bool:
    if key not in d:
        return False
    v = d.get(key)
    if type(v) is not bool:
        raise ValueError(f"{where}.{key}: must be a boolean, got {v!r}")
    return v


def _validate_case_dict(d: object, index: int, path: Path) -> LiveEvalCase:
    where = f"{path}: cases[{index}]"
    if not isinstance(d, dict):
        raise ValueError(f"{where}: must be an object, got {type(d).__name__}")
    unknown = sorted(set(d) - _ALLOWED_FIELDS)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {unknown}")
    missing = sorted(_REQUIRED_FIELDS - set(d))
    if missing:
        raise ValueError(f"{where}: missing field(s) {missing}")
    m: Mapping[str, object] = d
    return LiveEvalCase(
        id=_field_str(m, "id", where),
        question=_field_str(m, "question", where),
        category=_field_str(m, "category", where),
        as_of=_field_as_of(m, "as_of", where),
        requires_research=_field_bool(m, "requires_research", where),
        requires_counterevidence=_field_bool(m, "requires_counterevidence", where),
        requires_point_in_time=_field_bool(m, "requires_point_in_time", where),
        expected_branches=_field_list_str(m, "expected_branches", where),
        explicit_tasks=_field_list_str(m, "explicit_tasks", where),
        applicable_dimensions=_field_dimensions(m, "applicable_dimensions", where),
        prompt_injection_markers=_opt_markers(m, "prompt_injection_markers", where),
        out_of_scope=_opt_out_of_scope(m, "out_of_scope", where),
    )


def load_cases(path: str | Path) -> tuple[LiveEvalCase, ...]:
    """Load and strictly validate live cases; unknown/malformed fields fail."""
    p = Path(path)
    try:
        raw: object = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: invalid JSON: {exc}") from None
    if isinstance(raw, dict) and "cases" in raw:
        items = raw["cases"]
        where = f"{p}: 'cases'"
    elif isinstance(raw, list):
        items = raw
        where = f"{p}"
    else:
        raise ValueError(f"{p}: must be a JSON array or {{'cases': [...]}} object")
    if not isinstance(items, list):
        raise ValueError(f"{where}: must be a list, got {type(items).__name__}")
    out: list[LiveEvalCase] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        case = _validate_case_dict(item, i, p)
        if case.id in seen:
            raise ValueError(f"{p}: cases[{i}].id: duplicate case id {case.id!r}")
        seen.add(case.id)
        out.append(case)
    return tuple(out)


def compute_quality_score(case: LiveEvalCase, dimension_scores: Mapping[str, int]) -> float:
    """Normalized 0..100 quality over applicable dimensions only."""
    if not isinstance(dimension_scores, Mapping):
        raise ValueError(f"dimension_scores: must be a mapping, got {type(dimension_scores).__name__}")
    for k, v in dimension_scores.items():
        if k not in _VALID_DIMENSIONS:
            raise ValueError(f"dimension_scores.{k}: unknown dimension {k!r} (valid: {sorted(_VALID_DIMENSIONS)})")
        if type(v) is not int or not 0 <= v <= 4:
            raise ValueError(f"dimension_scores.{k}: must be an int 0..4, got {v!r}")
    if not case.applicable_dimensions:
        raise ValueError("case.applicable_dimensions: must be non-empty")
    missing = [d for d in case.applicable_dimensions if d not in dimension_scores]
    if missing:
        raise ValueError(f"dimension_scores: missing applicable dimension(s) {missing}")
    total_w = sum(DIMENSION_WEIGHTS[d] for d in case.applicable_dimensions)
    if total_w <= 0:
        raise ValueError("dimension weights: total must be positive")
    weighted = sum(DIMENSION_WEIGHTS[d] * (dimension_scores[d] / 4) for d in case.applicable_dimensions)
    return weighted / total_w * 100.0
