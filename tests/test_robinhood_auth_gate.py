"""No unprompted Robinhood OAuth: token pre-check soft errors in
execute_tool, expiry validation, and the explicit login path.

Offline: execute_tool paths never construct a client;
authorize_robinhood_browser uses a fake client; the CLI login handler is
monkeypatched.
"""

import webbrowser

import pytest

import cli
from app import tools
from app.policy import LOCAL_BROKER_CONTEXT


def _no_tokens(*args, **kwargs):
    return None


def _invalid_tokens(*args, **kwargs):
    return False


# -- execute_tool soft error without stored tokens ---------------------------

def test_market_tool_fails_soft_without_tokens(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "has_valid_tokens", _invalid_tokens)
    monkeypatch.setattr(
        webbrowser, "open", lambda *a, **k: pytest.fail("browser must not open")
    )
    result = tools.execute_tool(
        "get_market_snapshot", {"ticker": "GPRO"}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True
    assert "robinhood-login" in result["error"]
    assert result["source"] == "robinhood_mcp"


def test_portfolio_tool_fails_soft_without_client_construction(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "has_valid_tokens", _invalid_tokens)
    monkeypatch.setattr(
        tools,
        "RobinhoodClient",
        lambda *a, **k: pytest.fail("RobinhoodClient must not be constructed"),
    )
    result = tools.execute_tool(
        "get_portfolio_snapshot", {}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True
    assert "robinhood-login" in result["error"]


# -- OAuth callback port -----------------------------------------------------

def test_oauth_config_defaults_to_dynamic_port():
    assert tools.OAuthConfig(tools.get_robinhood_mcp_url()).redirect_uri == "http://127.0.0.1:0/callback"
    assert "8765" not in tools.OAuthConfig(tools.get_robinhood_mcp_url()).redirect_uri


def test_oauth_callback_avoids_occupied_log_port(tmp_path):
    import http.server
    import threading
    from urllib.parse import urlparse

    from app.robinhood.auth import LoopbackCallback

    # Occupy the log server's fixed port (8765) with a throwaway listener
    # when it is free; when a real process already holds it, the bind below
    # simply fails and the callback must still bind elsewhere.
    blocker = None
    try:
        blocker = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), http.server.BaseHTTPRequestHandler)
        threading.Thread(target=blocker.serve_forever, daemon=True).start()
    except OSError:
        pass  # 8765 already occupied by a live log server

    try:
        config = tools.OAuthConfig(tools.get_robinhood_mcp_url())
        callback = LoopbackCallback(config.redirect_uri)
        callback.start()  # close() requires the serve_forever loop to run
        assert int(urlparse(callback.redirect_uri).port) > 0
        assert int(urlparse(callback.redirect_uri).port) != 8765
        callback.close()
    finally:
        if blocker is not None:
            blocker.shutdown()
            blocker.server_close()


# -- token expiry and record hygiene -----------------------------------------

def _token_path(tmp_path):
    return tmp_path / "oauth.json"


def _expired_state(origin, age_seconds=120, expires_in=60):
    from datetime import datetime, timedelta, timezone

    return {
        "server_origin": origin,
        "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
        "tokens": {"access_token": "x", "expires_in": expires_in},
    }


def test_expired_tokens_fail_auth_required(monkeypatch, tmp_path):
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(_expired_state(origin), path)
    assert tools.has_valid_tokens(origin, path) is False

    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "DEFAULT_TOKEN_PATH", path)
    monkeypatch.setattr(
        tools,
        "RobinhoodClient",
        lambda *a, **k: pytest.fail("RobinhoodClient must not be constructed"),
    )
    result = tools.execute_tool(
        "get_market_snapshot", {"ticker": "GPRO"}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True


def test_fresh_tokens_are_valid(tmp_path):
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(_expired_state(origin, age_seconds=30), path)
    assert tools.has_valid_tokens(origin, path) is True


def test_legacy_record_without_issued_at_is_valid(tmp_path):
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(
        {"server_origin": origin, "tokens": {"access_token": "x", "expires_in": 60}},
        path,
    )
    assert tools.has_valid_tokens(origin, path) is True


def test_corrupt_state_is_invalid(tmp_path):
    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    path.write_bytes(b"not json")
    assert tools.has_valid_tokens(origin, path) is False


# -- authorize_robinhood_browser ---------------------------------------------

def test_authorize_robinhood_browser_success(monkeypatch):
    calls = []

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_tools(self):
            calls.append("list_tools")

    monkeypatch.setattr(tools, "RobinhoodClient", _FakeClient)
    assert tools.authorize_robinhood_browser() is True
    assert calls == ["list_tools"]


def test_authorize_robinhood_browser_failure(monkeypatch):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_tools(self):
            raise RuntimeError("declined")

    monkeypatch.setattr(tools, "RobinhoodClient", _FakeClient)
    assert tools.authorize_robinhood_browser() is False


# -- cli robinhood-login ------------------------------------------------------

def test_cmd_robinhood_login_success(monkeypatch, capsys):
    monkeypatch.setattr(cli, "authorize_robinhood_browser", lambda: True)
    cli._cmd_robinhood_login()
    assert "Tokens stored at" in capsys.readouterr().out


def test_cmd_robinhood_login_failure_exits(monkeypatch, capsys):
    monkeypatch.setattr(cli, "authorize_robinhood_browser", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli._cmd_robinhood_login()
    assert exc.value.code == 1
    assert "failed or was declined" in capsys.readouterr().out
