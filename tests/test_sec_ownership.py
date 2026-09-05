"""Offline tests for app/sec/ownership.py (no network)."""

from types import SimpleNamespace

import app.sec.ownership as ownership
from app.sec.models import BeneficialOwnership


class _Person:
    def __init__(self, cik, name, sole_v=0, shared_v=0, sole_d=0, shared_d=0,
                 agg=0, pct=0.0):
        self.cik = cik
        self.name = name
        self.sole_voting_power = sole_v
        self.shared_voting_power = shared_v
        self.sole_dispositive_power = sole_d
        self.shared_dispositive_power = shared_d
        self.aggregate_amount = agg
        self.percent_of_class = pct


def _schedule(persons, purpose=None):
    items = SimpleNamespace(purpose_of_transaction=purpose)
    return SimpleNamespace(reporting_persons=persons, items=items)


def test_two_person_schedule():
    sched = _schedule([
        _Person("1", "Alice", sole_v=100, shared_v=50, sole_d=100, shared_d=50,
                agg=1000, pct=5.5),
        _Person("2", "Bob", sole_v=0, shared_v=200, sole_d=0, shared_d=200,
                agg=2000, pct="7.25"),
    ], purpose="control")
    recs = ownership.normalize_schedule(sched, issuer="ACME", form="SC 13D",
                                        filed_at="2024-01-15", accession_no="a1")
    assert len(recs) == 2
    assert recs[0].shares == 1000 and recs[0].percent == 5.5
    assert recs[0].sole_voting == 100 and recs[0].shared_dispositive == 50
    assert recs[1].shares == 2000 and recs[1].percent == 7.25
    assert recs[0].purpose_text == "control"
    assert recs[0].is_amendment is False


def test_amendment_diff_numbers_and_voting():
    prev = BeneficialOwnership("Alice", "1", "ACME", "SC 13D", "2024-01-15",
                               "a1", shares=1000, percent=5.0, sole_voting=100,
                               purpose_text="control")
    curr = BeneficialOwnership("Alice", "1", "ACME", "SC 13D/A", "2024-06-15",
                               "a2", shares=1500, percent=7.0, sole_voting=200,
                               purpose_text="activist")
    event = ownership.diff_ownership(prev, curr)
    assert event.share_change == 500
    assert event.percent_change == 7.0 - 5.0
    assert event.voting_changed is True
    assert event.text_changed is True
    assert event.previous_accession == "a1"
    assert event.current_accession == "a2"


def test_text_changed_needs_both_purposes():
    base = dict(filer_name="A", filer_cik="1", issuer="ACME", form="SC 13D",
                filed_at="2024-01-15", accession_no="a1")
    both_same = ownership.diff_ownership(
        BeneficialOwnership(**{**base, "purpose_text": "x"}),
        BeneficialOwnership(**{**base, "purpose_text": "x",
                               "accession_no": "a2"}))
    assert both_same.text_changed is False
    missing = ownership.diff_ownership(
        BeneficialOwnership(**{**base, "purpose_text": None}),
        BeneficialOwnership(**{**base, "purpose_text": "y",
                               "accession_no": "a2"}))
    assert missing.text_changed is False
    missing2 = ownership.diff_ownership(
        BeneficialOwnership(**{**base, "purpose_text": "x"}),
        BeneficialOwnership(**{**base, "purpose_text": None,
                               "accession_no": "a2"}))
    assert missing2.text_changed is False


def test_changes_group_by_filer_and_skip_failures(monkeypatch):
    filings = [
        SimpleNamespace(accession_no="a1", form="SC 13D",
                        filed_at="2024-01-15", company="ACME"),
        SimpleNamespace(accession_no="bad", form="SC 13D",
                        filed_at="2024-03-01", company="ACME"),
        SimpleNamespace(accession_no="a2", form="SC 13D/A",
                        filed_at="2024-06-15", company="ACME"),
    ]
    monkeypatch.setattr(ownership, "list_sec_filings",
                        lambda *a, **k: filings)

    def fake_load(accession):
        if accession == "bad":
            raise RuntimeError("boom")
        n = 1000 if accession == "a1" else 1500
        return _schedule([_Person("1", "Alice", agg=n)], purpose="p")

    monkeypatch.setattr(ownership, "load_schedule", fake_load)
    events = ownership.get_ownership_changes("ACME")
    assert len(events) == 1
    assert events[0].share_change == 500
    assert events[0].previous_accession == "a1"
    assert events[0].current_accession == "a2"


def test_unparseable_schedule_yields_no_records():
    assert ownership.normalize_schedule(
        SimpleNamespace(reporting_persons="not-a-list", items=None),
        issuer="ACME", form="SC 13G", filed_at=None,
        accession_no="a9") == []


def test_store_queries_both_ownership_directions(tmp_path):
    from app.sec.store import query_beneficial_ownership, store_beneficial_ownership

    assert store_beneficial_ownership({
        "accession": "0000000000-25-000013", "form": "SC 13D",
        "subject_cik": 320193, "subject_name": "Subject Co",
        "filer_cik": 999001, "filer_name": "Owner LP",
        "shares": 5000000, "percent": 6.2, "known_at": "2024-03-10",
    }, root=tmp_path) == 1
    by_subject = query_beneficial_ownership(subject_cik=320193, root=tmp_path)
    assert [r["filer_cik"] for r in by_subject] == ["999001"]
    assert by_subject[0]["subject_name"] == "Subject Co"
    by_owner = query_beneficial_ownership(owner_cik=999001, root=tmp_path)
    assert [r["subject_cik"] for r in by_owner] == ["320193"]
    # Filer and subject never share a fallback identity.
    assert by_owner[0]["filer_cik"] != by_owner[0]["subject_cik"]


def test_store_queries_13f_both_directions(tmp_path):
    from app.sec.store import query_13f_holdings, store_13f_holding

    assert store_13f_holding({
        "accession": "0000000000-25-000014", "form": "13F-HR",
        "manager_cik": 103567, "manager_name": "Sample Manager LLC",
        "issuer_name": "Sample Issuer Inc", "class_title": "COM",
        "cusip": "594918104", "shares": 1000, "value": 50000,
        "known_at": "2024-02-14",
    }, root=tmp_path) == 1
    by_manager = query_13f_holdings(manager_cik=103567, root=tmp_path)
    assert by_manager[0]["cusip"] == "594918104"
    by_security = query_13f_holdings(security="594918104", root=tmp_path)
    assert [r["manager_cik"] for r in by_security] == ["103567"]
