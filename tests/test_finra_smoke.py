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
from collections.abc import Iterator

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
def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FINRA_USE_MOCK", "1")
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()


def _as_seq(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    for item in value:
        assert isinstance(item, dict)
    return value


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_str_list(value: object) -> list[str]:
    assert isinstance(value, list)
    for item in value:
        assert isinstance(item, str)
    return value


def _acceptable(result: dict[str, object]) -> bool:
    """A smoke query passes when FINRA answered (briefing or honest no-data)."""
    if "metrics" in result:
        return True
    return str(result.get("error", "")).startswith("No data found")


def test_smoke_catalog_reachable():
    result = finra_client.list_datasets()
    assert "error" not in result, result
    count = result["count"]
    assert isinstance(count, int)
    assert count > 0


def test_smoke_catalog_ranked_weekly_summary_discovery():
    """'OTC weekly trading volume' must rank otcMarket/weeklySummary first."""
    result = finra_client.list_datasets(search="OTC weekly trading volume")
    assert "error" not in result, result
    datasets = _as_seq(result["datasets"])
    assert datasets, "expected ranked matches"
    assert datasets[0]["dataset"] == "otcMarket/weeklySummary", [
        d["dataset"] for d in datasets
    ]
    described = finra_client.describe_dataset("otcMarket/weeklySummary")
    assert "error" not in described, described
    assert described["ticker_field"] == "issueSymbolIdentifier"
    assert described["date_field"] == "summaryStartDate"


def test_smoke_short_interest_uses_canonical_fields():
    result = finra_client.query_dataset(
        "otcMarket/consolidatedShortInterest", ticker="AAPL", limit=5
    )
    if "metrics" in result:
        fields = _as_dict(result["metrics"])["fields"]
        assert isinstance(fields, list)
        assert "daysToCoverQuantity" in fields
        assert "averageDailyVolumeQuantity" in fields
        assert result["as_of_date"] is not None
        assert result["data_freshness"] in ("current", "stale")
        assert result["environment"] in ("production", "mock")
        # Truthfulness: stale data must be surfaced, never silently current.
        warnings = _as_str_list(result["warnings"])
        if result["data_freshness"] == "stale":
            assert any("STALE" in w for w in warnings)
            assert "STALE" in render_tool_result(result)
        else:
            assert not any("STALE" in w for w in warnings)


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
    records = _as_seq(result["records"])
    dates: list[str] = []
    for r in records:
        d = r["settlementDate"]
        assert isinstance(d, str)
        dates.append(d)
    assert dates == sorted(dates, reverse=True)
    row_fields = set(records[0])
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