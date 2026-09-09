import asyncio
import json
from pathlib import Path
import urllib.parse
import urllib.request

import pytest
from mcp.server import MCPServer

from app.robinhood.auth import LoopbackCallback, load_tokens, parse_callback_url, save_tokens
from app.robinhood.client import (
    RobinhoodClient,
    RobinhoodToolError,
    _HttpSessionContext,
    normalize_result,
)


def test_oauth_state_is_private_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "robinhood" / "oauth.json"
    save_tokens({"tokens": {"access_token": "redacted"}}, path)
    loaded = load_tokens(path)
    assert loaded is not None
    tokens = loaded["tokens"]
    assert isinstance(tokens, dict)
    assert tokens["access_token"] == "redacted"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_normalize_result_handles_structured_models() -> None:
    class Model:
        def model_dump(self, **kwargs: object) -> dict[str, object]:
            return {"structured_content": {"value": 1}, "secret": None}

    assert normalize_result(Model()) == {"structured_content": {"value": 1}, "secret": None}


def test_parse_callback_url_requires_code_and_preserves_issuer() -> None:
    result = parse_callback_url(
        "http://127.0.0.1/callback?code=abc&state=xyz&iss=https%3A%2F%2Fissuer"
    )
    assert result == ("abc", "xyz", "https://issuer")


def test_loopback_callback_receives_browser_redirect() -> None:
    callback = LoopbackCallback("http://127.0.0.1:0/callback")
    callback.start()
    url = callback.redirect_uri + "?" + urllib.parse.urlencode(
        {"code": "abc", "state": "xyz"}
    )
    with urllib.request.urlopen(url, timeout=2) as response:
        assert response.status == 200
    assert asyncio.run(callback.callback_handler()) == ("abc", "xyz", None)


def test_mutating_tools_are_rejected_before_network() -> None:
    client = RobinhoodClient("https://example.test", allowed_tools={"get_option_quotes"})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("place_option_order", {})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("get_option_positions", {})


def test_deprecated_allowed_tools_alias_restricts_account_reads() -> None:
    client = RobinhoodClient("https://example.test", allowed_tools={"get_equity_quotes"})
    with pytest.raises(RobinhoodToolError):
        client.call_tool("get_accounts", {})


def test_mcp_v2_transport_adapter_lists_and_calls_tools() -> None:
    server = MCPServer("fixture")

    @server.tool()
    def get_equity_quotes(symbol: str) -> dict[str, object]:
        return {"symbol": symbol, "last": "10.00"}

    def _factory(url: str, auth: object) -> object:
        return server

    client = RobinhoodClient(
        "fixture",
        transport_factory=_factory,
        allowed_tools={"get_equity_quotes"},
    )
    assert client.list_tools()[0]["name"] == "get_equity_quotes"
    result = client.call_tool("get_equity_quotes", {"symbol": "WING"})
    assert isinstance(result, dict)
    content = result["content"]
    assert isinstance(content, list)
    item = content[0]
    assert isinstance(item, dict)
    text = item["text"]
    assert isinstance(text, str)
    assert text
    assert '"last": "10.00"' in text


def test_authenticated_mcp_transport_does_not_follow_redirects() -> None:
    seen: dict[str, object] = {}

    class FakeHttpClient:
        async def __aenter__(self) -> FakeHttpClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeHttpx:
        @staticmethod
        def AsyncClient(**kwargs: object) -> FakeHttpClient:
            seen.update(kwargs)
            return FakeHttpClient()

    class FakeTransport:
        async def __aenter__(self) -> FakeTransport:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class FakeClient:
        def __init__(self, transport: object) -> None:
            self.transport = transport

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    def _transport(url: str, **kwargs: object) -> FakeTransport:
        return FakeTransport()

    context = _HttpSessionContext(
        "https://agent.robinhood.com/mcp/trading",
        object(),
        _transport,
        FakeClient,
        FakeHttpx,
    )
    asyncio.run(context.__aenter__())
    asyncio.run(context.__aexit__())

    assert seen["follow_redirects"] is False
