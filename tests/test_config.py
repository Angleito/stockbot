"""Configuration validation stays aligned with the documented .env template."""

import pytest

from app import config


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
