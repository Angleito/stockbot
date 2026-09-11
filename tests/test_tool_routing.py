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


def test_browse_all_covers_every_registry_name() -> None:
    result = _browse({})
    assert result["total"] == len(TOOL_DISCOVERY_REGISTRY)
    listed = next(
        (
            v
            for v in result.values()
            if isinstance(v, list)
            and v
            and all(isinstance(i, dict) and "name" in i and ("summary" in i or "domain" in i) for i in v)
        ),
        None,
    )
    assert listed is not None
    pairs = [(i.get("domain"), i.get("name")) for i in listed if isinstance(i, dict)]
    assert pairs == sorted(pairs)
    assert {n for _, n in pairs} == set(TOOL_DISCOVERY_REGISTRY)


def test_browse_domain_slice_equals_registry_slice() -> None:
    domain = TOOL_DISCOVERY_REGISTRY["get_short_interest"].domain
    result = _browse({"domain": domain})
    expected = sorted(n for n, m in TOOL_DISCOVERY_REGISTRY.items() if m.domain == domain)
    matches = result["matches"]
    assert isinstance(matches, list)
    assert [m["name"] for m in matches if isinstance(m, dict)] == expected
    assert result["total"] == len(expected)


def test_browse_name_returns_summary_plus_canonical_parameters() -> None:
    result = _browse({"name": "get_short_interest"})
    assert result["summary"] == TOOL_DISCOVERY_REGISTRY["get_short_interest"].summary
    canonical = next(t for t in TOOLS if _tool_function(t).get("name") == "get_short_interest")
    assert result["parameters"] == _tool_function(canonical)["parameters"]
    required = result.get("required", result.get("required_arguments"))
    assert isinstance(required, list) and "ticker" in required


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
