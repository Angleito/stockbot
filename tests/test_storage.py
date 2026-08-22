"""Tests for the raw archive, Parquet datasets, and DuckDB query layer."""

import json

import pytest

from app.storage import duckdb, ids, parquet, raw_archive


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