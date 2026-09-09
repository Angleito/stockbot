"""Global static registry gate: every schema has handler/capability/domain/envelope."""

from collections.abc import Mapping

from app.security.action_policy import TOOL_DOMAINS
from app.security.context_gateway import TOOL_ENVELOPES
from app import tools


def _schema_functions() -> dict[str, Mapping[str, object]]:
    functions: dict[str, Mapping[str, object]] = {}
    for tool in tools.TOOLS:
        function = tool.get("function")
        assert isinstance(function, dict)
        name = function.get("name")
        assert isinstance(name, str)
        functions[name] = function
    return functions


def test_tool_registry_five_way_parity():
    schemas = set(_schema_functions())
    handlers = set(tools._DIRECT_HANDLERS) | set(tools._FINRA_HANDLERS) | set(tools._ROBINHOOD_HANDLERS)
    assert schemas == handlers, f"Schemas without handlers: {sorted(schemas - handlers)}"
    assert schemas == set(tools.TOOL_CAPABILITIES), f"Missing capability: {sorted(schemas - set(tools.TOOL_CAPABILITIES))}"
    assert schemas == set(TOOL_DOMAINS), f"Missing security domain: {sorted(schemas - set(TOOL_DOMAINS))}"
    assert schemas == set(TOOL_ENVELOPES), f"Missing context envelope: {sorted(schemas - set(TOOL_ENVELOPES))}"

THESIS_TOOLS = frozenset({"thesis_create", "thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})


def test_thesis_domains_and_structured_schemas():
    assert {TOOL_DOMAINS[name] for name in THESIS_TOOLS} == {"financial_research"}

    schemas = _schema_functions()
    create = schemas["thesis_create"].get("parameters")
    assert isinstance(create, dict)
    assert create["required"] == ["user_thesis"]
    assert "user_thesis" in create["properties"]
    assert {"scope", "claims", "questions"} <= set(create["properties"])
    assert not ({"idea", "answers", "offline"} & set(create["properties"]))
    refine = schemas["thesis_refine"].get("parameters")
    assert isinstance(refine, dict)
    assert set(refine["required"]) == {"id", "clarification"}
    assert {"scope", "claims", "questions"} <= set(refine["properties"])
    assert not ({"idea", "answers", "offline"} & set(refine["properties"]))
