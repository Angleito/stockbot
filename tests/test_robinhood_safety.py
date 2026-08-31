"""Offline safety tests for the Robinhood read-only capability policy."""

import pytest
from mcp.server import MCPServer

from app.robinhood.client import RobinhoodClient, RobinhoodToolError


TRADING_TOOLS = [
    "place_order",
    "submit_order",
    "cancel_order",
    "replace_order",
    "modify_order",
    "review_equity_order",
    "place_equity_order",
    "cancel_equity_order",
    "replace_equity_order",
    "place_option_order",
    "exercise_option",
]

MONEY_MOVEMENT_TOOLS = ["withdraw", "deposit", "transfer", "bank_transfer"]


def _fixture_server() -> MCPServer:
    server = MCPServer("fixture")

    @server.tool()
    def get_accounts() -> dict:
        return {"accounts": []}

    @server.tool()
    def get_portfolio() -> dict:
        return {"portfolio": {}}

    @server.tool()
    def get_equity_positions() -> dict:
        return {"positions": []}

    @server.tool()
    def get_equity_quotes(symbol: str) -> dict:
        return {"symbol": symbol}

    @server.tool()
    def get_scanner_filter_specs() -> dict:
        return {"results": []}

    @server.tool()
    def get_scans() -> dict:
        return {"scans": []}

    @server.tool()
    def run_scan(scan_id: str) -> dict:
        return {"scan_id": scan_id, "results": []}

    return server


@pytest.mark.parametrize("tool", ["some_new_unknown_tool", "get_watchlists"])
def test_unknown_tools_are_denied_before_network(tool):
    client = RobinhoodClient("https://example.test")
    with pytest.raises(RobinhoodToolError):
        client.call_tool(tool, {})


@pytest.mark.parametrize("tool", TRADING_TOOLS)
def test_trading_tools_are_denied_before_network(tool):
    client = RobinhoodClient("https://example.test")
    with pytest.raises(RobinhoodToolError):
        client.call_tool(tool, {})


@pytest.mark.parametrize("tool", MONEY_MOVEMENT_TOOLS)
def test_money_movement_tools_are_denied_before_network(tool):
    client = RobinhoodClient("https://example.test")
    with pytest.raises(RobinhoodToolError):
        client.call_tool(tool, {})


def test_account_reads_allowed_only_when_explicitly_allowlisted():
    client = RobinhoodClient(
        "fixture",
        transport_factory=lambda url, auth: _fixture_server(),
        account_tools=frozenset({"get_accounts", "get_portfolio", "get_equity_positions"}),
    )
    for tool in ("get_accounts", "get_portfolio", "get_equity_positions"):
        result = client.call_tool(tool, {})
        assert result["content"][0]["text"]
    for tool in ("get_equity_orders", "get_transactions", "get_realized_pnl"):
        with pytest.raises(RobinhoodToolError):
            client.call_tool(tool, {})


def test_default_client_allows_market_reads_and_denies_account_and_trading_reads():
    client = RobinhoodClient(
        "fixture",
        transport_factory=lambda url, auth: _fixture_server(),
    )
    result = client.call_tool("get_equity_quotes", {"symbol": "WING"})
    assert result["content"][0]["text"]
    for tool in ("get_accounts", "get_portfolio", "get_equity_positions", "get_scans", "run_scan"):
        with pytest.raises(RobinhoodToolError):
            client.call_tool(tool, {})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("place_equity_order", {})


SCAN_READ_TOOLS = ["get_scanner_filter_specs", "get_scans", "run_scan"]
SCAN_WRITE_TOOLS = ["create_scan", "update_scan_filters", "update_scan_config"]


def test_scan_reads_require_explicit_account_capability():
    client = RobinhoodClient(
        "fixture",
        transport_factory=lambda url, auth: _fixture_server(),
        account_tools=frozenset({"get_scans", "run_scan"}),
    )
    for tool in ("get_scanner_filter_specs", "get_scans", "run_scan"):
        result = client.call_tool(tool, {"scan_id": "s"} if tool == "run_scan" else {})
        assert result["content"][0]["text"]
    for tool in ("get_equity_orders", "place_equity_order"):
        with pytest.raises(RobinhoodToolError):
            client.call_tool(tool, {})


def test_scan_writes_are_unknown_and_denied():
    client = RobinhoodClient("https://example.test")
    for tool in SCAN_WRITE_TOOLS:
        with pytest.raises(RobinhoodToolError):
            client.call_tool(tool, {})


def test_deprecated_allowed_tools_alias_still_works():
    client = RobinhoodClient(
        "fixture",
        transport_factory=lambda url, auth: _fixture_server(),
        allowed_tools={"get_equity_quotes"},
    )
    result = client.call_tool("get_equity_quotes", {"symbol": "WING"})
    assert result["content"][0]["text"]
    with pytest.raises(RobinhoodToolError):
        client.call_tool("get_accounts", {})


def test_capability_categories_cannot_be_cross_configured():
    with pytest.raises(ValueError, match="Unknown market_read"):
        RobinhoodClient("https://example.test", market_tools=frozenset({"get_accounts"}))
    with pytest.raises(ValueError, match="Unknown account_read"):
        RobinhoodClient("https://example.test", account_tools=frozenset({"get_equity_quotes"}))
