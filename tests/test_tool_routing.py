"""Behavioral dispatch checks over the discovery catalog (no rank asserts)."""

import pytest

from app import tools as tools_mod
from app.policy import Capability, RequestContext
from app.tools import (
    TOOLS,
    TOOL_DISCOVERY_REGISTRY,
    _tool_function,
    execute_tool,
)

_CTX = RequestContext("test", frozenset({Capability.RESEARCH}))


def _browse(args: dict[str, object]) -> dict[str, object]:
    result = execute_tool("browse_tools", args, "test", context=_CTX)
    assert "error" not in result, result
    return result


def test_browse_root_lists_domains_only() -> None:
    result = _browse({})
    assert result["path"] == "/"
    domains = result["domains"]
    assert isinstance(domains, list)
    names = {d["name"] for d in domains if isinstance(d, dict)}
    assert names == {m.domain for m in TOOL_DISCOVERY_REGISTRY.values()}
    assert all(isinstance(d, dict) and d["path"] == f"/{d['name']}" for d in domains)
    assert "tools" not in result and "matches" not in result


def test_browse_domain_lists_families() -> None:
    result = _browse({"domain": "finra"})
    assert result["path"] == "/finra"
    fams_raw = result["families"]
    assert isinstance(fams_raw, list)
    fams = {f["name"]: f for f in fams_raw if isinstance(f, dict)}
    assert fams["short-interest"]["tool_count"] == 4
    assert fams["short-interest"]["path"] == "/finra/short-interest"


def test_browse_family_returns_four_tools_plus_contrast() -> None:
    result = _browse({"domain": "finra", "family": "short-interest"})
    assert result["path"] == "/finra/short-interest"
    assert result["count"] == 4
    tools_raw = result["tools"]
    assert isinstance(tools_raw, list)
    assert [t["name"] for t in tools_raw if isinstance(t, dict)] == ["get_finra_datapoints", "get_short_interest", "get_short_pressure_profile", "query_finra"]
    contrast_raw = result["contrast_table"]
    assert isinstance(contrast_raw, list)
    assert len(contrast_raw) == 4
    by_tool = {r["tool"]: r for r in contrast_raw if isinstance(r, dict)}
    assert by_tool["get_short_interest"]["use_it_for"]
    assert "get_finra_datapoints" in str(by_tool["get_short_interest"]["do_not_use_it_for"])
    assert "query_finra" in str(by_tool["get_short_interest"]["do_not_use_it_for"])


def test_browse_name_returns_full_contract() -> None:
    result = _browse({"name": "get_short_interest"})
    assert result["summary"] == TOOL_DISCOVERY_REGISTRY["get_short_interest"].summary
    assert result["family"] == "short-interest"
    assert result["intent"] == "current_reported_short_position"
    canonical = next(t for t in TOOLS if _tool_function(t).get("name") == "get_short_interest")
    assert result["parameters"] == _tool_function(canonical)["parameters"]
    assert isinstance(result["required_arguments"], list) and "ticker" in result["required_arguments"]


def test_browse_rejects_bad_paths() -> None:
    bad_family = execute_tool("browse_tools", {"family": "short-interest"}, "test", context=_CTX)
    assert "error" in bad_family
    unknown_domain = execute_tool("browse_tools", {"domain": "nope"}, "test", context=_CTX)
    assert unknown_domain.get("error") == "unknown_domain"
    unknown_family = execute_tool("browse_tools", {"domain": "finra", "family": "nope"}, "test", context=_CTX)
    assert "error" in unknown_family
    both = execute_tool("browse_tools", {"domain": "finra", "name": "get_short_interest"}, "test", context=_CTX)
    assert "error" not in both and both["name"] == "get_short_interest"


def test_invalid_arguments_return_repairable_shape_and_execute_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def _canary(args: dict[str, object], model: str) -> dict[str, object]:
        calls.append(dict(args))
        return {"unexpected": True}

    for handlers in (
        tools_mod._MODEL_HANDLERS,
        tools_mod._FINRA_HANDLERS,
        tools_mod._ROBINHOOD_HANDLERS,
        tools_mod._THESIS_HANDLERS,
    ):
        if "get_short_interest" in handlers:
            monkeypatch.setitem(handlers, "get_short_interest", _canary)
    result = execute_tool("get_short_interest", {}, "test", context=_CTX)
    assert result["error_type"] == "invalid_tool_arguments"
    assert result["tool"] == "get_short_interest"
    required = result["required"]
    assert isinstance(required, list) and "ticker" in required
    assert calls == []


def test_unknown_tool_executes_nothing() -> None:
    result = execute_tool("no_such_tool", {}, "test", context=_CTX)
    assert "error" in result
    assert "no_such_tool" in str(result["error"])
