"""Agent-loop integration tests for the optional search_web tool: soft
failures, the per-run cap, evidence recording, and redaction."""

from dataclasses import replace

from app import agent, exa_client
from app.policy import LOCAL_CONTEXT, RunLimits
from app.prompts import SYSTEM_PROMPT
from app.storage.runs import get_evidence, get_run, get_tool_calls

from tests.test_observability import TEST_POLICY, FakeOpenRouter, _final, _tool_round

CAP_CONTEXT = replace(LOCAL_CONTEXT, run_limits=RunLimits(max_exa_searches=2))


def _success_result(query="AMD news"):
    return {
        "result_type": "web_search",
        "query": query,
        "search_type": "auto",
        "evidence": [{
            "title": "AMD MI400 Launch",
            "url": "https://example.com/amd-news",
            "source_domain": "example.com",
            "published_at": "2026-08-01T10:00:00.000Z",
            "retrieved_at": "2026-08-02T00:00:00+00:00",
            "highlight": "AMD announced its MI400 accelerator.",
            "category": None,
        }],
        "omitted_count": 0,
        "row_count": 1,
        "source": "exa",
        "retrieved_at": "2026-08-02T00:00:00+00:00",
    }


def test_soft_failure_continues_to_final_answer(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("search_web", {"query": "AMD latest news"}),
        _final("Here is what I found in current web coverage of AMD."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent,
        "execute_tool",
        lambda name, args, model, **kwargs: {
            "error": "Exa search unavailable", "source": "exa", "soft": True,
        },
    )
    result = agent.run_chat(
        [{"role": "user", "content": "What's the latest news on AMD?"}],
        model="test",
        context=LOCAL_CONTEXT,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer == "Here is what I found in current web coverage of AMD."
    assert get_run(result.run_id)["status"] == "completed"
    tool_calls = get_tool_calls(result.run_id)
    assert len(tool_calls) == 1
    assert tool_calls[0]["status"] == "failed"
    assert tool_calls[0]["error_type"] == "tool_error"
    assert tool_calls[0]["error_message"] == "Exa search unavailable"


def test_hard_canonical_failure_still_stops(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AMD", "metric": "eps"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent,
        "execute_tool",
        lambda name, args, model, **kwargs: {"error": "no data found"},
    )
    result = agent.run_chat(
        [{"role": "user", "content": "What is AMD EPS?"}],
        model="test",
        context=LOCAL_CONTEXT,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer.startswith("The requested data is unavailable")
    assert get_run(result.run_id)["status"] == "partial"


def test_exa_search_cap_enforced(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("search_web", {"query": "AMD news 1"}),
        _tool_round("search_web", {"query": "AMD news 2"}),
        _tool_round("search_web", {"query": "AMD news 3"}),
        _final("Synthesis complete."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    calls = []
    monkeypatch.setattr(
        exa_client,
        "search",
        lambda query, **kwargs: (calls.append(query), _success_result(query))[1],
    )
    result = agent.run_chat(
        [{"role": "user", "content": "Research AMD's news this week"}],
        model="test",
        context=CAP_CONTEXT,
        policy=TEST_POLICY,
        return_result=True,
    )
    # Attempted searches count: exactly two reached the client, the third was refused.
    assert calls == ["AMD news 1", "AMD news 2"]
    assert result.answer == "Synthesis complete."
    assert get_run(result.run_id)["status"] == "completed"
    tool_calls = get_tool_calls(result.run_id)
    assert [tc["status"] for tc in tool_calls] == ["completed", "completed", "failed"]
    assert tool_calls[2]["error_message"] == "Exa search budget exhausted (max 3 per run)"


def test_search_web_evidence_recorded(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("search_web", {"query": "AMD news"}),
        _final("Based on the search results, AMD shipped its MI400."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(exa_client, "search", lambda query, **kwargs: _success_result(query))
    result = agent.run_chat(
        [{"role": "user", "content": "What's new with AMD?"}],
        model="test",
        context=LOCAL_CONTEXT,
        policy=TEST_POLICY,
        return_result=True,
    )
    evidence = get_evidence(result.run_id)
    rows = [ev for ev in evidence if ev["tool_name"] == "search_web"]
    assert len(rows) == 1
    assert "https://example.com/amd-news" in rows[0]["rendered_text"]
    assert "AMD MI400 Launch" in rows[0]["rendered_text"]


def test_search_web_arguments_redacted_in_store(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("search_web", {"query": "AMD outlook account 123456789"}),
        _final("Summary."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(exa_client, "search", lambda query, **kwargs: _success_result(query))
    result = agent.run_chat(
        [{"role": "user", "content": "AMD outlook"}],
        model="test",
        context=LOCAL_CONTEXT,
        policy=TEST_POLICY,
        return_result=True,
    )
    tool_calls = get_tool_calls(result.run_id)
    assert "[REDACTED]" in tool_calls[0]["arguments_json"]
    assert "123456789" not in tool_calls[0]["arguments_json"]


def test_system_prompt_search_web_policy():
    assert "never substitute a web-search snippet" in SYSTEM_PROMPT
    assert "counterevidence" in SYSTEM_PROMPT
    assert "at most 3 search_web" in SYSTEM_PROMPT
    assert "never account identifiers" in SYSTEM_PROMPT
