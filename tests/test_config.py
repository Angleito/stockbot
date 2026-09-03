"""Configuration validation stays aligned with the documented .env template."""

import pytest

from app import config
from app.robinhood.auth import OAuthStoreError


@pytest.mark.parametrize(
    "value",
    ("Your Name your.email@example.com", "YourName [EMAIL]", "  sk-or-...  ", "your_finra_client_id"),
)
def test_require_env_rejects_documented_placeholder_values(monkeypatch, value):
    monkeypatch.setenv("TEST_SETTING", value)

    with pytest.raises(ValueError, match="TEST_SETTING is not properly set"):
        config._require_env("TEST_SETTING")


def test_require_env_strips_and_returns_configured_value(monkeypatch):
    monkeypatch.setenv("TEST_SETTING", "  configured-value  ")

    assert config._require_env("TEST_SETTING") == "configured-value"


def test_local_chat_policy_reads_server_controls(monkeypatch):
    monkeypatch.setenv("DEFAULT_MODEL", "provider/default")
    monkeypatch.setenv("CHAT_ALLOWED_MODELS", "provider/secondary")
    monkeypatch.setenv("CHAT_MAX_MESSAGES", "3")
    monkeypatch.setenv("CHAT_MAX_CONTENT_CHARS", "40")
    monkeypatch.setenv("OPENROUTER_TIMEOUT_SECONDS", "2.5")

    policy = config.get_local_chat_policy()

    assert policy.allowed_models == {"provider/default", "provider/secondary"}
    assert policy.max_messages == 3
    assert policy.max_message_chars == 40
    assert policy.upstream_timeout_seconds == 2.5


@pytest.mark.parametrize(
    "url",
    (
        "http://agent.robinhood.com/mcp/trading",
        "https://agent.robinhood.com:8443/mcp/trading",
        "https://agent.robinhood.com/other",
        "https://user@agent.robinhood.com/mcp/trading",
        "https://agent.robinhood.com/mcp/trading?x=1",
    ),
)
def test_robinhood_endpoint_is_pinned(monkeypatch, url):
    monkeypatch.setenv("ROBINHOOD_MCP_URL", url)
    with pytest.raises(OAuthStoreError, match="Robinhood MCP URL"):
        config.get_robinhood_mcp_url()

def test_broker_enabled_prefers_new_true(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setenv("ROBINHOOD_ENABLED", "false")
    assert config.broker_enabled() is True

def test_broker_enabled_explicit_new_false_wins(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "false")
    monkeypatch.setenv("ROBINHOOD_ENABLED", "true")
    assert config.broker_enabled() is False

def test_broker_enabled_legacy_fallback(monkeypatch):
    monkeypatch.delenv("BROKER_ENABLED", raising=False)
    monkeypatch.setenv("ROBINHOOD_ENABLED", "true")
    assert config.broker_enabled() is True

def test_broker_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("BROKER_ENABLED", raising=False)
    monkeypatch.delenv("ROBINHOOD_ENABLED", raising=False)
    assert config.broker_enabled() is False
