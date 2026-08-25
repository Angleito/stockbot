import asyncio
import json
from pathlib import Path
import urllib.parse
import urllib.request

import pytest
from mcp.server import MCPServer

from app.robinhood.auth import LoopbackCallback, load_tokens, parse_callback_url, save_tokens
from app.robinhood.client import RobinhoodClient, RobinhoodToolError, normalize_result


def test_oauth_state_is_private_and_round_trips(tmp_path):
    path = tmp_path / "robinhood" / "oauth.json"
    save_tokens({"tokens": {"access_token": "redacted"}}, path)
    assert load_tokens(path)["tokens"]["access_token"] == "redacted"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_normalize_result_handles_structured_models():
    class Model:
        def model_dump(self, **kwargs):
            return {"structured_content": {"value": 1}, "secret": None}

    assert normalize_result(Model()) == {"structured_content": {"value": 1}, "secret": None}


def test_parse_callback_url_requires_code_and_preserves_issuer():
    result = parse_callback_url(
        "http://127.0.0.1/callback?code=abc&state=xyz&iss=https%3A%2F%2Fissuer"
    )
    assert result == ("abc", "xyz", "https://issuer")


def test_loopback_callback_receives_browser_redirect():
    callback = LoopbackCallback("http://127.0.0.1:0/callback")
    callback.start()
    url = callback.redirect_uri + "?" + urllib.parse.urlencode(
        {"code": "abc", "state": "xyz"}
    )
    with urllib.request.urlopen(url, timeout=2) as response:
        assert response.status == 200
    assert asyncio.run(callback.callback_handler()) == ("abc", "xyz", None)


def test_mutating_tools_are_rejected_before_network():
    client = RobinhoodClient("https://example.test", allowed_tools={"get_option_quotes"})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("place_option_order", {})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("get_option_positions", {})


def test_mcp_v2_transport_adapter_lists_and_calls_tools():
    server = MCPServer("fixture")

    @server.tool()
    def quote(symbol: str) -> dict:
        return {"symbol": symbol, "last": "10.00"}

    client = RobinhoodClient(
        "fixture",
        transport_factory=lambda url, auth: server,
        allowed_tools={"quote"},
    )
    assert client.list_tools()[0]["name"] == "quote"
    result = client.call_tool("quote", {"symbol": "WING"})
    assert result["content"][0]["text"]
    assert '"last": "10.00"' in result["content"][0]["text"]
