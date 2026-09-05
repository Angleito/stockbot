"""Registration and dispatch tests for the search_web tool."""

import pytest

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
    assert props["limit"]["maximum"] == 10
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


def test_pi_search_web_ignores_tool_calls(monkeypatch):
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    monkeypatch.delenv("EXA_ENABLED", raising=False)
    session = PiSessionContext(session_id="default")
    assert session.budget.max_exa_searches == 25
    for _ in range(64):
        assert session.budget.reserve_tool_call()
    assert session.budget.reserve_tool_call() is False
    result = execute_pi_tool("search_web", {"query": "probe"}, session)
    assert result.get("soft") is True
    assert "budget" not in str(result.get("error", ""))


def test_pi_search_web_caps_at_25(monkeypatch):
    from app.pi_gateway import PiSessionContext, execute_pi_tool

    monkeypatch.delenv("EXA_ENABLED", raising=False)
    session = PiSessionContext(session_id="default")
    for i in range(25):
        result = execute_pi_tool("search_web", {"query": "probe %d" % i}, session)
        assert result.get("soft") is True
        assert "budget" not in str(result.get("error", ""))
    capped = execute_pi_tool("search_web", {"query": "probe over"}, session)
    assert "budget" in str(capped.get("error", "")).lower()
