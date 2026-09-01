"""Tests for the portfolio research view (SEC + FINRA enrichment).

Offline by construction: normalized rows are seeded into a tmp_path parquet
store via ``parquet.write_rows`` and every query runs through
``duckdb.query(data_root=tmp_path/"data")``, mirroring the screens test
seeding pattern.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.portfolio import PortfolioSnapshot, Position
from app.services.portfolio_research import (
    SEC_CONCEPTS,
    PortfolioResearchPosition,
    enrich_portfolio_research,
)
from app.storage import parquet

ENTITY_ID = "sec:cik:0000320193"
SECURITY_ID = "sec:equity:0000320193"
RETRIEVED_AT = "2026-08-25T12:00:00Z"
FINRA_SOURCE_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _fact_source_url(entity_id: str = ENTITY_ID) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/{entity_id.split(':')[-1]}.json"


def _seed_entity(data_root: Path, entity_id: str = ENTITY_ID) -> None:
    parquet.write_rows("entities", [{
        "entity_id": entity_id,
        "name": "Advanced Micro Devices, Inc.",
        "entity_type": "unknown",
        "sic": None,
        "source": "test",
        "known_at": "2026-08-01T00:00:00Z",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "content_hash": "entities-hash",
        "parser_version": "test-v1",
    }], root=data_root / "parquet")


def _seed_alias(data_root: Path, alias_value: str = "AMD", entity_id: str = ENTITY_ID) -> None:
    parquet.write_rows("entity_aliases", [{
        "alias_type": "ticker",
        "alias_value": alias_value,
        "entity_id": entity_id,
        "security_id": SECURITY_ID,
        "source": "test",
        "valid_from": None,
        "valid_to": None,
        "known_at": "2026-08-01T00:00:00Z",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "content_hash": "alias-hash",
        "parser_version": "test-v1",
    }], root=data_root / "parquet")


def _seed_security(data_root: Path) -> None:
    parquet.write_rows("securities", [{
        "security_id": SECURITY_ID,
        "entity_id": ENTITY_ID,
        "security_type": "equity-common",
        "ticker": None,
        "exchange": None,
        "source": "test",
        "known_at": "2026-08-01T00:00:00Z",
        "retrieved_at": "2026-08-01T00:00:00Z",
        "content_hash": "security-hash",
        "parser_version": "test-v1",
    }], root=data_root / "parquet")


def _seed_fact(
    data_root: Path,
    concept: str,
    value: float,
    period_end: str,
    filed_at: str,
    accession: str,
    entity_id: str = ENTITY_ID,
) -> None:
    parquet.write_rows("financial_facts", [{
        "fact_id": f"sec:fact:{entity_id}:{concept}:{accession}",
        "entity_id": entity_id,
        "security_id": SECURITY_ID,
        "concept": concept,
        "original_concept": concept,
        "value": value,
        "unit": "shares" if concept == "EntityCommonStockSharesOutstanding" else "USD",
        "duration_type": "instant" if concept == "EntityCommonStockSharesOutstanding" else "duration",
        "period_end": period_end,
        "filed_at": filed_at,
        "accession": accession,
        "frame": None,
        "known_at": filed_at,
        "retrieved_at": RETRIEVED_AT,
        "source_url": _fact_source_url(entity_id),
        "source_record_id": "cik0000320193",
        "content_hash": f"facts-{concept}-{accession}",
        "parser_version": "test-v1",
    }], root=data_root / "parquet")


def _seed_short_interest(
    data_root: Path,
    settlement_date: str,
    short_position: float,
    known_at: str,
    prev_position: float | None = None,
    avg_daily_volume: float | None = None,
    days_to_cover: float | None = None,
    symbol: str = "AMD",
    content_hash: str = "finra-hash",
) -> None:
    parquet.write_rows("short_interest", [{
        "row_id": f"finra:row:{settlement_date}:{symbol}:{content_hash[:12]}",
        "entity_id": None,
        "security_id": None,
        "symbol_code": symbol,
        "issue_name": "Advanced Micro Devices, Inc.",
        "settlement_date": settlement_date,
        "short_position": short_position,
        "prev_position": prev_position,
        "avg_daily_volume": avg_daily_volume,
        "days_to_cover": days_to_cover,
        "source_url": FINRA_SOURCE_URL,
        "source_record_id": f"otcMarket/consolidatedShortInterest:{settlement_date}",
        "known_at": known_at,
        "retrieved_at": known_at,
        "content_hash": content_hash,
        "parser_version": "test-v1",
    }], root=data_root / "parquet")


def _position(
    position_id: str = "pos-1",
    entity_id: str | None = ENTITY_ID,
    security_id: str | None = SECURITY_ID,
    ticker: str = "AMD",
) -> Position:
    return Position(
        position_id=position_id,
        account_id="acc-1",
        security_id=security_id,
        entity_id=entity_id,
        ticker=ticker,
        quantity=Decimal("10"),
        average_cost=Decimal("100"),
        market_price=Decimal("110"),
        market_value=Decimal("1100"),
        unrealized_gain=Decimal("100"),
        unrealized_gain_pct=Decimal("10"),
        portfolio_weight=Decimal("0.05"),
        source="test",
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )


def _snapshot(positions: list[Position]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_id="snap-1",
        created_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        broker="test",
        account_ids=("acc-1",),
        cash=Decimal("0"),
        invested_value=None,
        total_value=None,
        positions=tuple(positions),
    )


# ---------------------------------------------------------------------------
# Cross-source integration (spec §30)
# ---------------------------------------------------------------------------


def test_cross_source_integration_enriches_resolved_position(data_root):
    _seed_entity(data_root)
    _seed_alias(data_root)
    _seed_security(data_root)
    _seed_fact(data_root, "Revenue", 5_860_000_000.0, "2026-06-30", "2026-08-05", "accn-rev-1")
    _seed_fact(data_root, "Revenue", 5_890_000_000.0, "2026-06-30", "2026-08-20", "accn-rev-2")
    _seed_fact(data_root, "NetIncomeLoss", 265_000_000.0, "2026-06-30", "2026-08-05", "accn-ni-1")
    _seed_fact(data_root, "CashAndCashEquivalents", 4_100_000_000.0, "2026-06-30", "2026-08-05", "accn-cash-1")
    _seed_fact(data_root, "LongTermDebt", 2_300_000_000.0, "2026-06-30", "2026-08-05", "accn-debt-1")
    _seed_fact(data_root, "EntityCommonStockSharesOutstanding", 1_610_000_000.0, "2026-07-01", "2026-08-06", "accn-shares-1")
    _seed_short_interest(
        data_root, settlement_date="2026-08-07", short_position=1_000_000,
        prev_position=950_000, known_at="2026-08-10T12:00:00Z",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        prev_position=1_000_000, known_at="2026-08-17T12:00:00Z",
        content_hash="finra-v1-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=1_000_000, avg_daily_volume=38_000_000, days_to_cover=2.3,
        known_at="2026-08-20T12:00:00Z", content_hash="finra-v2-hash",
    )

    position = _position()
    results = enrich_portfolio_research(_snapshot([position]), data_root=data_root)

    assert len(results) == 1
    research = results[0]
    assert isinstance(research, PortfolioResearchPosition)
    assert research.position is position

    sec = research.latest_sec_metrics
    assert set(sec) == set(SEC_CONCEPTS)
    assert sec["Revenue"] == {
        "value": Decimal("5890000000"),
        "period_end": "2026-06-30",
        "filed_at": "2026-08-20",
        "accession": "accn-rev-2",
        "source_url": _fact_source_url(),
    }
    assert sec["NetIncomeLoss"]["value"] == Decimal("265000000")
    assert sec["CashAndCashEquivalents"]["value"] == Decimal("4100000000")
    assert sec["LongTermDebt"]["value"] == Decimal("2300000000")
    assert sec["EntityCommonStockSharesOutstanding"]["value"] == Decimal("1610000000")

    finra = research.latest_finra_metrics
    assert finra == {
        "short_position": Decimal("1150000"),
        "prev_position": Decimal("1000000"),
        "short_interest_change": Decimal("150000"),
        "short_interest_change_pct": Decimal("15"),
        "days_to_cover": Decimal("2.3"),
        "settlement_date": "2026-08-14",
        "avg_daily_volume": Decimal("38000000"),
        "known_at": "2026-08-20T12:00:00Z",
    }

    freshness = research.research_data_freshness
    assert freshness == {
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "sec_latest_filed_at": date(2026, 8, 20),
        "finra_settlement_date": date(2026, 8, 14),
        "finra_known_at": "2026-08-20T12:00:00Z",
    }


# ---------------------------------------------------------------------------
# Unresolved positions
# ---------------------------------------------------------------------------


def test_unresolved_position_gets_empty_sec_and_symbol_based_finra(data_root):
    _seed_entity(data_root)
    _seed_fact(data_root, "Revenue", 5_860_000_000.0, "2026-06-30", "2026-08-05", "accn-rev-1")
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=1_000_000, known_at="2026-08-20T12:00:00Z",
    )

    resolved = _position()
    unresolved = _position(position_id="pos-2", entity_id=None, security_id=None)
    results = enrich_portfolio_research(_snapshot([resolved, unresolved]), data_root=data_root)

    assert [r.position.position_id for r in results] == ["pos-1", "pos-2"]
    assert set(results[0].latest_sec_metrics) == {"Revenue"}
    assert results[1].latest_sec_metrics == {}
    assert results[1].latest_finra_metrics["short_position"] == Decimal("1150000")
    assert results[1].research_data_freshness["sec_latest_filed_at"] is None
    assert results[1].research_data_freshness["finra_settlement_date"] == date(2026, 8, 14)
    assert results[1].research_data_freshness["finra_known_at"] == "2026-08-20T12:00:00Z"


# ---------------------------------------------------------------------------
# As-of regression (spec §29/§30)
# ---------------------------------------------------------------------------


def test_as_of_regression_facts_after_as_of_are_excluded(data_root):
    _seed_entity(data_root)
    _seed_fact(data_root, "Revenue", 5_860_000_000.0, "2026-06-30", "2026-08-05", "accn-rev-1")
    _seed_fact(data_root, "Revenue", 5_890_000_000.0, "2026-06-30", "2026-08-20", "accn-rev-2")
    _seed_fact(data_root, "LongTermDebt", 2_300_000_000.0, "2026-06-30", "2026-08-30", "accn-debt-1")

    position = _position()
    early = enrich_portfolio_research(_snapshot([position]), as_of=date(2026, 8, 14), data_root=data_root)[0]
    assert early.latest_sec_metrics["Revenue"] == {
        "value": Decimal("5860000000"),
        "period_end": "2026-06-30",
        "filed_at": "2026-08-05",
        "accession": "accn-rev-1",
        "source_url": _fact_source_url(),
    }
    assert "LongTermDebt" not in early.latest_sec_metrics
    assert early.research_data_freshness == {
        "as_of": "2026-08-14",
        "sec_latest_filed_at": date(2026, 8, 5),
        "finra_settlement_date": None,
        "finra_known_at": None,
    }

    later = enrich_portfolio_research(_snapshot([position]), as_of=date(2026, 8, 25), data_root=data_root)[0]
    assert later.latest_sec_metrics["Revenue"]["value"] == Decimal("5890000000")
    assert later.latest_sec_metrics["Revenue"]["accession"] == "accn-rev-2"
    assert "LongTermDebt" not in later.latest_sec_metrics
    assert later.research_data_freshness["sec_latest_filed_at"] == date(2026, 8, 20)

    future = enrich_portfolio_research(_snapshot([position]), as_of=date(2026, 9, 5), data_root=data_root)[0]
    assert future.latest_sec_metrics["LongTermDebt"]["value"] == Decimal("2300000000")


# ---------------------------------------------------------------------------
# FINRA newest-version semantics and missing values
# ---------------------------------------------------------------------------


def test_finra_newest_version_wins_per_symbol(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        prev_position=1_000_000, known_at="2026-08-17T12:00:00Z", content_hash="finra-v1-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=1_000_000, known_at="2026-08-20T12:00:00Z", content_hash="finra-v2-hash",
    )

    research = enrich_portfolio_research(_snapshot([_position(entity_id=None)]), data_root=data_root)[0]
    assert research.latest_finra_metrics["short_position"] == Decimal("1150000")
    assert research.latest_finra_metrics["settlement_date"] == "2026-08-14"
    assert research.latest_finra_metrics["known_at"] == "2026-08-20T12:00:00Z"

def test_finra_mixed_offset_newest_version_wins_per_symbol(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        prev_position=1_000_000, known_at="2026-08-17T13:00:00+01:00", content_hash="finra-v1-mixed-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=1_000_000, known_at="2026-08-17T12:30:00Z", content_hash="finra-v2-hash",
    )

    research = enrich_portfolio_research(_snapshot([_position(entity_id=None)]), data_root=data_root)[0]

    # 13:00+01:00 (= 12:00Z) sorts first lexically but is chronologically
    # older than 12:30Z — the 12:30Z (v2) values must win.
    assert research.latest_finra_metrics["short_position"] == Decimal("1150000")
    assert research.latest_finra_metrics["known_at"] == "2026-08-17T12:30:00Z"


def test_finra_same_instant_conflicting_versions_empty(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        prev_position=1_000_000, known_at="2026-08-17T12:00:00Z", content_hash="finra-c1-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=1_000_000, known_at="2026-08-17T12:00:00Z", content_hash="finra-c2-hash",
    )
    research = enrich_portfolio_research(_snapshot([_position(entity_id=None)]), data_root=data_root)[0]
    assert research.latest_finra_metrics == {}


def test_finra_older_settlement_correction_does_not_beat_newer_settlement(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        known_at="2026-08-20T12:00:00Z", content_hash="finra-a14-v1-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-29", short_position=900_000,
        known_at="2026-09-02T12:00:00Z", content_hash="finra-a29-v1-hash",
    )
    # Correction to the OLDER settlement, learned after the Aug 29 cycle:
    # must not replace the newer settlement's metrics.
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        known_at="2026-09-03T12:00:00Z", content_hash="finra-a14-v2-hash",
    )
    research = enrich_portfolio_research(
        _snapshot([_position(entity_id=None)]), as_of=date(2026, 9, 5), data_root=data_root
    )[0]
    assert research.latest_finra_metrics["settlement_date"] == "2026-08-29"
    assert research.latest_finra_metrics["short_position"] == Decimal("900000")
    assert research.latest_finra_metrics["known_at"] == "2026-09-02T12:00:00Z"


def test_finra_same_instant_ingestion_across_settlements_is_not_conflict(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_200_000,
        known_at="2026-09-01T12:00:00Z", content_hash="finra-a14-hash",
    )
    _seed_short_interest(
        data_root, settlement_date="2026-08-29", short_position=900_000,
        known_at="2026-09-01T12:00:00Z", content_hash="finra-a29-hash",
    )
    research = enrich_portfolio_research(
        _snapshot([_position(entity_id=None)]), as_of=date(2026, 9, 5), data_root=data_root
    )[0]
    # Same instant, two different settlements: NOT a conflict — the newer
    # settlement wins with real metrics.
    assert research.latest_finra_metrics["settlement_date"] == "2026-08-29"
    assert research.latest_finra_metrics["short_position"] == Decimal("900000")



def test_no_data_reports_empty_metrics_without_raising(data_root):
    position = _position(position_id="pos-1", entity_id=None, security_id=None, ticker="NODATA")
    research = enrich_portfolio_research(_snapshot([position]), data_root=data_root)[0]

    assert research.latest_sec_metrics == {}
    assert research.latest_finra_metrics == {}
    assert research.research_data_freshness == {
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "sec_latest_filed_at": None,
        "finra_settlement_date": None,
        "finra_known_at": None,
    }
    assert enrich_portfolio_research(_snapshot([]), data_root=data_root) == []


def test_missing_values_are_none_never_zero(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=1_150_000,
        prev_position=None, avg_daily_volume=None, days_to_cover=None,
        known_at="2026-08-20T12:00:00Z",
    )

    finra = enrich_portfolio_research(_snapshot([_position(entity_id=None)]), data_root=data_root)[0].latest_finra_metrics

    assert finra["short_position"] == Decimal("1150000")
    assert finra["prev_position"] is None
    assert finra["avg_daily_volume"] is None
    assert finra["days_to_cover"] is None
    assert finra["short_interest_change"] is None
    assert finra["short_interest_change_pct"] is None


def test_change_pct_is_none_when_prev_is_zero(data_root):
    _seed_short_interest(
        data_root, settlement_date="2026-08-14", short_position=100,
        prev_position=0, known_at="2026-08-20T12:00:00Z",
    )

    finra = enrich_portfolio_research(_snapshot([_position(entity_id=None)]), data_root=data_root)[0].latest_finra_metrics

    assert finra["short_interest_change"] == Decimal("100")
    assert finra["short_interest_change_pct"] is None