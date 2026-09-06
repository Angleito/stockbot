"""Offline tests for app/sec/ownership.py (no network)."""

from types import SimpleNamespace
import pytest

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
        "known_at": "2024-02-14", "source_row": 1,
    }, root=tmp_path) == 1
    by_manager = query_13f_holdings(manager_cik=103567, root=tmp_path)
    assert by_manager[0]["cusip"] == "594918104"
    by_security = query_13f_holdings(security="594918104", root=tmp_path)
    assert [r["manager_cik"] for r in by_security] == ["103567"]


def test_13f_former_name_validity_and_as_of(tmp_path):
    from app.sec.store import query_13f_holdings_for_issuer, query_13f_issuer_candidates, store_13f_holding
    from app.storage import parquet as _pq
    now = "2024-06-01T00:00:00Z"
    _pq.write_rows("entities", [{"entity_id": "sec:cik:0000000009", "name": "New Co",
                                 "entity_type": "company", "sic": None, "source": "sec-submissions",
                                 "known_at": "2024-01-01T00:00:00Z", "retrieved_at": now,
                                 "content_hash": None, "parser_version": "1"}], root=tmp_path / "parquet")
    _pq.write_rows("entity_aliases", [{"alias_type": "former_name", "alias_value": "Old Co",
                                       "entity_id": "sec:cik:0000000009", "security_id": None,
                                       "source": "sec-submissions", "valid_from": "2020-01-01",
                                       "valid_to": "2025-01-01", "known_at": "2024-01-01T00:00:00Z",
                                       "retrieved_at": now, "content_hash": None,
                                       "parser_version": "1"}], root=tmp_path / "parquet")
    assert len(query_13f_issuer_candidates("Old Co", report_period="2024-03-31",
                                           holding_known_at=None, root=tmp_path)) == 1
    assert query_13f_issuer_candidates("Old Co", report_period="2026-01-01",
                                       holding_known_at=None, root=tmp_path) == []
    from app.sec import insider as _ins
    h = _ins._holding_row_to_record({"Cusip": "123456789", "Issuer": "Old Co",
                                     "ReportPeriod": "2024-03-31"},
                                    manager_name="M", manager_cik="5", accession_no="ACC-F",
                                    report_period="2024-03-31", filed_at="2024-05-15",
                                    document_name="d", known_at="2024-05-15T00:00:00Z",
                                    source_url=None, source_row=1)
    _ins.observe_13f_security(h, raw_archive_path="/tmp/p", content_hash="hf",
                              retrieved_at=now, root=tmp_path)
    store_13f_holding(h.to_dict(), root=tmp_path)
    assert len(query_13f_holdings_for_issuer("sec:cik:0000000009", as_of="2024-06-01", root=tmp_path)) == 1
    assert query_13f_holdings_for_issuer("sec:cik:0000000009", as_of="2024-01-01", root=tmp_path) == []
    # Legacy null security_id still maps via CUSIP.
    store_13f_holding({"accession": "ACC-LEG", "document_name": "d", "manager_cik": "6",
                       "manager_name": "LM", "report_period": "2024-03-31",
                       "issuer_name": "Old Co", "entity_id": None, "security_id": None,
                       "class_title": "COM", "cusip": "123456789", "filed_at": "2024-05-15",
                       "known_at": "2024-05-15T00:00:00Z", "source_row": 1}, root=tmp_path)
    assert len(query_13f_holdings_for_issuer("sec:cik:0000000009", root=tmp_path)) == 2


def test_13f_cusip_canonical_round_trip(tmp_path):
    from app.sec.store import query_13f_holdings, store_13f_holding
    from app.storage import parquet as _pq

    assert store_13f_holding({
        "accession": "0000000000-25-000020", "manager_cik": 103567,
        "manager_name": "Sample Manager LLC", "issuer_name": "Apple Inc",
        "class_title": "COM", "cusip": "0378-33100", "shares": 1000,
        "value": 50000, "known_at": "2024-02-14", "source_row": 1,
    }, root=tmp_path) == 1
    by_manager = query_13f_holdings(manager_cik=103567, root=tmp_path)
    assert by_manager[0]["cusip"] == "037833100"
    assert [r["cusip"] for r in query_13f_holdings(
        security="0378-33100", root=tmp_path)] == ["037833100"]
    # Legacy dashed-stored row is found by normalized input (no migration).
    _pq.write_rows("sec_13f_holdings", [{
        "accession": "0000000000-25-000021", "manager_cik": "103567",
        "cusip": "0378-33100", "isin": None, "security_id": None,
        "known_at": "2024-02-14",
    }], root=tmp_path / "parquet")
    assert len(query_13f_holdings(security="037833100", root=tmp_path)) == 2
    # Empty-after-strip maps to None, not "".
    assert store_13f_holding({
        "accession": "0000000000-25-000022", "manager_cik": 103567,
        "cusip": "- -", "known_at": "2024-02-14", "source_row": 1,
    }, root=tmp_path) == 1
    by_accession = query_13f_holdings(accession="0000000000-25-000022",
                                      root=tmp_path)
    assert by_accession[0]["cusip"] is None


def test_13f_issuer_dedupes_legacy_cusip(tmp_path):
    from app.sec.store import query_13f_holdings_for_issuer
    from app.storage import parquet as _pq
    now = "2024-06-01T00:00:00Z"
    _pq.write_rows("entities", [{"entity_id": "sec:cik:0000320193",
                                 "name": "Apple Inc",
                                 "entity_type": "company", "sic": None,
                                 "source": "sec-submissions",
                                 "known_at": "2024-01-01T00:00:00Z",
                                 "retrieved_at": now, "content_hash": None,
                                 "parser_version": "1"}],
                   root=tmp_path / "parquet")
    _pq.write_rows("entity_aliases", [{
        "alias_type": "cusip", "alias_value": "037833100",
        "entity_id": "sec:cik:0000320193",
        "security_id": "cusip:037833100", "source": "sec-13f",
        "valid_from": "2024-01-01", "valid_to": "2025-01-01",
        "known_at": "2024-01-01T00:00:00Z", "retrieved_at": now,
        "content_hash": None, "parser_version": "sec-13f-security-v1"}],
        root=tmp_path / "parquet")
    base = {"accession": "ACC-DUP", "manager_cik": "5",
            "manager_name": "M", "issuer_name": "Apple Inc",
            "report_period": "2024-03-31", "class_title": "COM",
            "filed_at": "2024-05-15", "known_at": "2024-05-15T00:00:00Z"}
    _pq.write_rows("sec_13f_holdings", [
        {**base, "cusip": "037833100", "isin": None, "security_id": None,
         "entity_id": None, "source_row": None, "holding_id": None},
        {**base, "cusip": "0378-33100", "isin": None, "security_id": None,
         "entity_id": None, "source_row": None, "holding_id": None},
        {**base, "cusip": "0378-33100", "isin": None, "security_id": None,
         "entity_id": None, "shares": 999, "source_row": None,
         "holding_id": None},
        {**base, "cusip": "037833100", "isin": None, "security_id": None,
         "entity_id": None, "voting": "sole=1000", "source_row": None,
         "holding_id": None},
    ], root=tmp_path / "parquet")
    rows = query_13f_holdings_for_issuer("sec:cik:0000320193", root=tmp_path)
    assert len(rows) == 3
    assert all("_rn" not in r for r in rows)
    by_key = {(r.get("shares"), r.get("voting")): r for r in rows}
    assert by_key[(None, None)]["cusip"] == "037833100"
    assert by_key[(999.0, None)]["cusip"] == "0378-33100"
    assert (None, "sole=1000") in by_key


def test_13f_distinct_rows_survive_shared_filing(tmp_path):
    from app.sec import insider as _ins
    from app.sec.store import (
        query_13f_holdings,
        query_13f_holdings_for_issuer,
        store_13f_holding,
    )
    from app.storage import parquet as _pq
    now = "2024-06-01T00:00:00Z"
    _pq.write_rows("entities", [{"entity_id": "sec:cik:0000320193",
                                 "name": "Apple Inc",
                                 "entity_type": "company", "sic": None,
                                 "source": "sec-submissions",
                                 "known_at": "2024-01-01T00:00:00Z",
                                 "retrieved_at": now, "content_hash": None,
                                 "parser_version": "1"}],
                   root=tmp_path / "parquet")
    _pq.write_rows("entity_aliases", [{
        "alias_type": "cusip", "alias_value": "037833100",
        "entity_id": "sec:cik:0000320193",
        "security_id": "cusip:037833100", "source": "sec-13f",
        "valid_from": "2024-01-01", "valid_to": "2025-01-01",
        "known_at": "2024-01-01T00:00:00Z", "retrieved_at": now,
        "content_hash": None, "parser_version": "sec-13f-security-v1"}],
        root=tmp_path / "parquet")
    base_row = {"Cusip": "037833100", "Issuer": "Apple Inc",
                "ReportPeriod": "2024-03-31", "Class": "COM",
                "SharesPrnAmount": 1000, "Value": 50000,
                "InvestmentDiscretion": "Sole", "OtherManager": "1",
                "Type": "Shares", "SoleVoting": 1000,
                "SharedVoting": 0, "NonVoting": 0}
    third_row = {**base_row, "SharesPrnAmount": 2000, "Value": 60000,
                 "SoleVoting": 1500, "SharedVoting": 500,
                 "OtherManager": "2", "Type": "Principal"}
    rows = [dict(base_row), dict(base_row), dict(third_row)]
    holdings = _ins.normalize_13f_holdings(
        rows, manager_name="M", manager_cik="5", accession_no="ACC-ROWS",
        report_period="2024-03-31", filed_at="2024-05-15",
        document_name="infotable.xml",
        known_at="2024-05-15T00:00:00Z")
    assert [h.source_row for h in holdings] == [1, 2, 3]
    assert [h.shares_prn_type for h in holdings] == ["SH", "SH", "PRN"]
    assert [h.discretion for h in holdings] == ["Sole", "Sole", "Sole"]
    assert [h.other_manager for h in holdings] == ["1", "1", "2"]
    holding_ids = [h.holding_id for h in holdings]
    assert len(set(holding_ids)) == 3
    repeat = _ins.normalize_13f_holdings(
        rows, manager_name="M", manager_cik="5", accession_no="ACC-ROWS",
        report_period="2024-03-31", filed_at="2024-05-15",
        document_name="infotable.xml",
        known_at="2024-05-15T00:00:00Z")
    assert [h.holding_id for h in repeat] == holding_ids
    dicts = []
    for h in holdings:
        d = h.to_dict()
        d["content_hash"] = "shared-filing-hash"
        dicts.append(d)
    assert [store_13f_holding(d, root=tmp_path) for d in dicts] == [1, 1, 1]
    assert [store_13f_holding(d, root=tmp_path) for d in dicts] == [0, 0, 0]
    direct = sorted(query_13f_holdings(manager_cik="5", root=tmp_path),
                    key=lambda r: r["source_row"])
    assert [r["source_row"] for r in direct] == [1, 2, 3]
    assert [r["shares_prn_type"] for r in direct] == ["SH", "SH", "PRN"]
    governed = sorted(
        query_13f_holdings_for_issuer("sec:cik:0000320193", root=tmp_path),
        key=lambda r: r["source_row"])
    assert all("_rn" not in r for r in governed)
    # Rows 1-2 are one logical holding (differ only by source_row) and dedupe;
    # the shares/value/voting-distinct third row survives.
    assert len(governed) == 2
    assert governed[-1]["source_row"] == 3
    assert governed[0]["source_row"] in (1, 2)



@pytest.mark.parametrize(("raw", "expected"), [
    ("037833100", "037833100"),
    ("0378-33100", "037833100"),
    ("0378 33100", "037833100"),
    (" 037833100 ", "037833100"),
    (None, None),
    ("", None),
    ("---", None),
])
def test_cusip_contract(raw, expected):
    from app.sec.cusip import cusip_security_id, normalize_cusip

    assert normalize_cusip(raw) == expected
    assert cusip_security_id("0378-33100") == "cusip:037833100"
    assert cusip_security_id("---") is None


def test_13f_cusip_form_insensitive_dedup(tmp_path):
    from app.sec.store import query_13f_holdings, store_13f_holding
    from app.storage import parquet as _pq

    base = {"accession": "ACC-DEDUP", "manager_cik": "5",
            "manager_name": "M", "issuer_name": "Apple Inc",
            "report_period": "2024-03-31", "class_title": "COM",
            "filed_at": "2024-05-15", "known_at": "2024-05-15T00:00:00Z",
            "source_row": 1}
    assert store_13f_holding({**base, "cusip": "0378-33100"},
                             root=tmp_path) == 1
    assert query_13f_holdings(
        manager_cik="5", root=tmp_path)[0]["cusip"] == "037833100"
    assert store_13f_holding({**base, "cusip": "037833100"},
                             root=tmp_path) == 0
    assert store_13f_holding({**base, "cusip": " 0378 33100 "},
                             root=tmp_path) == 0
    assert _pq.count_rows("sec_13f_holdings",
                          root=tmp_path / "parquet") == 1
    assert store_13f_holding({**base, "accession": "ACC-EMPTY",
                              "cusip": "---"}, root=tmp_path) == 1
    assert store_13f_holding({**base, "accession": "ACC-EMPTY2",
                              "cusip": ""}, root=tmp_path) == 1
    assert query_13f_holdings(
        accession="ACC-EMPTY", root=tmp_path)[0]["cusip"] is None
    assert query_13f_holdings(
        accession="ACC-EMPTY2", root=tmp_path)[0]["cusip"] is None


def test_13f_dashed_query_finds_canonical(tmp_path):
    from app.sec.store import query_13f_holdings, store_13f_holding

    assert store_13f_holding({
        "accession": "0000000000-25-000030", "manager_cik": 103567,
        "manager_name": "Sample Manager LLC", "issuer_name": "Apple Inc",
        "class_title": "COM", "cusip": "037833100", "shares": 1000,
        "value": 50000, "known_at": "2024-02-14", "source_row": 1,
    }, root=tmp_path) == 1
    assert len(query_13f_holdings(
        security="0378-33100", root=tmp_path)) == 1
    assert len(query_13f_holdings(
        cusip="0378-33100", root=tmp_path)) == 1
    assert len(query_13f_holdings(
        security="cusip:037833100", root=tmp_path)) == 1


def test_13f_legacy_dashed_row_maps_to_issuer(tmp_path):
    from app.sec.store import query_13f_holdings_for_issuer
    from app.storage import parquet as _pq
    now = "2024-06-01T00:00:00Z"
    _pq.write_rows("entities", [{"entity_id": "sec:cik:0000320193",
                                 "name": "Apple Inc",
                                 "entity_type": "company", "sic": None,
                                 "source": "sec-submissions",
                                 "known_at": "2024-01-01T00:00:00Z",
                                 "retrieved_at": now, "content_hash": None,
                                 "parser_version": "1"}],
                   root=tmp_path / "parquet")
    _pq.write_rows("entity_aliases", [{
        "alias_type": "cusip", "alias_value": "037833100",
        "entity_id": "sec:cik:0000320193",
        "security_id": "cusip:037833100", "source": "sec-13f",
        "valid_from": "2024-01-01", "valid_to": "2025-01-01",
        "known_at": "2024-01-01T00:00:00Z", "retrieved_at": now,
        "content_hash": None, "parser_version": "sec-13f-security-v1"}],
        root=tmp_path / "parquet")
    # Pre-fix dashed row written direct, bypassing the writer normalizer.
    _pq.write_rows("sec_13f_holdings", [{
        "accession": "ACC-LEGACY", "manager_cik": "5", "manager_name": "M",
        "report_period": "2024-03-31", "issuer_name": "Apple Inc",
        "class_title": "COM", "cusip": "0378-33100", "isin": None,
        "security_id": None, "entity_id": None, "filed_at": "2024-05-15",
        "known_at": "2024-05-15T00:00:00Z"}],
        root=tmp_path / "parquet")
    rows = query_13f_holdings_for_issuer("sec:cik:0000320193", root=tmp_path)
    assert len(rows) == 1
    assert rows[0]["cusip"] == "0378-33100"
