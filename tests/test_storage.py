"""Tests for the raw archive, Parquet datasets, and DuckDB query layer."""

import json
from decimal import Decimal

import pytest
import pyarrow as pa

from app.storage import duckdb, parquet, raw_archive
from app.domain.market import ids


@pytest.fixture
def archive_root(tmp_path):
    return tmp_path / "raw"


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _payload(text: str) -> bytes:
    return json.dumps({"content": text}).encode()


# ---------------------------------------------------------------------------
# Raw archive
# ---------------------------------------------------------------------------


def test_archive_stores_payload_and_manifest(archive_root):
    record = raw_archive.archive(
        "sec", "companyfacts", "cik0000320193", _payload("hello"),
        url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        retrieved_at="2026-08-21T12:00:00Z",
        metadata={"cik": "0000320193"},
        root=archive_root,
    )
    assert record.payload_path.is_file()
    assert record.manifest_path.is_file()
    assert record.sha256 == raw_archive.content_hash(_payload("hello"))
    assert record.size == len(_payload("hello"))
    assert record.url.endswith("CIK0000320193.json")
    assert record.metadata["cik"] == "0000320193"
    manifest = json.loads(record.manifest_path.read_text())
    assert manifest["sha256"] == record.sha256


def test_archive_is_immutable_and_idempotent(archive_root):
    first = raw_archive.archive("finra", "data", "otc/cycle", _payload("x"), url="u", root=archive_root)
    second = raw_archive.archive("finra", "data", "otc/cycle", _payload("x"), url="u", root=archive_root)
    assert first.payload_path == second.payload_path
    assert first.manifest_path == second.manifest_path
    assert {p for p in archive_root.rglob("*.json")} == {second.payload_path, second.manifest_path}


def test_archive_keeps_distinct_payload_revisions(archive_root):
    first = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v1"), url="u", root=archive_root)
    second = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v2"), url="u", root=archive_root)
    assert first.payload_path != second.payload_path
    revisions = list(raw_archive.iter_archive("sec", "companyfacts", "cik1", root=archive_root))
    assert [r.sha256 for r in revisions] == sorted(r.sha256 for r in revisions)


def test_find_and_has_payload(archive_root):
    record = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v1"), url="u", root=archive_root)
    assert raw_archive.find("sec", "companyfacts", "cik1", root=archive_root) == record
    assert raw_archive.find("sec", "companyfacts", "cik1", sha256=record.sha256, root=archive_root) == record
    assert raw_archive.find("sec", "companyfacts", "cik1", sha256="0" * 64, root=archive_root) is None
    assert raw_archive.find("sec", "companyfacts", "nope", root=archive_root) is None
    assert raw_archive.has_payload("sec", "companyfacts", "cik1", record.sha256, root=archive_root)
    assert not raw_archive.has_payload("sec", "companyfacts", "cik1", "0" * 64, root=archive_root)


# ---------------------------------------------------------------------------
# Parquet datasets
# ---------------------------------------------------------------------------


def _fact_row(entity_id="sec:cik:0000320193", value=100.0, period_end="2026-08-01",
              filed="2026-08-02", accession="0000320193-26-000001", known_at="2026-08-02T00:00:00Z"):
    cik = int(entity_id.removeprefix("sec:cik:"))
    return {
        "fact_id": ids.sec_fact_id(cik, accession, "EntityCommonStockSharesOutstanding", period_end, value),
        "entity_id": entity_id,
        "security_id": ids.sec_security_id(cik),
        "concept": "EntityCommonStockSharesOutstanding",
        "original_concept": "dei:EntityCommonStockSharesOutstanding",
        "value": value,
        "unit": "shares",
        "duration_type": "instant",
        "period_end": period_end,
        "filed_at": filed,
        "accession": accession,
        "frame": None,
        "known_at": known_at,
        "retrieved_at": "2026-08-21T12:00:00Z",
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "source_record_id": "cik0000320193",
        "content_hash": "abc",
        "parser_version": "financial-facts-v1",
    }


def test_parquet_roundtrip_and_hive_partitions(data_root):
    row = _fact_row()
    assert parquet.write_rows("financial_facts", [row], root=data_root / "parquet") == 1
    table = parquet.read_table("financial_facts", root=data_root / "parquet")
    assert table.num_rows == 1
    assert table.column("period_end").to_pylist() == ["2026-08-01"]
    partition_dir = data_root / "parquet" / "financial_facts" / "period_end_year=2026"
    assert partition_dir.is_dir()
    assert list(partition_dir.glob("*.parquet"))


def test_parquet_rerun_is_deterministic_no_duplicates(data_root):
    row = _fact_row()
    assert parquet.write_rows("financial_facts", [row], root=data_root / "parquet") == 1
    assert parquet.write_rows("financial_facts", [row], root=data_root / "parquet") == 0
    assert parquet.write_rows("financial_facts", [row], root=data_root / "parquet") == 0
    assert parquet.count_rows("financial_facts", root=data_root / "parquet") == 1


def test_parquet_unknown_dataset_rejected(data_root):
    with pytest.raises(ValueError, match="Unknown parquet dataset"):
        parquet.write_rows("nope", [{}], root=data_root / "parquet")


# ---------------------------------------------------------------------------
# Portfolio snapshot / position datasets
# ---------------------------------------------------------------------------


def _snapshot_row(snapshot_id="snap-001", broker="robinhood",
                  created_at="2026-08-25T12:00:00Z", cash=Decimal("1234.5"),
                  invested_value=Decimal("23456.78"), total_value=Decimal("24691.28"),
                  account_count=2, position_count=5, priced_position_count=4,
                  unresolved_position_count=1, source="robinhood-api",
                  parser_version="portfolio-parser-v1",
                  calculation_version="portfolio-calc-v1"):
    return {
        "snapshot_id": snapshot_id,
        "broker": broker,
        "created_at": created_at,
        "cash": cash,
        "invested_value": invested_value,
        "total_value": total_value,
        "account_count": account_count,
        "position_count": position_count,
        "priced_position_count": priced_position_count,
        "unresolved_position_count": unresolved_position_count,
        "source": source,
        "parser_version": parser_version,
        "calculation_version": calculation_version,
    }


def _position_row(snapshot_id="snap-001", position_id="pos-001", account_id="acc-001",
                  security_id="sec:cik:0000320193", entity_id="sec:cik:0000320193",
                  ticker="AAPL", quantity=Decimal("10.0"), average_cost=Decimal("150.25"),
                  market_price=Decimal("160.0"), price_type="last_trade",
                  market_value=Decimal("1600.0"), unrealized_gain=Decimal("97.5"),
                  unrealized_gain_pct=Decimal("0.0649"), portfolio_weight=Decimal("0.0648"),
                  source="robinhood-api", quote_retrieved_at="2026-08-25T12:00:00Z",
                  asset_type="equity"):
    return {
        "snapshot_id": snapshot_id,
        "position_id": position_id,
        "account_id": account_id,
        "security_id": security_id,
        "entity_id": entity_id,
        "ticker": ticker,
        "quantity": quantity,
        "average_cost": average_cost,
        "market_price": market_price,
        "price_type": price_type,
        "market_value": market_value,
        "unrealized_gain": unrealized_gain,
        "unrealized_gain_pct": unrealized_gain_pct,
        "portfolio_weight": portfolio_weight,
        "source": source,
        "quote_retrieved_at": quote_retrieved_at,
        "asset_type": asset_type,
    }


def test_portfolio_snapshot_roundtrip(data_root):
    row = _snapshot_row()
    assert parquet.write_rows("portfolio_snapshots", [row], root=data_root / "parquet") == 1
    table = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    assert table.num_rows == 1
    assert table.column("snapshot_id").to_pylist() == ["snap-001"]
    assert table.column("total_value").to_pylist() == [Decimal("24691.28")]
    assert table.column("created_at").to_pylist() == ["2026-08-25T12:00:00Z"]


def test_portfolio_position_roundtrip(data_root):
    row = _position_row()
    assert parquet.write_rows("portfolio_positions", [row], root=data_root / "parquet") == 1
    table = parquet.read_table("portfolio_positions", root=data_root / "parquet")
    assert table.num_rows == 1
    assert table.column("position_id").to_pylist() == ["pos-001"]
    assert table.column("ticker").to_pylist() == ["AAPL"]
    assert table.column("market_price").to_pylist() == [Decimal("160.0")]


def test_portfolio_snapshot_immutability(data_root):
    row = _snapshot_row()
    assert parquet.write_rows("portfolio_snapshots", [row], root=data_root / "parquet") == 1
    assert parquet.write_rows("portfolio_snapshots", [row], root=data_root / "parquet") == 0
    assert parquet.count_rows("portfolio_snapshots", root=data_root / "parquet") == 1
    second = _snapshot_row(snapshot_id="snap-002")
    assert parquet.write_rows("portfolio_snapshots", [second], root=data_root / "parquet") == 1
    table = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    assert set(table.column("snapshot_id").to_pylist()) == {"snap-001", "snap-002"}


def test_portfolio_positions_link_to_snapshot(data_root):
    parquet.write_rows("portfolio_snapshots", [_snapshot_row()], root=data_root / "parquet")
    positions = [
        _position_row(snapshot_id="snap-001", position_id="pos-001", ticker="AAPL"),
        _position_row(snapshot_id="snap-001", position_id="pos-002", ticker="MSFT"),
        _position_row(snapshot_id="snap-002", position_id="pos-003", ticker="TSLA"),
    ]
    assert parquet.write_rows("portfolio_positions", positions, root=data_root / "parquet") == 3
    rows = duckdb.query(
        "SELECT position_id, ticker FROM portfolio_positions WHERE snapshot_id = ?",
        ["snap-001"],
        data_root=data_root,
    )
    assert rows == [
        {"position_id": "pos-001", "ticker": "AAPL"},
        {"position_id": "pos-002", "ticker": "MSFT"},
    ]


def test_portfolio_datasets_are_unpartitioned(data_root):
    assert parquet.write_rows("portfolio_snapshots", [_snapshot_row()], root=data_root / "parquet") == 1
    assert parquet.write_rows("portfolio_positions", [_position_row()], root=data_root / "parquet") == 1
    snap_dir = data_root / "parquet" / "portfolio_snapshots" / "partition=none"
    pos_dir = data_root / "parquet" / "portfolio_positions" / "partition=none"
    assert snap_dir.is_dir() and list(snap_dir.glob("*.parquet"))
    assert pos_dir.is_dir() and list(pos_dir.glob("*.parquet"))
    assert parquet.read_table("portfolio_snapshots", root=data_root / "parquet").num_rows == 1
    assert parquet.read_table("portfolio_positions", root=data_root / "parquet").num_rows == 1


def test_portfolio_columns_are_typed(data_root):
    parquet.write_rows("portfolio_snapshots", [_snapshot_row()], root=data_root / "parquet")
    parquet.write_rows("portfolio_positions", [_position_row()], root=data_root / "parquet")
    snap = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    assert snap.schema.field("snapshot_id").type == pa.string()
    assert snap.schema.field("cash").type == pa.decimal128(38, 14)
    assert snap.schema.field("total_value").type == pa.decimal128(38, 14)
    assert snap.schema.field("account_count").type == pa.int64()
    assert snap.schema.field("position_count").type == pa.int64()
    assert snap.column("account_count").to_pylist() == [2]
    assert snap.column("cash").to_pylist() == [Decimal("1234.5")]
    assert snap.column("broker").to_pylist() == ["robinhood"]
    pos = parquet.read_table("portfolio_positions", root=data_root / "parquet")
    assert pos.schema.field("position_id").type == pa.string()
    assert pos.schema.field("quantity").type == pa.decimal128(38, 8)
    assert pos.schema.field("market_price").type == pa.decimal128(38, 6)
    assert pos.schema.field("unrealized_gain").type == pa.decimal128(38, 14)
    assert pos.schema.field("asset_type").type == pa.string()
    assert pos.column("quantity").to_pylist() == [Decimal("10.0")]
    assert pos.column("ticker").to_pylist() == ["AAPL"]
    assert pos.column("price_type").to_pylist() == ["last_trade"]


def test_portfolio_schemas_have_no_oauth_columns():
    forbidden = ("token", "oauth", "secret", "access", "refresh", "authorization")
    for name in ("portfolio_snapshots", "portfolio_positions"):
        ds = parquet.DATASETS[name]
        for field in ds.schema:
            assert not any(part in field.name.lower() for part in forbidden), (
                f"{name}.{field.name} is a forbidden OAuth column"
            )


def test_portfolio_empty_read_returns_empty_table(data_root):
    for name in ("portfolio_snapshots", "portfolio_positions"):
        table = parquet.read_table(name, root=data_root / "parquet")
        assert table.num_rows == 0
        assert table.schema == parquet.DATASETS[name].schema


# ---------------------------------------------------------------------------
# DuckDB query layer and as-of enforcement
# ---------------------------------------------------------------------------


def test_duckdb_query_returns_rows_as_dicts(data_root):
    parquet.write_rows("financial_facts", [_fact_row(), _fact_row(value=200.0, period_end="2026-08-15", filed="2026-08-16", known_at="2026-08-16T00:00:00Z", accession="0000320193-26-000002")], root=data_root / "parquet")
    rows = duckdb.query("SELECT concept, value FROM financial_facts ORDER BY value", data_root=data_root)
    assert rows == [
        {"concept": "EntityCommonStockSharesOutstanding", "value": 100.0},
        {"concept": "EntityCommonStockSharesOutstanding", "value": 200.0},
    ]


def _as_of_rows(sql: str, as_of: str, params=(), data_root=None) -> list[dict]:
    clause, param = duckdb.as_of_clause(as_of)
    return duckdb.query(
        f"SELECT * FROM ({sql}) AS _pt WHERE {clause}",
        (*params, param),
        data_root=data_root,
    )


def test_as_of_blocks_later_known_at(data_root):
    early = _fact_row(value=100.0, period_end="2026-08-01", filed="2026-08-02", known_at="2026-08-02T00:00:00Z")
    late = _fact_row(value=300.0, period_end="2026-08-10", filed="2026-08-20", known_at="2026-08-20T00:00:00Z", accession="0000320193-26-000002")
    parquet.write_rows("financial_facts", [early, late], root=data_root / "parquet")

    rows = _as_of_rows(
        "SELECT * FROM financial_facts WHERE entity_id = ?",
        as_of="2026-08-14",
        params=["sec:cik:0000320193"],
        data_root=data_root,
    )
    assert [r["value"] for r in rows] == [100.0]
    rows = _as_of_rows(
        "SELECT * FROM financial_facts WHERE entity_id = ?",
        as_of="2026-08-21",
        params=["sec:cik:0000320193"],
        data_root=data_root,
    )
    assert {r["value"] for r in rows} == {100.0, 300.0}


def test_as_of_includes_facts_filed_on_the_as_of_date(data_root):
    row = _fact_row(value=100.0, filed="2026-08-14", known_at="2026-08-14T00:00:00Z")
    parquet.write_rows("financial_facts", [row], root=data_root / "parquet")
    rows = _as_of_rows("SELECT * FROM financial_facts", as_of="2026-08-14", data_root=data_root)
    assert [r["value"] for r in rows] == [100.0]


def test_as_of_timestamp_granularity(data_root):
    row = _fact_row(value=100.0, filed="2026-08-14", known_at="2026-08-14T09:30:00Z")
    parquet.write_rows("financial_facts", [row], root=data_root / "parquet")
    before = _as_of_rows("SELECT * FROM financial_facts", as_of="2026-08-14T09:00:00Z", data_root=data_root)
    at = _as_of_rows("SELECT * FROM financial_facts", as_of="2026-08-14T09:30:00Z", data_root=data_root)
    assert before == []
    assert [r["value"] for r in at] == [100.0]

def test_events_evidence_registry_roundtrip(data_root):
    """CorporateEvent/Evidence datasets dedup on rerun."""
    event_row = {
        "event_id": "sec:event:NVDA:0123456789abcdef",
        "entity_id": "sec:cik:0001045810",
        "security_id": None,
        "ticker": "NVDA",
        "event_type": "supply_commitments",
        "amount_billions": 119.0,
        "certainty": "contingent",
        "status": "future_cash_obligation",
        "revenue_matched": True,
        "default_triggered": False,
        "fiscal_year": None,
        "filed_at": "2026-02-25",
        "known_at": "2026-02-25",
        "retrieved_at": "2026-08-26T12:00:00Z",
        "accession": None,
        "source": "SEC EDGAR test",
        "source_url": "https://www.sec.gov/",
        "content_hash": "h1",
        "parser_version": "obligations-v2",
    }
    evidence_row = {
        "evidence_id": "sec:evidence:abcdef0123456789",
        "event_id": event_row["event_id"],
        "source_type": "filing_text",
        "archive_key": "filing-text:NVDA:2026-02-25:0001",
        "content_hash": "h1",
        "excerpt": "supply commitments were $119 billion",
        "span_start": 10,
        "span_end": 45,
        "retrieved_at": "2026-08-26T12:00:00Z",
        "parser_version": "obligations-v2",
    }
    root = data_root / "parquet"
    assert parquet.write_rows("events", [event_row], root=root) == 1
    assert parquet.write_rows("events", [event_row], root=root) == 0
    assert parquet.write_rows("evidence", [evidence_row], root=root) == 1
    assert parquet.write_rows("evidence", [evidence_row], root=root) == 0

    events = parquet.read_table("events", root=root)
    assert events.num_rows == 1
    assert events.column("event_id").to_pylist() == ["sec:event:NVDA:0123456789abcdef"]
    assert events.column("amount_billions").to_pylist() == [119.0]
    assert events.column("revenue_matched").to_pylist() == [True]
    evidence = parquet.read_table("evidence", root=root)
    assert evidence.num_rows == 1
    assert evidence.column("span_start").to_pylist() == [10]
    assert evidence.column("span_end").to_pylist() == [45]
