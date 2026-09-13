"""Offline tests for the research data refresh path (P2 throttle/retry, P1
optional universe enrichment, market-wide coverage).  Fetch is mocked at the
HTTP layer; archive, normalize, Parquet, and the leaderboard screen all run
for real against a tmp data root.
"""

import json
from pathlib import Path

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


def _facts_payload(cik: int, val: int) -> dict[str, object]:
    return {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2026-08-01", "val": val, "accn": f"a{cik}", "filed": "2026-08-02"},
        ]}},
    }}}


def _finra_row(symbol: str, pos: int) -> dict[str, object]:
    return {
        "symbolCode": symbol, "issueName": symbol,
        "settlementDate": "2026-08-14", "currentShortPositionQuantity": pos,
    }


def _page(
    rows: list[dict[str, object]], total: int, offset: int
) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
    return (
        json.dumps(rows).encode(), rows,
        {"record-total": str(total), "record-offset": str(offset), "record-limit": "1000"},
    )


class _Resp:
    def __init__(self, content: bytes, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _install_mocks(
    monkeypatch: pytest.MonkeyPatch,
    get_script: list[_Resp],
    page_script: list[tuple[bytes, list[dict[str, object]], dict[str, str]]],
    sleeps: list[float],
) -> list[str]:
    """Scripted HTTP responses; records SEC URLs and every sleep duration."""
    get_calls: list[str] = []

    def fake_get(url: str, **kwargs: object) -> _Resp:
        get_calls.append(url)
        return get_script.pop(0)  # IndexError when the script is exhausted

    def fake_ingestion_post_query(
        group: str, dataset_name: str, payload: dict[str, object]
    ) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
        return page_script.pop(0)

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(research_data.requests, "get", fake_get)
    monkeypatch.setattr(
        research_data.finra_client, "ingestion_post_query",
        fake_ingestion_post_query,
    )
    monkeypatch.setattr(research_data.time, "sleep", fake_sleep)
    return get_calls

def test_refresh_data_offline_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    sec_facts = summary["sec_facts"]
    assert isinstance(sec_facts, list)
    assert len(sec_facts) == 2
    sec_tickers = summary["sec_tickers"]
    assert isinstance(sec_tickers, dict)
    assert "ticker_ciks" not in sec_tickers  # full map stays internal
    assert sec_tickers["ticker_count"] == 2
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
    result = screens.materialize_short_interest_screen("2026-08-14", data_root=tmp_path)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [e["ticker"] for e in entries if isinstance(e, dict)] == ["AAPL", "AMD"]  # 20/100 > 20/200
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
    assert coverage["finra_rows"] == 3
    assert coverage["eligible_rows"] == 2
    exclusions = coverage["exclusions"]
    assert isinstance(exclusions, dict)
    assert exclusions["unmapped_symbol"] == 1  # XOM


def test_cli_refresh_data_coverage_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
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

    cli._cmd_refresh_data("2026-08-14", ["AAPL", "AMD"], [], data_root=str(tmp_path))
    out = capsys.readouterr().out

    assert "FINRA securities:             3" in out
    assert "Ticker mappings:              2" in out
    assert "Shares-outstanding coverage:  2" in out
    assert "Eligible screen universe:     2" in out
    assert "Coverage: 66.7%" in out
    assert "Leaderboard entries: ['AAPL', 'AMD']" in out


def test_unresolved_ticker_is_reported_not_fetched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
    ]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", tickers=["AAPL", "ZZZZ"], data_root=tmp_path)

    assert summary["unresolved_tickers"] == ["ZZZZ"]
    sec_facts = summary["sec_facts"]
    assert isinstance(sec_facts, list)
    assert len(sec_facts) == 1
    assert len(get_calls) == 2  # tickers + AAPL facts; no facts request for ZZZZ


def test_prepare_without_universe_skips_sec_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    get_script = [_Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200)]
    page_script = [_page([_finra_row("AAPL", 20)], 1, 0)]
    get_calls = _install_mocks(monkeypatch, get_script, page_script, sleeps)

    summary = prepare_short_interest_data("2026-08-14", data_root=tmp_path)

    assert summary["sec_facts"] == []
    assert len(get_calls) == 1  # SEC ticker universe only
    assert summary["unresolved_tickers"] == []
    sec_tickers = summary["sec_tickers"]
    assert isinstance(sec_tickers, dict)
    assert "ticker_ciks" not in sec_tickers
    assert sec_tickers["ticker_count"] == 2


def test_finra_missing_record_total_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []
    get_script = [
        _Resp(json.dumps(TICKERS_PAYLOAD).encode(), 200),
        _Resp(json.dumps(_facts_payload(320193, 100)).encode(), 200),
    ]
    page_script: list[tuple[bytes, list[dict[str, object]], dict[str, str]]] = [(b"[]", [], {})]  # no record-total header
    _install_mocks(monkeypatch, get_script, page_script, sleeps)

    with pytest.raises(ValueError, match="Record-Total"):
        prepare_short_interest_data("2026-08-14", tickers=["AAPL"], data_root=tmp_path)

def test_enrichment_failure_does_not_block_finra_or_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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

    finra = summary["finra"]
    assert isinstance(finra, dict)
    assert finra["rows"] == 1  # market-wide snapshot still landed
    sec_facts = summary["sec_facts"]
    assert isinstance(sec_facts, list)
    assert len(sec_facts) == 1  # AAPL enrichment succeeded
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


def test_cik_only_enrichment_failure_reports_null_ticker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_coverage_counters_truthful_with_invalid_short_interest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
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

    # One CLI run drives prepare + materialize; a replay of the screen then
    # re-reads the persisted run (unique-key dedup makes it a no-op write).
    cli._cmd_refresh_data("2026-08-14", ["AAPL", "AMD"], [], data_root=str(tmp_path))
    out = capsys.readouterr().out

    result = screens.materialize_short_interest_screen("2026-08-14", data_root=tmp_path)
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
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
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [e["ticker"] for e in entries if isinstance(e, dict)] == ["AAPL", "AMD"]

    # CLI prints the counters: BAD never inflated mapping/shares coverage
    # (the old derived formula would have printed "Ticker mappings: 3").
    assert "FINRA securities:             4" in out
    assert "Ticker mappings:              2" in out
    assert "Shares-outstanding coverage:  2" in out
    assert "Eligible screen universe:     2" in out
    assert "Coverage: 50.0%" in out
    assert "Leaderboard entries: ['AAPL', 'AMD']" in out


def _replay_facts_payload(cik: int) -> dict[str, object]:
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


def test_replay_sec_facts_adds_eps_rows_deterministically(tmp_path: Path):
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


def test_replay_sec_facts_isolates_corrupt_payloads(tmp_path: Path):
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
    failed = summary["failed"]
    assert isinstance(failed, list)
    assert len(failed) == 1
    first_failure = failed[0]
    assert isinstance(first_failure, dict)
    assert first_failure["cik"] == "cik0000000007"
    failure_error = first_failure["error"]
    assert isinstance(failure_error, str)
    assert "JSONDecodeError" in failure_error
    written_rows = summary["written_rows"]
    assert isinstance(written_rows, (int, float))
    assert written_rows > 0  # the valid payload still processed


def test_backfill_finra_known_at_rewrites_only_settlement_stamped(tmp_path: Path):
    """Settlement-stamped rows (known_at=settlement_date) gain retrieved_at;
    rerun is a no-op; a later correction wins newest-wins."""
    from app.normalization import normalize_finra_short_interest
    from app.services.research_data import backfill_finra_known_at

    parquet.write_rows("short_interest", [{
        "row_id": "finra:row:2026-08-14:AAA:oldhash1234", "entity_id": None, "security_id": None,
        "symbol_code": "AAA", "issue_name": "Alpha", "settlement_date": "2026-08-14",
        "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
        "source_url": "u", "source_record_id": "r",
        "known_at": "2026-08-14", "retrieved_at": "2026-08-30T12:00:00Z",
        "content_hash": "old", "parser_version": "finra-short-interest-v1",
    }], root=tmp_path / "parquet")
    datasets = normalize_finra_short_interest(
        [{"symbolCode": "BBB", "currentShortPositionQuantity": 5}],
        settlement_date="2026-08-14", retrieved_at="2026-08-30T12:00:00Z",
        content_hash="newhash", source_url="u", source_record_id="r")
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")

    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 1}
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}

    rows = {r["symbol_code"]: r for r in parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist()}
    assert rows["AAA"]["known_at"] == "2026-08-30T12:00:00Z"
    assert rows["AAA"]["retrieved_at"] == "2026-08-30T12:00:00Z"
    assert rows["BBB"]["known_at"] == "2026-08-30T12:00:00Z"

    correction = normalize_finra_short_interest(
        [{"symbolCode": "AAA", "issueName": "Alpha", "currentShortPositionQuantity": 25}],
        settlement_date="2026-08-14", retrieved_at="2026-09-03T12:00:00Z",
        content_hash="correction-hash", source_url="u", source_record_id="r")
    for name, rows_ in correction.items():
        parquet.write_rows(name, rows_, root=tmp_path / "parquet")
    all_rows: list[dict[str, object]] = parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist()
    def _retrieved(row: dict[str, object]) -> str:
        return str(row.get("retrieved_at"))
    versions = sorted([r for r in all_rows if r["symbol_code"] == "AAA"], key=_retrieved)
    assert [str(r["known_at"]) for r in versions] == ["2026-08-30T12:00:00Z", "2026-09-03T12:00:00Z"]
    assert versions[-1]["short_position"] == 25.0  # late-fetched correction wins newest-wins


def test_refresh_repairs_settlement_stamped_row_on_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same-snapshot re-ingest writes 0 rows (row_id dedupe) but backfills known_at."""
    import hashlib

    from app.normalization import normalize_finra_short_interest
    from app.services.research_data import backfill_finra_known_at, refresh_finra_short_interest

    settlement = "2026-08-14"
    payload: list[dict[str, object]] = [{"symbolCode": "AAA", "issueName": "Alpha",
                "settlementDate": settlement, "currentShortPositionQuantity": 20}]
    snapshot_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    parquet.write_rows("short_interest", [{
        "row_id": f"finra:row:{settlement}:AAA:{snapshot_hash[:12]}", "entity_id": None, "security_id": None,
        "symbol_code": "AAA", "issue_name": "Alpha", "settlement_date": settlement,
        "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
        "source_url": "u", "source_record_id": "r",
        "known_at": settlement, "retrieved_at": "2026-08-30T12:00:00Z",
        "content_hash": snapshot_hash, "parser_version": "finra-short-interest-v1",
    }], root=tmp_path / "parquet")

    datasets = normalize_finra_short_interest(
        payload, settlement_date=settlement, retrieved_at="2026-08-30T12:00:00Z",
        content_hash=snapshot_hash, source_url="u", source_record_id="r")
    assert sum(parquet.write_rows(n, r, root=tmp_path / "parquet") for n, r in datasets.items()) == 0

    content = json.dumps(payload).encode()

    def _fake_query(group: str, name: str, req: dict[str, object]) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
        return (content, payload, {"record-total": str(len(payload))})

    monkeypatch.setattr(research_data.finra_client, "ingestion_post_query", _fake_query)
    def _fake_sleep(seconds: float) -> None:
        return None
    monkeypatch.setattr(research_data.time, "sleep", _fake_sleep)
    result = refresh_finra_short_interest(settlement, data_root=tmp_path)
    assert result["written"] == 0
    assert result["backfilled"] == 1
    (stored,) = parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist()
    # Backfill is conservative: known_at becomes the row's own retrieved_at
    # (its original fetch), not the re-ingest wall-clock.
    assert stored["retrieved_at"] == "2026-08-30T12:00:00Z"
    assert stored["known_at"] == stored["retrieved_at"]
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}


def test_backfill_recovers_interrupted_swap(tmp_path: Path) -> None:
    from app.services.research_data import backfill_finra_known_at

    parquet.write_rows("short_interest", [{
        "row_id": "finra:row:2026-08-14:AAA:oldhash1234", "entity_id": None, "security_id": None,
        "symbol_code": "AAA", "issue_name": "Alpha", "settlement_date": "2026-08-14",
        "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
        "source_url": "u", "source_record_id": "r",
        "known_at": "2026-08-14", "retrieved_at": "2026-08-30T12:00:00Z",
        "content_hash": "old", "parser_version": "finra-short-interest-v1",
    }], root=tmp_path / "parquet")
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 1}
    dataset_dir = tmp_path / "parquet" / "short_interest"
    backup_dir = tmp_path / "parquet" / "short_interest-backfill-bak"
    dataset_dir.rename(backup_dir)
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}
    assert dataset_dir.exists()
    assert not backup_dir.exists()
    (row,) = parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist()
    assert row["symbol_code"] == "AAA"
    assert row["known_at"] == "2026-08-30T12:00:00Z"


def test_backfill_clears_stale_backup(tmp_path: Path) -> None:
    from app.services.research_data import backfill_finra_known_at

    parquet.write_rows("short_interest", [{
        "row_id": "finra:row:2026-08-14:AAA:oldhash1234", "entity_id": None, "security_id": None,
        "symbol_code": "AAA", "issue_name": "Alpha", "settlement_date": "2026-08-14",
        "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
        "source_url": "u", "source_record_id": "r",
        "known_at": "2026-08-30T12:00:00Z", "retrieved_at": "2026-08-30T12:00:00Z",
        "content_hash": "old", "parser_version": "t",
    }], root=tmp_path / "parquet")
    backup_dir = tmp_path / "parquet" / "short_interest-backfill-bak"
    backup_dir.mkdir(parents=True)
    (backup_dir / "junk.parquet").write_bytes(b"junk")
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}
    assert not backup_dir.exists()


def test_backfill_targets_only_legacy_v1_rows(tmp_path: Path) -> None:
    from app.services.research_data import backfill_finra_known_at

    parquet.write_rows("short_interest", [
        {
            "row_id": "finra:row:2026-08-14:AAA:oldhash1234", "entity_id": None, "security_id": None,
            "symbol_code": "AAA", "issue_name": "Alpha", "settlement_date": "2026-08-14",
            "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
            "source_url": "u", "source_record_id": "r",
            "known_at": "2026-08-14", "retrieved_at": "2026-08-30T12:00:00Z",
            "content_hash": "old", "parser_version": "finra-short-interest-v1",
        },
        {
            "row_id": "finra:row:2026-08-14:BBB:newhash5678", "entity_id": None, "security_id": None,
            "symbol_code": "BBB", "issue_name": "Beta", "settlement_date": "2026-08-14",
            "short_position": 5.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
            "source_url": "u", "source_record_id": "r",
            "known_at": "2026-08-14", "retrieved_at": "2026-08-30T12:00:00Z",
            "content_hash": "new", "parser_version": "finra-short-interest-v2",
        },
    ], root=tmp_path / "parquet")
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 1}
    rows = {str(r.get("symbol_code")): r for r in parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist() if isinstance(r, dict)}
    assert str(rows["AAA"].get("known_at")) == "2026-08-30T12:00:00Z"
    assert str(rows["AAA"].get("parser_version")) == "finra-short-interest-v2"
    assert str(rows["BBB"].get("known_at")) == "2026-08-14"
    assert str(rows["BBB"].get("parser_version")) == "finra-short-interest-v2"
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}


def test_finra_short_interest_lock_excludes(tmp_path: Path) -> None:
    import subprocess
    import sys

    from app.services.research_data import _finra_short_interest_lock

    parquet_root = tmp_path / "parquet"
    lock_path = parquet_root / "short_interest.lock"
    probe = (
        "import fcntl, sys; "
        "fh = open(sys.argv[1], 'a+b'); "
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)"
    )
    with _finra_short_interest_lock(parquet_root):
        held = subprocess.run([sys.executable, "-c", probe, str(lock_path)], capture_output=True, text=True)
        assert held.returncode != 0
        assert "BlockingIOError" in held.stderr
    free = subprocess.run([sys.executable, "-c", probe, str(lock_path)], capture_output=True, text=True)
    assert free.returncode == 0

def test_short_interest_has_legacy_v1_probe(tmp_path: Path) -> None:
    from app.services.research_data import _short_interest_has_legacy_v1

    parquet_root = tmp_path / "parquet"
    assert _short_interest_has_legacy_v1(parquet_root) is False
    v2_rows: list[dict[str, object]] = [
        {
            "row_id": f"finra:row:2026-08-14:{sym}:v2hash{i:04d}", "entity_id": None, "security_id": None,
            "symbol_code": sym, "issue_name": sym, "settlement_date": "2026-08-14",
            "short_position": 5.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
            "source_url": "u", "source_record_id": "r",
            "known_at": "2026-08-30T12:00:00Z", "retrieved_at": "2026-08-30T12:00:00Z",
            "content_hash": f"v2-{i}", "parser_version": "finra-short-interest-v2",
        }
        for i, sym in enumerate(["AAA", "BBB", "CCC"])
    ]
    parquet.write_rows("short_interest", v2_rows, root=parquet_root)
    assert _short_interest_has_legacy_v1(parquet_root) is False
    parquet.write_rows("short_interest", [
        {
            "row_id": "finra:row:2026-08-14:DDD:oldhash1234", "entity_id": None, "security_id": None,
            "symbol_code": "DDD", "issue_name": "Delta", "settlement_date": "2026-08-14",
            "short_position": 20.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
            "source_url": "u", "source_record_id": "r",
            "known_at": "2026-08-14", "retrieved_at": "2026-08-30T12:00:00Z",
            "content_hash": "old", "parser_version": "finra-short-interest-v1",
        },
    ], root=parquet_root)
    assert _short_interest_has_legacy_v1(parquet_root) is True


def test_backfill_skips_clean_table_without_rewrite(tmp_path: Path) -> None:
    from app.services.research_data import backfill_finra_known_at

    rows: list[dict[str, object]] = []
    for i, sym in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"]):
        known_at = "2026-08-14" if sym == "AAA" else "2026-08-30T12:00:00Z"
        rows.append(
            {
                "row_id": f"finra:row:2026-08-14:{sym}:v2hash{i:04d}", "entity_id": None, "security_id": None,
                "symbol_code": sym, "issue_name": sym, "settlement_date": "2026-08-14",
                "short_position": 5.0, "prev_position": None, "avg_daily_volume": None, "days_to_cover": None,
                "source_url": "u", "source_record_id": "r",
                "known_at": known_at, "retrieved_at": "2026-08-30T12:00:00Z",
                "content_hash": f"v2-{i}", "parser_version": "finra-short-interest-v2",
            }
        )
    parquet.write_rows("short_interest", rows, root=tmp_path / "parquet")
    backup_dir = tmp_path / "parquet" / "short_interest-backfill-bak"
    backup_dir.mkdir(parents=True)
    (backup_dir / "junk.parquet").write_bytes(b"junk")
    assert backfill_finra_known_at(data_root=tmp_path) == {"rewritten": 0}
    assert not backup_dir.exists()
    stored = {
        str(r.get("symbol_code")): r
        for r in parquet.read_table("short_interest", root=tmp_path / "parquet").to_pylist()
        if isinstance(r, dict)
    }
    assert len(stored) == 5
    for sym in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
        assert str(stored[sym].get("parser_version")) == "finra-short-interest-v2"
    assert str(stored["AAA"].get("known_at")) == "2026-08-14"
    for sym in ["BBB", "CCC", "DDD", "EEE"]:
        assert str(stored[sym].get("known_at")) == "2026-08-30T12:00:00Z"
