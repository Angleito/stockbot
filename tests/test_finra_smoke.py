"""Opt-in live smoke test against FINRA mock datasets.

Run with:
    RUN_FINRA_SMOKE=1 venv/bin/pytest -m finra_smoke -q

Requires FINRA_CLIENT_ID / FINRA_CLIENT_SECRET (a Mock credential is the
right fit: mock mode appends "Mock" to dataset names automatically).
Skipped automatically unless both credentials and RUN_FINRA_SMOKE=1 exist.

Verifies the production agent contract end-to-end: ranked catalog discovery,
canonical field names, partition-aware latest retrieval with no HTTP 400,
and truthful freshness surfacing (never a silent 'current' claim on stale
data).
"""

import os

import pytest

from app import finra_client
from app.tool_render import render_tool_result

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
    finra_client.reset_partitions_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()


def _acceptable(result: dict) -> bool:
    """A smoke query passes when FINRA answered (briefing or honest no-data)."""
    if "metrics" in result:
        return True
    return str(result.get("error", "")).startswith("No data found")


def test_smoke_catalog_reachable():
    result = finra_client.list_datasets()
    assert "error" not in result, result
    assert result["count"] > 0


def test_smoke_catalog_ranked_weekly_summary_discovery():
    """'OTC weekly trading volume' must rank otcMarket/weeklySummary first."""
    result = finra_client.list_datasets(search="OTC weekly trading volume")
    assert "error" not in result, result
    assert result["datasets"], "expected ranked matches"
    assert result["datasets"][0]["dataset"] == "otcMarket/weeklySummary", [
        d["dataset"] for d in result["datasets"]
    ]
    described = finra_client.describe_dataset("otcMarket/weeklySummary")
    assert "error" not in described, described
    assert described["ticker_field"] == "issueSymbolIdentifier"
    assert described["date_field"] == "summaryStartDate"


def test_smoke_short_interest_uses_canonical_fields():
    result = finra_client.query_dataset(
        "otcMarket/consolidatedShortInterest", ticker="AAPL", limit=5
    )
    assert _acceptable(result), result
    if "metrics" in result:
        fields = result["metrics"]["fields"]
        assert "daysToCoverQuantity" in fields
        assert "averageDailyVolumeQuantity" in fields
        assert result["as_of_date"] is not None
        assert result["data_freshness"] in ("current", "stale")
        assert result["environment"] in ("production", "mock")
        # Truthfulness: stale data must be surfaced, never silently current.
        if result["data_freshness"] == "stale":
            assert any("STALE" in w for w in result["warnings"])
            assert "STALE" in render_tool_result(result)
        else:
            assert not any("STALE" in w for w in result["warnings"])


def test_smoke_latest_five_datapoints_partitions_flow():
    """'Latest five' sorts via the partitions walk: no 400, descending
    newest-first records, canonical fields, honest freshness."""
    result = finra_client.get_finra_datapoints(
        "otcMarket/consolidatedShortInterest",
        fields=[
            "settlementDate",
            "currentShortPositionQuantity",
            "daysToCoverQuantity",
            "averageDailyVolumeQuantity",
        ],
        ticker="AAPL",
        limit=5,
        sort_order="desc",
    )
    assert "error" not in result, result
    assert "http_status" not in result  # no 400: unrestricted sortFields never sent
    assert result["sort_source"] == "partitions"
    assert result["pagination_source"] == "partitions"
    dates = [r["settlementDate"] for r in result["records"]]
    assert dates == sorted(dates, reverse=True)
    row_fields = set(result["records"][0])
    assert {"settlementDate", "daysToCoverQuantity", "averageDailyVolumeQuantity"} <= row_fields
    assert result["as_of_date"] is not None
    assert result["data_freshness"] in ("current", "stale")
    assert result["environment"] in ("production", "mock")
    rendered = render_tool_result(result)
    assert "Source: FINRA Query API" in rendered
    if result["data_freshness"] == "stale":
        assert "STALE/HISTORICAL DATA" in rendered
    else:
        assert "STALE/HISTORICAL DATA" not in rendered
        assert "freshness: current" in rendered