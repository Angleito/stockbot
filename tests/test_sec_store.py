"""Offline tests for the SEC live read seam (raw archive + typed filing reads)."""

from pathlib import Path

import pytest

from app.sec.archive import archive_sec_filing, find_archived
from app.sec.models import Filing
from app.sec.store import query_filings
from app.storage import raw_archive

URL = "https://www.sec.gov/Archives/edgar/data/1234567/000000000025000001/"


def _filing(
    accession: str,
    form: str = "10-K",
    filed_at: str = "2024-02-01",
    known_at: str = "2024-02-01",
    amendment_of: str | None = None,
    is_amendment: bool = False,
) -> Filing:
    return Filing(
        accession_no=accession,
        form=form,
        filer_cik=1234567,
        filer_name="Test Co",
        filed_at=filed_at,
        accepted_at=f"{filed_at}T00:00:00Z",
        known_at=known_at,
        report_period="2023-12-31",
        primary_document="test-10k.htm",
        is_amendment=is_amendment,
        amendment_of=amendment_of,
        subject_cik=1234567,
        subject_name="Test Co",
        source=URL,
    )


def _stub_live_list(filings: list[Filing]):
    """app.sec.filings.list_sec_filings double: PIT-gate then limit (no warehouse)."""
    def _fake_list(
        cik: object,
        forms: object = None,
        start_date: object = None,
        end_date: object = None,
        as_of: object = None,
        limit: object = 50,
    ) -> list[Filing]:
        del cik, forms, start_date, end_date
        bound = str(as_of) if isinstance(as_of, str) else None
        kept = [f for f in filings if bound is None or f.known_at[:10] <= bound]
        if isinstance(limit, int):
            return kept[:limit]
        return kept

    return _fake_list


def test_rearchive_same_bytes_is_idempotent(tmp_path: Path) -> None:
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


def test_unseeded_queries_are_empty(tmp_path: Path) -> None:
    """No persisted universe: unseeded live queries stay empty (providers authoritative)."""
    assert query_filings(root=tmp_path) == []
    assert query_filings(cik=1234567, forms=["10-K"], root=tmp_path) == []


def test_query_filings_gateway_pit_and_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    original = _filing("0000000000-25-000001", filed_at="2024-02-01", known_at="2024-02-01")
    amendment = _filing(
        "0000000000-25-000002",
        form="10-K/A",
        filed_at="2024-03-01",
        known_at="2024-03-01",
        is_amendment=True,
        amendment_of="0000000000-25-000001",
    )
    monkeypatch.setattr(
        "app.sec.filings.list_sec_filings", _stub_live_list([original, amendment])
    )

    rows = query_filings(cik=1234567, root=tmp_path)
    assert [f.accession_no for f in rows] == ["0000000000-25-000001", "0000000000-25-000002"]
    assert rows[1].amendment_of == "0000000000-25-000001"
    assert rows[1].is_amendment is True

    earlier_only = query_filings(cik=1234567, as_of="2024-02-15", root=tmp_path)
    assert [f.accession_no for f in earlier_only] == ["0000000000-25-000001"]

    assert query_filings(cik=1234567, forms=["10-K"], root=tmp_path, limit=1) == [rows[0]]


def test_query_filings_rejects_non_date_as_of(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        query_filings(as_of="recently", root=tmp_path)
    with pytest.raises(ValueError):
        query_filings(as_of="2024/02/01", root=tmp_path)


def test_archive_document_revisions_retained(tmp_path: Path) -> None:
    import warnings

    from app.sec.archive import archive_sec_document, find_archived_document
    from app.storage import raw_archive

    root = tmp_path / "raw"
    acc, doc = "0000000000-25-000001", "primary.htm"
    first = archive_sec_document(acc, doc, b"v1", url=URL, root=root)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        second = archive_sec_document(acc, doc, b"v2", url=URL, root=root)
    assert first.sha256 != second.sha256
    from app.sec.archive import DOCUMENT_KIND, _document_key

    revisions = list(raw_archive.iter_archive("sec", DOCUMENT_KIND, _document_key(acc, doc), root=root))
    assert {r.sha256 for r in revisions} == {first.sha256, second.sha256}
    assert find_archived_document(acc, doc, root=root) is not None
    # Identical bytes re-archive cleanly (no new revision, no warning).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        again = archive_sec_document(acc, doc, b"v2", url=URL, root=root)
    assert again.sha256 == second.sha256




def test_coverage_reads_derive_from_jobs(tmp_path: Path) -> None:
    """Coverage derives from the job ledger; no persisted coverage table remains."""
    from app.sec.store import (
        complete_job,
        enqueue_backfill_job,
        get_job,
        is_partition_covered,
        query_coverage,
    )

    assert not is_partition_covered("sec-global", "10-K", "2024-01-01:2024-03-31", root=tmp_path)
    assert query_coverage(source="sec-global", form="10-K", root=tmp_path) == []

    job_id = enqueue_backfill_job("sec-global", "10-K", "2024-01-01", "2024-03-31", root=tmp_path)
    assert job_id and get_job(job_id, root=tmp_path) is not None
    assert query_coverage(source="sec-global", form="10-K", root=tmp_path) == []
    assert complete_job(job_id, root=tmp_path) is not None
    assert is_partition_covered("sec-global", "10-K", "2024-01-01:2024-03-31", root=tmp_path)
    rows = query_coverage(source="sec-global", form="10-K", root=tmp_path)
    assert rows and rows[0]["status"] == "complete"


def test_backfill_jobs_idempotent_queue_and_resume(tmp_path: Path) -> None:
    from app.sec.store import (
        claim_job,
        complete_job,
        enqueue_backfill_job,
        fail_job,
        get_job,
        list_jobs,
        requeue_job,
    )

    first = enqueue_backfill_job("sec-global", "10-K", "2024-01-01", "2024-03-31", root=tmp_path)
    assert enqueue_backfill_job("sec-global", "10-K", "2024-01-01", "2024-03-31", root=tmp_path) == first
    assert [j["id"] for j in list_jobs(root=tmp_path)] == [first]
def test_document_text_reads_need_accession(tmp_path: Path) -> None:
    """No persisted text index: reads need an accession; empty query rejected."""
    from app.sec.store import query_document_text, search_document_text

    assert query_document_text(root=tmp_path) == []
    with pytest.raises(ValueError):
        search_document_text("", root=tmp_path)


def test_typed_rows_without_accession_are_empty(tmp_path: Path) -> None:
    """No persisted typed index: accession-less queries stay empty."""
    from app.sec.store import query_beneficial_ownership

    assert query_beneficial_ownership(subject_cik=320193, root=tmp_path) == []
    assert query_beneficial_ownership(subject_cik=320193, as_of="2024-01-01", root=tmp_path) == []


def test_query_filings_date_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.store import query_filings

    forms = ["4"]
    old = _filing("0000000000-25-000109", form="4", filed_at="2024-02-15", known_at="2024-02-15")
    new = _filing("0000000000-25-000101", form="4", filed_at="2024-06-15", known_at="2024-06-15")

    def _fake_list(
        cik: object,
        forms: object = None,
        start_date: object = None,
        end_date: object = None,
        as_of: object = None,
        limit: object = 50,
    ) -> list[Filing]:
        del cik, forms, as_of
        assert start_date == "2024-01-01" or start_date is None
        assert end_date == "2024-03-31" or end_date is None
        picked = [old] if end_date == "2024-03-31" else [new, old]
        if isinstance(limit, int):
            return picked[:limit]
        return picked

    monkeypatch.setattr("app.sec.filings.list_sec_filings", _fake_list)
    rows = query_filings(
        cik=1234567, forms=forms, start_date="2024-01-01", end_date="2024-03-31", limit=1, root=tmp_path
    )
    assert len(rows) == 1 and rows[0].accession_no == "0000000000-25-000109"
    # Unbounded limit=None returns all provider filings.
    assert len(query_filings(cik=1234567, forms=forms, limit=None, root=tmp_path)) == 2
    with pytest.raises(ValueError):
        query_filings(cik=1234567, forms=forms, start_date="2024/01/01", root=tmp_path)
    with pytest.raises(ValueError):
        query_filings(cik=1234567, forms=forms, end_date="2024-13-01", root=tmp_path)
