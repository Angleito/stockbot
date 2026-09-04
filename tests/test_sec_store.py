"""Offline tests for the SEC raw archive + normalized sec_filings store."""

from pathlib import Path

import pytest

from app.sec.archive import archive_sec_filing, find_archived
from app.sec.models import Filing
from app.sec.store import query_filings, store_filing
from app.storage import raw_archive

URL = "https://www.sec.gov/Archives/edgar/data/1234567/000000000025000001/"


def _filing(accession, form="10-K", filed_at="2024-02-01", known_at="2024-02-01",
            amendment_of=None, is_amendment=False):
    return Filing(
        accession_no=accession,
        form=form,
        cik=1234567,
        company="Test Co",
        filed_at=filed_at,
        accepted_at=f"{filed_at}T00:00:00Z",
        known_at=known_at,
        report_period="2023-12-31",
        primary_document="test-10k.htm",
        is_amendment=is_amendment,
        amendment_of=amendment_of,
        issuer_cik=1234567,
        source=URL,
    )


def test_rearchive_same_bytes_is_idempotent(tmp_path):
    raw_root = tmp_path / "raw"
    filing = _filing("0000000000-25-000001")
    payloads = {"primary": b"<html>hi</html>", "submission": b'{"x": 1}'}

    first = archive_sec_filing(filing, payloads, url=URL, root=raw_root)
    second = archive_sec_filing(filing, payloads, url=URL, root=raw_root)

    assert first["primary"].sha256 == second["primary"].sha256
    assert first["primary"].sha256 == raw_archive.content_hash(b"<html>hi</html>")
    assert Path(first["primary"].payload_path).read_bytes() == b"<html>hi</html>"

    found = find_archived("0000000000-25-000001", "primary", root=raw_root)
    assert found is not None and found.sha256 == first["primary"].sha256
    assert find_archived("0000000000-25-000001", "primary", root=tmp_path / "elsewhere") is None


def test_store_two_accessions_and_point_in_time(tmp_path):
    raw_root = tmp_path / "raw"
    original = _filing("0000000000-25-000001", filed_at="2024-02-01", known_at="2024-02-01")
    amendment = _filing("0000000000-25-000002", form="10-K/A", filed_at="2024-03-01",
                        known_at="2024-03-01", is_amendment=True,
                        amendment_of="0000000000-25-000001")

    recs = archive_sec_filing(original, {"primary": b"v1"}, url=URL, root=raw_root)
    assert store_filing(original, raw_primary_path=recs["primary"].payload_path,
                         root=tmp_path) == 1
    # Deterministic rerun writes nothing.
    assert store_filing(original, raw_primary_path=recs["primary"].payload_path,
                         root=tmp_path) == 0

    recs_a = archive_sec_filing(amendment, {"primary": b"v2"}, url=URL, root=raw_root)
    assert store_filing(amendment, raw_primary_path=recs_a["primary"].payload_path,
                         root=tmp_path) == 1

    rows = query_filings(root=tmp_path)
    assert [r["accession"] for r in rows] == [
        "0000000000-25-000002", "0000000000-25-000001"]  # newest first
    assert rows[0]["amendment_of"] == "0000000000-25-000001"
    assert rows[0]["is_amendment"] is True

    earlier_only = query_filings(as_of="2024-02-15", root=tmp_path)
    assert [r["accession"] for r in earlier_only] == ["0000000000-25-000001"]

    assert len(query_filings(cik=1234567, forms=["10-K"], root=tmp_path)) == 1


def test_query_filings_rejects_non_date_as_of(tmp_path):
    with pytest.raises(ValueError):
        query_filings(as_of="recently", root=tmp_path)
    with pytest.raises(ValueError):
        query_filings(as_of="2024/02/01", root=tmp_path)
