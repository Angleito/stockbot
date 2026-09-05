"""Registration and dispatch tests for the search_web tool."""

import json
import uuid

import pytest

import scripts.pi_bridge as pi_bridge
from app import tools
from app.policy import Capability, RequestContext

RESEARCH_CONTEXT = RequestContext("test", frozenset({Capability.RESEARCH}))

_APPROVED_SEARCH_TYPES = {"auto", "fast", "deep-lite"}
_APPROVED_CATEGORIES = {"news", "company", "publication", "financial report"}


def _search_web_schema() -> dict:
    return next(
        entry["function"]
        for entry in tools.TOOLS
        if entry["function"]["name"] == "search_web"
    )


def test_search_web_registered_everywhere():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    assert "search_web" in names
    assert "search_web" in tools._DIRECT_HANDLERS
    assert tools.TOOL_CAPABILITIES["search_web"] == Capability.RESEARCH


def test_search_web_schema_shape():
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


def test_search_web_dispatcher_parity(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append((query, kwargs))
        return {"result_type": "web_search", "query": query, "evidence": []}

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


def test_search_web_dispatcher_passes_optional_args(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append((query, kwargs))
        return {"result_type": "web_search", "query": query, "evidence": []}

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


def test_search_web_disabled_is_soft(monkeypatch):
    monkeypatch.delenv("EXA_ENABLED", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    result = tools.execute_tool(
        "search_web", {"query": "AMD news"}, model="test", context=RESEARCH_CONTEXT
    )
    assert result["error"] == "Exa search unavailable"
    assert result["source"] == "exa"
    assert result["soft"] is True


def test_search_web_invalid_args_are_soft(monkeypatch):
    monkeypatch.setenv("EXA_ENABLED", "true")
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    result = tools.execute_tool(
        "search_web",
        {"query": "AMD news", "category": "gossip"},
        model="test",
        context=RESEARCH_CONTEXT,
    )
    assert "Unsupported category 'gossip'" in result["error"]
    assert result["soft"] is True

    result = tools.execute_tool(
        "search_web",
        {"query": "AMD news", "search_type": "deep"},
        model="test",
        context=RESEARCH_CONTEXT,
    )
    assert "Unsupported search_type 'deep'" in result["error"]
    assert result["soft"] is True


def _bridge_request(payload: dict) -> dict:
    return pi_bridge._handle(json.dumps(payload))


def _start_run(run_id: str) -> dict:
    return _bridge_request({"op": "pi_event", "run_id": run_id, "event": "agent_start"})


def _end_run(run_id: str) -> dict:
    return _bridge_request({"op": "pi_event", "run_id": run_id, "event": "agent_end"})


def _bridge_search(run_id: str, query: str) -> dict:
    return _bridge_request(
        {"op": "tool_call", "run_id": run_id, "name": "search_web", "arguments": {"query": query}}
    )


def _fake_exa_search(calls: list, evidence=None):
    def fake_search(query, **kwargs):
        calls.append(query)
        return {
            "result_type": "web_search",
            "query": query,
            "evidence": evidence if evidence is not None else [],
            "row_count": 0,
            "source": "exa",
        }

    return fake_search


def _new_run_id(tag: str) -> str:
    return f"run-{tag}-{uuid.uuid4().hex[:8]}"


def test_pi_each_agent_run_gets_fresh_session():
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


def test_pi_second_run_does_not_inherit_first_run_budget(monkeypatch):
    from app.pi_gateway import execute_pi_tool

    calls = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_one = _new_run_id("budget-one")
    run_two = _new_run_id("budget-two")
    try:
        assert _start_run(run_one) == {"ok": True}
        first = pi_bridge._sessions[run_one]
        for _ in range(first.budget.max_tool_calls):
            assert first.budget.reserve_tool_call()
        assert first.budget.reserve_tool_call() is False
        refused = execute_pi_tool("search_tools", {}, first)
        assert refused.get("error_type") == "budget_exhausted"
        assert _end_run(run_one) == {"ok": True}
        assert _start_run(run_two) == {"ok": True}
        response = _bridge_search(run_two, "AMD revenue")
        result = response["result"]
        assert "content" in result
        assert result["meta"]["status"] == "completed"
        assert len(calls) == 1
    finally:
        _end_run(run_one)
        _end_run(run_two)


def test_pi_model_receives_exact_security_checked_text(monkeypatch):
    from app.pi_gateway import PiSessionContext
    from app.security.context_gateway import (
        QuarantinedContext,
        envelope_for_tool,
        prepare_context,
    )
    from app.security.response_guard import guard_response
    from app.tool_render import render_tool_result

    evidence = [
        {
            "title": "AMD Q3",
            "url": "https://example.com/amd-q3",
            "source_domain": "example.com",
            "highlight": "Q3 revenue grew 12 percent amid account_number is 12345678 review",
        }
    ]
    calls = []
    seen = []

    def fake_search(query, **kwargs):
        calls.append(query)
        result = {
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
        text = response["result"]["content"]
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


def test_pi_tool_call_is_written_to_run_recorder(monkeypatch):
    from app.storage.runs import get_tool_calls

    calls = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_id = _new_run_id("recorder")
    try:
        assert _start_run(run_id) == {"ok": True}
        response = _bridge_search(run_id, "AMD revenue")
        assert "content" in response["result"]
        rows = get_tool_calls(run_id)
        assert any(row["tool_name"] == "search_web" for row in rows)
    finally:
        _end_run(run_id)


def test_pi_agent_end_closes_recorder_and_removes_session(monkeypatch):
    from app.storage.runs import RunRecorder

    exited = []
    orig_exit = RunRecorder.__exit__

    def spy_exit(self, exc_type, exc, tb):
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


def test_pi_search_web_caps_at_25_per_run(monkeypatch):
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    calls = []
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


def test_pi_search_web_resets_cap_for_next_run(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    run_a = _new_run_id("cap-a")
    run_b = _new_run_id("cap-b")
    try:
        assert _start_run(run_a) == {"ok": True}
        for i in range(25):
            response = _bridge_search(run_a, f"probe {i}")
            assert "content" in response["result"]
        capped = _bridge_search(run_a, "probe 25")
        assert capped["result"].get("error_type") == "budget_exhausted"
        assert len(calls) == 25
        assert _end_run(run_a) == {"ok": True}
        assert _start_run(run_b) == {"ok": True}
        response = _bridge_search(run_b, "probe fresh")
        assert "content" in response["result"]
        assert len(calls) == 26
    finally:
        _end_run(run_a)
        _end_run(run_b)


def test_pi_search_web_respects_runtime_budget(monkeypatch):
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    calls = []
    monkeypatch.setattr(tools.exa_client, "search", _fake_exa_search(calls))
    session = PiSessionContext(session_id=_new_run_id("runtime"))
    session.budget.max_runtime = 0.0
    result = execute_pi_tool("search_web", {"query": "probe"}, session)
    assert result.get("error_type") == "budget_exhausted"
    assert "error" in result
    assert calls == []
