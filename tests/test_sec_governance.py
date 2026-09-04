"""Offline tests for proxy/governance parsing (no network)."""

from types import SimpleNamespace

import pytest

from app.sec import governance
from app.sec.governance import (
    extract_proposals,
    extract_votes,
    normalize_proxy,
)


def test_contested_form_is_proxy_contest():
    event = normalize_proxy("acc-1", "DFAN14A", issuer="AAA")
    assert event.event_type == "proxy_contest"
    assert event.contested is True
    assert event.event_id == "acc-1:gov"


def test_def14a_is_annual_meeting():
    event = normalize_proxy("acc-2", "DEF 14A", issuer="AAA",
                            filed_at="2024-04-01")
    assert event.event_type == "annual_meeting"
    assert event.contested is False
    assert event.filed_at == "2024-04-01"


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError):
        normalize_proxy("acc-3", "DEF 14A", issuer="AAA").__class__(
            event_id="x", issuer="AAA", event_type="nope",
            accession_no="acc-3")


def test_extract_proposals_with_board_rec():
    text = ("Proposal No. 1 Election of directors\n"
            "The board recommends a vote for this proposal.\n"
            "Proposal No. 2 Ratify auditors\n"
            "The board recommends a vote against this proposal.\n")
    out = extract_proposals(text, issuer="AAA", accession_no="acc-1")
    assert [p.proposal_id for p in out] == ["acc-1:p1", "acc-1:p2"]
    assert "Proposal No. 1" in out[0].description
    assert out[0].board_recommendation == "for"
    assert out[1].board_recommendation == "against"
    assert out[0].status == "unknown"


def test_extract_proposals_empty():
    assert extract_proposals("", issuer="AAA",
                             accession_no="acc-1") == []
    assert extract_proposals(None, issuer="AAA",
                             accession_no="acc-1") == []


def test_extract_votes_with_numbers():
    text = ("Votes for 12,345,678 and votes against 1,234. "
            "Abstentions 500. The proposal was approved.")
    out = extract_votes(text, issuer="AAA", accession_no="acc-1")
    assert len(out) == 1
    assert out[0].votes_for == 12345678
    assert out[0].votes_against == 1234
    assert out[0].abstentions == 500
    assert out[0].outcome == "approved"


def test_extract_votes_no_numbers():
    assert extract_votes("no vote counts here", issuer="AAA",
                         accession_no="acc-1") == []


def test_get_governance_events_survives_text_failure(monkeypatch):
    filings = [SimpleNamespace(accession_no="a1", form="DEF 14A",
                               filed_at="2024-04-01", company="AAA"),
               SimpleNamespace(accession_no="a2", form="DFAN14A",
                               filed_at="2024-05-01", company="AAA")]

    def fake_list(ticker_or_cik, **kwargs):
        return filings

    def fake_text(accession_no):
        raise RuntimeError("offline")

    monkeypatch.setattr(governance, "list_sec_filings", fake_list)
    monkeypatch.setattr(governance, "load_proxy_text", fake_text)
    out = governance.get_governance_events("AAA")
    assert [e.event_type for e in out] == ["annual_meeting",
                                           "proxy_contest"]
    assert [e.accession_no for e in out] == ["a1", "a2"]
