"""Registration and dispatch tests for the search_web tool."""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

import pytest

import scripts.pi_bridge as pi_bridge
from app import tools
from app.policy import Capability, RequestContext

RESEARCH_CONTEXT = RequestContext("test", frozenset({Capability.RESEARCH}))

_APPROVED_SEARCH_TYPES = {"auto", "fast", "deep-lite"}
_APPROVED_CATEGORIES = {"news", "company", "publication", "financial report"}


def _search_web_schema():
    for entry in tools.TOOLS:
        fn = entry.get("function")
        assert isinstance(fn, dict)
        if fn.get("name") == "search_web":
            return fn
    raise AssertionError("search_web not registered")


def test_search_web_registered_everywhere() -> None:
    names: set[object] = set()
    for entry in tools.TOOLS:
        fn = entry.get("function")
        assert isinstance(fn, dict)
        names.add(fn.get("name"))
    assert "search_web" in names
    assert "search_web" in tools._DIRECT_HANDLERS
    assert tools.TOOL_CAPABILITIES["search_web"] == Capability.RESEARCH


def test_search_web_schema_shape() -> None:
    schema = _search_web_schema()
    params = schema["parameters"]
    assert params["required"] == ["query"]
    props = params["properties"]
    assert props["query"]["type"] == "string"
    assert set(props["category"]["enum"]) == _APPROVED_CATEGORIES
    assert set(props["search_type"]["enum"]) == _APPROVED_SEARCH_TYPES
    assert props["include_domains"]["type"] == "array"
    assert props["exclude_domains"]["type"] == "array"
    assert "YYYY-MM-DD" in props["start_published_date"]["description"]
    assert props["limit"]["minimum"] == 1
    assert props["limit"]["maximum"] == 25
    # Optional fields are plain types absent from `required` (repo style).
    for key in ("category", "search_type", "limit", "include_domains"):
        assert key not in params["required"]


def test_search_web_dispatcher_parity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_search(query: str, **kwargs: object) -> dict[str, object]:
        calls.append((query, kwargs))
        return {"result_type": "web_search", "query": query, "evidence": list[dict[str, object]]()}

    monkeypatch.setattr(tools.exa_client, "search", fake_search)
    result = tools.execute_tool(
        "search_web", {"query": "AMD"}, model="test", context=RESEARCH_CONTEXT
    )
    assert result["result_type"] == "web_search"
    query, kwargs = calls[0]
    assert query == "AMD"
    assert kwargs == {
        "category": None,
        "include_domains": None,
        "exclude_domains": None,
        "start_published_date": None,
        "end_published_date": None,
        "search_type": "auto",
        "limit": 5,
    }


def test_search_web_dispatcher_passes_optional_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_search(query: str, **kwargs: object) -> dict[str, object]:
        calls.append((query, kwargs))
        return {"result_type": "web_search", "query": query, "evidence": list[dict[str, object]]()}

    monkeypatch.setattr(tools.exa_client, "search", fake_search)
    tools.execute_tool(
        "search_web",
        {
            "query": "AMD competition",
            "category": "news",
            "search_type": "fast",
            "limit": 3,
            "include_domains": ["amd.com"],
            "start_published_date": "2026-07-01",
        },
        model="test",
        context=RESEARCH_CONTEXT,
    )
    query, kwargs = calls[0]
    assert query == "AMD competition"
    assert kwargs["category"] == "news"
    assert kwargs["search_type"] == "fast"
    assert kwargs["limit"] == 3
    assert kwargs["include_domains"] == ["amd.com"]
    assert kwargs["start_published_date"] == "2026-07-01"


def test_search_web_disabled_is_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_ENABLED", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    result = tools.execute_tool(
        "search_web", {"query": "AMD news"}, model="test", context=RESEARCH_CONTEXT
    )
    assert result["error"] == "Exa search unavailable"
    assert result["source"] == "exa"
    assert result["soft"] is True


def test_search_web_invalid_args_are_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXA_ENABLED", "true")
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    result = tools.execute_tool(
        "search_web",
        {"query": "AMD news", "category": "gossip"},
        model="test",
        context=RESEARCH_CONTEXT,
    )
    error = result["error"]
    assert isinstance(error, str)
    assert "Unsupported category 'gossip'" in error
    assert result["soft"] is True

    result = tools.execute_tool(
        "search_web",
        {"query": "AMD news", "search_type": "deep"},
        model="test",
        context=RESEARCH_CONTEXT,
    )
    error = result["error"]
    assert isinstance(error, str)
    assert "Unsupported search_type 'deep'" in error
    assert result["soft"] is True


def _bridge_request(payload: dict[str, object]) -> dict[str, object]:
    payload = {"id": _next_msg_id("ev"), **payload}
    if payload.get("op") == "tool_call":
        responses: list[dict[str, object]] = []

        def _capture(response: dict[str, object]) -> None:
            responses.append(response)

        orig_write = pi_bridge._write
        pi_bridge._write = _capture
        try:
            assert pi_bridge._handle(json.dumps(payload)) is None
            run_id_val = payload["run_id"]
            assert isinstance(run_id_val, str)
            for fut in pi_bridge._run_futures(run_id_val):
                fut.result(timeout=60)
        finally:
            pi_bridge._write = orig_write
        return next(r for r in responses if r.get("id") == payload["id"])
    response = pi_bridge._handle(json.dumps(payload))
    assert response is not None
    response = dict(response)
    response.pop("id", None)
    return response


def _next_msg_id(tag: str) -> str:
    return f"{tag}-{uuid.uuid4().hex[:8]}"


def _start_run(run_id: str) -> dict[str, object]:
    return _bridge_request(
        {"id": _next_msg_id("ev"), "op": "pi_event", "run_id": run_id, "event": "agent_start"}
    )


def _end_run(run_id: str) -> dict[str, object]:
    return _bridge_request(
        {"id": _next_msg_id("ev"), "op": "pi_event", "run_id": run_id, "event": "agent_end"}
    )


def _bridge_search(run_id: str, query: str) -> dict[str, object]:
    return _bridge_request(
        {
            "id": _next_msg_id("tc"),
            "op": "tool_call",
            "run_id": run_id,
            "tool_call_id": _next_msg_id("call"),
            "name": "search_web",
            "arguments": {"query": query},
            "bridge_queue_ms": 0.0,
        }
    )


def _fake_exa_search(calls: list[str], evidence: list[dict[str, object]] | None = None) -> Callable[..., dict[str, object]]:
    def fake_search(query: str, **kwargs: object) -> dict[str, object]:
        calls.append(query)
        return {
            "result_type": "web_search",
            "query": query,
            "evidence": evidence if evidence is not None else list[dict[str, object]](),
            "row_count": 0,
            "source": "exa",
        }

    return fake_search


def _new_run_id(tag: str) -> str:
    return f"run-{tag}-{uuid.uuid4().hex[:8]}"


def test_pi_each_agent_run_gets_fresh_session() -> None:
    run_a = _new_run_id("fresh-a")
    run_b = _new_run_id("fresh-b")
    assert run_a != run_b
    try:
        assert _start_run(run_a) == {"ok": True}
        assert _start_run(run_b) == {"ok": True}
        session_a = pi_bridge._sessions[run_a]
        session_b = pi_bridge._sessions[run_b]
        assert session_a is not session_b
        assert session_a.session_id == run_a
        assert session_b.session_id == run_b
    finally:
        _end_run(run_a)
        _end_run(run_b)


def test_pi_second_run_does_not_inherit_first_run_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import execute_pi_tool

    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_one = _new_run_id("budget-one")
    run_two = _new_run_id("budget-two")
    try:
        assert _start_run(run_one) == {"ok": True}
        first = pi_bridge._sessions[run_one]
        for _ in range(first.budget.max_tool_calls):
            assert first.budget.reserve_tool_call()
        assert first.budget.reserve_tool_call() is False
        refused = execute_pi_tool("search_tools", {"query": "budget probe"}, first)
        assert refused.get("error_type") == "budget_exhausted"
        assert _end_run(run_one) == {"ok": True}
        assert _start_run(run_two) == {"ok": True}
        response = _bridge_search(run_two, "AMD revenue")
        result = response["result"]
        assert isinstance(result, dict)
        assert "content" in result
        meta = result["meta"]
        assert isinstance(meta, dict)
        assert meta["status"] == "completed"
        assert len(calls) == 1
    finally:
        _end_run(run_one)
        _end_run(run_two)


def test_pi_model_receives_exact_security_checked_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import PiSessionContext
    from app.security.context_gateway import (
        QuarantinedContext,
        envelope_for_tool,
        prepare_context,
    )
    from app.security.response_guard import guard_response
    from app.tool_render import render_tool_result

    evidence: list[dict[str, object]] = [
        {
            "title": "AMD Q3",
            "url": "https://example.com/amd-q3",
            "source_domain": "example.com",
            "highlight": "Q3 revenue grew 12 percent amid account_number is 12345678 review",
        }
    ]
    calls: list[str] = []
    seen: list[dict[str, object]] = []

    def fake_search(query: str, **kwargs: object) -> dict[str, object]:
        calls.append(query)
        result: dict[str, object] = {
            "result_type": "web_search",
            "query": query,
            "evidence": evidence,
            "row_count": 0,
            "source": "exa",
        }
        seen.append(result)
        return result

    monkeypatch.setattr(tools.exa_client, "search", fake_search)
    run_id = _new_run_id("checked")
    try:
        assert _start_run(run_id) == {"ok": True}
        response = _bridge_search(run_id, "AMD revenue")
        search_result = response["result"]
        assert isinstance(search_result, dict)
        text = search_result["content"]
        raw = seen[0]
        assert "12345678" in json.dumps(raw)
        rendered = render_tool_result(raw)
        envelope = envelope_for_tool("search_web", raw)
        outcome = prepare_context(envelope, rendered)
        assert not isinstance(outcome, QuarantinedContext)
        expected = guard_response(
            outcome.text, PiSessionContext(session_id="expected").run_security, "expected"
        )
        assert text == expected
        assert "12345678" not in text
        assert "revenue grew" in text
    finally:
        _end_run(run_id)


def test_pi_tool_call_is_written_to_run_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.runs import get_tool_calls

    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_id = _new_run_id("recorder")
    try:
        assert _start_run(run_id) == {"ok": True}
        response = _bridge_search(run_id, "AMD revenue")
        recorder_result = response["result"]
        assert isinstance(recorder_result, dict)
        assert "content" in recorder_result
        rows = get_tool_calls(run_id)
        assert any(row["tool_name"] == "search_web" for row in rows)
    finally:
        _end_run(run_id)


def test_pi_agent_end_closes_recorder_and_removes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage.runs import RunRecorder

    exited: list[str] = []
    orig_exit = RunRecorder.__exit__

    def spy_exit(
        self: RunRecorder,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        exited.append(self.run_id)
        return orig_exit(self, exc_type, exc, tb)

    monkeypatch.setattr(RunRecorder, "__exit__", spy_exit)
    run_id = _new_run_id("end")
    try:
        assert _start_run(run_id) == {"ok": True}
        assert pi_bridge._recorders.get(run_id) is not None
        assert _end_run(run_id) == {"ok": True}
        assert run_id not in pi_bridge._sessions
        assert run_id not in pi_bridge._recorders
        assert exited == [run_id]
    finally:
        pi_bridge._sessions.pop(run_id, None)
        pi_bridge._recorders.pop(run_id, None)


def test_pi_search_web_caps_at_25_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    session = PiSessionContext(session_id=_new_run_id("cap"))
    results = [
        execute_pi_tool("search_web", {"query": f"probe {i}"}, session) for i in range(26)
    ]
    assert len(calls) == 25
    for result in results[:25]:
        assert "content" in result
        assert "result_type" not in result
    capped = results[25]
    assert capped.get("error_type") == "budget_exhausted"
    assert "error" in capped
    assert len(calls) == 25


def test_pi_search_web_resets_cap_for_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_a = _new_run_id("cap-a")
    run_b = _new_run_id("cap-b")
    try:
        assert _start_run(run_a) == {"ok": True}
        for i in range(25):
            response = _bridge_search(run_a, f"probe {i}")
            probe_result = response["result"]
            assert isinstance(probe_result, dict)
            assert "content" in probe_result
        capped = _bridge_search(run_a, "probe 25")
        capped_result = capped["result"]
        assert isinstance(capped_result, dict)
        assert capped_result.get("error_type") == "budget_exhausted"
        assert len(calls) == 25
        assert _end_run(run_a) == {"ok": True}
        assert _start_run(run_b) == {"ok": True}
        response = _bridge_search(run_b, "probe fresh")
        fresh_result = response["result"]
        assert isinstance(fresh_result, dict)
        assert "content" in fresh_result
        assert len(calls) == 26
    finally:
        _end_run(run_a)
        _end_run(run_b)


def test_pi_search_web_respects_runtime_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    session = PiSessionContext(session_id=_new_run_id("runtime"))
    session.budget.max_runtime = 0.0
    result = execute_pi_tool("search_web", {"query": "probe"}, session)
    assert result.get("error_type") == "budget_exhausted"
    assert "error" in result
    assert calls == []


def test_pi_search_evidence_tokens_enforce_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    evidence: list[dict[str, object]] = [
        {
            "title": "AMD Q3",
            "url": "https://example.com/amd-q3",
            "source_domain": "example.com",
            "highlight": "Q3 revenue grew 12 percent on data-center demand",
        }
    ]
    calls: list[str] = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls, evidence=evidence))
    session = PiSessionContext(session_id=_new_run_id("evidence-budget"))
    session.budget.max_evidence_tokens = 5
    result = execute_pi_tool("search_web", {"query": "AMD revenue"}, session)
    assert result.get("error_type") == "budget_exhausted"
    assert len(calls) == 1
    assert session.budget.evidence_tokens == 0


def test_pi_recorder_lifecycle_persists_question_model_answer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import hashlib

    from app.storage.runs import get_model_calls, get_run

    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    run_id = _new_run_id("lifecycle")
    question = "What drove AMD revenue growth in Q3?"
    model = "test-model-lifecycle"
    started_at = "2026-09-05T00:00:00+00:00"
    completed_at = "2026-09-05T00:00:01+00:00"
    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "reasoning_tokens": 10,
        "prompt_tokens_details": {"cached_tokens": 30},
        "total_tokens": 165,
        "cost": 0.0025,
    }
    answer = "AMD revenue grew 12 percent on data-center demand."
    try:
        assert _bridge_request(
            {"op": "pi_event", "run_id": run_id, "event": "agent_start", "question": question}
        ) == {"ok": True}
        assert _bridge_request(
            {
                "op": "pi_event", "run_id": run_id, "event": "message_end",
                "role": "assistant", "turn": 0, "model": model,
                "started_at": started_at, "completed_at": completed_at,
                "usage": usage, "tool_call_count": 2,
            }
        ) == {"ok": True}
        assert _bridge_request(
            {"op": "pi_event", "run_id": run_id, "event": "agent_end", "answer": answer}
        ) == {"ok": True}
        run = get_run(run_id)
        assert run is not None
        assert run["question"] == question
        assert run["status"] == "completed"
        assert run["input_tokens"] == 120
        assert run["output_tokens"] == 45
        assert run["total_tokens"] == 165
        assert run["estimated_model_cost"] == 0.0025
        assert run["final_answer_hash"] == hashlib.sha256(answer.encode()).hexdigest()
        calls = get_model_calls(run_id)
        assert len(calls) == 1
        call = calls[0]
        assert call["model"] == model
        assert call["started_at"] == started_at
        assert call["completed_at"] == completed_at
        assert call["input_tokens"] == 120
        assert call["output_tokens"] == 45
        assert call["reasoning_tokens"] == 10
        assert call["cached_tokens"] == 30
        assert call["estimated_cost"] == 0.0025
        assert call["tool_call_count"] == 2
    finally:
        _end_run(run_id)

def test_pi_abort_orphan_run_finalized_failed_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.storage.runs import get_run

    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    run_r = _new_run_id("orphan-r")
    run_s = _new_run_id("orphan-s")
    try:
        assert _start_run(run_r) == {"ok": True}
        # Simulate a killed predecessor: drop memory without completing.
        rec = pi_bridge._recorders.pop(run_r, None)
        if rec is not None:
            try:
                rec.__exit__(None, None, None)
            except Exception:
                pass
        pi_bridge._sessions.pop(run_r, None)
        with pi_bridge._state_lock:
            pi_bridge._inflight.pop(run_r, None)
        message = "Tool call timed out after 50ms"
        terminal: dict[str, object] = {
            "op": "pi_event", "run_id": run_r, "event": "agent_end",
            "status": "failed", "answer": "",
            "error_type": "tool_timeout", "error_message": message,
        }
        assert _bridge_request(terminal) == {"ok": True}
        run = get_run(run_r)
        assert run is not None
        assert run["status"] == "failed"
        assert run["error_type"] == "tool_timeout"
        assert run["completed_at"] is not None
        assert run["duration_ms"] is not None
        first_completed = run["completed_at"]
        assert _bridge_request(terminal) == {"ok": True}
        again = get_run(run_r)
        assert again is not None
        assert again["completed_at"] == first_completed
        assert again["status"] == "failed"
        assert _start_run(run_s) == {"ok": True}
        end_ok: dict[str, object] = {
            "op": "pi_event", "run_id": run_s, "event": "agent_end", "answer": "ok"
        }
        assert _bridge_request(end_ok) == {"ok": True}
        completed = get_run(run_s)
        assert completed is not None
        assert completed["status"] == "completed"
    finally:
        for rid in (run_r, run_s):
            pi_bridge._sessions.pop(rid, None)
            pi_bridge._recorders.pop(rid, None)
            with pi_bridge._state_lock:
                pi_bridge._inflight.pop(rid, None)
