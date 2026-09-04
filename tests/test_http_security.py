"""Security properties at the untrusted HTTP and agent boundaries."""

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.robinhood.auth import OAuthConfig, OAuthStoreError, load_tokens_for_origin, save_tokens
from app.robinhood.client import RobinhoodClient


def test_chat_route_is_gone():
    client = TestClient(app)
    assert client.post("/chat", json={"messages": [{"role": "user", "content": "hello"}]}).status_code == 404


def test_oauth_state_is_bound_to_the_robinhood_origin(tmp_path):
    path = tmp_path / "oauth.json"
    save_tokens({"server_origin": "https://agent.robinhood.com", "tokens": {"access_token": "x"}}, path)
    assert load_tokens_for_origin("https://agent.robinhood.com", path)
    assert load_tokens_for_origin("https://other.example", path) is None
    with pytest.raises(OAuthStoreError):
        OAuthConfig("https://other.example/mcp")
    with pytest.raises(OAuthStoreError):
        OAuthConfig("https://agent.robinhood.com:8443/mcp/trading")
    with pytest.raises(OAuthStoreError):
        OAuthConfig("https://agent.robinhood.com/other")


def test_oauth_client_rejects_mismatched_mcp_transport():
    oauth = OAuthConfig("https://agent.robinhood.com/mcp/trading")
    with pytest.raises(ValueError, match="must match"):
        RobinhoodClient("https://other.example/mcp", oauth=oauth)


def test_custom_robinhood_tool_configuration_cannot_widen_permissions():
    with pytest.raises(ValueError, match="Unknown"):
        RobinhoodClient("https://example.test", market_tools=frozenset({"delete_watchlist"}))
