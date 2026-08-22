"""Tests for the foundation-backed short-interest screen.

The acceptance criteria under test:

- a screen result exposes settlement date, as-of timestamp, source records,
  coverage/exclusions, and calculation version;
- changing the requested as_of cannot use facts with a later known_at (the
  as-of regression test: a later filing cannot affect an earlier ranking);
- rerunning the same screen is deterministic and creates no duplicates;
- only eligible, classified equity securities are ranked.
"""

import pytest

from app.analytics import screens
from app.normalization import finra as finra_norm
from app.normalization import sec as sec_norm
from app.storage import parquet

SETTLEMENT = "2026-08-14"


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _seed_tickers(data_root, tickers=("AAA", "BBB", "CCC")):
    payload = {
        str(i): {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}
        for i, (ticker, cik) in enumerate(zip(tickers, range(1, len(tickers) + 1)), start=0)
    }
    datasets = sec_norm.normalize_company_tickers(
        payload, retrieved_at="2026-08-21T12:00:00Z", content_hash="tickers-hash",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_facts(data_root, facts_by_cik):
    for cik, facts in facts_by_cik.items():
        payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": facts}},
        }}}
        datasets = sec_norm.normalize_company_facts(
            payload, retrieved_at="2026-08-21T12:00:00Z", content_hash=f"facts-{cik}",
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            source_record_id=f"cik{cik:010d}",
        )
        for name, rows in datasets.items():
            parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_short_interest(data_root, rows, known_at="2026-08-21T12:00:00Z"):
    datasets = finra_norm.normalize_short_interest_snapshot(
        rows, settlement_date=SETTLEMENT, known_at=known_at,
        retrieved_at=known_at, content_hash="snapshot-hash",
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
        "exclusions": {"unmapped_symbol": 0, "ambiguous_ticker_mapping": 0,
                       "not_classified_common_equity": 0, "invalid_short_interest": 0},
    }
    assert result["calculation_version"] == screens.SCREEN_CALC_VERSION
    assert result["as_of_date"] == SETTLEMENT
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
    date; a fact with a later period end is not eligible."""
    _seed_tickers(data_root)
    _seed_facts(data_root, {
        1: [{"end": "2026-09-01", "val": 100, "accn": "a1", "filed": "2026-09-02"}],
        2: [{"end": "2026-08-01", "val": 200, "accn": "b1", "filed": "2026-08-02"}],
        3: [{"end": "2026-08-01", "val": 10, "accn": "c1", "filed": "2026-08-02"}],
    })
    _seed_short_interest(data_root, _default_rows())

    result = screens.materialize_short_interest_screen(SETTLEMENT, data_root=data_root)

    assert result["coverage"]["exclusions"]["not_classified_common_equity"] == 1
    assert [e["ticker"] for e in result["entries"]] == ["CCC", "BBB"]


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
        "invalid_short_interest": 1,     # FFF
    }
    assert [e["ticker"] for e in result["entries"]] == ["CCC", "AAA", "BBB"]


def test_stale_settlement_is_surfaced(data_root):
    _seed_tickers(data_root)
    _seed_facts(data_root, _default_facts())
    stale_date = "2025-01-15"
    datasets = finra_norm.normalize_short_interest_snapshot(
        _default_rows(), settlement_date=stale_date, known_at="2025-01-20T12:00:00Z",
        retrieved_at="2025-01-20T12:00:00Z", content_hash="snapshot-hash-2",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id=f"otcMarket/consolidatedShortInterest:{stale_date}",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")

    stale = screens.materialize_short_interest_screen(stale_date, data_root=data_root)
    assert stale["data_freshness"] == "stale"
    assert stale["as_of_date"] == stale_date