"""Focused tests for the live-eval runner: fake HTTP + injected judge, no network.

Covers SSE parsing strictness, asOf/auth request shape, full artifact
preservation, no-overwrite, summary math (mean/median/dimension/disagreement/
operations), report sections, partial-artifact behavior, and exit mapping.
Peer payloads stay opaque: gate/judge doubles expose only ``as_dict()`` plus
the public attributes the suite reads (``passed``/``scores``/``disagreement``).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.evals import live_quality as lq


class FakeGate:
    def __init__(self, passed: bool) -> None:
        self.passed = passed

    def as_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "reasons": [] if self.passed else ["gate tripped"], "details": {}}


class FakeJudgeRun:
    def __init__(self, scores: dict[str, int]) -> None:
        self.scores = dict(scores)

    def as_dict(self) -> dict[str, object]:
        return {
            "scores": dict(self.scores),
            "reasons": {k: "ok" for k in self.scores},
            "material_issues": [],
            "raw": {"scores": dict(self.scores)},
        }


class FakeAggregate:
    def __init__(self, scores: dict[str, int], disagreement: float = 0.0) -> None:
        self.scores = dict(scores)
        self.disagreement = disagreement
        self.runs = (FakeJudgeRun(scores),)

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": "x",
            "model": "fake-judge",
            "scores": dict(self.scores),
            "reasons": {k: "ok" for k in self.scores},
            "material_issues": [],
            "disagreement": self.disagreement,
            "runs": [r.as_dict() for r in self.runs],
        }


def _sse(*events: Mapping[str, object]) -> str:
    return "".join(f"data: {json.dumps(dict(e))}\n\n" for e in events)


def _ok_body(
    answer: str = "hi",
    metrics: Mapping[str, object] | None = None,
    trace: Mapping[str, object] | None = None,
) -> str:
    trace_map: Mapping[str, object] = (
        {"session": "s1", "accepted_evidence": [], "nodes": []} if trace is None else trace
    )
    metrics_map: Mapping[str, object] = (
        {
            "totalMs": 5,
            "muse": {},
            "tools": {"calls": 1},
            "evidence": {"count": 0},
            "failures": {},
        }
        if metrics is None
        else metrics
    )
    return _sse(
        {"type": "agent_start", "prompt": "q"},
        {"type": "answer_delta", "text": answer},
        {"type": "evaluation_trace", "trace": dict(trace_map)},
        {"type": "done", "metrics": dict(metrics_map)},
    )


def _case(cid: str = "c1", as_of: str | None = "2026-01-01") -> dict[str, object]:
    return {
        "id": cid,
        "question": f"question {cid}?",
        "as_of": as_of,
        "applicable_dimensions": ["factual_correctness", "uncertainty"],
    }


def _all_pass_gates(case: object, answer: str, trace: Mapping[str, object]) -> dict[str, object]:
    return {f"gate-{i}": FakeGate(True) for i in range(8)}


def _fixed_judge(scores: dict[str, int], disagreement: float = 0.0) -> object:
    def _judge(case: object, answer: str, trace: Mapping[str, object], client: object = None) -> object:
        return FakeAggregate(scores, disagreement)

    return _judge


def _run(
    monkeypatch: pytest.MonkeyPatch,
    cases: list[dict[str, object]],
    post: lq.HttpPost,
    judge_scores: dict[str, int] | None = None,
) -> lq.RunArtifact:
    monkeypatch.setattr(lq, "_evaluate_gates", _all_pass_gates)
    monkeypatch.setattr(lq, "_judge_case", _fixed_judge(judge_scores or {"factual_correctness": 3, "uncertainty": 2}))
    return lq.run_live_suite(list(cases), "http://127.0.0.1:9", "tok", judge_client=None, http_post=post)


def test_parse_sse_strict_rejects_non_object_and_missing_data() -> None:
    with pytest.raises(lq.LiveEvalInfraError):
        lq.parse_sse_body("data: [1,2]\n\n")
    with pytest.raises(lq.LiveEvalInfraError):
        lq.parse_sse_body("event: x\nfoo: bar\n\n")
    with pytest.raises(lq.LiveEvalInfraError):
        lq.parse_sse_body("data: {oops\n\n")


def test_request_shape_sends_asof_auth_trace_and_parses_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        seen.update({"url": url, "headers": dict(headers), "payload": dict(payload)})
        return 200, _ok_body("ab" + "cd")

    artifact = _run(monkeypatch, [_case("c1", "2026-03-04")], post)
    assert seen["url"] == "http://127.0.0.1:9/api/agent"
    assert seen["headers"] == {"Content-Type": "application/json", "Authorization": "Bearer tok"}
    assert seen["payload"] == {"prompt": "question c1?", "asOf": "2026-03-04", "includeTrace": True}
    first = artifact.cases[0]
    assert first.answer == "abcd"
    assert first.trace.get("session") == "s1"
    assert first.events and first.events[-1].get("type") == "done"


def test_requires_exactly_one_trace_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    def post_two_traces(
        url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float
    ) -> tuple[int, str]:
        trace_event: dict[str, object] = {"type": "evaluation_trace", "trace": {"session": "s"}}
        return 200, _sse(trace_event, trace_event, {"type": "done", "metrics": {}})

    artifact = _run(monkeypatch, [_case()], post_two_traces)
    assert artifact.cases[0].errors and "exactly one evaluation_trace" in artifact.cases[0].errors[0]
    assert lq.artifact_exit_code(artifact) == 2


def test_full_artifact_preservation_and_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    trace: dict[str, object] = {
        "session": "s1",
        "accepted_evidence": [{"id": "e1", "source": "SEC", "content": "x"}],
        "nodes": [{"node_id": "n1"}],
        "decisions": [{"d": 1}],
    }
    metrics: dict[str, object] = {
        "totalMs": 11,
        "muse": {"inputTokens": 10, "outputTokens": 5, "cost": 0.5},
        "tools": {"calls": 2},
        "evidence": {"count": 7},
        "failures": {"tool_error": 1},
    }

    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        return 200, _ok_body("answer!", metrics, trace)

    artifact = _run(monkeypatch, [_case()], post)
    first = artifact.cases[0]
    assert first.errors == []
    assert first.trace == trace
    assert first.operational_metrics["input_tokens"] == 10
    assert first.operational_metrics["total_tokens"] == 15
    assert first.operational_metrics["estimated_cost"] == 0.5
    assert first.operational_metrics["sources"] == ["SEC"]
    assert first.operational_metrics["evidence_count"] == 7
    assert first.judge_scores == {"factual_correctness": 3, "uncertainty": 2}
    assert first.disagreement == 0.0
    # Weights 15/7 over ratings 3/2: (15*.75 + 7*.5)/22*100.
    assert first.score == pytest.approx((15 * 0.75 + 7 * 0.5) / 22 * 100)
    assert first.passed and lq.artifact_exit_code(artifact) == 0
    assert set(first.hard_gates) == {f"gate-{i}" for i in range(8)}
    assert artifact.run["prompt_version"] == "v1" and artifact.run["case_version"] == "v1"
    assert artifact.run["production_model"] == "muse-spark-1.3-contributor"


def test_hard_failure_exits_1_scores_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        return 200, _ok_body("ans")

    def gates(case: object, answer: str, trace: Mapping[str, object]) -> dict[str, object]:
        return {"ok-gate": FakeGate(True), "bad-gate": FakeGate(False)}

    monkeypatch.setattr(lq, "_evaluate_gates", gates)
    monkeypatch.setattr(lq, "_judge_case", _fixed_judge({"factual_correctness": 1, "uncertainty": 1}))
    artifact = lq.run_live_suite([_case()], "http://x", "tok", http_post=post)
    first = artifact.cases[0]
    assert first.hard_failures == ["bad-gate"]
    assert first.passed is False
    assert first.score > 0  # quality reported separately from the gate verdict
    assert lq.artifact_exit_code(artifact) == 1


def test_summary_math_report_sections_and_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = [
        _ok_body(
            "a1",
            {
                "muse": {"inputTokens": 1, "outputTokens": 1},
                "tools": {"calls": 1},
                "evidence": {"count": 1},
                "failures": {},
            },
        ),
        _ok_body(
            "a2",
            {
                "muse": {"inputTokens": 3, "outputTokens": 3},
                "tools": {"calls": 2},
                "evidence": {"count": 2},
                "failures": {},
            },
        ),
    ]
    calls = {"n": 0}

    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        body = bodies[calls["n"]]
        calls["n"] += 1
        return 200, body

    monkeypatch.setattr(lq, "_evaluate_gates", _all_pass_gates)
    scores = [
        FakeAggregate({"factual_correctness": 4, "uncertainty": 0}, disagreement=0.5),
        FakeAggregate({"factual_correctness": 0, "uncertainty": 4}, disagreement=0.5),
    ]

    def rotating_judge(case: object, answer: str, trace: Mapping[str, object], client: object = None) -> object:
        return scores[calls["n"] - 1]

    monkeypatch.setattr(lq, "_judge_case", rotating_judge)
    artifact = lq.run_live_suite([_case("c1"), _case("c2")], "http://x", "tok", http_post=post)
    summary = artifact.summary
    qualities = [c.score for c in artifact.cases]
    assert float(str(summary["mean_quality"])) == pytest.approx(sum(qualities) / 2)
    assert summary["dimension_means"] == {"factual_correctness": 2.0, "uncertainty": 2.0}
    assert float(str(summary["mean_disagreement"])) == pytest.approx(0.5)
    ops = summary["operations"]
    assert isinstance(ops, Mapping)
    assert ops["total_input_tokens"] == 4
    assert ops["total_tool_calls"] == 3
    lowest = summary["lowest_cases"]
    assert isinstance(lowest, list) and len(lowest) == 2
    report = lq.format_report(artifact)
    for section in ("Lowest cases:", "Hard-gate failures:", "Dimension means:", "Operations:"):
        assert section in report


def test_no_overwrite_and_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        return 200, _ok_body("x")

    artifact = _run(monkeypatch, [_case()], post)
    dest = tmp_path / "out.json"
    written = lq.write_artifact(artifact, dest)
    assert written == dest
    decoded: object = json.loads(dest.read_text())
    assert isinstance(decoded, Mapping)
    summary = decoded.get("summary")
    assert isinstance(summary, Mapping) and summary.get("total") == 1
    with pytest.raises(lq.LiveEvalInfraError):
        lq.write_artifact(artifact, dest)


def test_partial_artifact_when_later_case_infra_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        if payload.get("prompt") == "question bad?":
            raise lq.LiveEvalInfraError("boom transport")
        return 200, _ok_body("fine")

    artifact = _run(monkeypatch, [_case("good"), _case("bad")], post)
    assert artifact.cases[0].errors == [] and artifact.cases[1].errors == ["boom transport"]
    assert artifact.summary["completed"] == 1 and artifact.summary["infra_errors"] == 1
    assert lq.artifact_exit_code(artifact) == 2


def test_malformed_judge_scores_are_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    class BadScores:
        def __init__(self) -> None:
            self.scores = {"factual_correctness": 9}

        def as_dict(self) -> dict[str, object]:
            return {"scores": {"factual_correctness": 9}, "disagreement": 0.0, "runs": []}

    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        return 200, _ok_body("x")

    def bad_judge(case: object, answer: str, trace: Mapping[str, object], client: object = None) -> object:
        return BadScores()

    monkeypatch.setattr(lq, "_evaluate_gates", _all_pass_gates)
    monkeypatch.setattr(lq, "_judge_case", bad_judge)
    artifact = lq.run_live_suite([_case()], "http://x", "tok", http_post=post)
    assert artifact.cases[0].errors and "int 0..4" in artifact.cases[0].errors[0]
    assert lq.artifact_exit_code(artifact) == 2


def test_objective_scores_carry_stage2_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(url: str, headers: dict[str, str], payload: dict[str, object], timeout_s: float) -> tuple[int, str]:
        return 200, _ok_body("answer!")

    artifact = _run(monkeypatch, [_case()], post)
    first = artifact.cases[0]
    assert first.errors == []
    scores = first.objective.get("objective_scores")
    assert isinstance(scores, dict)
    assert set(scores) == {
        "citation_resolution",
        "quantitative_support",
        "pit_compliance",
        "explicit_task_coverage",
        "declared_branch_coverage",
        "expected_branch_coverage",
        "limitation_preservation",
    }


def test_invalid_timeout_rejected() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/eval_stockbot_live.py", "--timeout-s", "0"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 2
    assert "--timeout-s must be a positive number" in proc.stderr
