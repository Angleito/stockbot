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


def is_pit_safe(instance: dict) -> bool:
    """True only when evidence ``known_at`` strictly precedes the prediction."""
    known = _date_part(instance.get("evidence_known_at"))
    predicted = _date_part(instance.get("prediction_date"))
    return bool(known) and bool(predicted) and known < predicted


def _rate(items: list[dict], key: str, default: bool) -> float:
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


def _forward_excess(
    entity: object,
    day: str,
    horizon: int,
    calendar: list[str],
    positions: dict[str, int],
    observations: dict,
    benchmark: dict,
) -> float | None:
    """Benchmark-adjusted forward return, or None when any price is missing."""
    start = positions.get(day)
    if start is None or start + horizon >= len(calendar):
        return None
    later = calendar[start + horizon]
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
    except (TypeError, ValueError):
        return None
    if p0 == 0 or b0 == 0:
        return None
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


def evaluate_window(
    instances: list[dict],
    observations: dict | None,
    benchmark: dict | None,
    window_start: str,
    window_end: str,
    horizons: tuple[int, ...] = HORIZONS,
) -> dict:
    """Evaluate one chronological window; never raises on missing data."""
    in_window = sorted(
        (it for it in instances
         if window_start <= _date_part(it.get("prediction_date")) <= window_end),
        key=lambda it: (_date_part(it.get("prediction_date")),
                        str(it.get("instance_id") or "")),
    )
    safe = [it for it in in_window if is_pit_safe(it)]
    violations = len(in_window) - len(safe)

    tp = sum(1 for it in safe if it.get("predicted") and it.get("relevant"))
    fp = sum(1 for it in safe if it.get("predicted") and not it.get("relevant"))
    fn = sum(1 for it in safe if not it.get("predicted") and it.get("relevant"))
    precision, recall, f1 = _prf(tp, fp, fn)
    b_tp = sum(1 for it in safe if it.get("baseline_predicted") and it.get("relevant"))
    b_fp = sum(1 for it in safe if it.get("baseline_predicted") and not it.get("relevant"))
    b_fn = sum(1 for it in safe if not it.get("baseline_predicted") and it.get("relevant"))
    _, _, b_f1 = _prf(b_tp, b_fp, b_fn)

    identity = _rate(safe, "identity_correct", True)
    baseline_identity = _rate(safe, "baseline_identity_correct", True)
    result: dict = {
        "window_start": window_start,
        "window_end": window_end,
        "n_instances": len(in_window),
        "n_pit_safe": len(safe),
        "pit_violations": violations,
        "retrieval": {
            "precision": precision, "recall": recall, "f1": f1,
            "utility": f1,
            "baseline_f1": b_f1, "baseline_utility": b_f1,
        },
        "identity_accuracy": identity,
        "baseline_identity_accuracy": baseline_identity,
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
    if not in_window:
        return result  # empty window: complete but neutral, breaks streaks

    if not observations or not benchmark:
        result.update(complete=False,
                      incomplete_reason="missing-observations-or-benchmark")
        return result
    calendar = sorted(str(d) for d in benchmark)
    positions = {day: idx for idx, day in enumerate(calendar)}

    def _composite(flag: str) -> tuple[dict, float] | None:
        picked = sorted(
            (it for it in safe if it.get(flag)),
            key=lambda it: (_date_part(it.get("prediction_date")),
                            str(it.get("instance_id") or "")),
        )
        per_horizon: dict = {}
        parts: list[float] = []
        for horizon in horizons:
            excess = [
                _forward_excess(it.get("entity_id"), _date_part(it.get("prediction_date")),
                                horizon, calendar, positions, observations, benchmark)
                for it in picked
            ]
            if any(value is None for value in excess):
                return None  # missing market data is never zero-filled
            ordered = [float(v) for v in excess]  # type: ignore[misc]
            mean = sum(ordered) / len(ordered) if ordered else 0.0
            vol = statistics.pstdev(ordered) if len(ordered) > 1 else 0.0
            drawdown = _max_drawdown(ordered)
            per_horizon[str(horizon)] = {
                "n": len(ordered), "mean_excess": mean,
                "volatility": vol, "max_drawdown": drawdown,
            }
            if ordered:
                parts.append(mean - 0.5 * vol - 0.5 * drawdown)
        composite = sum(parts) / len(parts) if parts else 0.0
        return per_horizon, composite

    model_market = _composite("predicted")
    baseline_market = _composite("baseline_predicted")
    if model_market is None or baseline_market is None:
        result.update(complete=False, incomplete_reason="missing-market-prices")
        return result
    result["market"], result["market_composite"] = model_market
    _, result["baseline_market_composite"] = baseline_market


    retrieval_ok = _improves(f1, b_f1)
    market_ok = _improves(result["market_composite"], result["baseline_market_composite"])
    no_regression = violations == 0 and identity >= baseline_identity
    result["qualifying"] = bool(retrieval_ok and market_ok and no_regression)
    result["below_baseline"] = bool(
        f1 < b_f1 or result["market_composite"] < result["baseline_market_composite"])
    return result


def evaluate_type(
    relationship_type: str,
    instances: list[dict],
    observations: dict | None,
    benchmark: dict | None,
    windows: list[tuple[str, str]],
    horizons: tuple[int, ...] = HORIZONS,
) -> dict:
    """Walk windows chronologically; return per-window metrics plus a decision.

    Decisions: ``activate`` (100+ PIT-safe instances and the trailing two
    windows both qualify), ``demote`` (trailing two windows below baseline),
    ``incomplete`` (some window lacks market data — type unchanged), else
    ``no_change``. A PIT leak can never qualify, so it blocks promotion.
    """
    ordered = sorted(windows)
    evaluated = [evaluate_window(list(instances or []), observations, benchmark,
                                 start, end, horizons) for start, end in ordered]
    total_safe = sum(w["n_pit_safe"] for w in evaluated)
    if any(not w["complete"] for w in evaluated):
        decision, reason = "incomplete", "missing-market-data-type-unchanged"
    elif total_safe < MIN_PIT_SAFE_INSTANCES:
        decision, reason = "no_change", f"only-{total_safe}-pit-safe-instances-need-100"
    elif (len(evaluated) >= REQUIRED_QUALIFYING_WINDOWS
          and all(w["qualifying"] for w in evaluated[-REQUIRED_QUALIFYING_WINDOWS:])):
        decision, reason = "activate", "two-consecutive-qualifying-windows"
    elif (len(evaluated) >= DEMOTE_WINDOWS
          and all(w["below_baseline"] for w in evaluated[-DEMOTE_WINDOWS:])):
        decision, reason = "demote", "two-consecutive-below-baseline-windows"
    else:
        decision, reason = "no_change", "thresholds-not-met"
    return {
        "relationship_type": relationship_type,
        "windows": evaluated,
        "total_pit_safe": total_safe,
        "decision": decision,
        "reason": reason,
    }


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
    return hashlib.sha256(
        json.dumps(_canon(payload), sort_keys=True, default=str).encode("utf-8")).hexdigest()


def ontology_boost(label: object, active_types=(), demoted_types=()) -> float:
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
