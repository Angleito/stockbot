"""Global static registry gate: every schema has handler/capability/domain/envelope."""

from app.security.action_policy import TOOL_DOMAINS
from app.security.context_gateway import TOOL_ENVELOPES
from app import tools


def test_tool_registry_five_way_parity():
    schemas = {t["function"]["name"] for t in tools.TOOLS}
    handlers = set(tools._DIRECT_HANDLERS) | set(tools._FINRA_HANDLERS) | set(tools._ROBINHOOD_HANDLERS)
    assert schemas == handlers, f"Schemas without handlers: {sorted(schemas - handlers)}"
    assert schemas == set(tools.TOOL_CAPABILITIES), f"Missing capability: {sorted(schemas - set(tools.TOOL_CAPABILITIES))}"
    assert schemas == set(TOOL_DOMAINS), f"Missing security domain: {sorted(schemas - set(TOOL_DOMAINS))}"
    assert schemas == set(TOOL_ENVELOPES), f"Missing context envelope: {sorted(schemas - set(TOOL_ENVELOPES))}"

THESIS_TOOLS = frozenset({"thesis_create", "thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})


def test_thesis_domains_and_structured_schemas():
    assert {TOOL_DOMAINS[name] for name in THESIS_TOOLS} == {"financial_research"}

    schemas = {t["function"]["name"]: t["function"] for t in tools.TOOLS}
    create = schemas["thesis_create"]["parameters"]
    assert create["required"] == ["user_thesis"]
    assert "user_thesis" in create["properties"]
    assert {"scope", "claims", "questions"} <= set(create["properties"])
    assert not ({"idea", "answers", "offline"} & set(create["properties"]))
    refine = schemas["thesis_refine"]["parameters"]
    assert set(refine["required"]) == {"id", "clarification"}
    assert {"scope", "claims", "questions"} <= set(refine["properties"])
    assert not ({"idea", "answers", "offline"} & set(refine["properties"]))
