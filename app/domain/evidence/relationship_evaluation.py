"""Deterministic point-in-time walk-forward evaluation for open relationship types.

Pure functions only: plain dicts in, plain dicts out. No I/O and no DuckDB,
SEC, broker, or vendor dependencies — the application service supplies stored
relationships plus local market observations as arguments.

Walk-forward discipline: ``windows`` are chronological ``(start, end)``
date bounds and instances are assigned to a window by ``prediction_date``.
Only evidence with ``evidence_known_at`` strictly preceding the prediction
timestamp counts as PIT-safe; anything else is a PIT violation and blocks
promotion. Random splits are never used.

Instance dict fields (all optional except the dates; tolerant defaults keep
older callers working)::

    instance_id, relationship_type, entity_id,
    prediction_date:  "YYYY-MM-DD" (the prediction timestamp, date precision)
    evidence_known_at: ISO-8601 or "YYYY-MM-DD" (max evidence known_at)
    relevant:          held-out ground truth (default False)
    predicted:         model retrieval decision (default False)
    baseline_predicted / baseline_identity_correct: matched baseline (False/True)
    identity_correct / relationship_correct / has_provenance (default True)
    agent_useful (default False)

Market inputs: ``observations`` maps ``(entity_id, date)`` to a closing
price; ``benchmark`` maps ``date`` to the benchmark level. The sorted
benchmark dates are the trading calendar, so a horizon ``h`` means ``h``
calendar steps. Missing prices are never zero-filled: any window whose
retrieved (predicted or baseline) instances lack required prices is
``incomplete`` and the type is left unchanged.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Collection, Mapping

#: Horizons (trading days) always reported separately in stored output.
HORIZONS = (1, 5, 20)

#: Promotion gate: PIT-safe evaluated instances across the evaluated windows.
MIN_PIT_SAFE_INSTANCES = 100

#: Required relative improvement over the same-date/entity-matched baseline in
#: BOTH retrieval utility (F1) and the market utility composite.
MIN_RELATIVE_IMPROVEMENT = 0.05

#: Consecutive qualifying walk-forward windows required to activate.
REQUIRED_QUALIFYING_WINDOWS = 2

#: Consecutive below-baseline windows required to demote.
DEMOTE_WINDOWS = 2

#: Ontology boost factors for ranking only. Active types sort first, demoted
#: types sort last; nothing is ever filtered by ontology state.
ACTIVE_BOOST = 1.5
DEMOTED_BOOST = 0.5


def _date_part(value: object) -> str:
    return str(value or "")[:10]


def is_pit_safe(instance: dict[str, object]) -> bool:
    """True only when evidence ``known_at`` strictly precedes the prediction."""
    known = _date_part(instance.get("evidence_known_at"))
    predicted = _date_part(instance.get("prediction_date"))
    return bool(known) and bool(predicted) and known < predicted


def _rate(items: list[dict[str, object]], key: str, default: bool) -> float:
    if not items:
        return 0.0
    return sum(1 for it in items if bool(it.get(key, default))) / len(items)


def _prf(n_tp: int, n_fp: int, n_fn: int) -> tuple[float, float, float]:
    precision = n_tp / (n_tp + n_fp) if (n_tp + n_fp) else 0.0
    recall = n_tp / (n_tp + n_fn) if (n_tp + n_fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def _improves(model: float, baseline: float) -> bool:
    """>=5% better than baseline; absolute +0.05 when the baseline is zero."""
    if baseline == 0:
        return model >= MIN_RELATIVE_IMPROVEMENT
    return (model - baseline) / abs(baseline) >= MIN_RELATIVE_IMPROVEMENT


def _forward_window(day: str, horizon: int, calendar: list[str], positions: dict[str, int]) -> str | None:
    """Later calendar date for the horizon, or None when out of range (existing boundary)."""
    start = positions.get(day)
    if start is None or start + horizon >= len(calendar):
        return None
    return calendar[start + horizon]


def _forward_prices(
    entity: object,
    day: str,
    later: str,
    observations: Mapping[tuple[str, str], int | float],
    benchmark: Mapping[str, int | float],
) -> tuple[float, float, float, float] | None:
    """Entity/benchmark price quad, or None when missing/unparseable/zero (existing boundary)."""
    entity_key = (str(entity), day)
    entity_later_key = (str(entity), later)
    if entity_key not in observations or entity_later_key not in observations:
        return None
    if day not in benchmark or later not in benchmark:
        return None
    try:
        p0 = float(observations[entity_key])
        p1 = float(observations[entity_later_key])
        b0 = float(benchmark[day])
        b1 = float(benchmark[later])
    except TypeError, ValueError:
        return None
    if p0 == 0 or b0 == 0:
        return None
    return p0, p1, b0, b1


def _forward_excess(
    entity: object,
    day: str,
    horizon: int,
    calendar: list[str],
    positions: dict[str, int],
    observations: Mapping[tuple[str, str], int | float] | None,
    benchmark: Mapping[str, int | float] | None,
) -> float | None:
    """Benchmark-adjusted forward return, or None when any price is missing."""
    if observations is None or benchmark is None:
        return None
    later = _forward_window(day, horizon, calendar, positions)
    if later is None:
        return None
    prices = _forward_prices(entity, day, later, observations, benchmark)
    if prices is None:
        return None
    p0, p1, b0, b1 = prices
    return (p1 - p0) / p0 - (b1 - b0) / b0


def _max_drawdown(ordered_excess: list[float]) -> float:
    """Max peak-to-trough decline of the cumulative excess curve."""
    peak = 0.0
    running = 0.0
    worst = 0.0
    for value in ordered_excess:
        running += value
        peak = max(peak, running)
        worst = max(worst, peak - running)
    return worst


def _instance_key(it: dict[str, object]) -> tuple[str, str]:
    """Chronological sort key: prediction date, then instance id."""
    return (_date_part(it.get("prediction_date")), str(it.get("instance_id") or ""))


def _window_instances(
    instances: list[dict[str, object]], window_start: str, window_end: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Chronological in-window plus PIT-safe split (existing boundary)."""
    in_window = sorted(
        (it for it in instances if window_start <= _date_part(it.get("prediction_date")) <= window_end),
        key=_instance_key,
    )
    return in_window, [it for it in in_window if is_pit_safe(it)]


def _confusion(safe: list[dict[str, object]], flag: str) -> tuple[int, int, int]:
    """TP/FP/FN counts for one retrieval flag (existing boundary)."""
    tp = sum(1 for it in safe if it.get(flag) and it.get("relevant"))
    fp = sum(1 for it in safe if it.get(flag) and not it.get("relevant"))
    fn = sum(1 for it in safe if not it.get(flag) and it.get("relevant"))
    return tp, fp, fn


def _model_f1(safe: list[dict[str, object]]) -> tuple[int, int, int, float]:
    """Model counts plus F1 (existing boundary)."""
    tp, fp, fn = _confusion(safe, "predicted")
    _, _, f1 = _prf(tp, fp, fn)
    return tp, fp, fn, f1


def _window_retrieval(safe: list[dict[str, object]]) -> tuple[float, float, float, float]:
    """Model/baseline F1 pair over the PIT-safe set (existing boundary)."""
    _, _, _, f1 = _model_f1(safe)
    return 0, 0, 0, f1


def _baseline_f1(safe: list[dict[str, object]]) -> float:
    """Baseline retrieval F1 over the PIT-safe set (existing boundary)."""
    b_tp = sum(1 for it in safe if it.get("baseline_predicted") and it.get("relevant"))
    b_fp = sum(1 for it in safe if it.get("baseline_predicted") and not it.get("relevant"))
    b_fn = sum(1 for it in safe if not it.get("baseline_predicted") and it.get("relevant"))
    _, _, b_f1 = _prf(b_tp, b_fp, b_fn)
    return b_f1


def _window_result(
    window_start: str,
    window_end: str,
    in_window: list[dict[str, object]],
    safe: list[dict[str, object]],
    f1: float,
    b_f1: float,
) -> dict[str, object]:
    """Neutral result skeleton with retrieval/quality metrics (existing boundary)."""
    tp, fp, fn = _confusion(safe, "predicted")
    precision, recall, _ = _prf(tp, fp, fn)
    identity = _rate(safe, "identity_correct", True)
    return {
        "window_start": window_start,
        "window_end": window_end,
        "n_instances": len(in_window),
        "n_pit_safe": len(safe),
        "pit_violations": len(in_window) - len(safe),
        "retrieval": {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "utility": f1,
            "baseline_f1": b_f1,
            "baseline_utility": b_f1,
        },
        "identity_accuracy": identity,
        "baseline_identity_accuracy": _rate(safe, "baseline_identity_correct", True),
        "relationship_accuracy": _rate(safe, "relationship_correct", True),
        "coverage": (len(safe) / len(in_window)) if in_window else 0.0,
        "provenance_completeness": _rate(safe, "has_provenance", True),
        "agent_usefulness": _rate(safe, "agent_useful", False),
        "market": {},
        "market_composite": 0.0,
        "baseline_market_composite": 0.0,
        "complete": True,
        "incomplete_reason": None,
        "qualifying": False,
        "below_baseline": False,
    }


def _horizon_stats(ordered: list[float]) -> dict[str, float]:
    """Per-horizon n/mean/vol/drawdown (existing boundary)."""
    mean = sum(ordered) / len(ordered) if ordered else 0.0
    vol = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
    return {"n": len(ordered), "mean_excess": mean, "volatility": vol, "max_drawdown": _max_drawdown(ordered)}


def _horizon_excess(
    picked: list[dict[str, object]],
    horizon: int,
    calendar: list[str],
    positions: dict[str, int],
    observations: Mapping[tuple[str, str], int | float] | None,
    benchmark: Mapping[str, int | float] | None,
) -> list[float] | None:
    """Forward excess for one horizon, None when any price is missing (existing boundary)."""
    excess = [
        _forward_excess(
            it.get("entity_id"),
            _date_part(it.get("prediction_date")),
            horizon,
            calendar,
            positions,
            observations,
            benchmark,
        )
        for it in picked
    ]
    if any(value is None for value in excess):
        return None  # missing market data is never zero-filled
    return [value for value in excess if value is not None]


def _composite_for(
    safe: list[dict[str, object]],
    flag: str,
    horizons: tuple[int, ...],
    calendar: list[str],
    positions: dict[str, int],
    observations: Mapping[tuple[str, str], int | float] | None,
    benchmark: Mapping[str, int | float] | None,
) -> tuple[dict[str, dict[str, float]], float] | None:
    """Market composite for one retrieval flag (existing boundary)."""
    picked = sorted((it for it in safe if it.get(flag)), key=_instance_key)
    per_horizon: dict[str, dict[str, float]] = {}
    parts: list[float] = []
    for horizon in horizons:
        ordered = _horizon_excess(picked, horizon, calendar, positions, observations, benchmark)
        if ordered is None:
            return None
        stats = _horizon_stats(ordered)
        per_horizon[str(horizon)] = stats
        if ordered:
            parts.append(stats["mean_excess"] - 0.5 * stats["volatility"] - 0.5 * stats["max_drawdown"])
    return per_horizon, (sum(parts) / len(parts) if parts else 0.0)


def _market_pair(
    safe: list[dict[str, object]],
    horizons: tuple[int, ...],
    calendar: list[str],
    positions: dict[str, int],
    observations: Mapping[tuple[str, str], int | float] | None,
    benchmark: Mapping[str, int | float] | None,
) -> tuple[tuple[dict[str, dict[str, float]], float], tuple[dict[str, dict[str, float]], float]] | None:
    """Model/baseline composite pair, None when either lacks prices (existing boundary)."""
    model = _composite_for(safe, "predicted", horizons, calendar, positions, observations, benchmark)
    baseline = _composite_for(safe, "baseline_predicted", horizons, calendar, positions, observations, benchmark)
    if model is None or baseline is None:
        return None
    return model, baseline


def _apply_market(
    result: dict[str, object],
    pair: tuple[tuple[dict[str, dict[str, float]], float], tuple[dict[str, dict[str, float]], float]],
) -> tuple[float, float]:
    """Store the market pair on the result (existing boundary)."""
    (model_per_horizon, model_composite), (_, baseline_composite) = pair
    result["market"], result["market_composite"] = model_per_horizon, model_composite
    result["baseline_market_composite"] = baseline_composite
    return model_composite, baseline_composite


def _result_float(result: dict[str, object], key: str) -> float:
    """float() over a stored result metric, narrowed without escapes."""
    value = result[key]
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"evaluation result {key!r} is not numeric")


def _result_int(result: dict[str, object], key: str) -> int:
    """int() over a stored result count, narrowed without escapes."""
    value = result[key]
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"evaluation result {key!r} is not an integer")


def _window_verdict(
    result: dict[str, object],
    f1: float,
    b_f1: float,
    model_composite: float,
    baseline_composite: float,
    violations: int,
    identity: float,
    baseline_identity: float,
) -> dict[str, object]:
    """Qualifying/below-baseline gates (existing boundary)."""
    result["qualifying"] = (
        _improves(f1, b_f1)
        and _improves(model_composite, baseline_composite)
        and violations == 0
        and identity >= baseline_identity
    )
    result["below_baseline"] = f1 < b_f1 or model_composite < baseline_composite
    return result


def evaluate_window(
    instances: list[dict[str, object]],
    observations: dict[tuple[str, str], float] | None,
    benchmark: dict[str, float] | None,
    window_start: str,
    window_end: str,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, object]:
    """Evaluate one chronological window; never raises on missing data."""
    in_window, safe = _window_instances(instances, window_start, window_end)
    _, _, _, f1 = _window_retrieval(safe)
    b_f1 = _baseline_f1(safe)
    result = _window_result(window_start, window_end, in_window, safe, f1, b_f1)
    if not in_window:
        return result  # empty window: complete but neutral, breaks streaks
    if not observations or not benchmark:
        result.update(complete=False, incomplete_reason="missing-observations-or-benchmark")
        return result
    calendar = sorted(d for d in benchmark)
    positions = {day: idx for idx, day in enumerate(calendar)}
    pair = _market_pair(safe, horizons, calendar, positions, observations, benchmark)
    if pair is None:
        result.update(complete=False, incomplete_reason="missing-market-prices")
        return result
    model_composite, baseline_composite = _apply_market(result, pair)
    identity = _result_float(result, "identity_accuracy")
    baseline_identity = _result_float(result, "baseline_identity_accuracy")
    return _window_verdict(
        result,
        f1,
        b_f1,
        model_composite,
        baseline_composite,
        _result_int(result, "pit_violations"),
        identity,
        baseline_identity,
    )


def evaluate_type(
    relationship_type: str,
    instances: list[dict[str, object]],
    observations: dict[tuple[str, str], float] | None,
    benchmark: dict[str, float] | None,
    windows: list[tuple[str, str]],
    horizons: tuple[int, ...] = HORIZONS,
) -> dict[str, object]:
    """Walk windows chronologically; return per-window metrics plus a decision.

    Decisions: ``activate`` (100+ PIT-safe instances and the trailing two
    windows both qualify), ``demote`` (trailing two windows below baseline),
    ``incomplete`` (some window lacks market data — type unchanged), else
    ``no_change``. A PIT leak can never qualify, so it blocks promotion.
    """
    ordered = sorted(windows)
    evaluated = [
        evaluate_window(list(instances or []), observations, benchmark, start, end, horizons) for start, end in ordered
    ]
    total_safe = _total_pit_safe(evaluated)
    decision, reason = _type_decision(evaluated, total_safe)
    return {
        "relationship_type": relationship_type,
        "windows": evaluated,
        "total_pit_safe": total_safe,
        "decision": decision,
        "reason": reason,
    }


def _total_pit_safe(evaluated: list[dict[str, object]]) -> int:
    """Summed PIT-safe instances (existing boundary)."""
    total_safe = 0
    for w in evaluated:
        n = w.get("n_pit_safe")
        if isinstance(n, int):
            total_safe += n
    return total_safe


def _type_decision(evaluated: list[dict[str, object]], total_safe: int) -> tuple[str, str]:
    """Type-level gate in decision order (existing boundary)."""
    if any(not w["complete"] for w in evaluated):
        return "incomplete", "missing-market-data-type-unchanged"
    if total_safe < MIN_PIT_SAFE_INSTANCES:
        return "no_change", f"only-{total_safe}-pit-safe-instances-need-100"
    if _trailing_qualifying(evaluated):
        return "activate", "two-consecutive-qualifying-windows"
    if _trailing_below(evaluated):
        return "demote", "two-consecutive-below-baseline-windows"
    return "no_change", "thresholds-not-met"


def _trailing_qualifying(evaluated: list[dict[str, object]]) -> bool:
    """Trailing-two qualifying streak (existing boundary)."""
    return len(evaluated) >= REQUIRED_QUALIFYING_WINDOWS and all(
        w["qualifying"] for w in evaluated[-REQUIRED_QUALIFYING_WINDOWS:]
    )


def _trailing_below(evaluated: list[dict[str, object]]) -> bool:
    """Trailing-two below-baseline streak (existing boundary)."""
    return len(evaluated) >= DEMOTE_WINDOWS and all(w["below_baseline"] for w in evaluated[-DEMOTE_WINDOWS:])


def _canon_key(key: object) -> str:
    if isinstance(key, tuple):
        return "tuple:" + "|".join(str(part) for part in key)
    return "scalar:" + str(key)


def _canon(value: object) -> object:
    if isinstance(value, dict):
        return {_canon_key(k): _canon(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def hash_inputs(payload: object) -> str:
    """Deterministic sha256 over canonical JSON for evaluation provenance."""
    return hashlib.sha256(json.dumps(_canon(payload), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def ontology_boost(label: object, active_types: Collection[str] = (), demoted_types: Collection[str] = ()) -> float:
    """Ranking-only boost for one normalized type label; never filters."""
    from .relationships import normalize_label

    key = normalize_label(label)
    active = {normalize_label(t) for t in active_types or ()}
    demoted = {normalize_label(t) for t in demoted_types or ()}
    if key in active:
        return ACTIVE_BOOST
    if key in demoted:
        return DEMOTED_BOOST
    return 1.0
