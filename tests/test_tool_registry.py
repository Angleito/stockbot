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
