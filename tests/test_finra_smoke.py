"""Opt-in live smoke test against FINRA mock datasets.

Run with:
    RUN_FINRA_SMOKE=1 venv/bin/pytest -m finra_smoke -q

Requires FINRA_CLIENT_ID / FINRA_CLIENT_SECRET (a Mock credential is the
right fit: mock mode appends "Mock" to dataset names automatically).
Skipped automatically unless both credentials and RUN_FINRA_SMOKE=1 exist.
"""

import os

import pytest

from app import finra_client

_HAS_CREDS = bool(os.getenv("FINRA_CLIENT_ID")) and bool(
    os.getenv("FINRA_CLIENT_SECRET")
)
_SMOKE_ENABLED = os.getenv("RUN_FINRA_SMOKE") == "1"

pytestmark = [
    pytest.mark.finra_smoke,
    pytest.mark.skipif(
        not (_SMOKE_ENABLED and _HAS_CREDS),
        reason="requires RUN_FINRA_SMOKE=1 and FINRA_CLIENT_ID/FINRA_CLIENT_SECRET",
    ),
]


@pytest.fixture(autouse=True)
def _mock_mode(monkeypatch):
    monkeypatch.setenv("FINRA_USE_MOCK", "1")
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()


def _acceptable(result: dict) -> bool:
    """A smoke query passes when FINRA answered (data or honest no-data)."""
    if "records" in result:
        return True
    return str(result.get("error", "")).startswith("No data found")


def test_smoke_catalog_reachable():
    result = finra_client.list_datasets()
    assert "error" not in result, result
    assert result["count"] > 0


def test_smoke_mock_short_interest_query():
    result = finra_client.query_dataset(
        "otcMarket/consolidatedShortInterest", limit=5
    )
    assert _acceptable(result), result


def test_smoke_mock_weekly_summary_query():
    result = finra_client.query_dataset("otcMarket/weeklySummary", limit=5)
    assert _acceptable(result), result
