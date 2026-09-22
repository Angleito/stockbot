"""Live Stockbot evaluation suite: real HTTP route + deterministic gates + judge medians.

Production path invokes only the real Next ``/api/agent`` route (stdlib
urllib) plus the shared peer contracts::

    quality_models.load_cases / compute_quality_score
    hard_gates.evaluate_hard_gates
    quality_judge.judge_answer

Peer payloads stay opaque: cases are read via attribute-or-mapping, quality
and operations are derived here, and gate/judge results are embedded only
through their ``as_dict()`` mappings. Operational metrics never affect
quality. Per-case transport/judge faults are recorded as case errors
(exit 2); hard-gate failures on completed cases are exit 1.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PROMPT_VERSION = "v1"
CASE_VERSION = "v1"
PRODUCTION_MODEL = "muse-spark-1.3-contributor"

# Mirror of the ticket weights; quality_models.compute_quality_score is
# authoritative when importable — this only keeps the boundary deterministic.
DIMENSION_WEIGHTS: dict[str, float] = {
    "factual_correctness": 15.0,
    "evidence_entailment": 10.0,
    "task_coverage": 6.0,
    "causal_reasoning": 9.0,
    "uncertainty": 7.0,
    "question_fidelity": 5.0,
    "decision_usefulness": 5.0,
}

AGENT_PATH = "/api/agent"
DEFAULT_TIMEOUT_S = 600.0

HttpPost = Callable[[str, dict[str, str], dict[str, object], float], tuple[int, str]]


class LiveEvalError(Exception):
    """Base live-eval failure."""


class LiveEvalInfraError(LiveEvalError):
    """Infrastructure/transport/malformed failure (exit 2)."""

    def __init__(self, message: str, partial_artifact: object = None) -> None:
        super().__init__(message)
        self.partial_artifact = partial_artifact


def _case_get(case: object, name: str, default: object = None) -> object:
    if isinstance(case, Mapping):
        value: object = case.get(name, default)
        return value
    attr: object = getattr(case, name, default)
    return attr


def _case_id(case: object) -> str:
    raw = _case_get(case, "id", "unknown")
    return str(raw) if raw is not None else "unknown"


def _case_question(case: object) -> str:
    raw = _case_get(case, "question", _case_get(case, "prompt", ""))
    return str(raw) if raw is not None else ""


def _as_dict_opaque(obj: object) -> dict[str, object]:
    as_dict = getattr(obj, "as_dict", None)
    if callable(as_dict):
        out: object = as_dict()
        if isinstance(out, Mapping):
            return {str(k): v for k, v in out.items()}
    if isinstance(obj, Mapping):
        return {str(k): v for k, v in obj.items()}
    return {"value": obj}


def parse_sse_body(body: str) -> list[dict[str, object]]:
    """Parse a ``text/event-stream`` body into event dicts; strict."""
    if not isinstance(body, str):
        raise LiveEvalInfraError(f"SSE body must be str, got {type(body).__name__}")
    events: list[dict[str, object]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        data_lines = [ln[5:] for ln in block.split("\n") if ln.startswith("data:")]
        if not data_lines:
            raise LiveEvalInfraError(f"SSE block without data line: {block[:120]!r}")
        try:
            decoded: object = json.loads("\n".join(data_lines))
        except json.JSONDecodeError as exc:
            raise LiveEvalInfraError(f"malformed SSE JSON: {exc}") from exc
        if not isinstance(decoded, dict):
            raise LiveEvalInfraError("SSE event must be a JSON object")
        events.append({str(k): v for k, v in decoded.items()})
    return events


def _extract_completed(events: Sequence[Mapping[str, object]]) -> tuple[str, dict[str, object], dict[str, object]]:
    """Concatenate answer deltas; require exactly one trace before one done."""
    answer_parts: list[str] = []
    traces: list[object] = []
    trace_idx = done_idx = -1
    done_metrics: dict[str, object] = {}
    done_count = 0
    for i, event in enumerate(events):
        etype = event.get("type")
        if etype == "answer_delta":
            text = event.get("text", "")
            if not isinstance(text, str):
                raise LiveEvalInfraError("answer_delta text must be a string")
            answer_parts.append(text)
        elif etype == "evaluation_trace":
            traces.append(event.get("trace"))
            if trace_idx == -1:
                trace_idx = i
        elif etype == "done":
            done_count += 1
            done_idx = i
            metrics = event.get("metrics", {})
            done_metrics = dict(metrics) if isinstance(metrics, Mapping) else {}
    if len(traces) != 1:
        raise LiveEvalInfraError(f"expected exactly one evaluation_trace, got {len(traces)}")
    if done_count != 1:
        raise LiveEvalInfraError(f"expected exactly one terminal done event, got {done_count}")
    if not isinstance(traces[0], Mapping):
        raise LiveEvalInfraError("evaluation_trace trace must be an object")
    if trace_idx > done_idx:
        raise LiveEvalInfraError("evaluation_trace must precede terminal done")
    return "".join(answer_parts), dict(traces[0]), done_metrics


def _default_http_post(
    url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float
) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status: object = getattr(resp, "status", 200)
            code = status if isinstance(status, int) and not isinstance(status, bool) else 200
            return code, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return int(exc.code), err_body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LiveEvalInfraError(f"transport error POST {url}: {exc}") from exc


def _post_one_case(
    base_url: str,
    token: str,
    prompt: str,
    as_of: object,
    timeout_s: float,
    http_post: HttpPost | None,
    case: object = None,
) -> tuple[list[dict[str, object]], float]:
    url = base_url.rstrip("/") + AGENT_PATH
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    payload: dict[str, object] = {"prompt": prompt, "asOf": as_of, "includeTrace": True}
    seed = _case_get(case, "eval_seed_evidence", None)
    if isinstance(seed, (list, tuple)) and seed:
        items: list[dict[str, object]] = []
        for entry in seed:
            raw = entry.as_dict() if hasattr(entry, "as_dict") else entry
            if isinstance(raw, Mapping):
                rec = {str(k): raw[k] for k in ("id", "source", "content", "title") if k in raw}
                if isinstance(rec.get("id"), str) and isinstance(rec.get("content"), str):
                    items.append(rec)
        if items:
            payload["evalSeedEvidence"] = items
    post = http_post or _default_http_post
    start = time.monotonic()
    try:
        status, body = post(url, headers, payload, timeout_s)
    except LiveEvalInfraError:
        raise
    except Exception as exc:
        raise LiveEvalInfraError(f"transport error POST {url}: {exc}") from exc
    latency_ms = (time.monotonic() - start) * 1000.0
    if status != 200:
        raise LiveEvalInfraError(f"agent HTTP {status}: {body[:300]}")
    return parse_sse_body(body), latency_ms


def _evaluate_gates(case: object, answer: str, trace: Mapping[str, object]) -> dict[str, object]:
    from app.research.evals.hard_gates import evaluate_hard_gates  # type: ignore[import-not-found]

    out: object = evaluate_hard_gates(case, answer, trace)  # type: ignore[arg-type]
    if not isinstance(out, Mapping):
        raise LiveEvalInfraError("hard-gate result must be a mapping")
    return dict(out)


def _judge_case(case: object, answer: str, trace: Mapping[str, object], judge_client: object = None) -> object:
    from app.research.evals.quality_judge import judge_answer  # type: ignore[import-not-found]

    return judge_answer(case, answer, trace, client=judge_client, runs=3)


def _fallback_quality_score(case: object, dimension_scores: Mapping[str, object]) -> float:
    applicable = _case_get(case, "applicable_dimensions", None)
    if isinstance(applicable, (list, tuple)):
        dims = [str(d) for d in applicable if str(d) in DIMENSION_WEIGHTS]
    else:
        dims = [str(d) for d in dimension_scores if str(d) in DIMENSION_WEIGHTS]
    total = sum(DIMENSION_WEIGHTS[d] for d in dims)
    if total <= 0:
        return 0.0
    weighted = 0.0
    for dim in dims:
        value = dimension_scores.get(dim, 0)
        rating = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        weighted += rating / 4.0 * DIMENSION_WEIGHTS[dim]
    return max(0.0, min(100.0, weighted / total * 100.0))


def _quality_score(case: object, dimension_scores: Mapping[str, object]) -> float:
    try:
        from app.research.evals.quality_models import LiveEvalCase, compute_quality_score  # type: ignore[import-not-found]
    except ImportError:
        return _fallback_quality_score(case, dimension_scores)
    if isinstance(case, LiveEvalCase):
        typed_case = case
    elif isinstance(case, Mapping):
        typed_case = LiveEvalCase(
            id=str(case.get("id", "unknown")),
            question=str(case.get("question", case.get("prompt", ""))),
            category=str(case.get("category", "live")),
            as_of=case.get("as_of") if isinstance(case.get("as_of"), str) or case.get("as_of") is None else None,
            requires_research=case.get("requires_research", False) is True,
            requires_counterevidence=case.get("requires_counterevidence", False) is True,
            requires_point_in_time=case.get("requires_point_in_time", False) is True,
            expected_branches=tuple(x for x in case.get("expected_branches", []) if isinstance(x, str))
            if isinstance(case.get("expected_branches"), (list, tuple))
            else (),
            explicit_tasks=tuple(x for x in case.get("explicit_tasks", []) if isinstance(x, str))
            if isinstance(case.get("explicit_tasks"), (list, tuple))
            else (),
            applicable_dimensions=tuple(
                x for x in case.get("applicable_dimensions", []) if isinstance(x, str) and x in DIMENSION_WEIGHTS
            )
            if isinstance(case.get("applicable_dimensions"), (list, tuple))
            else tuple(str(d) for d in dimension_scores if str(d) in DIMENSION_WEIGHTS),
            prompt_injection_markers=tuple(x for x in case.get("prompt_injection_markers", []) if isinstance(x, str))
            if isinstance(case.get("prompt_injection_markers"), (list, tuple))
            else (),
            out_of_scope=case.get("out_of_scope", False) is True,
        )
    else:
        return _fallback_quality_score(case, dimension_scores)
    clean_scores: dict[str, int] = {}
    for key, value in dimension_scores.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise LiveEvalInfraError(f"judge score {key!r} must be int 0..4")
        clean_scores[str(key)] = value
    return float(compute_quality_score(typed_case, clean_scores))


def _gate_passed(key: str, result: object, as_dict: Mapping[str, object]) -> bool:
    passed = getattr(result, "passed", None)
    if passed is None:
        passed = result.get("passed") if isinstance(result, Mapping) else as_dict.get("passed")
    if passed is None and isinstance(as_dict, Mapping):
        passed = as_dict.get("passed")
    if isinstance(passed, bool):
        return passed
    raise LiveEvalInfraError(f"hard gate {key!r} missing bool passed")


def _judge_scores(aggregate: object, as_dict: Mapping[str, object]) -> dict[str, int]:
    raw: object = getattr(aggregate, "scores", None)
    if raw is None:
        raw = as_dict.get("scores")
    if not isinstance(raw, Mapping) or not raw:
        raise LiveEvalInfraError("judge scores must be a nonempty mapping")
    scores: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
            raise LiveEvalInfraError(f"judge score {key!r} must be int 0..4")
        scores[str(key)] = value
    return scores


def _judge_disagreement(aggregate: object, as_dict: Mapping[str, object]) -> float:
    raw: object = getattr(aggregate, "disagreement", None)
    if raw is None:
        raw = as_dict.get("disagreement", 0.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(raw)))


def _first_list(trace: Mapping[str, object], *keys: str) -> list[object]:
    for key in keys:
        value = trace.get(key)
        if isinstance(value, list):
            return list(value)
    return []


def _evidence_list(trace: Mapping[str, object]) -> list[object]:
    return _first_list(trace, "accepted_evidence", "evidence", "evidenceRecords", "evidence_records")


def _trace_sources(trace: Mapping[str, object]) -> list[str]:
    sources: set[str] = set()
    for entry in _evidence_list(trace):
        if isinstance(entry, Mapping):
            source = entry.get("source")
            if isinstance(source, str) and source.strip():
                sources.add(source.strip())
    return sorted(sources)


def _safe_int(*candidates: object) -> int | None:
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    return None


def _safe_float(*candidates: object) -> float | None:
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _tool_success_failure(trace: Mapping[str, object], failures: Mapping[str, object]) -> tuple[int | None, int | None]:
    calls = _first_list(trace, "toolCalls", "tool_calls", "toolResults", "tool_results")
    ok_true = ok_false = 0
    seen = False
    for entry in calls:
        if isinstance(entry, Mapping) and isinstance(entry.get("ok"), bool):
            seen = True
            if entry["ok"] is True:
                ok_true += 1
            else:
                ok_false += 1
    if seen:
        return ok_true, ok_false
    if not failures:
        return None, None
    total = sum(v for v in failures.values() if isinstance(v, int) and not isinstance(v, bool))
    return None, total


def _operational_metrics(
    latency_ms: float, metrics: Mapping[str, object], trace: Mapping[str, object]
) -> dict[str, object]:
    muse = metrics.get("muse")
    tools = metrics.get("tools")
    evm = metrics.get("evidence")
    failures = metrics.get("failures")
    muse_map: Mapping[str, object] = muse if isinstance(muse, Mapping) else {}
    tools_map: Mapping[str, object] = tools if isinstance(tools, Mapping) else {}
    evm_map: Mapping[str, object] = evm if isinstance(evm, Mapping) else {}
    failures_map = failures if isinstance(failures, Mapping) else {}
    input_tokens = _safe_int(muse_map.get("inputTokens"), muse_map.get("input_tokens"))
    output_tokens = _safe_int(muse_map.get("outputTokens"), muse_map.get("output_tokens"))
    total_tokens: int | None = None
    if input_tokens is not None or output_tokens is not None:
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
    successes, tool_failures = _tool_success_failure(trace, failures_map)
    evidence_count = _safe_int(evm_map.get("count"))
    if evidence_count is None:
        evidence_count = len(_evidence_list(trace))
    return {
        "latency_ms": float(latency_ms),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": _safe_float(muse_map.get("cost"), muse_map.get("estimated_cost")),
        "tool_calls": _safe_int(tools_map.get("calls")),
        "tool_successes": successes,
        "tool_failures": tool_failures,
        "sources": _trace_sources(trace),
        "evidence_count": evidence_count,
        "raw_metrics": dict(metrics),
    }


def _objective_measurements(case: object, answer: str, trace: Mapping[str, object]) -> dict[str, object]:
    from app.research.evals.hard_gates import measure_objectives  # type: ignore[import-not-found]

    branches = _case_get(case, "expected_branches", [])
    tasks = _case_get(case, "explicit_tasks", [])
    base: dict[str, object] = {
        "answer_present": bool(answer and answer.strip()),
        "answer_chars": len(answer or ""),
        "evidence_count": len(_evidence_list(trace)),
        "expected_branches": list(branches) if isinstance(branches, (list, tuple)) else [],
        "expected_tasks": list(tasks) if isinstance(tasks, (list, tuple)) else [],
    }
    try:
        measured: object = measure_objectives(case, answer, trace)  # type: ignore[arg-type]
    except Exception:
        return base
    if isinstance(measured, Mapping):
        base["objective_scores"] = {str(k): v for k, v in measured.items()}
    return base


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
        sha = proc.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _production_model_actual(results: Sequence[CaseResult] | None = None) -> str:
    """Runtime-resolved model: first reasoning_start model across cases, else PRODUCTION_MODEL."""
    try:
        for result in results or ():
            for event in result.events:
                if (
                    isinstance(event, Mapping)
                    and event.get("type") == "reasoning_start"
                    and isinstance(event.get("model"), str)
                    and str(event.get("model")).strip()
                ):
                    return str(event.get("model")).strip()
    except Exception:
        pass
    return PRODUCTION_MODEL


def _case_set_sha256(cases: Sequence[object]) -> str:
    try:
        parts: list[str] = []
        for case in cases:
            as_dict = getattr(case, "as_dict", None)
            if callable(as_dict):
                parts.append(json.dumps(as_dict(), sort_keys=True, default=str))
            elif isinstance(case, Mapping):
                parts.append(json.dumps(dict(case), sort_keys=True, default=str))
            else:
                parts.append(str(case))
        return _sha256_text("\n".join(parts))
    except Exception:
        return "unknown"


def _judge_rubric_sha256() -> str:
    try:
        from app.research.evals.quality_judge import _DIMENSION_RUBRIC

        return _sha256_text(json.dumps(_DIMENSION_RUBRIC, sort_keys=True, default=str))
    except Exception:
        return "unknown"


def _production_prompt_sha256() -> str:
    """Best-effort hash of the production writer prompt (Muse SYSTEM); unknown when unreadable."""
    try:
        from pathlib import Path as _Path

        for candidate in (
            _Path("needle-harness/lib/muse/client.ts"),
            _Path(__file__).resolve().parent.parent.parent.parent / "needle-harness" / "lib" / "muse" / "client.ts",
        ):
            if candidate.exists():
                return _sha256_text(candidate.read_text(encoding="utf-8"))
        return "unknown"
    except Exception:
        return "unknown"


@dataclass
class CaseResult:
    case_id: str
    question: str
    answer: str = ""
    events: list[dict[str, object]] = field(default_factory=list)
    trace: dict[str, object] = field(default_factory=dict)
    hard_gates: dict[str, object] = field(default_factory=dict)
    hard_passed: bool = False
    hard_failures: list[str] = field(default_factory=list)
    objective: dict[str, object] = field(default_factory=dict)
    judge: dict[str, object] | None = None
    judge_scores: dict[str, int] = field(default_factory=dict)
    disagreement: float | None = None
    score: float = 0.0
    operational_metrics: dict[str, object] = field(default_factory=dict)
    passed: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.case_id,
            "question": self.question,
            "answer": self.answer,
            "events": list(self.events),
            "trace": dict(self.trace),
            "hard_gates": dict(self.hard_gates),
            "hard_passed": self.hard_passed,
            "hard_failures": list(self.hard_failures),
            "objective": dict(self.objective),
            "judge": dict(self.judge) if isinstance(self.judge, Mapping) else None,
            "judge_scores": dict(self.judge_scores),
            "disagreement": self.disagreement,
            "score": float(self.score),
            "operational_metrics": dict(self.operational_metrics),
            "passed": self.passed,
            "errors": list(self.errors),
        }


@dataclass
class RunArtifact:
    run: dict[str, object] = field(default_factory=dict)
    cases: tuple[CaseResult, ...] = ()
    summary: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "run": dict(self.run),
            "cases": [c.as_dict() for c in self.cases],
            "summary": dict(self.summary),
        }


def _infra_case_result(case: object, message: str) -> CaseResult:
    return CaseResult(
        case_id=_case_id(case),
        question=_case_question(case),
        hard_passed=False,
        passed=False,
        errors=[message],
        operational_metrics={"latency_ms": 0.0, "sources": [], "evidence_count": 0, "raw_metrics": {}},
        objective={
            "answer_present": False,
            "answer_chars": 0,
            "evidence_count": 0,
            "expected_branches": [],
            "expected_tasks": [],
        },
    )


def run_one_case(
    case: object,
    base_url: str,
    token: str,
    judge_client: object = None,
    http_post: HttpPost | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> CaseResult:
    """Run one case through the real HTTP route; transport/judge faults raise infra."""
    if not base_url or not token:
        raise LiveEvalInfraError("base_url and token are required")
    question = _case_question(case)
    if not question.strip():
        raise LiveEvalInfraError(f"case {_case_id(case)!r} has an empty question")
    events, latency_ms = _post_one_case(base_url, token, question, _case_get(case, "as_of"), timeout_s, http_post, case)
    answer, trace, done_metrics = _extract_completed(events)
    gates_raw = _evaluate_gates(case, answer, trace)
    hard_gates: dict[str, object] = {}
    hard_failures: list[str] = []
    for key, result in gates_raw.items():
        gate_dict = _as_dict_opaque(result)
        hard_gates[str(key)] = gate_dict
        if not _gate_passed(str(key), result, gate_dict):
            hard_failures.append(str(key))
    hard_failures.sort()
    try:
        aggregate = _judge_case(case, answer, trace, judge_client)
    except LiveEvalInfraError:
        raise
    except Exception as exc:
        raise LiveEvalInfraError(f"judge failed for case {_case_id(case)!r}: {exc}") from exc
    judge_dict = _as_dict_opaque(aggregate)
    scores = _judge_scores(aggregate, judge_dict)
    disagreement = _judge_disagreement(aggregate, judge_dict)
    try:
        score = float(_quality_score(case, scores))
    except LiveEvalInfraError:
        raise
    except Exception as exc:
        raise LiveEvalInfraError(f"quality scoring failed for case {_case_id(case)!r}: {exc}") from exc
    hard_passed = not hard_failures
    return CaseResult(
        case_id=_case_id(case),
        question=question,
        answer=answer,
        events=list(events),
        trace=dict(trace),
        hard_gates=hard_gates,
        hard_passed=hard_passed,
        hard_failures=hard_failures,
        objective=_objective_measurements(case, answer, trace),
        judge=dict(judge_dict),
        judge_scores=scores,
        disagreement=disagreement,
        score=score,
        operational_metrics=_operational_metrics(latency_ms, done_metrics, trace),
        passed=hard_passed,
        errors=[],
    )


def _case_score(result: CaseResult) -> float:
    return result.score


def _summarize(results: Sequence[CaseResult]) -> dict[str, object]:
    completed = [c for c in results if not c.errors]
    passed = [c for c in completed if c.passed]
    failed = [c for c in completed if not c.passed]
    scores = [c.score for c in completed]
    dim_vals: dict[str, list[float]] = {}
    for case in completed:
        for dim, value in case.judge_scores.items():
            dim_vals.setdefault(str(dim), []).append(float(value))
    disagreements: list[float] = []
    for case in completed:
        if isinstance(case.disagreement, (int, float)) and not isinstance(case.disagreement, bool):
            disagreements.append(float(case.disagreement))
    ops = [c.operational_metrics for c in results if isinstance(c.operational_metrics, Mapping)]
    lowest = sorted(completed, key=_case_score)[:3]
    ops_integers: dict[str, int] = {"input": 0, "output": 0, "tools": 0, "evidence": 0}
    ops_floats: dict[str, float] = {"latency": 0.0, "cost": 0.0}
    for op in ops:
        ops_floats["latency"] += float(_safe_float(op.get("latency_ms")) or 0.0)
        for total_key, op_key in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("tools", "tool_calls"),
            ("evidence", "evidence_count"),
        ):
            hit = _safe_int(op.get(op_key))
            if hit is not None:
                ops_integers[total_key] += hit
        cost = _safe_float(op.get("estimated_cost"))
        if cost is not None:
            ops_floats["cost"] += cost
    return {
        "total": len(results),
        "completed": len(completed),
        "passed": len(passed),
        "failed": len(failed),
        "infra_errors": len(results) - len(completed),
        "mean_quality": float(statistics.fmean(scores)) if scores else 0.0,
        "median_quality": float(statistics.median(scores)) if scores else 0.0,
        "dimension_means": {d: float(statistics.fmean(v)) for d, v in sorted(dim_vals.items())},
        "mean_disagreement": float(statistics.fmean(disagreements)) if disagreements else 0.0,
        "hard_failures": {c.case_id: list(c.hard_failures) for c in failed},
        "lowest_cases": [{"id": c.case_id, "score": float(c.score)} for c in lowest],
        "operations": {
            "total_latency_ms": ops_floats["latency"],
            "total_input_tokens": ops_integers["input"],
            "total_output_tokens": ops_integers["output"],
            "total_cost": ops_floats["cost"],
            "total_tool_calls": ops_integers["tools"],
            "total_evidence": ops_integers["evidence"],
        },
    }


def run_live_suite(
    cases: Sequence[object],
    base_url: str,
    token: str,
    judge_client: object = None,
    http_post: HttpPost | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> RunArtifact:
    """Run every case; per-case infra faults are recorded, never raised."""
    if not base_url or not token:
        raise LiveEvalInfraError("base_url and token are required")
    if not cases:
        raise LiveEvalInfraError("no cases to run")
    started = datetime.now(UTC).isoformat()
    results: list[CaseResult] = []
    for case in cases:
        try:
            results.append(run_one_case(case, base_url, token, judge_client, http_post, timeout_s))
        except LiveEvalInfraError as exc:
            results.append(_infra_case_result(case, str(exc)))
        except Exception as exc:
            results.append(_infra_case_result(case, f"unexpected {type(exc).__name__}: {exc}"))
    run: dict[str, object] = {
        "git_sha": _git_sha(),
        "started_at_utc": started,
        "production_model": PRODUCTION_MODEL,
        "production_model_actual": _production_model_actual(results),
        "judge_model": os.environ.get("STOCKBOT_EVAL_JUDGE_MODEL", "unknown"),
        "judge_rubric_sha256": _judge_rubric_sha256(),
        "production_prompt_sha256": _production_prompt_sha256(),
        "case_set_sha256": _case_set_sha256(cases),
        "prompt_version": PROMPT_VERSION,
        "case_version": CASE_VERSION,
        "case_count": len(results),
        "base_url": base_url.rstrip("/"),
    }
    return RunArtifact(run=run, cases=tuple(results), summary=_summarize(results))


def artifact_exit_code(artifact: RunArtifact) -> int:
    """0 all-hard-pass, 1 completed hard failures, 2 any infra errors."""
    if any(c.errors for c in artifact.cases):
        return 2
    if any(not c.passed for c in artifact.cases):
        return 1
    return 0


def write_artifact(artifact: RunArtifact, path: str | Path) -> Path:
    """Write one indented JSON artifact atomically; refuse an existing path."""
    dest = Path(path)
    if dest.exists():
        raise LiveEvalInfraError(f"refuse to overwrite existing artifact: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise LiveEvalInfraError(f"refuse to overwrite existing artifact: {dest}")
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(artifact.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, dest)
    return dest


def _report_num(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def format_report(artifact: RunArtifact) -> str:
    """Compact stdout report: lowest cases, hard failures, dimension means, operations."""
    s = artifact.summary
    lines = [
        f"Stockbot live eval: {int(_report_num(s.get('passed')))}"
        f"/{int(_report_num(s.get('completed')))} passed "
        f"({int(_report_num(s.get('total')))} total, {int(_report_num(s.get('infra_errors')))} infra errors)",
        f"Mean quality: {_report_num(s.get('mean_quality')):.1f}  "
        f"Median: {_report_num(s.get('median_quality')):.1f}  "
        f"Mean disagreement: {_report_num(s.get('mean_disagreement')):.2f}",
        "Lowest cases:",
    ]
    lowest = s.get("lowest_cases")
    lowest_rows: list[object] = lowest if isinstance(lowest, list) else []
    if lowest_rows:
        for row in lowest_rows:
            if isinstance(row, Mapping):
                lines.append(f"  - {row.get('id')}: {_report_num(row.get('score')):.1f}")
    else:
        lines.append("  (none)")
    lines.append("Hard-gate failures:")
    hard_failures = s.get("hard_failures")
    hard_map: Mapping[str, object] = hard_failures if isinstance(hard_failures, Mapping) else {}
    if hard_map:
        for cid in sorted(str(k) for k in hard_map):
            gates = hard_map[cid]
            names = ", ".join(str(g) for g in gates) if isinstance(gates, list) else str(gates)
            lines.append(f"  - {cid}: {names}")
    else:
        lines.append("  (none)")
    lines.append("Dimension means:")
    dim_means = s.get("dimension_means")
    dim_map: Mapping[str, object] = dim_means if isinstance(dim_means, Mapping) else {}
    if dim_map:
        for dim in sorted(str(k) for k in dim_map):
            lines.append(f"  - {dim}: {_report_num(dim_map[dim]):.2f}")
    else:
        lines.append("  (none)")
    lines.append("Operations:")
    ops = s.get("operations")
    ops_map: Mapping[str, object] = ops if isinstance(ops, Mapping) else {}
    lines.append(
        f"  latency_ms={_report_num(ops_map.get('total_latency_ms')):.0f} "
        f"in={int(_report_num(ops_map.get('total_input_tokens')))} "
        f"out={int(_report_num(ops_map.get('total_output_tokens')))} "
        f"cost={_report_num(ops_map.get('total_cost'))} "
        f"tools={int(_report_num(ops_map.get('total_tool_calls')))} "
        f"evidence={int(_report_num(ops_map.get('total_evidence')))}"
    )
    lines.append("Coverage (diagnostic, not a gate): lexical task/branch rates only.")
    return "\n".join(lines) + "\n"
