"""Robinhood read-only boundary tests at the tool-registry level."""

from app.robinhood.capabilities import (
    BLOCKED_KEYWORDS,
    BLOCKED_TOOLS,
    is_blocked,
    tool_capability,
)
from app.security.action_policy import TOOL_DOMAINS
from app.security.context_gateway import TOOL_ENVELOPES
from app.tool_render import render_tool_result
from app.tools import TOOLS


def test_no_trading_tool_schemas_in_registry():
    names = [tool["function"]["name"] for tool in TOOLS]
    for name in names:
        lowered = name.lower()
        assert not is_blocked(lowered), f"trading-like tool in registry: {name}"
        assert lowered not in BLOCKED_TOOLS, f"blocked tool in registry: {name}"
    assert not any(
        keyword in name for name in names for keyword in BLOCKED_KEYWORDS
    ), "trading keyword leaked into a model-visible tool schema"


def test_blocked_keyword_set_covers_trading_verbs():
    for keyword in ("order", "trade", "place", "submit", "cancel", "replace",
                    "modify", "exercise", "withdraw", "deposit", "transfer"):
        assert keyword in BLOCKED_KEYWORDS


def test_is_blocked_denies_trading_tools():
    assert is_blocked("place_equity_order")
    assert is_blocked("review_equity_order")
    assert is_blocked("withdraw")
    assert not is_blocked("get_market_snapshot")
    assert not is_blocked("get_portfolio_snapshot")


def test_unknown_tools_have_no_capability():
    # Unknown tools are denied by capability classification, not is_blocked.
    assert tool_capability("some_new_unknown_tool") is None
    assert tool_capability("get_watchlists") is None
    assert tool_capability("place_equity_order") is None
    assert tool_capability("get_equity_quotes") is not None
    assert tool_capability("get_portfolio") is not None


def test_portfolio_tools_map_to_portfolio_read_domain():
    for name in ("get_portfolio_snapshot", "get_scans", "run_scan"):
        assert TOOL_DOMAINS[name] == "portfolio_read"
        assert TOOL_ENVELOPES[name].sensitivity.value == "private"


def test_portfolio_render_never_dumps_raw_mcp_payload():
    result = {
        "result_type": "portfolio_snapshot",
        "broker": "robinhood",
        "created_at": "2026-08-01T00:00:00Z",
        "total_value": "$1000.00",
        "cash": "$50.00",
        "invested_value": "$950.00",
        "position_count": 1,
        "priced_position_count": 1,
        "unresolved_position_count": 0,
        "source": "robinhood_mcp",
        "positions": [{
            "ticker": "AMD",
            "quantity": "10.0",
            "market_price": "100.00",
            "market_value": "1000.00",
            "portfolio_weight": "1.0",
            "unrealized_gain": "20.00",
        }],
    }
    rendered = render_tool_result(result)
    # Normalized fields render; raw MCP payload keys never do.
    assert "AMD" in rendered
    assert "robinhood_mcp" in rendered
    for raw_key in ("instrument", "url", "metadata", "account_id", "quantity_qty"):
        assert raw_key not in rendered
    # Rendered as text lines, not JSON.
    assert rendered.strip().startswith("Portfolio snapshot")
