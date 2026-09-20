"""Tests for the raw archive store and warehouse-removal gates."""

import importlib
import json
from pathlib import Path

import pytest

from app.storage import raw_archive


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "raw"


def _payload(text: str) -> bytes:
    return json.dumps({"content": text}).encode()


# ---------------------------------------------------------------------------
# Raw archive
# ---------------------------------------------------------------------------


def test_archive_stores_payload_and_manifest(archive_root: Path):
    record = raw_archive.archive(
        "sec",
        "companyfacts",
        "cik0000320193",
        _payload("hello"),
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


def test_archive_is_immutable_and_idempotent(archive_root: Path):
    first = raw_archive.archive("finra", "data", "otc/cycle", _payload("x"), url="u", root=archive_root)
    second = raw_archive.archive("finra", "data", "otc/cycle", _payload("x"), url="u", root=archive_root)
    assert first.payload_path == second.payload_path
    assert first.manifest_path == second.manifest_path
    assert {p for p in archive_root.rglob("*.json")} == {second.payload_path, second.manifest_path}


def test_archive_keeps_distinct_payload_revisions(archive_root: Path):
    first = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v1"), url="u", root=archive_root)
    second = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v2"), url="u", root=archive_root)
    assert first.payload_path != second.payload_path
    revisions = list(raw_archive.iter_archive("sec", "companyfacts", "cik1", root=archive_root))
    assert [r.sha256 for r in revisions] == sorted(r.sha256 for r in revisions)


def test_find_and_has_payload(archive_root: Path):
    record = raw_archive.archive("sec", "companyfacts", "cik1", _payload("v1"), url="u", root=archive_root)
    assert raw_archive.find("sec", "companyfacts", "cik1", root=archive_root) == record
    assert raw_archive.find("sec", "companyfacts", "cik1", sha256=record.sha256, root=archive_root) == record
    assert raw_archive.find("sec", "companyfacts", "cik1", sha256="0" * 64, root=archive_root) is None
    assert raw_archive.find("sec", "companyfacts", "nope", root=archive_root) is None
    assert raw_archive.has_payload("sec", "companyfacts", "cik1", record.sha256, root=archive_root)
    assert not raw_archive.has_payload("sec", "companyfacts", "cik1", "0" * 64, root=archive_root)


# ---------------------------------------------------------------------------
# Warehouse-removal gates
# ---------------------------------------------------------------------------


def test_migration_warehouse_modules_removed() -> None:
    """The warehouse query layer is gone; live providers serve reads now."""
    with pytest.raises(ImportError):
        importlib.import_module("app.storage.parquet")
    with pytest.raises(ImportError):
        importlib.import_module("app.storage.duckdb")


def test_migration_sec_facts_live_assembly() -> None:
    """sec_facts still exposes its live assembly helpers."""
    from app.services import sec_facts as _sec_facts

    assert _sec_facts._assemble_eps_payload is not None
    assert _sec_facts._store_rows is not None
