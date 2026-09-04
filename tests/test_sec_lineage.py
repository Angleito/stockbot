"""Offline tests for app/sec/lineage.py (no network)."""

import pytest

import app.sec.lineage as lineage
from app.storage import parquet


def test_fact_lineage_projects_all_keys():
    row = {"concept": "Revenues", "value": 5.0, "extra": "dropped"}
    out = lineage.fact_lineage(row)
    assert sorted(out) == sorted(["concept", "value", "period_start",
                                  "period_end", "fiscal_year", "fiscal_period",
                                  "filed_at", "accession", "source_url",
                                  "known_at"])
    assert out["concept"] == "Revenues" and out["value"] == 5.0
    assert out["filed_at"] is None and "extra" not in out


def test_period_lineage_restatement():
    rows = [
        {"period_end": "2024-12-31", "value": 1.0, "filed_at": "2025-02-01",
         "known_at": "2025-02-02", "accession": "a1", "concept": "Revenues"},
        {"period_end": "2024-12-31", "value": 2.0, "filed_at": "2025-03-01",
         "known_at": "2025-03-02", "accession": "a2", "concept": "Revenues"},
    ]
    out = lineage.period_lineage(rows)
    assert len(out) == 1 and out[0]["restated"] is True
    assert out[0]["originally_reported"]["value"] == 1.0
    assert out[0]["latest"]["value"] == 2.0
    assert out[0]["originally_reported"] != out[0]["latest"]


def test_xbrl_lineage_as_of_excludes_restatement(tmp_path):
    rows = [
        {"fact_id": "f1", "entity_id": "E1", "concept": "Revenues",
         "value": 1.0, "period_end": "2024-12-31",
         "filed_at": "2025-02-01", "known_at": "2025-02-01",
         "accession": "a1"},
        {"fact_id": "f2", "entity_id": "E1", "concept": "Revenues",
         "value": 2.0, "period_end": "2024-12-31",
         "filed_at": "2025-03-01", "known_at": "2025-03-01",
         "accession": "a2"},
    ]
    assert parquet.write_rows("financial_facts", rows,
                              root=tmp_path / "parquet") == 2
    early = lineage.xbrl_lineage("E1", "Revenues", as_of="2025-02-15",
                                 root=tmp_path)
    assert len(early) == 1 and early[0]["value"] == 1.0
    assert lineage.period_lineage(early)[0]["restated"] is False
    assert len(lineage.xbrl_lineage("E1", "Revenues", root=tmp_path)) == 2
    with pytest.raises(ValueError):
        lineage.xbrl_lineage("E1", "Revenues", as_of="02/15/2025",
                             root=tmp_path)
