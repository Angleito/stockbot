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


def test_archive_document_revisions_retained(tmp_path):
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

    revisions = list(raw_archive.iter_archive(
        "sec", DOCUMENT_KIND, _document_key(acc, doc), root=root))
    assert {r.sha256 for r in revisions} == {first.sha256, second.sha256}
    assert find_archived_document(acc, doc, root=root) is not None
    # Identical bytes re-archive cleanly (no new revision, no warning).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        again = archive_sec_document(acc, doc, b"v2", url=URL, root=root)
    assert again.sha256 == second.sha256


def test_search_ledger_round_trip(tmp_path):
    from app.sec.models import (
        SECSearchRequest,
        SECTextHit,
        SearchAttempt,
    )
    from app.sec.store import (
        persist_search_ledger,
        query_attempts,
        query_hits,
        query_search,
    )

    request = SECSearchRequest(query="Acme", as_of="2024-06-01")
    attempt = SearchAttempt(
        attempt_id="s9-efts-1", search_id="s9", backend="efts",
        query="Acme", status="complete", results_reported=1,
        results_retrieved=1, pages_retrieved=1, pit_basis="known_at",
    )
    hit = SECTextHit(
        search_id="s9", attempt_id="s9-efts-1", query="Acme",
        accession_no="0000000000-25-000001", form="10-K",
        filed_at="2024-02-01", filer_cik=1234567,
        filer_name="Test Co", matched_document="primary.htm", score=1.0,
    )
    written = persist_search_ledger(
        search_id="s9", request=request, text_hits=(hit,),
        attempts=(attempt,), coverage_status="complete",
        sources_attempted=("efts",), sources_completed=("efts",),
        results_reported=1, results_retrieved=1, pages=1,
        pending_backfill_jobs=("job-1",), root=tmp_path,
    )
    assert written == {"searches": 1, "attempts": 1, "hits": 1}
    search = query_search("s9", root=tmp_path)
    assert search["coverage_status"] == "complete"
    assert "job-1" in search["pending_jobs_json"]
    attempts = query_attempts("s9", root=tmp_path)
    assert [a["backend"] for a in attempts] == ["efts"]
    assert attempts[0]["pit_basis"] == "known_at"
    hits = query_hits("s9", root=tmp_path)
    assert hits[0]["matched_document"] == "primary.htm"
    assert hits[0]["filer_name"] == "Test Co"
    assert query_search("missing", root=tmp_path) is None


def test_coverage_partition_lifecycle(tmp_path):
    from app.sec.store import (
        is_partition_covered,
        query_coverage,
        store_coverage,
    )

    assert store_coverage("sec-global", "10-K", "2024-Q1", "complete",
                          root=tmp_path) == 1
    assert is_partition_covered("sec-global", "10-K", "2024-Q1", root=tmp_path)
    assert not is_partition_covered("sec-global", "10-K", "2024-Q2", root=tmp_path)
    rows = query_coverage(source="sec-global", form="10-K", root=tmp_path)
    assert rows and rows[0]["status"] == "complete"


def test_backfill_jobs_idempotent_queue_and_resume(tmp_path):
    from app.sec.store import (
        claim_job,
        complete_job,
        enqueue_backfill_job,
        fail_job,
        get_job,
        list_jobs,
        requeue_job,
    )

    first = enqueue_backfill_job("sec-global", "10-K", "2024-01-01", "2024-03-31",
                                 root=tmp_path)
    assert enqueue_backfill_job("sec-global", "10-K", "2024-01-01", "2024-03-31",
                                root=tmp_path) == first
    assert [j["id"] for j in list_jobs(root=tmp_path)] == [first]
    assert claim_job(root=tmp_path)["status"] == "running"
    assert complete_job(first, root=tmp_path)["status"] == "complete"
    assert claim_job(root=tmp_path) is None  # complete jobs are not auto-claimed
    assert fail_job(first, "boom", root=tmp_path)["status"] == "failed"
    assert requeue_job(first, root=tmp_path)["status"] == "queued"
    assert get_job(first, root=tmp_path)["status"] == "queued"


def test_document_text_literal_and_term_paths(tmp_path):
    from app.sec.store import (
        search_document_text,
        store_document_text,
    )

    assert store_document_text("ACC:doc1", "Risk Factors: supply chainoso disruption",
                               accession="ACC", document_name="doc1",
                               filed_at="2024-02-01", known_at="2024-02-01",
                               root=tmp_path) == 1
    assert store_document_text("ACC:doc2", "UnrelatedMD&A prose here",
                               accession="ACC", document_name="doc2",
                               filed_at="2024-05-01", known_at="2024-05-01",
                               root=tmp_path) == 1
    literal = search_document_text("supply chainoso", literal=True, root=tmp_path)
    assert [r["document_name"] for r in literal] == ["doc1"]
    terms = search_document_text("supply disruption", root=tmp_path)
    assert "doc1" in [r["document_name"] for r in terms]
    assert "doc2" not in [r["document_name"] for r in terms]
    # PIT excludes the later document.
    early = search_document_text("prose", as_of="2024-03-01", root=tmp_path)
    assert early == []


def test_typed_rows_pit_exclusion(tmp_path):
    from app.sec.store import (
        query_beneficial_ownership,
        store_beneficial_ownership,
    )

    assert store_beneficial_ownership({
        "accession": "0000000000-25-000009", "form": "SC 13D",
        "subject_cik": 320193, "subject_name": "Subject Co",
        "filer_cik": 999999, "filer_name": "Owner LP",
        "shares": 100, "percent": 6.0, "known_at": "2024-06-01",
    }, root=tmp_path) == 1
    assert query_beneficial_ownership(subject_cik=320193, root=tmp_path)
    assert query_beneficial_ownership(
        subject_cik=320193, as_of="2024-01-01", root=tmp_path) == []
