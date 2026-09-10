"""Offline regression tests for the verify fetch-once gate (fakes only, no Pi/network)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import scripts.verify_pi_tools as v
from app.normalization import (
    normalize_finra_short_interest,
    normalize_sec_company_facts,
    normalize_sec_tickers,
)
from app.storage import parquet

SETTLEMENT = "2026-08-14"


def _seed_all(durable: Path) -> None:
    tickers = normalize_sec_tickers(
        {"0": {"cik_str": 1, "ticker": "AAA", "title": "AAA Corp"}},
        retrieved_at="2026-08-10T12:00:00Z",
        content_hash="tickers-hash",
    )
    for name, rows in tickers.items():
        if name in v.FINRA_SEED_DATASETS:
            parquet.write_rows(name, rows, root=durable / "parquet")
    facts = normalize_sec_company_facts(
        {"cik": 1, "entityName": "CIK1", "facts": {"dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"},
            ]}},
        }}},
        retrieved_at="2026-08-10T12:00:00Z",
        content_hash="facts-1",
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        source_record_id="cik0000000001",
    )
    for name, rows in facts.items():
        if name in v.FINRA_SEED_DATASETS:
            parquet.write_rows(name, rows, root=durable / "parquet")
    snap = normalize_finra_short_interest(
        [{"symbolCode": "AAA", "issueName": "Alpha",
          "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20}],
        settlement_date=SETTLEMENT,
        known_at="2026-08-10T12:00:00Z",
        retrieved_at="2026-08-10T12:00:00Z",
        content_hash="snapshot-hash",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{SETTLEMENT}",
    )
    for name, rows in snap.items():
        parquet.write_rows(name, rows, root=durable / "parquet")


def _main_mocks(
    monkeypatch: pytest.MonkeyPatch, durable: Path, tmp_path: Path,
) -> list[list[tuple[str, int]]]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify", "--tool", "get_short_interest_leaderboard"])
    monkeypatch.setenv("PI_VERIFY_REPETITIONS", "1")
    monkeypatch.setattr(v, "get_data_root", lambda: durable)
    monkeypatch.setattr(
        v, "discover",
        lambda: (
            {"tools": [{"function": {"name": "get_short_interest_leaderboard"}}]},
            {"bridge_ok": True, "tool_count": 1,
             "tool_names": ["get_short_interest_leaderboard"]},
        ),
    )
    matrix_calls: list[list[tuple[str, int]]] = []
    def _matrix(
        jobs: list[tuple[str, int]], _worker: object, _concurrency: int,
    ) -> list[object]:
        matrix_calls.append(jobs)
        return []

    monkeypatch.setattr(v, "run_matrix", _matrix)
    return matrix_calls


def test_all_present_no_fetch_and_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    durable = tmp_path / "durable"
    _seed_all(durable)

    def _never(_d: Path, _s: str) -> None:
        raise AssertionError("must not fetch")

    monkeypatch.setattr(v, "fetch_finra_fixture", _never)
    assert v.ensure_finra_fixture(durable, ["get_short_interest_leaderboard"]) == 0
    store = tmp_path / "store"
    v.seed_finra_fixture(store, durable)
    assert (store / "parquet" / "short_interest").is_dir()
    assert not (store / "parquet" / "short_interest").is_symlink()


def test_missing_fetch_called_once_then_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    durable = tmp_path / "durable"
    _seed_all(durable)
    monkeypatch.setenv("PI_VERIFY_SETTLEMENT_DATE", SETTLEMENT)
    calls: list[tuple[Path, str]] = []

    def _fake(d: Path, s: str) -> None:
        calls.append((d, s))
        target = d / "parquet" / "financial_facts"
        target.mkdir(parents=True, exist_ok=True)
        (target / "fetched.marker").write_text("fetched")

    shutil.rmtree(durable / "parquet" / "financial_facts")
    monkeypatch.setattr(v, "fetch_finra_fixture", _fake)
    assert v.ensure_finra_fixture(durable, ["get_short_interest_leaderboard"]) == 0
    assert calls == [(durable, SETTLEMENT)]
    store = tmp_path / "store"
    v.seed_finra_fixture(store, durable)
    assert (store / "parquet" / "financial_facts" / "fetched.marker").read_text() == "fetched"


def test_missing_env_unset_main_fails_without_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    durable = tmp_path / "durable"
    _seed_all(durable)
    shutil.rmtree(durable / "parquet" / "securities")
    monkeypatch.delenv("PI_VERIFY_SETTLEMENT_DATE", raising=False)
    matrix_calls = _main_mocks(monkeypatch, durable, tmp_path)
    assert v.main() != 0
    assert "PI_VERIFY_SETTLEMENT_DATE" in capsys.readouterr().err
    assert matrix_calls == []


def test_fetch_failure_main_fails_without_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    durable = tmp_path / "durable"
    _seed_all(durable)
    shutil.rmtree(durable / "parquet" / "short_interest")
    monkeypatch.setenv("PI_VERIFY_SETTLEMENT_DATE", SETTLEMENT)

    def _boom(_d: Path, _s: str) -> None:
        raise ValueError("FINRA_CLIENT_ID is required")

    monkeypatch.setattr(v, "fetch_finra_fixture", _boom)
    matrix_calls = _main_mocks(monkeypatch, durable, tmp_path)
    assert v.main() != 0
    assert "FINRA_CLIENT_ID" in capsys.readouterr().err
    assert matrix_calls == []


def test_fetch_orchestrates_refresh_confirm_and_tolerates_one_cik(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.analytics.screens as screens
    import app.services.research_data as rd
    import app.storage.duckdb as dd

    durable = tmp_path / "durable"
    seen: list[int] = []

    def _tickers(*, data_root: Path | None = None) -> dict[str, object]:
        return {"ticker_ciks": {"AAA": 1, "BBB": 2}}

    def _finra(settlement_date: str, *, data_root: Path | None = None) -> dict[str, object]:
        return {"rows": 3}

    def _facts(cik: int, *, data_root: Path | None = None) -> dict[str, object]:
        seen.append(cik)
        if cik == 1:
            raise ValueError("boom")
        return {"cik": cik}

    def _rows(
        sql: str, params: object = (), data_root: Path | None = None,
    ) -> list[dict[str, object]]:
        return [
            {"symbol_code": "AAA", "pos": 100},
            {"symbol_code": "BBB", "pos": 50},
            {"symbol_code": "ZZZ", "pos": 10},  # unmapped: skipped
        ]

    def _ok(**_kw: object) -> dict[str, object]:
        return {"entries": [{"ticker": "AAA"}]}

    def _empty(**_kw: object) -> dict[str, object]:
        return {"error": "empty"}

    monkeypatch.setattr(rd, "refresh_sec_tickers", _tickers)
    monkeypatch.setattr(rd, "refresh_finra_short_interest", _finra)
    monkeypatch.setattr(rd, "refresh_sec_company_facts", _facts)
    monkeypatch.setattr(dd, "query", _rows)
    monkeypatch.setattr(screens, "get_short_interest_leaderboard", _ok)
    v.fetch_finra_fixture(durable, SETTLEMENT)
    assert sorted(seen) == [1, 2]
    monkeypatch.setattr(screens, "get_short_interest_leaderboard", _empty)
    with pytest.raises(RuntimeError, match="confirmation failed"):
        v.fetch_finra_fixture(durable, SETTLEMENT)


def test_fetch_sql_and_confirm_work_against_real_store(tmp_path: Path) -> None:
    """Pin the fetch SQL + confirm call to the real duckdb/screens layer (no fakes)."""
    from app.analytics import screens
    from app.storage import duckdb

    durable = tmp_path / "durable"
    _seed_all(durable)
    rows = duckdb.query(v.FETCH_TOP_SYMBOLS_SQL, params=[SETTLEMENT], data_root=durable)
    assert [(r["symbol_code"], r["pos"]) for r in rows] == [("AAA", 20.0)]
    result = screens.get_short_interest_leaderboard(limit=5, data_root=durable)
    assert "error" not in result
    entries = result["entries"]
    assert isinstance(entries, list) and [e["ticker"] for e in entries] == ["AAA"]
