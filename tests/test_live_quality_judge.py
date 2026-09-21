"""Focused tests for the evaluator-only structured quality judge (offline only)."""

from __future__ import annotations

import json
import urllib.request

import pytest

from app.research.evals.quality_judge import (
    JUDGE_API_KEY_ENV,
    JUDGE_BASE_URL_ENV,
    JUDGE_MODEL_ENV,
    JudgeError,
    judge_answer,
)

_DIMS = ["factual_correctness", "uncertainty"]


def _case(dims: list[str] | None = None) -> dict[str, object]:
    return {
        "id": "live-01",
        "question": "Is the dividend covered by free cash flow?",
        "category": "dividends",
        "as_of": "2026-01-31",
        "requires_research": True,
        "requires_counterevidence": False,
        "requires_point_in_time": True,
        "expected_branches": ["cash-flow"],
        "explicit_tasks": ["check coverage"],
        "applicable_dimensions": list(dims or _DIMS),
    }


def _trace() -> dict[str, object]:
    return {
        "accepted_evidence": [
            {"id": "e1", "source": "sec-10k", "title": "FY filing", "content": "FCF covered the dividend."}
        ],
        "known_limitations": ["Only FY filing reviewed."],
    }


def _payload(scores: dict[str, int], tag: str, issues: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "scores": dict(scores),
            "reasons": {d: f"{tag}-{d}-{s}" for d, s in scores.items()},
            "material_issues": list(issues or []),
        }
    )


class _StubClient:
    """Injected offline stand-in: records prompts, replays canned JSON payloads."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


def _client_of(responses: list[str]) -> _StubClient:
    return _StubClient(responses)


def _const(content: str) -> _StubClient:
    return _StubClient([content])


def test_rejects_free_prose_and_empty() -> None:
    for bad in ("looks good, score 3", "", "   "):
        with pytest.raises(JudgeError):
            judge_answer(_case(), "answer", _trace(), client=_const(bad))


def test_rejects_markdown_fence() -> None:
    fenced = "```json\n" + _payload({d: 2 for d in _DIMS}, "f") + "\n```"
    with pytest.raises(JudgeError, match="markdown"):
        judge_answer(_case(), "answer", _trace(), client=_const(fenced))


def test_rejects_extra_and_missing_dimensions() -> None:
    extra = json.dumps(
        {
            "scores": {"factual_correctness": 2, "uncertainty": 2, "causal_reasoning": 2},
            "reasons": {"factual_correctness": "r", "uncertainty": "r", "causal_reasoning": "r"},
            "material_issues": [],
        }
    )
    with pytest.raises(JudgeError, match="dimensions mismatch"):
        judge_answer(_case(), "answer", _trace(), client=_const(extra))
    missing = json.dumps({"scores": {"factual_correctness": 2}, "reasons": {"factual_correctness": "r"}})
    with pytest.raises(JudgeError, match="dimensions mismatch"):
        judge_answer(_case(), "answer", _trace(), client=_const(missing))


def test_rejects_top_level_and_range_violations() -> None:
    base_scores = {d: 2 for d in _DIMS}
    base_reasons = {d: "ok reason" for d in _DIMS}
    bad_top = json.dumps({**{"scores": base_scores, "reasons": base_reasons}, "extra": 1})
    with pytest.raises(JudgeError, match="unexpected top-level"):
        judge_answer(_case(), "answer", _trace(), client=_const(bad_top))
    for bad_score in (5, -1, 2.0, True, "3", None):
        bad = json.dumps({"scores": {d: bad_score for d in _DIMS}, "reasons": dict(base_reasons)})
        with pytest.raises(JudgeError, match="0\\.\\.4"):
            judge_answer(_case(), "answer", _trace(), client=_const(bad))
    bad_reason = json.dumps(
        {"scores": dict(base_scores), "reasons": {"factual_correctness": "  ", "uncertainty": "ok"}}
    )
    with pytest.raises(JudgeError, match="nonempty string"):
        judge_answer(_case(), "answer", _trace(), client=_const(bad_reason))
    bad_issue = json.dumps(
        {"scores": dict(base_scores), "reasons": dict(base_reasons), "material_issues": [{"type": "x"}]}
    )
    with pytest.raises(JudgeError, match="material issue"):
        judge_answer(_case(), "answer", _trace(), client=_const(bad_issue))


def test_median_scores_and_median_reasons() -> None:
    responses = [
        _payload({"factual_correctness": 1, "uncertainty": 2}, "r1"),
        _payload({"factual_correctness": 3, "uncertainty": 2}, "r2"),
        _payload({"factual_correctness": 2, "uncertainty": 2}, "r3"),
    ]
    agg = judge_answer(_case(), "answer", _trace(), client=_client_of(responses))
    assert agg.scores == {"factual_correctness": 2, "uncertainty": 2}
    assert agg.reasons["factual_correctness"] == "r3-factual_correctness-2"
    assert agg.reasons["uncertainty"] == "r1-uncertainty-2"
    assert len(agg.runs) == 3
    assert agg.disagreement == pytest.approx(0.5)
    dumped = json.dumps(agg.as_dict())
    assert '"factual_correctness": 2' in dumped


def test_issues_dedupe_and_disagreement_stable() -> None:
    dup = {"type": "unsupported-number", "description": "Number lacks evidence.", "claim": "FCF grew 40%."}
    other = {"type": "overclaim", "description": "Coverage overstated.", "claim": "Dividend is safe."}
    responses = [
        _payload({d: 1 for d in _DIMS}, "a", [dup, other]),
        _payload({d: 3 for d in _DIMS}, "b", [dict(dup), dict(other)]),
        _payload({d: 2 for d in _DIMS}, "c", [dict(dup)]),
    ]
    agg = judge_answer(_case(), "answer", _trace(), client=_client_of(responses))
    assert [(i.type, i.description, i.claim) for i in agg.issues] == [
        ("unsupported-number", "Number lacks evidence.", "FCF grew 40%."),
        ("overclaim", "Coverage overstated.", "Dividend is safe."),
    ]
    assert agg.disagreement == pytest.approx(1.0)
    again = judge_answer(_case(), "answer", _trace(), client=_client_of(responses))
    assert again.disagreement == agg.disagreement
    assert again.scores == agg.scores


def test_payload_excludes_hidden_reasoning() -> None:
    trace: dict[str, object] = {
        **_trace(),
        "nodes": ["HIDDEN-NODE-MARKER"],
        "tool_executions": [{"args": "HIDDEN-TOOL-ARGS"}],
        "session": {"id": "HIDDEN-SESSION"},
        "jobs": ["HIDDEN-JOB"],
        "model": "HIDDEN-MODEL",
        "prompt_metadata": {"sys": "HIDDEN-PROMPT"},
        "decisions": ["HIDDEN-DECISION"],
    }
    client = _client_of([_payload({d: 2 for d in _DIMS}, "h")])
    agg = judge_answer(_case(), "answer text here", trace, client=client)
    assert agg.scores
    system, user = client.calls[0]
    blob = system + "\n" + user
    for marker in ("HIDDEN-NODE", "HIDDEN-TOOL", "HIDDEN-SESSION", "HIDDEN-JOB", "HIDDEN-MODEL", "HIDDEN-PROMPT"):
        assert marker not in blob
    assert "Is the dividend covered" in user
    assert "answer text here" in user
    assert "FCF covered the dividend" in user
    assert "Only FY filing reviewed" in user


def test_env_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JUDGE_BASE_URL_ENV, raising=False)
    monkeypatch.delenv(JUDGE_MODEL_ENV, raising=False)
    with pytest.raises(JudgeError, match=JUDGE_BASE_URL_ENV):
        judge_answer(_case(), "answer", _trace(), client=None)
    monkeypatch.setenv(JUDGE_BASE_URL_ENV, "https://judge.example/v1")
    monkeypatch.delenv(JUDGE_MODEL_ENV, raising=False)
    with pytest.raises(JudgeError, match=JUDGE_MODEL_ENV):
        judge_answer(_case(), "answer", _trace(), client=None)
    with pytest.raises(JudgeError, match="must be callable"):
        judge_answer(_case(), "answer", _trace(), client=42)
    with pytest.raises(ValueError, match="runs"):
        judge_answer(_case(), "answer", _trace(), client=_const(_payload({d: 2 for d in _DIMS}, "x")), runs=0)


def test_judge_url_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class _Resp:
        def read(self) -> bytes:
            inner = _payload({d: 2 for d in _DIMS}, "u")
            return json.dumps({"choices": [{"message": {"content": inner}}]}).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _fake_urlopen(req: object, timeout: float | None = None) -> _Resp:
        seen.append(str(getattr(req, "full_url", "")))
        assert timeout == 60.0
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model-exact")
    for base, expected in (
        ("https://judge.example/v1", "https://judge.example/v1/chat/completions"),
        ("https://judge.example/v1/chat/completions", "https://judge.example/v1/chat/completions"),
        ("https://judge.example/v1/", "https://judge.example/v1/chat/completions"),
    ):
        monkeypatch.setenv(JUDGE_BASE_URL_ENV, base)
        seen.clear()
        judge_answer(_case(), "answer", _trace(), client=None, runs=1)
        assert seen == [expected]


def test_http_request_shape_and_no_key_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Resp:
        def read(self) -> bytes:
            inner = _payload({d: 2 for d in _DIMS}, "s")
            return json.dumps({"choices": [{"message": {"content": inner}}]}).encode()

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def _fake_urlopen(req: object, timeout: float | None = None) -> _Resp:
        raw_data = getattr(req, "data", b"")
        assert isinstance(raw_data, bytes)
        body = json.loads(raw_data.decode())
        assert isinstance(body, dict)
        captured["url"] = str(getattr(req, "full_url", ""))
        captured["body"] = body
        get_header = getattr(req, "get_header", None)
        captured["auth"] = get_header("Authorization") if callable(get_header) else None
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setenv(JUDGE_BASE_URL_ENV, "https://judge.example/v1")
    monkeypatch.setenv(JUDGE_MODEL_ENV, "judge-model-exact")
    monkeypatch.setenv(JUDGE_API_KEY_ENV, "sk-test-key-ABC")
    judge_answer(_case(), "answer", _trace(), client=None, runs=1)
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "judge-model-exact"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert captured["auth"] == "Bearer sk-test-key-ABC"
    assert captured["timeout"] == 60.0

    secret = "sk-live-secret-ZZZ-999"
    monkeypatch.setenv(JUDGE_API_KEY_ENV, secret)

    def _boom(req: object, timeout: float | None = None) -> object:
        raise OSError("connection reset by peer")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    with pytest.raises(JudgeError) as excinfo:
        judge_answer(_case(), "answer", _trace(), client=None, runs=1)
    assert secret not in str(excinfo.value)
    assert "ZZZ-999" not in str(excinfo.value)


def test_object_client_and_dict_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(JUDGE_MODEL_ENV, raising=False)
    decoded = json.loads(_payload({d: 3 for d in _DIMS}, "o"))

    class _Obj:
        model = "obj-model"

        def complete(self, system: str, user: str) -> object:
            assert "factual_correctness" in system
            assert "Is the dividend" in user
            return dict(decoded)

    agg = judge_answer(_case(), "answer", _trace(), client=_Obj())
    assert agg.scores == {d: 3 for d in _DIMS}
    assert agg.model == "obj-model"
