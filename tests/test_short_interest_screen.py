import sqlite3

import pytest
import requests

from app import agent
from app import finra_client, short_interest_screen as screen
from app.policy import ChatPolicy, LOCAL_CONTEXT
from app.tool_render import render_tool_result
from app.tools import TOOLS, execute_tool


TEST_POLICY = ChatPolicy(
    allowed_models=frozenset({"test"}),
    max_messages=20,
    max_message_chars=12_000,
    upstream_timeout_seconds=1,
)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(screen, "DB_PATH", str(tmp_path / "screen.db"))


def _rows():
    return [
        {"symbolCode": "AAA", "issueName": "Alpha", "currentShortPositionQuantity": 20},
        {"symbolCode": "BBB", "issueName": "Beta", "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "currentShortPositionQuantity": 5},
    ]


def _facts(cik):
    values = {
        1: [{"val": 100, "end": "2026-08-01", "filed": "2026-08-02", "accn": "a"}],
        2: [{"val": 200, "end": "2026-08-01", "filed": "2026-08-02", "accn": "b"}],
        3: [{"val": 10, "end": "2026-08-01", "filed": "2026-08-02", "accn": "c"}],
    }
    return values[cik], {"facts": {}}


def _ticker_map(**mapping):
    return screen.TickerMap(by_ticker=mapping, ambiguous={})


def test_refresh_ranks_complete_snapshot_and_persists(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", _facts)

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA", "BBB"]
    assert result["entries"][0]["short_interest_percent"] == 50
    assert result["coverage"]["finra_rows"] == 3
    assert result["coverage"]["eligible_rows"] == 3
    # A second read comes from the published screen rather than needing a refresh.
    result = screen.get_short_interest_leaderboard(2, "2026-08-14")
    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA"]


def test_select_fact_is_point_in_time():
    facts = [
        {"val": 100, "end": "2026-07-01", "filed": "2026-07-02"},
        {"val": 120, "end": "2026-08-15", "filed": "2026-08-16"},
        {"val": 110, "end": "2026-08-10", "filed": "2026-08-20"},
    ]
    selected = screen._select_fact(facts, "2026-08-14")
    assert selected["val"] == 100


def test_sec_failure_does_not_publish_partial_screen(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))

    def fail_on_beta(cik):
        if cik == 2:
            raise RuntimeError("SEC unavailable")
        return _facts(cik)

    monkeypatch.setattr(screen, "_fetch_sec_facts", fail_on_beta)
    with pytest.raises(RuntimeError, match="SEC unavailable"):
        screen.refresh_short_interest_leaderboard("2026-08-14")
    conn = sqlite3.connect(screen.DB_PATH)
    try:
        assert conn.execute("SELECT count(*) FROM leaderboard_run").fetchone()[0] == 0
    finally:
        conn.close()


def test_sec_404_is_excluded_and_leaderboard_publishes(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", lambda cik: None if cik == 2 else _facts(cik))

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert result["coverage"]["finra_rows"] == 3
    assert result["coverage"]["eligible_rows"] == 2
    assert result["coverage"]["exclusions"]["no_sec_companyfacts"] == 1
    assert result["coverage"]["exclusions"]["not_classified_common_equity"] == 0
    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA"]
    conn = sqlite3.connect(screen.DB_PATH)
    try:
        assert conn.execute("SELECT count(*) FROM leaderboard_run").fetchone()[0] == 1
    finally:
        conn.close()


def test_sec_adapter_classifies_404_only(monkeypatch):
    def _sec_get_with(status):
        def _get(url):
            response = requests.Response()
            response.status_code = status
            response.url = url
            if status >= 400:
                raise requests.HTTPError(response=response)
            return response

        return _get

    monkeypatch.setattr(screen, "_sec_get", _sec_get_with(404))
    assert screen._fetch_sec_facts(1) is None

    for status in (401, 403, 429, 500, 503):
        monkeypatch.setattr(screen, "_sec_get", _sec_get_with(status))
        with pytest.raises(requests.HTTPError):
            screen._fetch_sec_facts(1)

    class _InvalidBody:
        status_code = 200

        def json(self):
            raise ValueError("not JSON")

    monkeypatch.setattr(screen, "_sec_get", lambda url: _InvalidBody())
    with pytest.raises(ValueError, match="not JSON"):
        screen._fetch_sec_facts(1)


@pytest.mark.parametrize("failure", [
    pytest.param(requests.HTTPError("HTTP 429"), id="429"),
    pytest.param(requests.HTTPError("HTTP 500"), id="500"),
    pytest.param(requests.ConnectionError("network down"), id="network"),
])
def test_sec_service_failures_still_block_publish(isolated_store, monkeypatch, failure):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))

    def fail_on_beta(cik):
        if cik == 2:
            raise failure
        return _facts(cik)

    monkeypatch.setattr(screen, "_fetch_sec_facts", fail_on_beta)
    with pytest.raises((requests.HTTPError, requests.ConnectionError)):
        screen.refresh_short_interest_leaderboard("2026-08-14")
    conn = sqlite3.connect(screen.DB_PATH)
    try:
        assert conn.execute("SELECT count(*) FROM leaderboard_run").fetchone()[0] == 0
    finally:
        conn.close()


def test_cached_sec_facts_survive_later_failure_and_are_reused_on_retry(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    calls = []

    def facts(cik):
        calls.append(cik)
        if cik == 2:
            raise RuntimeError("SEC down")
        return _facts(cik)

    monkeypatch.setattr(screen, "_fetch_sec_facts", facts)
    with pytest.raises(RuntimeError, match="SEC down"):
        screen.refresh_short_interest_leaderboard("2026-08-14")
    conn = sqlite3.connect(screen.DB_PATH)
    try:
        stored = [row[0] for row in conn.execute("SELECT cik FROM sec_shares_fact")]
        assert stored == [1]
    finally:
        conn.close()

    calls.clear()

    def record(cik):
        calls.append(cik)
        return _facts(cik)

    monkeypatch.setattr(screen, "_fetch_sec_facts", record)
    result = screen.refresh_short_interest_leaderboard("2026-08-14")
    assert calls == [2, 3]
    assert result["coverage"]["eligible_rows"] == 3


def test_coverage_render_shows_no_sec_companyfacts(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", lambda cik: None if cik == 2 else _facts(cik))

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert "no_sec_companyfacts" in render_tool_result(result)


def test_finra_snapshot_pages_with_exact_date_filter(monkeypatch):
    entry = finra_client.CatalogEntry("otcMarket", "consolidatedShortInterest", "", supports_record_offset=True)
    spec = finra_client.DatasetSpec("otcMarket", "consolidatedShortInterest", "", fields=tuple({"name": n} for n in ("symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity")), partition_fields=("settlementDate",), date_field="settlementDate")
    calls = []
    monkeypatch.setattr(finra_client, "_resolve_dataset", lambda _: entry)
    monkeypatch.setattr(finra_client, "_get_dataset_spec", lambda _: spec)

    def build(_spec, _entry, _ticker, start, end, limit, _filters, offset=None, fields=None):
        calls.append((start, end, offset, limit, fields))
        return {"offset": offset}

    monkeypatch.setattr(finra_client, "_build_payload", build)
    monkeypatch.setattr(finra_client, "_cached_query", lambda _spec, payload: ([{"symbolCode": str(i)} for i in range(1000)] if payload["offset"] == 0 else [{"symbolCode": "1000"}], {"record-total": "1001"}))
    rows = screen._fetch_finra_snapshot("2026-08-14")
    assert len(rows) == 1001
    assert [(start, end) for start, end, *_ in calls] == [("2026-08-14", "2026-08-14")] * 2
    assert [call[2] for call in calls] == [0, 1000]


def test_missing_quantity_row_is_excluded_not_fatal(isolated_store, monkeypatch):
    rows = _rows() + [{"symbolCode": "DDD", "issueName": "Delta", "currentShortPositionQuantity": None}]
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: rows)
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", _facts)

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert result["coverage"]["finra_rows"] == 4
    assert result["coverage"]["eligible_rows"] == 3
    assert result["coverage"]["exclusions"]["invalid_short_interest"] == 1
    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA", "BBB"]
    conn = sqlite3.connect(screen.DB_PATH)
    try:
        stored = conn.execute("SELECT count(*) FROM finra_short_interest WHERE settlement_date = '2026-08-14'").fetchone()[0]
        assert stored == 3
    finally:
        conn.close()


def test_sec_facts_are_reused_within_ttl(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    calls = []
    monkeypatch.setattr(screen, "_fetch_sec_facts", lambda cik: calls.append(cik) or _facts(cik))

    screen.refresh_short_interest_leaderboard("2026-08-14")
    assert sorted(calls) == [1, 2, 3]
    calls.clear()
    result = screen.refresh_short_interest_leaderboard("2026-09-14")
    assert calls == []
    assert result["coverage"]["eligible_rows"] == 3


def test_leaderboard_surfaces_staleness(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", _facts)

    recent = screen.refresh_short_interest_leaderboard("2026-08-14")
    assert recent["data_freshness"] == "current"
    assert recent["as_of_date"] == "2026-08-14"
    stale = screen.refresh_short_interest_leaderboard("2025-01-15")
    assert stale["data_freshness"] == "stale"
    rendered = render_tool_result(stale)
    assert "STALE/HISTORICAL DATA" in rendered


def test_snapshot_requires_known_finra_fields(monkeypatch):
    entry = finra_client.CatalogEntry("otcMarket", "consolidatedShortInterest", "", supports_record_offset=True)
    spec = finra_client.DatasetSpec("otcMarket", "consolidatedShortInterest", "", fields=tuple({"name": n} for n in ("symbolCode", "settlementDate", "currentShortPositionQuantity")), partition_fields=("settlementDate",), date_field="settlementDate")
    monkeypatch.setattr(finra_client, "_resolve_dataset", lambda _: entry)
    monkeypatch.setattr(finra_client, "_get_dataset_spec", lambda _: spec)
    with pytest.raises(ValueError, match="missing required fields"):
        screen._fetch_finra_snapshot("2026-08-14")


def test_malformed_finra_rows_abort_snapshot(monkeypatch):
    entry = finra_client.CatalogEntry("otcMarket", "consolidatedShortInterest", "", supports_record_offset=True)
    spec = finra_client.DatasetSpec("otcMarket", "consolidatedShortInterest", "", fields=tuple({"name": n} for n in ("symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity")), partition_fields=("settlementDate",), date_field="settlementDate")
    monkeypatch.setattr(finra_client, "_resolve_dataset", lambda _: entry)
    monkeypatch.setattr(finra_client, "_get_dataset_spec", lambda _: spec)
    monkeypatch.setattr(finra_client, "_build_payload", lambda *args, **kwargs: {"offset": kwargs.get("offset")})
    monkeypatch.setattr(finra_client, "_cached_query", lambda _spec, payload: ([{"symbolCode": str(i)} for i in range(1000)] if payload["offset"] == 0 else ["not-a-row"], {"record-total": "1001"}))
    with pytest.raises(ValueError, match="malformed rows"):
        screen._fetch_finra_snapshot("2026-08-14")


def test_latest_settlement_date_uses_date_partition_field(monkeypatch):
    entry = finra_client.CatalogEntry("otcMarket", "consolidatedShortInterest", "", supports_record_offset=True)
    spec = finra_client.DatasetSpec("otcMarket", "consolidatedShortInterest", "", fields=(), partition_fields=("settlementDate",), date_field="settlementDate")
    monkeypatch.setattr(finra_client, "_resolve_dataset", lambda _: entry)
    monkeypatch.setattr(finra_client, "_get_dataset_spec", lambda _: spec)
    monkeypatch.setattr(finra_client, "_get_partitions", lambda _: [("2026-07-10",), ("2026-08-14",)])
    assert screen._latest_settlement_date() == "2026-08-14"


def test_sec_tickers_are_cached(monkeypatch):
    payload = {"0": {"ticker": "AAA", "cik_str": 1}, "1": {"ticker": "BBB", "cik_str": 2}}
    cached: dict = {}

    class _Response:
        def json(self):
            return payload

    calls = []
    monkeypatch.setattr(screen, "_sec_get", lambda url: calls.append(url) or _Response())
    monkeypatch.setattr(screen.cache, "get", lambda key, ttl=None: cached.get(key))
    monkeypatch.setattr(screen.cache, "set", lambda key, value: cached.__setitem__(key, value))

    first = screen._fetch_sec_tickers()
    assert first.by_ticker == {"AAA": 1, "BBB": 2}
    assert first.ambiguous == {}
    second = screen._fetch_sec_tickers()
    assert second.by_ticker == {"AAA": 1, "BBB": 2}
    assert len(calls) == 1


def test_ambiguous_ticker_mapping_is_excluded_and_counted(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    map_with_ambiguity = screen.TickerMap(by_ticker={"AAA": 1, "BBB": 2}, ambiguous={"CCC": {3, 30}})
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: map_with_ambiguity)
    monkeypatch.setattr(screen, "_fetch_sec_facts", _facts)

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert result["coverage"]["eligible_rows"] == 2
    assert result["coverage"]["exclusions"]["ambiguous_ticker_mapping"] == 1
    assert [entry["ticker"] for entry in result["entries"]] == ["AAA", "BBB"]


def test_unclassifiable_entity_is_excluded_as_non_equity(isolated_store, monkeypatch):
    rows = _rows() + [{"symbolCode": "DDD", "issueName": "Fund D", "currentShortPositionQuantity": 5}]
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: rows)
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3, DDD=4))
    monkeypatch.setattr(screen, "_fetch_sec_facts", lambda cik: ([], {"facts": {}}) if cik == 4 else _facts(cik))

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert result["coverage"]["eligible_rows"] == 3
    assert result["coverage"]["exclusions"]["not_classified_common_equity"] == 1
    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA", "BBB"]


def test_leaderboard_exposes_version_and_source_records(isolated_store, monkeypatch):
    monkeypatch.setattr(screen, "_fetch_finra_snapshot", lambda d: _rows())
    monkeypatch.setattr(screen, "_fetch_sec_tickers", lambda: _ticker_map(AAA=1, BBB=2, CCC=3))
    monkeypatch.setattr(screen, "_fetch_sec_facts", _facts)

    result = screen.refresh_short_interest_leaderboard("2026-08-14")

    assert result["calculation_version"] == screen.PARSER_VERSION
    assert result["as_of_date"] == "2026-08-14"
    assert any("consolidatedShortInterest" in s for s in result["source_records"])
    assert any(s.startswith("https://www.sec.gov/files/company_tickers.json") for s in result["source_records"])
    assert any("companyfacts" in s for s in result["source_records"])


def test_sec_get_retries_on_rate_limit_then_succeeds(monkeypatch):
    class _Response:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    calls = []
    monkeypatch.setattr(screen, "_throttle_sec_request", lambda: None)
    monkeypatch.setattr(screen.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        screen.requests,
        "get",
        lambda *a, **k: calls.append(a[0]) or (_Response(429) if len(calls) == 1 else _Response(200)),
    )

    response = screen._sec_get("https://example.test/x")

    assert response.status_code == 200
    assert len(calls) == 2


def test_sec_get_raises_after_rate_limit_retry_exhausted(monkeypatch):
    class _Response:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            raise RuntimeError(f"HTTP {self.status_code}")

    calls = []
    monkeypatch.setattr(screen, "_throttle_sec_request", lambda: None)
    monkeypatch.setattr(screen, "SEC_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(screen.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        screen.requests,
        "get",
        lambda *a, **k: calls.append(a[0]) or _Response(429),
    )

    with pytest.raises(RuntimeError, match="HTTP 429"):
        screen._sec_get("https://example.test/x")
    assert len(calls) == 2


def test_tool_is_registered_dispatched_and_rendered(monkeypatch):
    from app import analytics

    schema = next(item for item in TOOLS if item["function"]["name"] == "get_short_interest_leaderboard")
    assert schema["function"]["parameters"]["properties"]["limit"]
    monkeypatch.setattr("app.analytics.screens.get_short_interest_leaderboard", lambda limit=None, settlement_date=None, as_of=None, data_root=None: {
        "source": "FINRA + SEC", "metric": "shares outstanding", "settlement_date": "2026-08-14",
        "entries": [{"rank": 1, "ticker": "AAA", "short_shares": 5, "shares_outstanding": 10, "short_interest_percent": 50, "sec_shares_as_of": "2026-08-01", "sec_filed_at": "2026-08-02"}],
        "coverage": {"finra_rows": 1, "eligible_rows": 1, "exclusions": {}}, "environment": "mock",
    })
    result = execute_tool("get_short_interest_leaderboard", {"limit": 1}, "test")
    assert result["entries"][0]["ticker"] == "AAA"
    assert "50.00%" in render_tool_result(result)


def test_agent_highest_short_interest_routes_to_leaderboard(monkeypatch):
    responses = iter([
        {"choices": [{"message": {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "get_short_interest_leaderboard", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"role": "assistant", "content": "The screen is complete."}}]},
    ])
    monkeypatch.setattr(agent, "_call_openrouter", lambda *_args: next(responses))
    monkeypatch.setattr(agent, "execute_tool", lambda name, args, model, **kwargs: {"settlement_date": "2026-08-14", "entries": [], "coverage": {}, "source": "test"})
    text, trace = agent.run_chat(
        [{"role": "user", "content": "What stock has the highest short interest as a percent of total shares?"}],
        "test", context=LOCAL_CONTEXT, policy=TEST_POLICY, return_trace=True,
    )
    assert text == "The screen is complete."
    assert trace == ["get_short_interest_leaderboard"]
