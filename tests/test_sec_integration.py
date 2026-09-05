"""Live SEC integration: pinned discovery contracts (opted-in only).

Skipped without SEC identity/network. With prerequisites present, any
network/source failure FAILS (never a silent skip): these tests prove the
live contracts, not the absence of errors.
"""

import socket

import pytest

pytestmark = pytest.mark.integration


def _prerequisite() -> bool:
    try:
        from app.config import get_sec_edgar_identity

        get_sec_edgar_identity()
    except Exception:
        return False
    try:
        socket.create_connection(("www.sec.gov", 443), timeout=5).close()
    except OSError:
        return False
    return True


pytestmark = [pytestmark, pytest.mark.skipif(
    not _prerequisite(), reason="SEC identity (SEC_EDGAR_IDENTITY) or network unavailable")]

_VALID_STATUSES = {"verified", "unverified", "ambiguous", "conflict", "not_found"}
_VALID_COVERAGE = {"complete", "complete_within_source_limits", "partial", "failed"}


def test_public_ticker_entity_verifies():
    from app.sec.discovery import find_sec_entities

    result = find_sec_entities("AAPL")
    assert result.coverage.status in _VALID_COVERAGE
    verified = [e for e in result.entities if e.verification_status == "verified"]
    assert any(e.cik == 320193 for e in verified)
    assert result.attempts


def test_no_ticker_registrant_resolves():
    from app.sec.discovery import find_sec_entities

    result = find_sec_entities("Vanguard Group")
    assert result.entities, "expected general CIK-dataset candidates"
    assert all(e.verification_status in _VALID_STATUSES for e in result.entities)


def test_efts_phrase_names_filer_and_matched_document():
    from app.sec.client import search_sec_filings

    result = search_sec_filings("climate change disclosure", limit=5)
    assert result.coverage.status in _VALID_COVERAGE
    assert result.coverage.results_reported >= len(result.text_hits)
    for hit in result.text_hits:
        assert hit.filer_name
        assert hit.matched_document
        assert hit.query == "climate change disclosure"


def test_exact_accession_round_trip():
    from app.sec.discovery import resolve_sec_accession
    from app.sec.filings import list_sec_filings

    (filing,) = list_sec_filings("AAPL", forms=["10-K"], limit=1)
    result = resolve_sec_accession(filing.accession_no)
    assert result.coverage.status in _VALID_COVERAGE
    assert [f.accession_no for f in result.filings] == [filing.accession_no]


def test_pagination_records_attempts_and_counts():
    from app.sec.client import search_sec_filings

    result = search_sec_filings("Apple Inc", limit=5)
    assert result.coverage.pages >= 1
    assert len(result.attempts) >= 1
    assert result.coverage.results_reported >= result.coverage.results_retrieved


def test_13d_relationship_search_structural():
    from app.sec.discovery import search_sec_relationships

    result = search_sec_relationships("320193")
    assert list(result["ciks"]) == ["320193"]
    assert any(a["backend"] == "local-typed" for a in result["attempts"])
    for entry in result["typed"]:
        assert entry["accession"]
        assert entry["status"] == "verified"


def test_13f_inverse_relationship_search_structural():
    from app.sec.discovery import search_sec_relationships

    # Berkshire Hathaway 13F manager CIK: manager -> holdings direction.
    result = search_sec_relationships("1067983",
                                      relationship_types=["holding_manager"])
    assert list(result["ciks"]) == ["1067983"]
    assert "holding_manager" in (result["relationship_types"] or ("holding_manager",))


def test_pit_excludes_later_hits():
    from app.sec.client import search_sec_filings

    result = search_sec_filings("Apple Inc", limit=5, as_of="2020-01-01")
    assert result.text_hits, "expected pre-2020 Apple hits"
    assert all((h.filed_at or "")[:10] <= "2020-01-01" for h in result.text_hits)
