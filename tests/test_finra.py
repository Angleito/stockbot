"""Unit tests for the FINRA Query API tools.

Deterministic and offline: all FINRA HTTP is mocked against sanitized,
source-controlled fixtures in tests/fixtures/finra/. Live verification is
opt-in via tests/test_finra_smoke.py (pytest -m finra_smoke).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from app import finra_client
from app import tools as tools_module
from app.config import FINRA_API_BASE, FINRA_TOKEN_URL
from app.tools import execute_tool

FIXTURES = Path(__file__).parent / "fixtures" / "finra"


def _load_catalog() -> dict:
    return json.loads((FIXTURES / "catalog.json").read_text())


def _load_metadata(group: str, name: str):
    path = FIXTURES / "metadata" / f"{group}__{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _response(payload=None, status: int = 200, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = json.dumps(payload) if payload is not None else ""
    resp.headers = headers or {}
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _token_response() -> MagicMock:
    return _response({"access_token": "tok-123", "expires_in": 3600})


class FakeCache:
    """In-memory stand-in for app.cache that records every access."""

    def __init__(self):
        self.store = {}
        self.get_calls = []
        self.set_calls = []

    def get(self, key, ttl=None):
        self.get_calls.append(key)
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value
        self.set_calls.append(key)


def _mock_get(url, **_kwargs) -> MagicMock:
    if url.rstrip("/").endswith("/datasets"):
        return _response(_load_catalog())
    if "/metadata/group/" in url:
        group, name = url.split("/metadata/group/")[1].split("/name/")
        meta = _load_metadata(group, name)
        if meta is None:
            return _response({"error": "not found"}, status=404)
        return _response(meta)
    raise AssertionError(f"Unexpected GET {url}")


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    monkeypatch.setenv("FINRA_USE_MOCK", "")
    # Never let the analysis layer reach a live secondary model in offline tests.
    monkeypatch.setenv("FINRA_ANALYSIS_MODEL", "")
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()


@pytest.fixture(autouse=True)
def fake_cache(monkeypatch):
    fc = FakeCache()
    monkeypatch.setattr(finra_client, "cache", fc)
    return fc


@pytest.fixture
def http(monkeypatch):
    get_mock = MagicMock(side_effect=_mock_get)
    post_mock = MagicMock()
    monkeypatch.setattr("app.finra_client.requests.get", get_mock)
    monkeypatch.setattr("app.finra_client.requests.post", post_mock)
    monkeypatch.setattr("app.finra_client.get_finra_client_id", lambda: "client")
    monkeypatch.setattr(
        "app.finra_client.get_finra_client_secret", lambda: "secret"
    )
    return {"get": get_mock, "post": post_mock}


def _entry(list_result, dataset_id):
    return next(
        (d for d in list_result["datasets"] if d["dataset"] == dataset_id), None
    )


def _data_body(post_mock):
    return post_mock.call_args_list[-1].kwargs["json"]


# ---------------------------------------------------------------------------
# Direct helper tools
# ---------------------------------------------------------------------------


def test_short_interest_payload(http):
    records = [
        {
            "symbolCode": "AAPL",
            "issueName": "Apple Inc.",
            "settlementDate": "2026-08-14",
            "currentShortPositionQuantity": 100,
            "daysToCoverQuantity": 1.2,
        }
    ]
    http["post"].side_effect = [_token_response(), _response(records)]

    result = execute_tool(
        "get_short_interest",
        {"ticker": "aapl", "settlementDate": "2026-08-14"},
        model="test",
    )

    assert "error" not in result, result
    assert result["dataset"] == "consolidatedShortInterest"
    assert result["group"] == "otcMarket"
    assert "records" not in result
    assert "FINRA" in result["source"]
    assert result["returned_count"] == 1
    assert result["offset"] == 0
    assert result["next_offset"] == 1
    assert result["may_have_more"] is False
    # Briefing shape: coverage + deterministic metrics, no raw rows.
    assert result["coverage"]["rows_matched"] == 1
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["first_date"] == "2026-08-14"
    assert result["metrics"]["fields"]["currentShortPositionQuantity"] == {
        "min": 100, "max": 100, "mean": 100, "median": 100, "sum": 100
    }
    assert result["metrics"]["latest_vs_prior"] == []
    assert result["briefing"] is None
    assert result["briefing_source"] == "deterministic_only"
    assert result["total_records"] is None
    assert result["pagination_source"] == "estimate"
    # The main model's tool message must never contain raw records.
    serialized = json.dumps(result)
    assert '"records":' not in serialized
    assert '"symbolCode": "AAPL"' not in serialized
    assert '"issueName": "Apple Inc."' not in serialized

    # posts: one token (catalog auth) + one data query.
    assert http["post"].call_count == 2
    token_call, data_call = http["post"].call_args_list
    token_url = token_call.args[0] if token_call.args else token_call.kwargs["url"]
    data_url = data_call.args[0] if data_call.args else data_call.kwargs["url"]
    assert "oauth2/access_token" in token_url
    assert data_url.endswith("/data/group/otcMarket/name/consolidatedShortInterest")
    assert data_call.kwargs["json"]["compareFilters"] == [
        {"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": "AAPL"},
        {
            "compareType": "EQUAL",
            "fieldName": "settlementDate",
            "fieldValue": "2026-08-14",
        },
    ]


# ---------------------------------------------------------------------------
# Catalog → describe → query flow
# ---------------------------------------------------------------------------


def test_full_catalog_describe_query_flow(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL", "tradeDate": "2026-08-14"}]),
    ]

    listed = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" not in listed, listed
    entry = _entry(listed, "otcMarket/thresholdList")
    assert entry is not None
    assert entry["access"] == "unknown"

    described = execute_tool(
        "describe_finra_dataset", {"dataset_id": "otcMarket/thresholdList"}, model="test"
    )
    assert "error" not in described, described
    assert described["ticker_field"] == "issueSymbolIdentifier"
    assert described["date_field"] == "tradeDate"
    assert {f["name"] for f in described["fields"]} >= {
        "issueSymbolIdentifier",
        "tradeDate",
        "issueName",
    }

    queried = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/thresholdList",
            "ticker": "AAPL",
            "start_date": "2026-08-14",
            "end_date": "2026-08-14",
        },
        model="test",
    )
    assert "error" not in queried, queried
    body = _data_body(http["post"])
    assert {
        "compareType": "EQUAL",
        "fieldName": "issueSymbolIdentifier",
        "fieldValue": "AAPL",
    } in body["compareFilters"]
    assert {
        "compareType": "EQUAL",
        "fieldName": "tradeDate",
        "fieldValue": "2026-08-14",
    } in body["compareFilters"]
    assert queried["returned_count"] == 1
    assert "records" not in queried
    assert queried["coverage"]["rows_matched"] == 1
    assert queried["coverage"]["first_date"] == "2026-08-14"
    assert queried["coverage"]["last_date"] == "2026-08-14"
    assert queried["briefing_source"] == "deterministic_only"


# ---------------------------------------------------------------------------
# Catalog listing: access reporting + capability honesty + exclusions
# ---------------------------------------------------------------------------


def test_list_finra_datasets(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" not in result, result
    names = {d["dataset"] for d in result["datasets"]}
    assert "otcMarket/consolidatedShortInterest" in names
    assert "otcMarket/regShoDaily" in names
    assert "fixedIncomeMarket/treasuryDailyAggregates" in names
    assert "finra/industrySnapshotFirmsByRegistrationType" in names

    sample = _entry(result, "otcMarket/weeklySummary")
    assert "records" not in sample
    assert "fields" not in sample
    assert sample["access"] == "unknown"
    assert sample["supports_record_offset"] is True


def test_list_excludes_entitled_and_retired(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool("list_finra_datasets", {}, model="test")
    names = {d["dataset"] for d in result["datasets"]}
    # Confirmed non-public (accessType=Firm) and retired entries are dropped.
    assert "registration/firmRegistrationsSample" not in names
    assert "otcMarket/retiredDatasetSample" not in names


def test_access_reporting_public_and_unknown(http):
    http["post"].side_effect = [_token_response(), _token_response()]
    listed = execute_tool("list_finra_datasets", {}, model="test")
    snapshot = _entry(listed, "finra/industrySnapshotFirmsByRegistrationType")
    assert snapshot["access"] == "public"  # isPublic: true in fixture
    assert _entry(listed, "otcMarket/weeklySummary")["access"] == "unknown"

    finra_client.reset_discovery_cache()
    described = execute_tool(
        "describe_finra_dataset",
        {"dataset_id": "finra/industrySnapshotFirmsByRegistrationType"},
        model="test",
    )
    assert described.get("access") == "public"


def test_capabilities_not_guessed(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool("list_finra_datasets", {}, model="test")

    # No override for this dataset: capabilities must be null, not guessed.
    snapshot = _entry(result, "finra/industrySnapshotFirmsByRegistrationType")
    assert snapshot["supports_ticker"] is None
    assert snapshot["supports_date"] is None

    # Verified override corrections still surface.
    weekly = _entry(result, "otcMarket/weeklySummary")
    assert weekly["supports_ticker"] is True
    treasury = _entry(result, "fixedIncomeMarket/treasuryDailyAggregates")
    assert treasury["supports_ticker"] is False  # market-wide aggregate
    assert treasury["supports_date"] is True


def test_list_finra_datasets_group_search(http):
    http["post"].side_effect = [_token_response(), _token_response()]
    by_group = execute_tool(
        "list_finra_datasets", {"group": "fixedIncomeMarket"}, model="test"
    )
    assert all(d["group"] == "fixedIncomeMarket" for d in by_group["datasets"])

    finra_client.reset_discovery_cache()
    by_search = execute_tool(
        "list_finra_datasets", {"search": "short interest"}, model="test"
    )
    assert any(
        "consolidatedShortInterest" in d["dataset"] for d in by_search["datasets"]
    )


# ---------------------------------------------------------------------------
# Describe
# ---------------------------------------------------------------------------


def test_describe_finra_dataset(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "describe_finra_dataset",
        {"dataset_id": "otcMarket/weeklySummary"},
        model="test",
    )
    assert "error" not in result, result
    assert result["dataset"] == "otcMarket/weeklySummary"
    assert result["ticker_field"] == "issueSymbolIdentifier"
    assert result["date_field"] == "summaryStartDate"
    assert result["supports_record_offset"] is True
    field_names = {f["name"] for f in result["fields"]}
    assert "summaryTypeCode" in field_names
    assert "summaryTypeCode" in result.get("valid_filter_values", {})
    assert any(
        d["field"] == "summaryTypeCode" and d["value"] == "OTC_W_SMBL"
        for d in result.get("default_filters", [])
    )


# ---------------------------------------------------------------------------
# Query behavior: dates, filters, limits
# ---------------------------------------------------------------------------


def test_query_finra_date_range(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"tradeDate": "2026-08-01", "productCategory": "Bills"}]),
    ]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "treasuryDailyAggregates",
            "start_date": "2026-08-01",
            "end_date": "2026-08-07",
            "limit": 5,
        },
        model="test",
    )
    assert "error" not in result, result
    body = _data_body(http["post"])
    assert body["limit"] == 5
    assert body["dateRangeFilters"] == [
        {
            "fieldName": "tradeDate",
            "startDate": "2026-08-01",
            "endDate": "2026-08-07",
        }
    ]


def test_max_limit_clamped(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"tradeDate": "2026-08-01"}]),
    ]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "fixedIncomeMarket/treasuryDailyAggregates",
            "start_date": "2026-08-01",
            "end_date": "2026-08-01",
            "limit": 99999,
        },
        model="test",
    )
    assert "error" not in result, result
    assert _data_body(http["post"])["limit"] == finra_client.MAX_LIMIT
    assert result["limit"] == finra_client.MAX_LIMIT


def test_valid_filters_included(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
    ]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/weeklySummary",
            "filters": [
                {
                    "field": "totalWeeklyShareQuantity",
                    "op": "GREATER",
                    "value": "1000",
                }
            ],
        },
        model="test",
    )
    assert "error" not in result, result
    assert {
        "compareType": "GREATER",
        "fieldName": "totalWeeklyShareQuantity",
        "fieldValue": "1000",
    } in _data_body(http["post"])["compareFilters"]


def test_invalid_op_rejected(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/weeklySummary",
            "filters": [
                {"field": "totalWeeklyShareQuantity", "op": "CONTAINS", "value": "1"}
            ],
        },
        model="test",
    )
    assert "error" in result
    assert "Unsupported compare op" in result["error"]


def test_invalid_filter_field(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/consolidatedShortInterest",
            "filters": [{"field": "notARealField", "value": "x"}],
        },
        model="test",
    )
    assert "error" in result
    assert "notARealField" in result["error"]


def test_invalid_documented_enum_rejected_before_http(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/weeklySummary",
            "filters": [{"field": "summaryTypeCode", "value": "BOGUS_TYPE"}],
        },
        model="test",
    )
    assert "error" in result
    assert "Allowed values" in result["error"]
    # Rejected before any data POST: only the catalog token request happened.
    assert http["post"].call_count == 1
    assert http["get"].call_count == 2  # catalog + metadata only


@pytest.mark.parametrize(
    "bad_filter",
    [
        {"value": "x"},  # missing field
        {"field": "summaryTypeCode"},  # missing value
        {"field": "summaryTypeCode", "value": ""},  # empty value
        "not-an-object",
    ],
)
def test_malformed_filters_rejected(http, bad_filter):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "filters": [bad_filter]},
        model="test",
    )
    assert "error" in result
    lowered = result["error"].lower()
    assert "malformed" in lowered or "required" in lowered or "must be an object" in lowered
    assert http["post"].call_count == 1  # no data POST


def test_weekly_summary_default_filter_no_conflict(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
    ]
    # Explicit summaryTypeCode must suppress the OTC_W_SMBL default.
    result = execute_tool(
        "query_finra",
        {
            "dataset": "otcMarket/weeklySummary",
            "ticker": "AAPL",
            "filters": [
                {"field": "summaryTypeCode", "op": "EQUAL", "value": "ATS_W_SMBL"}
            ],
        },
        model="test",
    )
    assert "error" not in result, result
    type_filters = [
        f
        for f in _data_body(http["post"])["compareFilters"]
        if f["fieldName"] == "summaryTypeCode"
    ]
    assert len(type_filters) == 1
    assert type_filters[0]["fieldValue"] == "ATS_W_SMBL"


def test_weekly_summary_applies_default_when_absent(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
    ]
    result = execute_tool(
        "query_finra",
        {"dataset": "weeklySummary", "ticker": "AAPL"},
        model="test",
    )
    assert "error" not in result, result
    assert {
        "compareType": "EQUAL",
        "fieldName": "summaryTypeCode",
        "fieldValue": "OTC_W_SMBL",
    } in _data_body(http["post"])["compareFilters"]


def test_ticker_on_non_symbol_dataset(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {"dataset": "treasuryDailyAggregates", "ticker": "AAPL"},
        model="test",
    )
    assert "error" in result
    assert "ticker/symbol" in result["error"]
    assert "aggregate" in result["error"].lower()


def test_treasury_monthly_date_field(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"beginningOfTheMonthDate": "2026-08-01"}]),
    ]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "fixedIncomeMarket/treasuryMonthlyAggregates",
            "start_date": "2026-08-01",
            "end_date": "2026-08-01",
            "limit": 3,
        },
        model="test",
    )
    assert "error" not in result, result
    assert _data_body(http["post"])["compareFilters"] == [
        {
            "compareType": "EQUAL",
            "fieldName": "beginningOfTheMonthDate",
            "fieldValue": "2026-08-01",
        }
    ]


def test_query_finra_unknown_dataset(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra", {"dataset": "notARealDataset"}, model="test"
    )
    assert "error" in result
    assert "Unknown FINRA dataset" in result["error"]
    assert "list_finra_datasets" in result["error"]


def test_legacy_bare_name(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"symbolCode": "AAPL"}]),
    ]
    result = execute_tool(
        "query_finra",
        {"dataset": "consolidatedShortInterest", "ticker": "AAPL"},
        model="test",
    )
    assert "error" not in result, result
    assert result["dataset"] == "consolidatedShortInterest"
    assert result["group"] == "otcMarket"


def test_ambiguous_bare_name(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool("query_finra", {"dataset": "sharedName"}, model="test")
    assert "error" in result
    assert "Ambiguous" in result["error"]
    assert "groupA/sharedName" in result["error"]
    assert "groupB/sharedName" in result["error"]


def test_industry_snapshot_queries_dataset(http):
    http["post"].side_effect = [
        _token_response(),
        _response([
            {"registrationTypeCode": "BD", "firmCount": 10},
            {"registrationTypeCode": "IA", "firmCount": 25},
        ]),
    ]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "finra/industrySnapshotFirmsByRegistrationType",
            "limit": 50,
        },
        model="test",
    )
    assert "error" not in result, result
    data_url = http["post"].call_args_list[1].args[0]
    assert data_url.endswith(
        "/data/group/finra/name/industrySnapshotFirmsByRegistrationType"
    )
    assert "records" not in result
    assert result["metrics"]["fields"]["firmCount"] == {
        "min": 10, "max": 25, "mean": 17.5, "median": 17.5, "sum": 35
    }
    assert result["metrics"]["categorical"]["registrationTypeCode"] == {
        "IA": 1,
        "BD": 1,
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_valid_full_page(http):
    records = [{"issueSymbolIdentifier": f"S{i}"} for i in range(50)]
    http["post"].side_effect = [_token_response(), _response(records)]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "limit": 50, "offset": 100},
        model="test",
    )
    assert "error" not in result, result
    body = _data_body(http["post"])
    assert body["offset"] == 100
    assert body["limit"] == 50
    assert result["returned_count"] == 50
    assert result["offset"] == 100
    assert result["next_offset"] == 150
    assert result["may_have_more"] is True
    assert result["total_records"] is None
    assert result["pagination_source"] == "estimate"


def test_pagination_partial_page(http):
    records = [{"issueSymbolIdentifier": f"S{i}"} for i in range(2)]
    http["post"].side_effect = [_token_response(), _response(records)]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "limit": 50, "offset": 100},
        model="test",
    )
    assert "error" not in result, result
    assert result["returned_count"] == 2
    assert result["next_offset"] == 102
    assert result["may_have_more"] is False


def test_pagination_header_driven(http):
    records = [{"issueSymbolIdentifier": f"S{i}"} for i in range(2)]
    http["post"].side_effect = [
        _token_response(),
        _response(records, headers={
            "Record-Total": "5",
            "Record-Offset": "0",
            "Record-Limit": "2",
            "Record-Max-Limit": "1000",
        }),
    ]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "limit": 2},
        model="test",
    )
    assert "error" not in result, result
    assert result["total_records"] == 5
    assert result["may_have_more"] is True
    assert result["pagination_source"] == "finra_header"


def test_pagination_header_driven_exhausted(http):
    records = [{"issueSymbolIdentifier": f"S{i}"} for i in range(5)]
    http["post"].side_effect = [
        _token_response(),
        _response(records, headers={"Record-Total": "5"}),
    ]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "limit": 5, "offset": 0},
        model="test",
    )
    assert "error" not in result, result
    assert result["total_records"] == 5
    assert result["may_have_more"] is False
    assert result["pagination_source"] == "finra_header"


@pytest.mark.parametrize("offset", [-1, -100])
def test_negative_offset_rejected(http, offset):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "offset": offset},
        model="test",
    )
    assert "error" in result
    assert "offset must be >= 0" in result["error"]
    assert http["post"].call_count == 1  # no data POST


def test_offset_exceeds_finra_max(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "offset": 500001},
        model="test",
    )
    assert "error" in result
    assert "500000" in result["error"]


def test_offset_rejected_when_unsupported(http):
    http["post"].side_effect = [_token_response()]
    result = execute_tool(
        "query_finra",
        {
            "dataset": "finra/industrySnapshotFirmsByRegistrationType",
            "offset": 10,
        },
        model="test",
    )
    assert "error" in result
    assert "does not support record offset" in result["error"]
    assert http["post"].call_count == 1  # no data POST


# ---------------------------------------------------------------------------
# Caching: discovery and results must not re-hit HTTP
# ---------------------------------------------------------------------------


def test_discovery_and_result_cache_hits(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
    ]

    first_list = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" not in first_list, first_list
    assert http["get"].call_count == 1

    # Simulate a new process: in-memory discovery cleared, SQLite cache kept.
    finra_client.reset_discovery_cache()
    second_list = execute_tool("list_finra_datasets", {}, model="test")
    assert second_list == first_list
    assert http["get"].call_count == 1  # served from cache, no new GET

    first_query = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "ticker": "AAPL"},
        model="test",
    )
    assert "error" not in first_query, first_query
    gets_after_first_query = http["get"].call_count  # catalog + metadata
    posts_after_first_query = http["post"].call_count  # token + data

    second_query = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "ticker": "AAPL"},
        model="test",
    )
    assert second_query == first_query
    assert http["get"].call_count == gets_after_first_query
    assert http["post"].call_count == posts_after_first_query


def test_token_fetched_once_across_calls(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
        _response([{"issueSymbolIdentifier": "AAPL"}]),
    ]
    for _ in range(2):
        result = execute_tool(
            "query_finra",
            {"dataset": "otcMarket/weeklySummary", "ticker": "AAPL"},
            model="test",
        )
        assert "error" not in result, result
    # One token POST even across two queries (result cache serves the second).
    token_calls = [
        c for c in http["post"].call_args_list
        if "oauth2/access_token" in (c.args[0] if c.args else c.kwargs.get("url", ""))
    ]
    assert len(token_calls) == 1


# ---------------------------------------------------------------------------
# URLs, headers, authentication
# ---------------------------------------------------------------------------


def test_catalog_metadata_data_urls_and_auth(http):
    http["post"].side_effect = [
        _token_response(),
        _response([{"symbolCode": "AAPL"}]),
    ]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/consolidatedShortInterest", "ticker": "AAPL"},
        model="test",
    )
    assert "error" not in result, result

    catalog_call, metadata_call = http["get"].call_args_list
    catalog_url = catalog_call.args[0]
    assert catalog_url == f"{FINRA_API_BASE}/datasets"
    assert catalog_call.kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert catalog_call.kwargs["headers"]["Accept"] == "application/json"

    metadata_url = metadata_call.args[0]
    assert metadata_url.endswith(
        "/metadata/group/otcMarket/name/consolidatedShortInterest"
    )
    # Metadata is public: no Authorization header.
    assert "Authorization" not in metadata_call.kwargs["headers"]

    token_call, data_call = http["post"].call_args_list
    assert token_call.args[0] == FINRA_TOKEN_URL
    assert token_call.kwargs["auth"] == ("client", "secret")

    data_url = data_call.args[0]
    assert data_url.endswith("/data/group/otcMarket/name/consolidatedShortInterest")
    assert data_call.kwargs["headers"]["Authorization"] == "Bearer tok-123"
    assert data_call.kwargs["headers"]["Content-Type"] == "application/json"
    assert "compareFilters" in data_call.kwargs["json"]


# ---------------------------------------------------------------------------
# HTTP failure modes
# ---------------------------------------------------------------------------


def test_catalog_connection_error(http):
    http["get"].side_effect = requests.ConnectionError("api.finra.org unreachable")
    result = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" in result
    assert "FINRA catalog unavailable" in result["error"]


def test_catalog_http_500(http):
    http["get"].side_effect = lambda url, **kw: _response({}, status=500)
    result = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" in result
    assert "FINRA catalog unavailable" in result["error"]


def test_catalog_malformed_response(http):
    http["get"].side_effect = lambda url, **kw: _response({"unexpected": 1})
    result = execute_tool("list_finra_datasets", {}, model="test")
    assert "error" in result
    assert "FINRA catalog unavailable" in result["error"]


@pytest.mark.parametrize("status", [401, 403])
def test_metadata_auth_error_describe(http, status):
    def side_effect(url, **kw):
        if url.rstrip("/").endswith("/datasets"):
            return _response(_load_catalog())
        return _response({}, status=status)

    http["get"].side_effect = side_effect
    result = execute_tool(
        "describe_finra_dataset",
        {"dataset_id": "otcMarket/weeklySummary"},
        model="test",
    )
    assert "error" in result
    assert str(status) in result["error"]
    assert "entitlement" in result["error"].lower()


@pytest.mark.parametrize("status", [401, 403])
def test_metadata_auth_error_query(http, status):
    def side_effect(url, **kw):
        if url.rstrip("/").endswith("/datasets"):
            return _response(_load_catalog())
        return _response({}, status=status)

    http["get"].side_effect = side_effect
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/weeklySummary", "ticker": "AAPL"},
        model="test",
    )
    assert "error" in result
    assert str(status) in result["error"]
    assert "entitlement" in result["error"].lower() or "public" in result["error"].lower()


def test_metadata_malformed_response(http):
    def side_effect(url, **kw):
        if url.rstrip("/").endswith("/datasets"):
            return _response(_load_catalog())
        return _response([1, 2, 3])  # not an object

    http["get"].side_effect = side_effect
    result = execute_tool(
        "describe_finra_dataset",
        {"dataset_id": "otcMarket/weeklySummary"},
        model="test",
    )
    assert "error" in result
    assert "Unexpected metadata response" in result["error"]


def test_entitlement_403_on_data(http):
    forbidden = _response({}, status=403)
    http["post"].side_effect = [_token_response(), forbidden]
    result = execute_tool(
        "query_finra",
        {"dataset": "otcMarket/consolidatedShortInterest", "ticker": "AAPL"},
        model="test",
    )
    assert "error" in result
    assert "403" in result["error"]
    assert "entitlement" in result["error"].lower() or "public" in result["error"].lower()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_missing_credentials(monkeypatch):
    def _raise_env(name):
        raise ValueError(
            f"{name} is not properly set in your environment or .env file. "
            "Phase 1 requires it to run. See .env.example."
        )

    monkeypatch.setattr("app.config._require_env", _raise_env)
    result = execute_tool("get_short_interest", {"ticker": "AAPL"}, model="test")
    assert "error" in result
    assert "FINRA_CLIENT_ID" in result["error"]


# ---------------------------------------------------------------------------
# Schema / dispatch parity
# ---------------------------------------------------------------------------


def test_finra_schema_dispatch_parity():
    finra_names = {
        "get_short_interest",
        "get_reg_sho_volume",
        "get_threshold_securities",
        "list_finra_datasets",
        "describe_finra_dataset",
        "get_finra_datapoints",
        "query_finra",
    }
    schema_names = {t["function"]["name"] for t in tools_module.TOOLS}
    assert finra_names <= schema_names, "FINRA schema missing from TOOLS"
    assert set(tools_module._FINRA_HANDLERS) == finra_names, (
        "FINRA dispatch registry out of sync with schemas"
    )
    assert all(callable(h) for h in tools_module._FINRA_HANDLERS.values())
