"""Tests for the foundation-backed short-interest screen.

The acceptance criteria under test:

- a screen result exposes settlement date, as-of timestamp, source records,
  coverage/exclusions, and calculation version;
- changing the requested as_of cannot use facts with a later known_at (the
  as-of regression test: a later filing cannot affect an earlier ranking);
- rerunning the same screen is deterministic and creates no duplicates;
- only eligible, classified equity securities are ranked.
"""

import json
from datetime import tzinfo
from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from app.analytics import screens
from app.normalization import (
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)
from app.storage import duckdb, parquet

from datetime import date, datetime, timezone

SETTLEMENT = "2026-08-14"

@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _seed_tickers(data_root: Path, tickers: tuple[str, ...] = ("AAA", "BBB", "CCC"), retrieved_at: str = "2026-08-10T12:00:00Z", cik_start: int = 1) -> None:
    payload = {
        str(i): {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}
        for i, (ticker, cik) in enumerate(zip(tickers, range(cik_start, cik_start + len(tickers))), start=0)
    }
    datasets = normalize_sec_tickers(
        payload, retrieved_at=retrieved_at, content_hash="tickers-hash",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_facts(data_root: Path, facts_by_cik: dict[int, list[dict[str, object]]], retrieved_at: str = "2026-08-10T12:00:00Z") -> None:
    for cik, facts in facts_by_cik.items():
        payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": facts}},
        }}}
        datasets = normalize_sec_company_facts(
            payload, retrieved_at=retrieved_at, content_hash=f"facts-{cik}",
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            source_record_id=f"cik{cik:010d}",
        )
        for name, rows in datasets.items():
            parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_short_interest(data_root: Path, rows: list[dict[str, object]], known_at: str = "2026-08-10T12:00:00Z", content_hash: str = "snapshot-hash") -> None:
    datasets = normalize_finra_short_interest(
        rows, settlement_date=SETTLEMENT, known_at=known_at,
        retrieved_at=known_at, content_hash=content_hash,
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{SETTLEMENT}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")


def _default_rows() -> list[dict[str, object]]:
    return [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]


def _default_facts() -> dict[int, list[dict[str, object]]]:
    return {
        1: [{"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    }


def _seed_default(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())


# ---------------------------------------------------------------------------
# Ranking, provenance, persistence
# ---------------------------------------------------------------------------


def test_materialize_ranks_complete_snapshot_and_persists(data_root: Path) -> None:
    _seed_default(data_root)

    result = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)

    assert [entry["ticker"] for entry in entries] == ["CCC", "AAA", "BBB"]
    assert entries[0]["short_interest_percent"] == 50
    assert result["coverage"] == {
        "finra_rows": 3, "eligible_rows": 3,
        "valid_short_interest_rows": 3, "mapped_rows": 3,
        "unambiguous_rows": 3, "common_equity_rows": 3,
        "shares_outstanding_rows": 3,
        "exclusions": {"unmapped_symbol": 0, "ambiguous_ticker_mapping": 0,
                       "not_classified_common_equity": 0, "missing_shares_outstanding": 0,
                       "invalid_short_interest": 0, "conflicting_versions": 0},
    }
    assert result["calculation_version"] == screens.SCREEN_CALC_VERSION
    # Default as_of is the live horizon (UTC today), not the settlement date.
    assert result["as_of_date"] == datetime.now(timezone.utc).date().isoformat()
    assert result["source_records"]
    assert entries[0]["sec_accession"] == "c1"
    assert entries[0]["sec_source_url"].endswith("CIK0000000003.json")


def test_rerun_is_deterministic_and_creates_no_duplicates(data_root: Path) -> None:
    _seed_default(data_root)
    first = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    second = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    first_entries = first["entries"]
    assert isinstance(first_entries, list)
    second_entries = second["entries"]
    assert isinstance(second_entries, list)
    assert [e["ticker"] for e in first_entries] == [e["ticker"] for e in second_entries]
    assert parquet.count_rows("screen_runs", root=data_root / "parquet") == 1
    assert parquet.count_rows("screen_entries", root=data_root / "parquet") == 3


def test_enrichment_publishes_new_version_and_keeps_old_immutable(data_root: Path) -> None:
    """Mid-day targeted enrichment publishes a new screen version instead of
    being deduplicated away; the old version stays immutable."""
    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC", "DDD"))
    extra_ddd: list[dict[str, object]] = [
        {"symbolCode": "DDD", "issueName": "Delta", "settlementDate": SETTLEMENT,
         "currentShortPositionQuantity": 20},
    ]
    _seed_short_interest(data_root, _default_rows() + extra_ddd)
    _seed_facts(data_root, _default_facts())  # DDD's SEC facts arrive later
    first = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    first_entries = first["entries"]
    assert isinstance(first_entries, list)
    assert [e["ticker"] for e in first_entries] == ["CCC", "AAA", "BBB"]
    first_coverage = first["coverage"]
    assert isinstance(first_coverage, dict)
    assert first_coverage["exclusions"]["not_classified_common_equity"] == 1
    # Mid-day enrichment: DDD facts (filed 2026-08-05 -> known_at, visible at as_of 08-14)
    _seed_facts(data_root, {4: [{"end": "2026-08-01", "val": 50, "accn": "d1", "filed": "2026-08-05"}]})
    second = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    second_entries = second["entries"]
    assert isinstance(second_entries, list)
    assert [e["ticker"] for e in second_entries] == ["CCC", "DDD", "AAA", "BBB"]
    # Both versions exist (append-only); deterministic no-op on identical inputs
    screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert parquet.count_rows("screen_runs", root=data_root / "parquet") == 2
    runs = duckdb.query("SELECT run_id FROM screen_runs ORDER BY created_at, run_id", data_root=data_root)
    assert len(runs) == 2 and runs[0]["run_id"] != runs[1]["run_id"]
    versions = set()
    for r in runs:
        versions.add(tuple(row["ticker"] for row in duckdb.query(
            "SELECT ticker FROM screen_entries WHERE run_id = ? ORDER BY rank",
            params=[r["run_id"]], data_root=data_root)))
    assert ("CCC", "DDD", "AAA", "BBB") in versions   # enriched version published
    assert ("CCC", "AAA", "BBB") in versions          # old version immutable
    # Reader serves the latest applicable version
    latest = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    latest_entries = latest["entries"]
    assert isinstance(latest_entries, list)
    assert [e["ticker"] for e in latest_entries] == ["CCC", "DDD", "AAA", "BBB"]


def test_created_at_has_sub_second_precision(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same-second publications get distinct created_at values, so the reader
    orders by publication time instead of the run_id hash tie-breaker."""
    class FrozenClock:
        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return datetime(2026, 8, 14, 12, 0, 0, 250000, tzinfo=timezone.utc)

    monkeypatch.setattr(screens, "datetime", FrozenClock)
    assert screens._utc_now() == "2026-08-14T12:00:00.250000+00:00"


def test_same_second_versions_ordered_by_publication(data_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The later of two same-second publications wins, regardless of run_id."""
    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC", "DDD"))
    extra_ddd: list[dict[str, object]] = [
        {"symbolCode": "DDD", "issueName": "Delta", "settlementDate": SETTLEMENT,
         "currentShortPositionQuantity": 20},
    ]
    _seed_short_interest(data_root, _default_rows() + extra_ddd)
    _seed_facts(data_root, _default_facts())  # DDD's SEC facts arrive later
    times = iter([
        "2026-08-14T12:00:00.250000+00:00",  # first version
        "2026-08-14T12:00:00.800000+00:00",  # enriched version, later same second
    ])
    monkeypatch.setattr(screens, "_utc_now", lambda: next(times))
    screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    _seed_facts(data_root, {4: [{"end": "2026-08-01", "val": 50, "accn": "d1", "filed": "2026-08-05"}]})
    screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    latest = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    latest_entries = latest["entries"]
    assert isinstance(latest_entries, list)
    assert [e["ticker"] for e in latest_entries] == ["CCC", "DDD", "AAA", "BBB"]


def test_old_schema_screen_run_is_reconstructed_and_coexists(data_root: Path) -> None:
    """A pre-stage-counter (11-column) run reads via union-by-name, its
    counters reconstruct from exclusions, and it coexists with a
    counter-bearing run written through the production path."""
    _seed_default(data_root)
    old_dir = data_root / "parquet" / "screen_runs" / "settlement_date_year=2026"
    old_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({
            "run_id": [f"{screens.SCREEN_NAME}:{SETTLEMENT}:2026-08-14"],
            "screen": [screens.SCREEN_NAME],
            "settlement_date": [SETTLEMENT],
            "as_of": ["2026-08-14"],
            "created_at": ["2026-08-14T00:00:00Z"],
            "calc_version": [screens.SCREEN_CALC_VERSION],
            "finra_rows": [6],
            "eligible_rows": [3],
            "exclusions_json": [json.dumps({
                "unmapped_symbol": 1, "ambiguous_ticker_mapping": 1,
                "not_classified_common_equity": 0, "missing_shares_outstanding": 0,
                "invalid_short_interest": 1,
            })],
            "environment": ["test"],
            "parser_version": ["pre-counter"],
        }, schema=pa.schema([
            pa.field("run_id", pa.string()), pa.field("screen", pa.string()),
            pa.field("settlement_date", pa.string()), pa.field("as_of", pa.string()),
            pa.field("created_at", pa.string()), pa.field("calc_version", pa.string()),
            pa.field("finra_rows", pa.int64()), pa.field("eligible_rows", pa.int64()),
            pa.field("exclusions_json", pa.string()), pa.field("environment", pa.string()),
            pa.field("parser_version", pa.string()),
        ])),
        str(old_dir / "part-old.parquet"),
    )
    result = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert result["coverage"] == {
        "finra_rows": 6, "eligible_rows": 3,
        "valid_short_interest_rows": 5, "mapped_rows": 4,
        "unambiguous_rows": 3, "common_equity_rows": 3,
        "shares_outstanding_rows": 3,
        "exclusions": {"unmapped_symbol": 1, "ambiguous_ticker_mapping": 1,
                       "not_classified_common_equity": 0, "missing_shares_outstanding": 0,
                       "invalid_short_interest": 1},
    }
    parquet.write_rows("screen_runs", [{
        "run_id": f"{screens.SCREEN_NAME}:{SETTLEMENT}:2026-08-21",
        "screen": screens.SCREEN_NAME,
        "settlement_date": SETTLEMENT,
        "as_of": "2026-08-21",
        "created_at": "2026-08-21T00:00:00Z",
        "calc_version": screens.SCREEN_CALC_VERSION,
        "finra_rows": 3, "eligible_rows": 2,
        "valid_short_interest_rows": 3, "mapped_rows": 2,
        "unambiguous_rows": 2, "common_equity_rows": 2,
        "shares_outstanding_rows": 2,
        "exclusions_json": json.dumps({
            "unmapped_symbol": 1, "ambiguous_ticker_mapping": 0,
            "not_classified_common_equity": 0, "missing_shares_outstanding": 0,
            "invalid_short_interest": 0,
        }),
        "environment": "test",
        "parser_version": screens.SCREEN_CALC_VERSION,
    }], root=data_root / "parquet")
    old_again = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    old_again_coverage = old_again["coverage"]
    assert isinstance(old_again_coverage, dict)
    assert old_again_coverage["mapped_rows"] == 4  # reconstructed, not clobbered
    new_result = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    new_result_coverage = new_result["coverage"]
    assert isinstance(new_result_coverage, dict)
    assert new_result_coverage["mapped_rows"] == 2  # stored counters used
    assert new_result_coverage["eligible_rows"] == 2


def test_read_is_bounded_by_limit(data_root: Path) -> None:
    _seed_default(data_root)
    screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    result = screens.get_short_interest_leaderboard(limit=2, settlement_date=SETTLEMENT, data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [entry["ticker"] for entry in entries] == ["CCC", "AAA"]
    result = screens.get_short_interest_leaderboard(limit=999, settlement_date=SETTLEMENT, data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert len(entries) == 3  # cap is a maximum, not a target
    assert len(entries) <= screens.MAX_LIMIT


def test_missing_settlement_date_is_honest_error(data_root: Path) -> None:
    _seed_default(data_root)
    # Historical reproduction never fetches: a missing cycle stays an error.
    result = screens.get_short_interest_leaderboard(
        settlement_date="2025-01-15", as_of="2026-08-14", data_root=data_root
    )
    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "not ingested" in error or "no normalized" in error.lower()


# ---------------------------------------------------------------------------
# As-of regression: a later filing cannot affect an earlier ranking
# ---------------------------------------------------------------------------


def test_as_of_regression_later_filing_does_not_change_earlier_ranking(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, {
        1: [{"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    })
    _seed_short_interest(data_root, _default_rows())

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    early_entries = early["entries"]
    assert isinstance(early_entries, list)
    assert [e["ticker"] for e in early_entries] == ["CCC", "AAA", "BBB"]
    assert early_entries[1]["short_interest_percent"] == 20  # AAA: 20/100

    # A later filing (known_at after 2026-08-14) restates AAA's shares to 400.
    _seed_facts(data_root, {
        1: [{"end": "2026-08-01", "val": 400, "accn": "a2", "filed": "2026-08-20"}],
    })

    # The earlier as-of ranking must be byte-identical after the later filing.
    rerun = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert rerun["entries"] == early["entries"]
    rerun_entries = rerun["entries"]
    assert isinstance(rerun_entries, list)
    assert rerun_entries[1]["sec_accession"] == "a1"
    assert rerun_entries[1]["short_interest_percent"] == 20

    # A later as-of sees the restatement: AAA falls from 20% to 5%.
    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    later_entries = later["entries"]
    assert isinstance(later_entries, list)
    by_ticker = {e["ticker"]: e for e in later_entries}
    assert [e["ticker"] for e in later_entries] == ["CCC", "BBB", "AAA"]
    assert by_ticker["AAA"]["sec_accession"] == "a2"
    assert by_ticker["AAA"]["short_interest_percent"] == 5


def test_fact_with_period_after_settlement_is_never_used(data_root: Path) -> None:
    """The shares-outstanding fact must be as of (or before) the settlement
    date; a fact with a later period end is not eligible — even when it is
    already knowable at the as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, {
        1: [{"end": "2026-09-01", "val": 100, "accn": "a1", "filed": "2026-08-20"}],
        2: [{"end": "2026-06-30", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    })
    _seed_short_interest(data_root, _default_rows())

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-30", data_root=data_root)
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
    entries = result["entries"]
    assert isinstance(entries, list)

    assert coverage["exclusions"]["missing_shares_outstanding"] == 1
    assert [e["ticker"] for e in entries] == ["CCC", "BBB"]


def test_e2e_fixtures_to_leaderboard_uses_production_only(tmp_path: Path) -> None:
    """Fresh data root built from raw fixtures via production normalizers
    only: seeding uses app.normalization + parquet.write_rows, and the
    leaderboard reads the real store — no normalized rows hand-constructed."""
    data_root = tmp_path / "data"
    _seed_default(data_root)

    result = screens.get_short_interest_leaderboard(settlement_date=SETTLEMENT, data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)

    assert [e["ticker"] for e in entries] == ["CCC", "AAA", "BBB"]
    assert [e["short_interest_percent"] for e in entries] == [50.0, 20.0, 10.0]
    assert result["source_records"]


# ---------------------------------------------------------------------------
# Universe and exclusions
# ---------------------------------------------------------------------------


def test_unmapped_ambiguous_and_unclassified_rows_are_excluded(data_root: Path) -> None:
    extra_unmapped: list[dict[str, object]] = [
        {"symbolCode": "DDD", "issueName": "Delta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 10},
        {"symbolCode": "EEE", "issueName": "Epsilon", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 10},
        {"symbolCode": "FFF", "issueName": "Phi", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": None},
    ]
    rows: list[dict[str, object]] = _default_rows() + extra_unmapped
    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC", "EEE"))
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, rows)
    # EEE also appears under a second CIK -> ambiguous.
    parquet.write_rows("entity_aliases", [{
        "alias_type": "ticker", "alias_value": "EEE", "entity_id": "sec:cik:0000000099",
        "security_id": "sec:equity:0000000099", "source": "sec:company_tickers",
        "valid_from": None, "valid_to": None, "known_at": "2026-08-21T12:00:00Z",
        "retrieved_at": "2026-08-21T12:00:00Z", "content_hash": "x", "parser_version": "t",
    }], root=data_root / "parquet")

    result = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
    entries = result["entries"]
    assert isinstance(entries, list)

    assert coverage["finra_rows"] == 6
    assert coverage["eligible_rows"] == 3
    assert coverage["exclusions"] == {
        "unmapped_symbol": 1,            # DDD
        "ambiguous_ticker_mapping": 1,   # EEE
        "not_classified_common_equity": 0,
        "missing_shares_outstanding": 0,
        "invalid_short_interest": 1,     # FFF
        "conflicting_versions": 0,
    }
    assert [e["ticker"] for e in entries] == ["CCC", "AAA", "BBB"]


def test_stale_settlement_is_surfaced(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    stale_date = "2025-01-15"
    datasets = normalize_finra_short_interest(
        _default_rows(), settlement_date=stale_date, known_at="2025-01-20T12:00:00Z",
        retrieved_at="2025-01-20T12:00:00Z", content_hash="snapshot-hash-2",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{stale_date}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")

    stale = screens.materialize_short_interest_screen(stale_date, as_of="2025-01-20", data_root=data_root)
    assert stale["data_freshness"] == "stale"
    assert stale["as_of_date"] == "2025-01-20"


# ---------------------------------------------------------------------------
# Point-in-time enforcement (P0): FINRA rows, aliases, and classifications
# ---------------------------------------------------------------------------


def test_snapshot_not_knowable_at_as_of_is_rejected(data_root: Path) -> None:
    """A snapshot archived after as_of is invisible to that as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-30T12:00:00Z")

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)

    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "knowable on or before 2026-08-14" in error


def test_ticker_alias_acquired_after_as_of_is_unusable(data_root: Path) -> None:
    """A ticker mapping acquired after as_of cannot be used by an earlier
    screen: CCC is unmapped at 2026-08-14 and mapped at 2026-08-21."""
    _seed_tickers(data_root, tickers=("AAA", "BBB"), retrieved_at="2026-08-10T12:00:00Z")
    _seed_tickers(data_root, tickers=("CCC",), retrieved_at="2026-08-20T12:00:00Z", cik_start=3)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    early_coverage = early["coverage"]
    assert isinstance(early_coverage, dict)
    early_entries = early["entries"]
    assert isinstance(early_entries, list)
    assert early_coverage["exclusions"]["unmapped_symbol"] == 1
    assert [e["ticker"] for e in early_entries] == ["AAA", "BBB"]

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    later_coverage = later["coverage"]
    assert isinstance(later_coverage, dict)
    later_entries = later["entries"]
    assert isinstance(later_entries, list)
    assert later_coverage["exclusions"]["unmapped_symbol"] == 0
    assert [e["ticker"] for e in later_entries] == ["CCC", "AAA", "BBB"]


def test_corrected_snapshot_versions_selected_by_as_of(data_root: Path) -> None:
    """A corrected snapshot is a new source version: the earlier as-of uses
    the original values, the later as-of uses the correction."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-10T12:00:00Z")
    corrected: list[dict[str, object]] = [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 25},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]
    _seed_short_interest(data_root, corrected, known_at="2026-08-20T12:00:00Z", content_hash="v2-snapshot-hash")

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    early_entries = early["entries"]
    assert isinstance(early_entries, list)
    assert [e["ticker"] for e in early_entries] == ["CCC", "AAA", "BBB"]
    assert early_entries[1]["short_shares"] == 20  # original version

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    later_entries = later["entries"]
    assert isinstance(later_entries, list)
    later_coverage = later["coverage"]
    assert isinstance(later_coverage, dict)
    assert later_entries[1]["short_shares"] == 25  # corrected version
    assert later_coverage["finra_rows"] == 3  # one version per symbol, not both


def test_security_classification_is_consulted(data_root: Path) -> None:
    """Eligibility comes from the securities classification, not a
    fact-presence proxy: reclassifying ETF (unknown type) excludes it even
    though a shares-outstanding fact exists."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC", "ETF"))
    _seed_facts(data_root, {
        **{cik: facts for cik, facts in _default_facts().items()},
        4: [{"end": "2026-08-01", "val": 50, "accn": "e1", "filed": "2026-08-02"}],
    })
    extra_etf: list[dict[str, object]] = [
        {"symbolCode": "ETF", "issueName": "Index Fund", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]
    rows: list[dict[str, object]] = _default_rows() + extra_etf
    _seed_short_interest(data_root, rows)
    # A later classification row reclassifies the ETF as not common equity.
    reclassified = {
        "security_id": "sec:equity:0000000004", "entity_id": "sec:cik:0000000004",
        "security_type": "unknown", "ticker": None, "exchange": None,
        "source": "provider-test", "known_at": "2026-08-25T12:00:00Z",
        "retrieved_at": "2026-08-25T12:00:00Z", "content_hash": "x", "parser_version": "t",
    }
    directory = data_root / "parquet" / "securities" / "partition=none"
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([reclassified], schema=parquet.dataset("securities").schema),
        str(directory / "part-reclassified.parquet"),
    )

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    early_entries = early["entries"]
    assert isinstance(early_entries, list)
    assert "ETF" in [e["ticker"] for e in early_entries]

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-30", data_root=data_root)
    later_coverage = later["coverage"]
    assert isinstance(later_coverage, dict)
    later_entries = later["entries"]
    assert isinstance(later_entries, list)
    assert later_coverage["exclusions"]["not_classified_common_equity"] == 1
    assert "ETF" not in [e["ticker"] for e in later_entries]


def test_corrected_snapshot_mixed_offsets_newest_wins(data_root: Path) -> None:
    """A mixed-offset revision: the lexically-larger but chronologically
    older 13:00+01:00 version must lose to the 12:30Z correction."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-10T13:00:00+01:00")
    corrected: list[dict[str, object]] = [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 25},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]
    _seed_short_interest(data_root, corrected, known_at="2026-08-10T12:30:00Z", content_hash="v2-mixed-offset-hash")

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [e["ticker"] for e in entries] == ["CCC", "AAA", "BBB"]
    assert entries[1]["short_shares"] == 25  # the 12:30Z correction wins


def test_security_type_map_mixed_offsets_newest_wins(data_root: Path) -> None:
    """A classification revision with mixed offsets: the lexically-larger
    but chronologically older 13:00+01:00 'unknown' row must not beat the
    12:30Z 'equity-common' correction."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC"))
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())
    directory = data_root / "parquet" / "securities" / "partition=none"
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([
            {
                "security_id": "sec:equity:0000000001", "entity_id": "sec:cik:0000000001",
                "security_type": "unknown", "ticker": "AAA", "exchange": None,
                "source": "provider-test", "known_at": "2026-08-10T13:00:00+01:00",
                "retrieved_at": "2026-08-10T13:00:00+01:00", "content_hash": "reclass-old", "parser_version": "t",
            },
            {
                "security_id": "sec:equity:0000000001", "entity_id": "sec:cik:0000000001",
                "security_type": "equity-common", "ticker": "AAA", "exchange": "NASDAQ",
                "source": "provider-test", "known_at": "2026-08-10T12:30:00Z",
                "retrieved_at": "2026-08-10T12:30:00Z", "content_hash": "reclass-new", "parser_version": "t",
            },
        ], schema=parquet.dataset("securities").schema),
        str(directory / "part-reclass-mixed-offset.parquet"),
    )

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [e["ticker"] for e in entries] == ["CCC", "AAA", "BBB"]  # AAA stays classified


def test_same_instant_conflicting_versions_exclude_symbol(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-10T12:00:00Z")
    _seed_short_interest(
        data_root,
        [{"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 99}],
        known_at="2026-08-10T12:00:00Z", content_hash="conflict-hash",
    )
    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
    assert [e["ticker"] for e in entries] == ["CCC", "BBB"]
    assert coverage["exclusions"]["conflicting_versions"] == 1

def test_all_versions_conflicting_reports_ambiguous_error(data_root: Path) -> None:
    _seed_short_interest(
        data_root,
        [{"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20}],
        known_at="2026-08-10T12:00:00Z",
    )
    _seed_short_interest(
        data_root,
        [{"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 99}],
        known_at="2026-08-10T12:00:00Z", content_hash="conflict-hash",
    )
    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "conflict at the same instant" in error


def test_same_instant_conflicting_classifications_exclude_entity(data_root: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC"))
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())
    directory = data_root / "parquet" / "securities" / "partition=none"
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([{
            "security_id": "sec:equity:0000000001", "entity_id": "sec:cik:0000000001",
            "security_type": "unknown", "ticker": "AAA", "exchange": None,
            "source": "provider-test", "known_at": "2026-08-10T12:00:00Z",
            "retrieved_at": "2026-08-10T12:00:00Z", "content_hash": "reclass-conflict", "parser_version": "t",
        }], schema=parquet.dataset("securities").schema),
        str(directory / "part-reclass-conflict.parquet"),
    )

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)
    coverage = result["coverage"]
    assert isinstance(coverage, dict)
    assert "AAA" not in [e["ticker"] for e in entries]
    assert coverage["exclusions"]["not_classified_common_equity"] == 1


# ---------------------------------------------------------------------------
# Research slice: short-interest change + shares-outstanding change
# ---------------------------------------------------------------------------


def _seed_cycle(data_root: Path, settlement_date: str, rows: list[dict[str, object]], known_at: str = "2026-08-10T12:00:00Z") -> None:
    datasets = normalize_finra_short_interest(
        rows, settlement_date=settlement_date, known_at=known_at,
        retrieved_at=known_at, content_hash=f"snapshot-{settlement_date}",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{settlement_date}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")


def test_change_slice_computes_changes_with_evidence(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, "2026-08-07", [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 5},
    ])
    _seed_cycle(data_root, SETTLEMENT, _default_rows())

    result = screens.short_interest_change_screen("2026-08-21", data_root=data_root)
    entries = result["entries"]
    assert isinstance(entries, list)

    assert result["settlement_current"] == SETTLEMENT
    assert result["settlement_prior"] == "2026-08-07"
    assert result["calculation_version"] == screens.SLICE_CALC_VERSION
    by_ticker = {e["ticker"]: e for e in entries}
    assert by_ticker["AAA"]["short_shares_current"] == 20
    assert by_ticker["AAA"]["short_shares_prior"] == 10
    assert by_ticker["AAA"]["short_change_pct"] == 100.0
    assert by_ticker["AAA"]["si_pp_change"] == 10.0  # 20% - 10%
    assert by_ticker["AAA"]["shares_change_abs"] == 0
    assert by_ticker["AAA"]["sec_accession_current"] == "a1"
    assert by_ticker["AAA"]["sec_accession_prior"] == "a1"
    assert by_ticker["AAA"]["finra_source_url"].startswith("https://api.finra.org")
    # Sorted by signed short-interest pp change: AAA moved most.
    assert [e["ticker"] for e in entries] == ["AAA", "BBB", "CCC"]


def test_change_slice_reports_missing_prior_cycle_as_none_not_zero(data_root: Path) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, SETTLEMENT, _default_rows())

    result = screens.short_interest_change_screen("2026-08-21", data_root=data_root)

    assert result["settlement_prior"] is None
    entries = result["entries"]
    assert isinstance(entries, list)
    entry = entries[0]
    assert entry["short_shares_prior"] is None
    assert entry["short_change_pct"] is None
    assert entry["si_pp_change"] is None


def test_change_slice_as_of_regression(data_root: Path) -> None:
    """A later filing cannot alter a slice computed at an earlier as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, {
        1: [{"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    })
    _seed_cycle(data_root, "2026-08-07", [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 5},
    ], known_at="2026-08-10T12:00:00Z")
    _seed_cycle(data_root, SETTLEMENT, _default_rows(), known_at="2026-08-10T12:00:00Z")

    early = screens.short_interest_change_screen("2026-08-14", data_root=data_root)
    early_entries = early["entries"]
    assert isinstance(early_entries, list)
    assert early_entries[0]["ticker"] == "AAA"
    assert early_entries[0]["shares_outstanding_current"] == 100.0

    # A filing known only after 2026-08-14 restates AAA's shares for a
    # period between the two settlements (end 2026-08-10, filed 2026-08-20).
    _seed_facts(data_root, {
        1: [{"end": "2026-08-10", "val": 400, "accn": "a2", "filed": "2026-08-20"}],
    })

    rerun = screens.short_interest_change_screen("2026-08-14", data_root=data_root)
    assert rerun["entries"] == early["entries"]

    later = screens.short_interest_change_screen("2026-08-21", data_root=data_root)
    later_entries = later["entries"]
    assert isinstance(later_entries, list)
    aaa = next(e for e in later_entries if e["ticker"] == "AAA")
    assert aaa["sec_accession_current"] == "a2"
    assert aaa["shares_outstanding_current"] == 400.0
    assert aaa["shares_change_abs"] == 300.0


def test_change_slice_honors_finra_known_at(data_root: Path) -> None:
    """A snapshot archived after as_of is not knowable at that as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, SETTLEMENT, _default_rows(), known_at="2026-08-30T12:00:00Z")

    result = screens.short_interest_change_screen("2026-08-14", data_root=data_root)
    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "knowable" in error

# ---------------------------------------------------------------------------
# Fetch-on-empty: live screens fetch from FINRA, historical screens never do
# ---------------------------------------------------------------------------


def _install_finra_fetch_fake(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    """Serve discovery probes (limit 1) and full snapshots from one fake.

    The first probe reports no published rows so discovery must skip it;
    later probes report rows.  Full fetches return AAA/BBB/CCC rows for the
    requested settlement date.
    """
    probed = {"count": 0}

    def fake(
        group: str, dataset_name: str, payload: dict[str, object]
    ) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
        calls.append(payload)
        raw_filters = payload.get("compareFilters", [])
        assert isinstance(raw_filters, list)
        filters = {
            f.get("fieldName"): f.get("fieldValue")
            for f in raw_filters
            if isinstance(f, dict)
        }
        settlement = str(filters.get("settlementDate"))
        if payload.get("limit") == 1:  # discovery probe: existence only
            probed["count"] += 1
            total = 3 if probed["count"] > 1 else 0
            return b"[]", [], {"record-total": str(total)}
        rows: list[dict[str, object]] = [
            {"symbolCode": symbol, "issueName": symbol, "settlementDate": settlement,
             "currentShortPositionQuantity": position}
            for symbol, position in (("AAA", 20), ("BBB", 20), ("CCC", 5))
        ]
        return (
            json.dumps(rows).encode(), rows,
            {"record-total": str(len(rows))},
        )

    monkeypatch.setattr(screens.finra_client, "ingestion_post_query", fake)


def test_live_leaderboard_empty_store_discovers_and_fetches_once(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    calls: list[dict[str, object]] = []
    _install_finra_fetch_fake(monkeypatch, calls)

    result = screens.get_short_interest_leaderboard(data_root=data_root)

    assert "error" not in result
    entries = result["entries"]
    assert isinstance(entries, list)
    assert [e["ticker"] for e in entries] == ["CCC", "AAA", "BBB"]
    probes = [c for c in calls if c.get("limit") == 1]
    full = [c for c in calls if c.get("limit") != 1]
    assert len(probes) == 2  # newest candidate empty, next one hits
    assert len(full) == 1  # exactly one full fetch
    full_filters = full[0]["compareFilters"]
    assert isinstance(full_filters, list) and full_filters
    full_first = full_filters[0]
    assert isinstance(full_first, dict)
    probe_filters = probes[1]["compareFilters"]
    assert isinstance(probe_filters, list) and probe_filters
    probe_first = probe_filters[0]
    assert isinstance(probe_first, dict)
    assert full_first["fieldValue"] == probe_first["fieldValue"]
    assert result["settlement_date"] == full_first["fieldValue"]


def test_live_leaderboard_explicit_date_fetches_exactly_that_date(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    calls: list[dict[str, object]] = []
    _install_finra_fetch_fake(monkeypatch, calls)

    result = screens.get_short_interest_leaderboard(settlement_date=SETTLEMENT, data_root=data_root)

    assert "error" not in result
    assert result["settlement_date"] == SETTLEMENT
    assert len(calls) == 1  # no discovery probes, one exact-date fetch
    filters = calls[0]["compareFilters"]
    assert isinstance(filters, list) and filters
    first = filters[0]
    assert isinstance(first, dict)
    assert first["fieldValue"] == SETTLEMENT


def test_historical_leaderboard_empty_store_never_fetches(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    _install_finra_fetch_fake(monkeypatch, calls)

    result = screens.get_short_interest_leaderboard(as_of="2026-08-14", data_root=data_root)

    assert calls == []
    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "knowable on or before 2026-08-14" in error


def test_discovery_probe_uses_mock_dataset_in_mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINRA_USE_MOCK", "1")
    names: list[str] = []

    def fake(
        group: str, dataset_name: str, payload: dict[str, object]
    ) -> tuple[bytes, list[dict[str, object]], dict[str, str]]:
        names.append(dataset_name)
        return b"[]", [], {"record-total": "0"}

    monkeypatch.setattr(screens.finra_client, "ingestion_post_query", fake)

    assert screens._discover_latest_published_settlement_date(date(2026, 9, 9)) is None
    assert len(names) == screens._FETCH_DISCOVERY_CYCLES
    assert all(name == "consolidatedShortInterestMock" for name in names)


def test_live_fetch_failure_returns_error_not_raise(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("network down")

    monkeypatch.setattr(screens.finra_client, "ingestion_post_query", boom)

    result = screens.get_short_interest_leaderboard(settlement_date=SETTLEMENT, data_root=data_root)

    assert "error" in result
    error = result["error"]
    assert isinstance(error, str)
    assert "network down" in error