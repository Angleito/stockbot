"""No unprompted Robinhood OAuth: token pre-check soft errors in
execute_tool, expiry validation, and the explicit login path.

Offline: execute_tool paths never construct a client;
authorize_robinhood_browser uses a fake client; the CLI login handler is
monkeypatched.
"""

import webbrowser

from pathlib import Path

import pytest

import cli
from app import tools
from app.policy import LOCAL_BROKER_CONTEXT


def _no_tokens(*args: object, **kwargs: object) -> None:
    return None


def _invalid_tokens(*args: object, **kwargs: object) -> bool:
    return False


# -- execute_tool soft error without stored tokens ---------------------------

def test_market_tool_fails_soft_without_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "has_valid_tokens", _invalid_tokens)
    def _no_browser(*args: object, **kwargs: object) -> None:
        pytest.fail("browser must not open")
    monkeypatch.setattr(
        webbrowser, "open", _no_browser
    )
    result = tools.execute_tool(
        "get_market_snapshot", {"ticker": "GPRO"}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True
    err = result["error"]
    assert isinstance(err, str)
    assert "robinhood-login" in err
    assert result["source"] == "robinhood_mcp"


def test_portfolio_tool_fails_soft_without_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "has_valid_tokens", _invalid_tokens)
    def _no_client(*args: object, **kwargs: object) -> None:
        pytest.fail("RobinhoodClient must not be constructed")
    monkeypatch.setattr(
        tools,
        "RobinhoodClient",
        _no_client,
    )
    result = tools.execute_tool(
        "get_portfolio_snapshot", {}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True
    err2 = result["error"]
    assert isinstance(err2, str)
    assert "robinhood-login" in err2


# -- OAuth callback port -----------------------------------------------------

def test_oauth_config_defaults_to_dynamic_port() -> None:
    assert tools.OAuthConfig(tools.get_robinhood_mcp_url()).redirect_uri == "http://127.0.0.1:0/callback"
    assert "8765" not in tools.OAuthConfig(tools.get_robinhood_mcp_url()).redirect_uri


def test_oauth_callback_avoids_occupied_log_port(tmp_path: Path) -> None:
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
        port = urlparse(callback.redirect_uri).port
        assert port is not None
        assert port > 0
        assert port != 8765
    finally:
        if blocker is not None:
            blocker.shutdown()
            blocker.server_close()


# -- token expiry and record hygiene -----------------------------------------

def _token_path(tmp_path: Path) -> Path:
    return tmp_path / "oauth.json"


def _expired_state(origin: str, age_seconds: int = 120, expires_in: int = 60) -> dict[str, object]:
    from datetime import datetime, timedelta, timezone

    return {
        "server_origin": origin,
        "issued_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
        "tokens": {"access_token": "x", "expires_in": expires_in},
    }


def test_expired_tokens_fail_auth_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(_expired_state(origin), path)
    assert tools.has_valid_tokens(origin, path) is False

    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setattr(tools, "DEFAULT_TOKEN_PATH", path)
    def _no_client2(*args: object, **kwargs: object) -> None:
        pytest.fail("RobinhoodClient must not be constructed")
    monkeypatch.setattr(
        tools,
        "RobinhoodClient",
        _no_client2,
    )
    result = tools.execute_tool(
        "get_market_snapshot", {"ticker": "GPRO"}, model="test", context=LOCAL_BROKER_CONTEXT
    )
    assert result["error_type"] == "auth_required"
    assert result["soft"] is True


def test_fresh_tokens_are_valid(tmp_path: Path) -> None:
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(_expired_state(origin, age_seconds=30), path)
    assert tools.has_valid_tokens(origin, path) is True


def test_legacy_record_without_issued_at_is_valid(tmp_path: Path) -> None:
    from app.robinhood.auth import save_tokens

    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    save_tokens(
        {"server_origin": origin, "tokens": {"access_token": "x", "expires_in": 60}},
        path,
    )
    assert tools.has_valid_tokens(origin, path) is True


def test_corrupt_state_is_invalid(tmp_path: Path) -> None:
    origin = tools.OAuthConfig(tools.get_robinhood_mcp_url()).server_origin
    path = _token_path(tmp_path)
    path.write_bytes(b"not json")
    assert tools.has_valid_tokens(origin, path) is False


# -- authorize_robinhood_browser ---------------------------------------------

def test_authorize_robinhood_browser_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_tools(self) -> None:
            calls.append("list_tools")

    monkeypatch.setattr(tools, "RobinhoodClient", _FakeClient)
    assert tools.authorize_robinhood_browser() is True
    assert calls == ["list_tools"]


def test_authorize_robinhood_browser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def list_tools(self) -> None:
            raise RuntimeError("declined")

    monkeypatch.setattr(tools, "RobinhoodClient", _FakeClient)
    assert tools.authorize_robinhood_browser() is False


# -- cli robinhood-login ------------------------------------------------------

def test_cmd_robinhood_login_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _ok() -> bool:
        return True
    monkeypatch.setattr(cli, "authorize_robinhood_browser", _ok)
    cli._cmd_robinhood_login()
    assert "Tokens stored at" in capsys.readouterr().out


def test_cmd_robinhood_login_failure_exits(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _no() -> bool:
        return False
    monkeypatch.setattr(cli, "authorize_robinhood_browser", _no)
