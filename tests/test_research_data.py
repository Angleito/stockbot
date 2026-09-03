"""Offline tests for the research data refresh path (P2 throttle/retry, P1
optional universe enrichment, market-wide coverage).  Fetch is mocked at the
HTTP layer; archive, normalize, Parquet, and the leaderboard screen all run
for real against a tmp data root.
"""

import json

import pytest

import cli
from app.analytics import screens
from app.services import research_data
from app.services.research_data import prepare_short_interest_data
from app.storage import parquet, raw_archive

TICKERS_PAYLOAD = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc"},
    "1": {"cik_str": 2488, "ticker": "AMD", "title": "Advanced Micro Devices"},
}


def _facts_payload(cik, val):
    return {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-08-01", "val": val, "accn": f"a{cik}", "filed": "2026-08-02"},
        ]}},
    }}}


def _finra_row(symbol, pos):
    return {
        "symbolCode": symbol, "issueName": symbol,
        "settlementDate": "2026-08-14", "currentShortPositionQuantity": pos,
    }


def _page(rows, total, offset):
    return (
        json.dumps(rows).encode(), rows,
        {"record-total": str(total), "record-offset": str(offset), "record-limit": "1000"},
    )


class _Resp:
    def __init__(self, content, status_code, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_mocks(monkeypatch, get_script, page_script, sleeps):
    """Scripted HTTP responses; records SEC URLs and every sleep duration."""
    get_calls = []

    def fake_get(url, **kwargs):
        get_calls.append(url)
        return get_script.pop(0)  # IndexError when the script is exhausted

    monkeypatch.setattr(research_data.requests, "get", fake_get)
    monkeypatch.setattr(
        research_data.finra_client, "ingestion_post_query",
        lambda *args, **kwargs: page_script.pop(0),
    )
    monkeypatch.setattr(research_data.time, "sleep", lambda s: sleeps.append(s))
    return get_calls


def test_refresh_data_offline_end_to_end(tmp_path, monkeypatch):
    """Fetch (with 429 retry) -> archive -> normalize -> Parquet -> market-wide screen."""
    sleeps: list[float] = []
    get_script = [
        _Resp(b"429", 429),
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
        _Resp(json.dumps(_facts_payload(2488, 200)).encode(), 200),
    ]
    page_script = [
        _page([_finra_row("AAPL", 20), _finra_row("AMD", 20)], 3, 0),
        _page([_finra_row("XOM", 5)], 3, 2),  # full snapshot: a non-universe symbol
    ]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", tickers=["AAPL", "AMD"], data_root=tmp_path)

    assert summary["unresolved_tickers"] == []
    assert len(summary["sec_facts"]) == 2
    assert "ticker_ciks" not in summary["sec_tickers"]  # full map stays internal
    assert summary["sec_tickers"]["ticker_count"] == 2
    assert get_calls == [
        research_data.SEC_TICKERS_URL,
        research_data.SEC_TICKERS_URL,  # retry after 429
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json",
    ]
    assert 0.13 in sleeps  # throttle
    assert 0.5 in sleeps  # first backoff: 0.5 * 2**0

    assert raw_archive.find("sec", "company_tickers", "company_tickers", root=tmp_path / "raw") is not None
    assert raw_archive.find("sec", "cik0000320193", "companyfacts", root=tmp_path / "raw") is not None
    assert raw_archive.find(
        "finra", "data_page", "otcMarket/consolidatedShortInterest:2026-08-14:offset0",
        root=tmp_path / "raw",
    ) is not None
    assert raw_archive.find(
        "finra", "data_page", "otcMarket/consolidatedShortInterest:2026-08-14:offset2",
        root=tmp_path / "raw",
    ) is not None

    assert parquet.read_table("entities", root=tmp_path / "parquet").num_rows == 2
    assert parquet.read_table("short_interest", root=tmp_path / "parquet").num_rows == 3
    assert parquet.read_table("financial_facts", root=tmp_path / "parquet").num_rows == 2

    # Market-wide screen (P1 promise): the leaderboard is not universe-bound.
    monkeypatch.setattr(screens, "DEFAULT_DATA_ROOT", tmp_path)
    result = screens.materialize_short_interest_screen("2026-08-14")
    assert [e["ticker"] for e in result["entries"]] == ["AAPL", "AMD"]  # 20/100 > 20/200
    assert result["coverage"]["finra_rows"] == 3
    assert result["coverage"]["eligible_rows"] == 2
    assert result["coverage"]["exclusions"]["unmapped_symbol"] == 1  # XOM


def test_cli_refresh_data_coverage_report(tmp_path, monkeypatch, capsys):
    sleeps: list[float] = []
    get_script = [
        _Resp(b"429", 429),
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
        _Resp(json.dumps(_facts_payload(2488, 200)).encode(), 200),
    ]
    page_script = [
        _page([_finra_row("AAPL", 20), _finra_row("AMD", 20)], 3, 0),
        _page([_finra_row("XOM", 5)], 3, 2),
    ]
    _install_mocks(monkeypatch, get_script, page_script, sleeps)
    monkeypatch.setattr(research_data, "DEFAULT_DATA_ROOT", tmp_path)
    monkeypatch.setattr(screens, "DEFAULT_DATA_ROOT", tmp_path)

    cli._cmd_refresh_data("2026-08-14", ["AAPL", "AMD"], [])
    out = capsys.readouterr().out

    assert "FINRA securities:             3" in out
    assert "Ticker mappings:              2" in out
    assert "Shares-outstanding coverage:  2" in out
    assert "Eligible screen universe:     2" in out
    assert "Coverage: 66.7%" in out
    assert "Leaderboard entries: ['AAPL', 'AMD']" in out


def test_unresolved_ticker_is_reported_not_fetched(tmp_path, monkeypatch):
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
    ]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", tickers=["AAPL", "ZZZZ"], data_root=tmp_path)

    assert summary["unresolved_tickers"] == ["ZZZZ"]
    assert len(summary["sec_facts"]) == 1
    assert len(get_calls) == 2  # tickers + AAPL facts; no facts request for ZZZZ


def test_prepare_without_universe_skips_sec_facts(tmp_path, monkeypatch):
    sleeps: list[float] = []
    get_script = [_Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200)]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", data_root=tmp_path)

    assert summary["sec_facts"] == []
    assert len(get_calls) == 1  # SEC ticker universe only
    assert summary["unresolved_tickers"] == []
    assert "ticker_ciks" not in summary["sec_tickers"]
    assert summary["sec_tickers"]["ticker_count"] == 2


def test_finra_missing_record_total_raises(tmp_path, monkeypatch):
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
    ]
    page_script = [(b"[]", [], {})]  # no record-total header
    _install_mocks(monkeypatch, get_script, page_script, sleeps)

    with pytest.raises(ValueError, match="Record-Total"):
        prepare_short_interest_data("2026-08-14", tickers=["AAPL"], data_root=tmp_path)

def test_enrichment_failure_does_not_block_finra_or_siblings(tmp_path, monkeypatch):
    """P1: a failed facts request must never prevent the FINRA snapshot."""
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
        _Resp(b"", 500), _Resp(b"", 500), _Resp(b"", 500),  # AMD: exhausted retries
    ]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", tickers=["AAPL", "AMD"], data_root=tmp_path)

    assert summary["finra"]["rows"] == 1  # market-wide snapshot still landed
    assert len(summary["sec_facts"]) == 1  # AAPL enrichment succeeded
    assert summary["failed_enrichments"] == [
        {"ticker": "AMD", "cik": 2488, "error": "RuntimeError: HTTP 500"},
    ]
    assert get_calls == [
        research_data.SEC_TICKERS_URL,
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000002488.json",
    ]


def test_cik_only_enrichment_failure_reports_null_ticker(tmp_path, monkeypatch):
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(b"", 500), _Resp(b"", 500), _Resp(b"", 500),
    ]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", ciks=[999999], data_root=tmp_path)

    assert summary["sec_facts"] == []
    assert summary["failed_enrichments"] == [
        {"ticker": None, "cik": 999999, "error": "RuntimeError: HTTP 500"},
    ]


def test_coverage_counters_truthful_with_invalid_short_interest(tmp_path, monkeypatch, capsys):
    """P2 regression: invalid rows never reach mapping/shares checks, so the
    CLI must print the screen's stage counters, not derived complements."""
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
        _Resp(json.dumps(_facts_payload(2488, 200)).encode(), 200),
    ]
    page_script = [
        _page([_finra_row("AAPL", 20), _finra_row("AMD", 20), _finra_row("BAD", -1)], 4, 0),
        _page([_finra_row("XOM", 5)], 4, 3),
    ]
    _install_mocks(monkeypatch, get_script, page_script, sleeps)
    monkeypatch.setattr(research_data, "DEFAULT_DATA_ROOT", tmp_path)
    monkeypatch.setattr(screens, "DEFAULT_DATA_ROOT", tmp_path)

    # One CLI run drives prepare + materialize; a replay of the screen then
    # re-reads the persisted run (unique-key dedup makes it a no-op write).
    cli._cmd_refresh_data("2026-08-14", ["AAPL", "AMD"], [])
    out = capsys.readouterr().out

    result = screens.materialize_short_interest_screen("2026-08-14")
    coverage = result["coverage"]
    assert coverage["finra_rows"] == 4
    assert coverage["valid_short_interest_rows"] == 3  # BAD excluded here
    assert coverage["mapped_rows"] == 2
    assert coverage["unambiguous_rows"] == 2
    assert coverage["common_equity_rows"] == 2
    assert coverage["shares_outstanding_rows"] == 2
    assert coverage["eligible_rows"] == 2
    assert coverage["exclusions"] == {
        "unmapped_symbol": 1,  # XOM
        "ambiguous_ticker_mapping": 0,
        "not_classified_common_equity": 0,
        "missing_shares_outstanding": 0,
        "invalid_short_interest": 1,  # BAD
        "conflicting_versions": 0,
    }
    assert [e["ticker"] for e in result["entries"]] == ["AAPL", "AMD"]

    # CLI prints the counters: BAD never inflated mapping/shares coverage
    # (the old derived formula would have printed "Ticker mappings: 3").
    assert "FINRA securities:             4" in out
    assert "Ticker mappings:              2" in out
    assert "Shares-outstanding coverage:  2" in out
    assert "Eligible screen universe:     2" in out
    assert "Coverage: 50.0%" in out
    assert "Leaderboard entries: ['AAPL', 'AMD']" in out


def _replay_facts_payload(cik):
    """Companyfacts payload with a shares fact plus EPS facts (pre/post-EPS)."""
    return {
        "cik": cik,
        "entityName": f"CIK{cik}",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-08-01", "val": 100, "accn": f"a{cik}", "filed": "2026-08-02"},
            ]}}},
            "us-gaap": {
                "EarningsPerShareDiluted": {"units": {"USD/shares": [
                    {"end": "2026-08-01", "val": 6.5, "accn": f"a{cik}", "filed": "2026-08-02",
                     "fy": 2026, "fp": "Q3"},
                ]}},
                "EarningsPerShareBasic": {"units": {"USD/shares": [
                    {"end": "2026-08-01", "val": 6.6, "accn": f"a{cik}", "filed": "2026-08-02",
                     "fy": 2026, "fp": "Q3"},
                ]}},
            },
        },
    }


def test_replay_sec_facts_adds_eps_rows_deterministically(tmp_path):
    """Offline replay: pre-EPS store rows stay put, EPS rows appear once,
    rerun writes zero, retrieved_at comes from the manifest not the clock."""
    from app.normalization import normalize_sec_company_facts
    from app.storage import parquet, raw_archive

    payload = json.dumps(_replay_facts_payload(320193)).encode()
    retrieved_at = "2026-08-10T12:00:00Z"
    raw_archive.archive(
        "sec", "cik0000320193", "companyfacts", payload,
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        retrieved_at=retrieved_at, root=tmp_path / "raw",
    )

    # Seed the store the way a pre-EPS ingestion would have: shares only.
    pre_eps = dict(json.loads(payload))
    del pre_eps["facts"]["us-gaap"]
    datasets = normalize_sec_company_facts(
        pre_eps, retrieved_at=retrieved_at, content_hash="seed",
        source_url="u", source_record_id="cik0000320193",
    )
    assert parquet.write_rows("financial_facts", datasets["financial_facts"], root=tmp_path / "parquet") == 1

    first = research_data.replay_sec_facts_from_archive(data_root=tmp_path)
    assert first["archived_payloads"] == 1
    assert first["written_rows"] == 4  # 2 EPS facts + documents + securities
    assert first["failed"] == []

    table = parquet.read_table("financial_facts", root=tmp_path / "parquet")
    concepts = sorted(table.column("concept").to_pylist())
    assert concepts == [
        "EarningsPerShareBasic", "EarningsPerShareDiluted", "EntityCommonStockSharesOutstanding",
    ]
    assert table.num_rows == 3
    eps_rows = [r for r in table.to_pylist() if r["concept"].startswith("EarningsPerShare")]
    assert all(r["unit"] == "USD/shares" for r in eps_rows)
    assert all(r["fiscal_year"] == 2026 and r["fiscal_period"] == "Q3" for r in eps_rows)
    # deterministic: retrieved_at from the archive manifest, not the wall clock
    assert all(r["retrieved_at"] == retrieved_at for r in eps_rows)

    second = research_data.replay_sec_facts_from_archive(data_root=tmp_path)
    assert second["written_rows"] == 0
    assert parquet.read_table("financial_facts", root=tmp_path / "parquet").num_rows == 3


def test_replay_sec_facts_isolates_corrupt_payloads(tmp_path):
    from app.storage import raw_archive

    good = json.dumps(_replay_facts_payload(320193)).encode()
    raw_archive.archive(
        "sec", "cik0000320193", "companyfacts", good,
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        retrieved_at="2026-08-10T12:00:00Z", root=tmp_path / "raw",
    )
    raw_archive.archive(
        "sec", "cik0000000007", "companyfacts", b"{not valid json",
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000007.json",
        retrieved_at="2026-08-10T12:00:00Z", root=tmp_path / "raw",
    )
    summary = research_data.replay_sec_facts_from_archive(data_root=tmp_path)
    assert summary["archived_payloads"] == 2
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["cik"] == "cik0000000007"
    assert "JSONDecodeError" in summary["failed"][0]["error"]
    assert summary["written_rows"] > 0  # the valid payload still processed
