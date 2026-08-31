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

from datetime import date

SETTLEMENT = "2026-08-14"

@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC"), retrieved_at="2026-08-10T12:00:00Z", cik_start=1):
    payload = {
        str(i): {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}
        for i, (ticker, cik) in enumerate(zip(tickers, range(cik_start, cik_start + len(tickers))), start=0)
    }
    datasets = normalize_sec_tickers(
        payload, retrieved_at=retrieved_at, content_hash="tickers-hash",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_facts(data_root, facts_by_cik, retrieved_at="2026-08-10T12:00:00Z"):
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


def _seed_short_interest(data_root, rows, known_at="2026-08-10T12:00:00Z", content_hash="snapshot-hash"):
    datasets = normalize_finra_short_interest(
        rows, settlement_date=SETTLEMENT, known_at=known_at,
        retrieved_at=known_at, content_hash=content_hash,
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{SETTLEMENT}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")


def _default_rows():
    return [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]


def _default_facts():
    return {
        1: [{"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    }


def _seed_default(data_root):
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())


# ---------------------------------------------------------------------------
# Ranking, provenance, persistence
# ---------------------------------------------------------------------------


def test_materialize_ranks_complete_snapshot_and_persists(data_root):
    _seed_default(data_root)

    result = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)

    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA", "BBB"]
    assert result["entries"][0]["short_interest_percent"] == 50
    assert result["coverage"] == {
        "finra_rows": 3, "eligible_rows": 3,
        "valid_short_interest_rows": 3, "mapped_rows": 3,
        "unambiguous_rows": 3, "common_equity_rows": 3,
        "shares_outstanding_rows": 3,
        "exclusions": {"unmapped_symbol": 0, "ambiguous_ticker_mapping": 0,
                       "not_classified_common_equity": 0, "missing_shares_outstanding": 0,
                       "invalid_short_interest": 0},
    }
    assert result["calculation_version"] == screens.SCREEN_CALC_VERSION
    # Default as_of is the live horizon (today), not the settlement date.
    assert result["as_of_date"] == date.today().isoformat()
    assert result["source_records"]
    assert result["entries"][0]["sec_accession"] == "c1"
    assert result["entries"][0]["sec_source_url"].endswith("CIK0000000003.json")


def test_rerun_is_deterministic_and_creates_no_duplicates(data_root):
    _seed_default(data_root)
    first = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    second = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    assert [e["ticker"] for e in first["entries"]] == [e["ticker"] for e in second["entries"]]
    assert parquet.count_rows("screen_runs", root=data_root / "parquet") == 1
    assert parquet.count_rows("screen_entries", root=data_root / "parquet") == 3


def test_enrichment_publishes_new_version_and_keeps_old_immutable(data_root):
    """Mid-day targeted enrichment publishes a new screen version instead of
    being deduplicated away; the old version stays immutable."""
    _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC", "DDD"))
    _seed_short_interest(data_root, _default_rows() + [
        {"symbolCode": "DDD", "issueName": "Delta", "settlementDate": SETTLEMENT,
         "currentShortPositionQuantity": 20},
    ])
    _seed_facts(data_root, _default_facts())  # DDD's SEC facts arrive later
    first = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert [e["ticker"] for e in first["entries"]] == ["CCC", "AAA", "BBB"]
    assert first["coverage"]["exclusions"]["not_classified_common_equity"] == 1
    # Mid-day enrichment: DDD facts (filed 2026-08-05 -> known_at, visible at as_of 08-14)
    _seed_facts(data_root, {4: [{"end": "2026-08-01", "val": 50, "accn": "d1", "filed": "2026-08-05"}]})
    second = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert [e["ticker"] for e in second["entries"]] == ["CCC", "DDD", "AAA", "BBB"]
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
    assert [e["ticker"] for e in latest["entries"]] == ["CCC", "DDD", "AAA", "BBB"]


def test_old_schema_screen_run_is_reconstructed_and_coexists(data_root):
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
    assert old_again["coverage"]["mapped_rows"] == 4  # reconstructed, not clobbered
    new_result = screens.read_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    assert new_result["coverage"]["mapped_rows"] == 2  # stored counters used
    assert new_result["coverage"]["eligible_rows"] == 2


def test_read_is_bounded_by_limit(data_root):
    _seed_default(data_root)
    screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)
    result = screens.get_short_interest_leaderboard(limit=2, settlement_date=SETTLEMENT, data_root=data_root)
    assert [entry["ticker"] for entry in result["entries"]] == ["CCC", "AAA"]
    result = screens.get_short_interest_leaderboard(limit=999, settlement_date=SETTLEMENT, data_root=data_root)
    assert len(result["entries"]) == 3  # cap is a maximum, not a target
    assert len(result["entries"]) <= screens.MAX_LIMIT


def test_missing_settlement_date_is_honest_error(data_root):
    _seed_default(data_root)
    result = screens.get_short_interest_leaderboard(settlement_date="2025-01-15", data_root=data_root)
    assert "error" in result
    assert "not ingested" in result["error"] or "no normalized" in result["error"].lower()


# ---------------------------------------------------------------------------
# As-of regression: a later filing cannot affect an earlier ranking
# ---------------------------------------------------------------------------


def test_as_of_regression_later_filing_does_not_change_earlier_ranking(data_root):
    _seed_tickers(data_root)
    _seed_facts(data_root, {
        1: [{"end": "2026-08-01", "val": 100, "accn": "a1", "filed": "2026-08-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    })
    _seed_short_interest(data_root, _default_rows())

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert [e["ticker"] for e in early["entries"]] == ["CCC", "AAA", "BBB"]
    assert early["entries"][1]["short_interest_percent"] == 20  # AAA: 20/100

    # A later filing (known_at after 2026-08-14) restates AAA's shares to 400.
    _seed_facts(data_root, {
        1: [{"end": "2026-08-01", "val": 400, "accn": "a2", "filed": "2026-08-20"}],
    })

    # The earlier as-of ranking must be byte-identical after the later filing.
    rerun = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert rerun["entries"] == early["entries"]
    assert rerun["entries"][1]["sec_accession"] == "a1"
    assert rerun["entries"][1]["short_interest_percent"] == 20

    # A later as-of sees the restatement: AAA falls from 20% to 5%.
    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    by_ticker = {e["ticker"]: e for e in later["entries"]}
    assert [e["ticker"] for e in later["entries"]] == ["CCC", "BBB", "AAA"]
    assert by_ticker["AAA"]["sec_accession"] == "a2"
    assert by_ticker["AAA"]["short_interest_percent"] == 5


def test_fact_with_period_after_settlement_is_never_used(data_root):
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

    assert result["coverage"]["exclusions"]["missing_shares_outstanding"] == 1
    assert [e["ticker"] for e in result["entries"]] == ["CCC", "BBB"]


def test_e2e_fixtures_to_leaderboard_uses_production_only(tmp_path):
    """Fresh data root built from raw fixtures via production normalizers
    only: seeding uses app.normalization + parquet.write_rows, and the
    leaderboard reads the real store — no normalized rows hand-constructed."""
    data_root = tmp_path / "data"
    _seed_default(data_root)

    result = screens.get_short_interest_leaderboard(settlement_date=SETTLEMENT, data_root=data_root)

    assert [e["ticker"] for e in result["entries"]] == ["CCC", "AAA", "BBB"]
    assert [e["short_interest_percent"] for e in result["entries"]] == [50.0, 20.0, 10.0]
    assert result["source_records"]


# ---------------------------------------------------------------------------
# Universe and exclusions
# ---------------------------------------------------------------------------


def test_unmapped_ambiguous_and_unclassified_rows_are_excluded(data_root):
    rows = _default_rows() + [
        {"symbolCode": "DDD", "issueName": "Delta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 10},
        {"symbolCode": "EEE", "issueName": "Epsilon", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 10},
        {"symbolCode": "FFF", "issueName": "Phi", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": None},
    ]
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

    assert result["coverage"]["finra_rows"] == 6
    assert result["coverage"]["eligible_rows"] == 3
    assert result["coverage"]["exclusions"] == {
        "unmapped_symbol": 1,            # DDD
        "ambiguous_ticker_mapping": 1,   # EEE
        "not_classified_common_equity": 0,
        "missing_shares_outstanding": 0,
        "invalid_short_interest": 1,     # FFF
    }
    assert [e["ticker"] for e in result["entries"]] == ["CCC", "AAA", "BBB"]


def test_stale_settlement_is_surfaced(data_root):
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


def test_snapshot_not_knowable_at_as_of_is_rejected(data_root):
    """A snapshot archived after as_of is invisible to that as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-30T12:00:00Z")

    result = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)

    assert "error" in result
    assert "knowable on or before 2026-08-14" in result["error"]


def test_ticker_alias_acquired_after_as_of_is_unusable(data_root):
    """A ticker mapping acquired after as_of cannot be used by an earlier
    screen: CCC is unmapped at 2026-08-14 and mapped at 2026-08-21."""
    _seed_tickers(data_root, tickers=("AAA", "BBB"), retrieved_at="2026-08-10T12:00:00Z")
    _seed_tickers(data_root, tickers=("CCC",), retrieved_at="2026-08-20T12:00:00Z", cik_start=3)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows())

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert early["coverage"]["exclusions"]["unmapped_symbol"] == 1
    assert [e["ticker"] for e in early["entries"]] == ["AAA", "BBB"]

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    assert later["coverage"]["exclusions"]["unmapped_symbol"] == 0
    assert [e["ticker"] for e in later["entries"]] == ["CCC", "AAA", "BBB"]


def test_corrected_snapshot_versions_selected_by_as_of(data_root):
    """A corrected snapshot is a new source version: the earlier as-of uses
    the original values, the later as-of uses the correction."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_short_interest(data_root, _default_rows(), known_at="2026-08-10T12:00:00Z")
    corrected = [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 25},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]
    _seed_short_interest(data_root, corrected, known_at="2026-08-20T12:00:00Z", content_hash="v2-snapshot-hash")

    early = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-14", data_root=data_root)
    assert [e["ticker"] for e in early["entries"]] == ["CCC", "AAA", "BBB"]
    assert early["entries"][1]["short_shares"] == 20  # original version

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-21", data_root=data_root)
    assert later["entries"][1]["short_shares"] == 25  # corrected version
    assert later["coverage"]["finra_rows"] == 3  # one version per symbol, not both


def test_security_classification_is_consulted(data_root):
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
    rows = _default_rows() + [
        {"symbolCode": "ETF", "issueName": "Index Fund", "settlementDate": SETTLEMENT, "currentShortPositionQuantity": 5},
    ]
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
    assert "ETF" in [e["ticker"] for e in early["entries"]]

    later = screens.materialize_short_interest_screen(SETTLEMENT, as_of="2026-08-30", data_root=data_root)
    assert later["coverage"]["exclusions"]["not_classified_common_equity"] == 1
    assert "ETF" not in [e["ticker"] for e in later["entries"]]


# ---------------------------------------------------------------------------
# Research slice: short-interest change + shares-outstanding change
# ---------------------------------------------------------------------------


def _seed_cycle(data_root, settlement_date, rows, known_at="2026-08-10T12:00:00Z"):
    datasets = normalize_finra_short_interest(
        rows, settlement_date=settlement_date, known_at=known_at,
        retrieved_at=known_at, content_hash=f"snapshot-{settlement_date}",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{settlement_date}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")


def test_change_slice_computes_changes_with_evidence(data_root):
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, "2026-08-07", [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 10},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": "2026-08-07", "currentShortPositionQuantity": 5},
    ])
    _seed_cycle(data_root, SETTLEMENT, _default_rows())

    result = screens.short_interest_change_screen("2026-08-21", data_root=data_root)

    assert result["settlement_current"] == SETTLEMENT
    assert result["settlement_prior"] == "2026-08-07"
    assert result["calculation_version"] == screens.SLICE_CALC_VERSION
    by_ticker = {e["ticker"]: e for e in result["entries"]}
    assert by_ticker["AAA"]["short_shares_current"] == 20
    assert by_ticker["AAA"]["short_shares_prior"] == 10
    assert by_ticker["AAA"]["short_change_pct"] == 100.0
    assert by_ticker["AAA"]["si_pp_change"] == 10.0  # 20% - 10%
    assert by_ticker["AAA"]["shares_change_abs"] == 0
    assert by_ticker["AAA"]["sec_accession_current"] == "a1"
    assert by_ticker["AAA"]["sec_accession_prior"] == "a1"
    assert by_ticker["AAA"]["finra_source_url"].startswith("https://api.finra.org")
    # Sorted by signed short-interest pp change: AAA moved most.
    assert [e["ticker"] for e in result["entries"]] == ["AAA", "BBB", "CCC"]


def test_change_slice_reports_missing_prior_cycle_as_none_not_zero(data_root):
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, SETTLEMENT, _default_rows())

    result = screens.short_interest_change_screen("2026-08-21", data_root=data_root)

    assert result["settlement_prior"] is None
    entry = result["entries"][0]
    assert entry["short_shares_prior"] is None
    assert entry["short_change_pct"] is None
    assert entry["si_pp_change"] is None


def test_change_slice_as_of_regression(data_root):
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
    assert early["entries"][0]["ticker"] == "AAA"
    assert early["entries"][0]["shares_outstanding_current"] == 100.0

    # A filing known only after 2026-08-14 restates AAA's shares for a
    # period between the two settlements (end 2026-08-10, filed 2026-08-20).
    _seed_facts(data_root, {
        1: [{"end": "2026-08-10", "val": 400, "accn": "a2", "filed": "2026-08-20"}],
    })

    rerun = screens.short_interest_change_screen("2026-08-14", data_root=data_root)
    assert rerun["entries"] == early["entries"]

    later = screens.short_interest_change_screen("2026-08-21", data_root=data_root)
    aaa = next(e for e in later["entries"] if e["ticker"] == "AAA")
    assert aaa["sec_accession_current"] == "a2"
    assert aaa["shares_outstanding_current"] == 400.0
    assert aaa["shares_change_abs"] == 300.0


def test_change_slice_honors_finra_known_at(data_root):
    """A snapshot archived after as_of is not knowable at that as_of."""
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    _seed_cycle(data_root, SETTLEMENT, _default_rows(), known_at="2026-08-30T12:00:00Z")

    result = screens.short_interest_change_screen("2026-08-14", data_root=data_root)
    assert "error" in result
    assert "knowable" in result["error"]