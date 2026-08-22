"""Opt-in live smoke test against FINRA production endpoints.

Run with:
    RUN_FINRA_PRODUCTION_SMOKE=1 venv/bin/pytest -m finra_production_smoke -q

Requires valid production FINRA credentials (FINRA_CLIENT_ID /
FINRA_CLIENT_SECRET) and real network access to api.finra.org. Unlike the
mock smoke suite (tests/test_finra_smoke.py), this suite never forces mock
mode: it exercises the production Query API exactly as the agent does.
Skipped automatically unless RUN_FINRA_PRODUCTION_SMOKE=1 and credentials
exist.

Verifies: catalog discovery, exact latest-five short interest without HTTP
400, canonical field names, tuple-safe multi-partition retrieval, and
truthful stale/current labeling.
"""

import os

import pytest

from app import finra_client
from app.tool_render import render_tool_result

_HAS_CREDS = bool(os.getenv("FINRA_CLIENT_ID")) and bool(
    os.getenv("FINRA_CLIENT_SECRET")
)
_SMOKE_ENABLED = os.getenv("RUN_FINRA_PRODUCTION_SMOKE") == "1"

pytestmark = [
    pytest.mark.finra_production_smoke,
    pytest.mark.skipif(
        not (_SMOKE_ENABLED and _HAS_CREDS),
        reason="requires RUN_FINRA_PRODUCTION_SMOKE=1 and FINRA_CLIENT_ID/FINRA_CLIENT_SECRET",
    ),
]


class _NoCache:
    """Ensure production smoke tests exercise FINRA, never SQLite history."""

    @staticmethod
    def get(_key, ttl=None):
        return None

    @staticmethod
    def set(_key, _value):
        pass


@pytest.fixture(autouse=True)
def _production_mode(monkeypatch):
    # Never force mock mode: this suite must exercise the production API.
    monkeypatch.delenv("FINRA_USE_MOCK", raising=False)
    monkeypatch.setattr(finra_client, "cache", _NoCache())
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()


def test_prod_smoke_catalog_discovery():
    result = finra_client.list_datasets()
    assert "error" not in result, result
    assert result["count"] > 0
    ids = {d["dataset"] for d in result["datasets"]}
    assert "otcMarket/consolidatedShortInterest" in ids


def test_prod_smoke_latest_five_short_interest_no_400():
    """'Latest five' short interest via the partitions walk: no HTTP 400,
    descending newest-first records, canonical fields."""
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
    assert result["environment"] == "production"
    assert result["data_freshness"] == "current", result


def test_prod_smoke_tuple_safe_partition_retrieval():
    """Multi-partition walk (weekStartDate + tierIdentifier) over published
    tuples only: the walk succeeds without HTTP 400 and returns the latest
    week's rows first."""
    result = finra_client.get_finra_datapoints(
        "otcMarket/weeklySummary",
        fields=["summaryStartDate", "totalWeeklyShareQuantity"],
        ticker="AAPL",
        limit=3,
        sort_order="desc",
    )
    assert "error" not in result, result
    assert "http_status" not in result
    assert result["sort_source"] == "partitions"
    dates = [r["summaryStartDate"] for r in result["records"]]
    assert dates == sorted(dates, reverse=True)
    assert result["environment"] == "production"


def test_prod_smoke_truthful_freshness_labeling():
    """The chatbot's latest-short-interest path must return current data."""
    result = finra_client.get_short_interest("AAPL")
    assert "error" not in result, result
    assert result["as_of_date"] is not None
    assert result["data_freshness"] == "current", result
    assert result["environment"] == "production"
    rendered = render_tool_result(result)
    assert not any("STALE" in w for w in result["warnings"])
    assert "STALE" not in rendered
