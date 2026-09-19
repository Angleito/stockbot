"""Offline regression tests for the verify fetch-once gate (fakes only, no Pi/network)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.verify_pi_tools as v
from app import finra_client
from app.analytics import screens
from app.domain.market.securities import TickerAlias
from app.normalization import (
    normalize_finra_short_interest,
    normalize_sec_company_facts,
    normalize_sec_tickers,
)
from app.services import research_data
from app.storage import raw_archive

SETTLEMENT = "2026-08-14"


class _ScreenGateway:
    """Screens gateway double built from normalizer output (PIT-filtered)."""

    def __init__(
        self, tickers_payload: dict[str, object], facts_by_cik: dict[int, dict[str, object]]
    ) -> None:
        alias_rows = normalize_sec_tickers(
            tickers_payload,
            retrieved_at="2026-08-10T12:00:00Z",
            content_hash="tickers",
        ).get("entity_aliases", [])
        self._aliases = [
            TickerAlias(
                alias_type=str(row.get("alias_type")),
                alias_value=str(row.get("alias_value")),
                entity_id=str(row.get("entity_id")),
                security_id=str(row.get("security_id")) if row.get("security_id") else None,
                source=str(row.get("source")),
                valid_from=str(row.get("valid_from")) if row.get("valid_from") else None,
                valid_to=str(row.get("valid_to")) if row.get("valid_to") else None,
                known_at=str(row.get("known_at")) if row.get("known_at") else None,
                retrieved_at=str(row.get("retrieved_at")) if row.get("retrieved_at") else None,
            )
            for row in alias_rows
            if isinstance(row, dict)
        ]
        self._facts: dict[int, dict[str, list[dict[str, object]]]] = {
            int(cik): normalize_sec_company_facts(
                payload,
                retrieved_at="2026-08-10T12:00:00Z",
                content_hash=f"facts-{cik}",
                source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json",
                source_record_id=f"cik{int(cik):010d}",
            )
            for cik, payload in facts_by_cik.items()
        }

    def ticker_candidates(self, ticker: str, as_of: object) -> list[TickerAlias]:
        del as_of  # PIT stays in resolve_ticker_aliases; return all aliases unfiltered.
        return [a for a in self._aliases if a.alias_value == ticker.strip().upper()]

    def company_facts(self, cik: int, as_of: str | None = None) -> dict[str, object]:
        datasets = self._facts.get(int(cik), {})
        out: dict[str, object] = {name: list(rows) for name, rows in datasets.items()}
        if as_of is not None:
            for name in ("financial_facts", "dividend_events"):
                rows = out.get(name)
                if isinstance(rows, list):
                    out[name] = [r for r in rows if str(r.get("known_at") or "")[:10] <= as_of]
        return out


def _stub_transports(
    monkeypatch: pytest.MonkeyPatch,
    tickers_payload: dict[str, object],
    facts_by_cik: dict[int, dict[str, object]],
    finra_pairs: list[tuple[str, int]],
) -> None:
    """Stub provider transports: SEC payloads, FINRA pages, enrichment confirm."""

    def _fake_get(url: str) -> bytes:
        if url == "https://www.sec.gov/files/company_tickers.json":
            return json.dumps(tickers_payload).encode()
        marker = "/companyfacts/CIK"
        if marker in url:
            cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
            return json.dumps(facts_by_cik[cik]).encode()
        raise AssertionError(f"unexpected SEC url: {url}")

    raw_rows: list[dict[str, object]] = [
        {
            "symbolCode": symbol,
            "issueName": symbol,
            "settlementDate": SETTLEMENT,
            "currentShortPositionQuantity": pos,
        }
        for symbol, pos in finra_pairs
    ]
    content = json.dumps(raw_rows).encode()

    def _fake_query(
        group: str, name: str, req: dict[str, object]
    ) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
        return (content, raw_rows, {"record-total": str(len(raw_rows))})

    class _Gateway:
        def company_facts(self, cik: int, as_of: str | None = None) -> dict[str, object]:
            del cik, as_of
            return {}

    typed = normalize_finra_short_interest(
        raw_rows,
        settlement_date=SETTLEMENT,
        retrieved_at="2026-08-30T12:00:00Z",
        content_hash="screen-snapshot",
        source_url="u",
        source_record_id=f"otcMarket/consolidatedShortInterest:{SETTLEMENT}",
    ).get("short_interest", [])
    screen_rows = [row for row in typed if isinstance(row, dict)]

    monkeypatch.setattr(research_data, "_edgar_get", _fake_get)
    monkeypatch.setattr(finra_client, "ingestion_post_query", _fake_query)
    monkeypatch.setattr(research_data, "_gateway", lambda: _Gateway())
    monkeypatch.setattr(research_data.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(screens, "_fetch_settlement_rows", lambda settlement: screen_rows)
    monkeypatch.setattr(screens, "_gateway", lambda: _ScreenGateway(tickers_payload, facts_by_cik))
    monkeypatch.setattr(screens.time, "sleep", lambda seconds: None)


def _tickers_payload() -> dict[str, object]:
    return {"0": {"cik_str": 1, "ticker": "AAA", "title": "AAA Corp"}}


def _facts_payload() -> dict[str, object]:
    return {
        "cik": 1,
        "entityName": "CIK1",
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"},
                        ]
                    }
                },
            }
        },
    }


def test_refresh_sec_tickers_returns_inline_rows_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])

    result = research_data.refresh_sec_tickers(data_root=tmp_path)

    assert result["ticker_ciks"] == {"AAA": 1}
    assert result["written"] == 0
    assert result["normalized_rows"] == 2  # one entity + one alias
    assert (
        raw_archive.find("sec", "company_tickers", "company_tickers", root=tmp_path / "raw")
        is not None
    )
    # pure normalizer agrees: parsed tickers match the inline summary
    datasets = normalize_sec_tickers(
        _tickers_payload(), retrieved_at="2026-08-10T12:00:00Z", content_hash="tickers-hash"
    )
    assert len(datasets["entity_aliases"]) == 1


def test_refresh_sec_company_facts_returns_inline_rows_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])

    result = research_data.refresh_sec_company_facts(1, data_root=tmp_path)

    assert result["cik"] == 1
    assert result["written"] == 0
    assert result["normalized_rows"] == 3  # documents + financial_facts + securities
    assert (
        raw_archive.find("sec", "cik0000000001", "companyfacts", root=tmp_path / "raw")
        is not None
    )
    # archive replays to the same rows
    rows = research_data.iter_archive_company_facts(1, data_root=tmp_path)
    assert len(rows) == 1
    assert rows[0]["concept"] == "EntityCommonStockSharesOutstanding"


def test_refresh_finra_short_interest_returns_inline_rows_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])

    result = research_data.refresh_finra_short_interest(SETTLEMENT, data_root=tmp_path)

    assert result["settlement_date"] == SETTLEMENT
    assert result["rows"] == 1
    assert result["normalized_rows"] == 1
    assert result["written"] == 0
    assert (
        raw_archive.find(
            "finra",
            "data_page",
            f"otcMarket/consolidatedShortInterest:{SETTLEMENT}:offset0",
            root=tmp_path / "raw",
        )
        is not None
    )
    datasets = normalize_finra_short_interest(
        [
            {
                "symbolCode": "AAA",
                "issueName": "Alpha",
                "settlementDate": SETTLEMENT,
                "currentShortPositionQuantity": 20,
            }
        ],
        settlement_date=SETTLEMENT,
        retrieved_at="2026-08-30T12:00:00Z",
        content_hash="snapshot-hash",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{SETTLEMENT}",
    )
    assert len(datasets["short_interest"]) == 1


def test_prepare_short_interest_data_returns_inline_rows_and_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])

    summary = research_data.prepare_short_interest_data(
        SETTLEMENT, tickers=["AAA"], data_root=tmp_path
    )

    assert summary["unresolved_tickers"] == []
    assert summary["failed_enrichments"] == []
    sec_facts = summary["sec_facts"]
    assert isinstance(sec_facts, list) and len(sec_facts) == 1
    finra = summary["finra"]
    assert isinstance(finra, dict)
    assert finra["rows"] == 1
    assert (
        raw_archive.find("sec", "company_tickers", "company_tickers", root=tmp_path / "raw")
        is not None
    )
    assert (
        raw_archive.find("sec", "cik0000000001", "companyfacts", root=tmp_path / "raw")
        is not None
    )
    assert (
        raw_archive.find(
            "finra",
            "data_page",
            f"otcMarket/consolidatedShortInterest:{SETTLEMENT}:offset0",
            root=tmp_path / "raw",
        )
        is not None
    )


def _main_mocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[list[tuple[str, int]]]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify", "--tool", "get_short_interest_leaderboard"])
    monkeypatch.setenv("PI_VERIFY_REPETITIONS", "1")
    monkeypatch.setattr(
        v,
        "discover",
        lambda: (
            {"tools": [{"function": {"name": "get_short_interest_leaderboard"}}]},
            {"bridge_ok": True, "tool_count": 1, "tool_names": ["get_short_interest_leaderboard"]},
        ),
    )
    matrix_calls: list[list[tuple[str, int]]] = []

    def _matrix(
        jobs: list[tuple[str, int]],
        _worker: object,
        _concurrency: int,
    ) -> list[object]:
        matrix_calls.append(jobs)
        return []

    monkeypatch.setattr(v, "run_matrix", _matrix)
    return matrix_calls


def test_all_present_no_fetch_and_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider smoke passes with stubbed transports; seed hook stays a no-op."""
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])
    monkeypatch.setenv("PI_VERIFY_SETTLEMENT_DATE", SETTLEMENT)

    assert v.ensure_finra_fixture(tmp_path, ["get_short_interest_leaderboard"]) == 0
    assert v.seed_finra_fixture(tmp_path / "store", tmp_path) is None


def test_missing_settlement_env_main_fails_without_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])
    # An unparseable settlement falls back to the newest cycle, which needs a
    # live FINRA probe; with no network the smoke gate fails and main never
    # reaches the matrix.
    monkeypatch.setattr(finra_client, "ingestion_post_query", lambda *a, **k: (_ for _ in ()).throw(ValueError("no network")))
    monkeypatch.delenv("PI_VERIFY_SETTLEMENT_DATE", raising=False)
    matrix_calls = _main_mocks(monkeypatch, tmp_path)
    assert v.main() != 0
    assert matrix_calls == []


def test_fetch_failure_main_fails_without_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])
    monkeypatch.setenv("PI_VERIFY_SETTLEMENT_DATE", SETTLEMENT)

    def _boom(*args: object, **kwargs: object) -> object:
        raise ValueError("FINRA_CLIENT_ID is required")

    monkeypatch.setattr(finra_client, "ingestion_post_query", _boom)
    monkeypatch.setattr(screens, "_fetch_settlement_rows", _boom)
    matrix_calls = _main_mocks(monkeypatch, tmp_path)
    assert v.main() != 0
    assert "FINRA_CLIENT_ID" in capsys.readouterr().err
    assert matrix_calls == []


def test_leaderboard_reads_live_providers_for_seeded_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inline refresh + live leaderboard: the confirm path works end to end."""
    _stub_transports(monkeypatch, _tickers_payload(), {1: _facts_payload()}, [("AAA", 20)])

    research_data.prepare_short_interest_data(SETTLEMENT, tickers=["AAA"], data_root=tmp_path)
    result = screens.get_short_interest_leaderboard(
        limit=5, settlement_date=SETTLEMENT, as_of="2026-08-30"
    )

    assert "error" not in result
    entries = result["entries"]
    assert isinstance(entries, list) and [e["ticker"] for e in entries] == ["AAA"]
