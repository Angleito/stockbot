"""Offline SEC fixture tests: generic retrieval paths, PIT, amendments, calcs.

Fully offline. Fixtures are plain JSON; all math goes through the pure
app.sec modules (events8k, ownership, insider, dilution).
"""

import json
import re
from pathlib import Path

from app.sec import dilution, events8k, insider, ownership
from app.sec.models import BeneficialOwnership

_FIX = Path(__file__).parent / "fixtures" / "sec" / "filings.json"
_ACC_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def _load() -> list[dict[str, object]]:
    # Fixture file is untyped JSON; callers validate the fields they consume.
    return json.loads(_FIX.read_text())


def _by_form(entries: list[dict[str, object]], form: str) -> dict[str, object]:
    return next(e for e in entries if e["form"] == form)


def _person(entry: dict[str, object], i: int = 0) -> BeneficialOwnership:
    persons = entry["persons"]
    assert isinstance(persons, list)
    p = persons[i]
    assert isinstance(p, dict)
    issuer = entry["issuer"]
    assert isinstance(issuer, str)
    form = entry["form"]
    assert isinstance(form, str)
    filed_at = entry["filed_at"]
    assert isinstance(filed_at, str)
    accession_no = entry["accession_no"]
    assert isinstance(accession_no, str)
    return BeneficialOwnership(
        filer_name=p["name"], filer_cik=None, issuer=issuer,
        form=form, filed_at=filed_at,
        accession_no=accession_no, shares=p["shares"],
        percent=p["percent"], sole_voting=p.get("sole_voting"),
        shared_voting=p.get("shared_voting"),
        sole_dispositive=p.get("sole_dispositive"),
        shared_dispositive=p.get("shared_dispositive"),
    )


def test_accessions_stable() -> None:
    entries = _load()
    accs = [e["accession_no"] for e in entries]
    assert all(isinstance(a, str) and _ACC_RE.match(a) for a in accs)
    assert len(set(accs)) == len(accs)


def test_amendments_linked() -> None:
    entries = _load()
    d13, d13a = _by_form(entries, "SC 13D"), _by_form(entries, "SC 13D/A")
    assert d13a["amendment_of"] == d13["accession_no"]
    s1, s1a = _by_form(entries, "S-1"), _by_form(entries, "S-1/A")
    assert s1a["amendment_of"] == s1["accession_no"]
    s3, b5 = _by_form(entries, "S-3"), _by_form(entries, "424B5")
    assert b5["source_registration"] == s3["accession_no"]


def test_pit_excludes_later_amendment() -> None:
    entries = _load()
    as_of = "2024-04-01"
    visible = [e for e in entries if str(e["filed_at"]) <= as_of]
    accs = {e["accession_no"] for e in visible}
    d13 = _by_form(entries, "SC 13D")
    d13a = _by_form(entries, "SC 13D/A")
    assert d13["accession_no"] in accs
    assert d13a["accession_no"] not in accs


def test_diff_ownership_deterministic() -> None:
    entries = _load()
    prev = _person(_by_form(entries, "SC 13D"))
    curr = _person(_by_form(entries, "SC 13D/A"))
    ev = ownership.diff_ownership(prev, curr)
    assert ev.share_change == 500000
    assert ev.percent_change == ownership.diff_ownership(prev, curr).percent_change
    assert ev.percent_change is not None
    assert abs(ev.percent_change - 0.6) < 1e-9


def test_dilution_profile_quantified_and_unknown() -> None:
    entries = _load()
    s1 = _by_form(entries, "S-1")
    terms = s1["terms"]
    assert isinstance(terms, dict)
    acc = s1["accession_no"]
    assert isinstance(acc, str)
    got = dilution.dilution_profile(
        existing_shares=terms["existing_shares"],
        new_shares=terms["shares"],
        source_accessions=(acc,),
    )
    assert isinstance(got["dilution_pct"], float) and got["dilution_pct"] > 0
    missing = _by_form(entries, "EFFECT")
    assert missing["terms"] is None
    nq = dilution.dilution_profile()
    assert nq["dilution_pct"] == "not_quantifiable"


def test_insider_kinds_purchase_and_other() -> None:
    entries = _load()
    f4 = _by_form(entries, "4")
    transactions = f4["transactions"]
    assert isinstance(transactions, list)
    kinds = {insider.classify_transaction(t["code"]) for t in transactions}
    assert "open_market_purchase" in kinds
    assert "other" in kinds  # unknown code -> non-bearish 'other', never bearish default


def test_unknown_markers_never_raise() -> None:
    entries = _load()
    null_terms = [e for e in entries if "terms" in e and e["terms"] is None]
    assert null_terms
    for e in null_terms:
        out = dilution.dilution_profile()  # null terms -> no inputs, not a crash
        assert out["dilution_pct"] == "not_quantifiable"
        assert out["fully_diluted_shares"] == "not_quantifiable"


def test_8k_items_parse_with_bankruptcy() -> None:
    entries = _load()
    e8k = next(e for e in entries if e["form"] == "8-K")
    accession_no = e8k["accession_no"]
    assert isinstance(accession_no, str)
    items = e8k["items"]
    assert isinstance(items, dict)
    events = events8k.parse_8k_events(accession_no, items)
    assert len(events) >= 2
    assert any(e.item_number == "1.03" for e in events)


def _by_accession(entries: list[dict[str, object]], accession: str) -> dict[str, object]:
    return next(e for e in entries if e["accession_no"] == accession)


def test_no_ticker_registrant_has_empty_tickers() -> None:
    entry = _by_accession(_load(), "0000320193-24-000101")
    assert entry["tickers"] == []
    accession_no = entry["accession_no"]
    assert isinstance(accession_no, str)
    assert _ACC_RE.match(accession_no)


def test_former_name_carries_validity_interval() -> None:
    entry = _by_accession(_load(), "0000320193-24-000102")
    former_names = entry["former_names"]
    assert isinstance(former_names, list)
    (former,) = former_names
    assert isinstance(former, dict)
    assert former["from"] < former["to"]
    assert former["name"] != entry["issuer"]


def test_13d_filer_and_subject_are_distinct() -> None:
    entry = _by_accession(_load(), "0000320193-24-000103")
    filer = entry["filer"]
    assert isinstance(filer, dict)
    subject = entry["subject"]
    assert isinstance(subject, dict)
    assert filer["cik"] != subject["cik"]
    assert subject["name"] == entry["issuer"]
    assert _person(entry).filer_name == filer["name"]


def test_13f_manager_and_held_issuer_are_distinct() -> None:
    entry = _by_accession(_load(), "0000320193-24-000104")
    holdings = entry["holdings"]
    assert isinstance(holdings, list)
    (holding,) = holdings
    assert isinstance(holding, dict)
    manager = entry["manager"]
    assert isinstance(manager, dict)
    assert manager["name"] == entry["issuer"]
    assert holding["issuer_name"] != manager["name"]
    assert len(holding["cusip"]) == 9


def test_form4_owner_and_issuer_roles_are_distinct() -> None:
    entry = _by_accession(_load(), "0000320193-24-000105")
    owner = entry["owner"]
    assert isinstance(owner, dict)
    assert owner["cik"] != entry["issuer_cik"]
    assert owner["is_officer"] is True
    transactions = entry["transactions"]
    assert isinstance(transactions, list)
    first = transactions[0]
    assert isinstance(first, dict)
    assert first["code"] == "P"


def test_merger_target_acquirer_unknown_without_closing() -> None:
    entry = _by_accession(_load(), "0000320193-24-000106")
    target = entry["target"]
    assert isinstance(target, dict)
    acquirer = entry["acquirer"]
    assert isinstance(acquirer, dict)
    assert target["cik"] != acquirer["cik"]
    assert entry["status"] == "unknown"


def test_multi_class_securities_stay_distinct() -> None:
    entry = _by_accession(_load(), "0000320193-24-000107")
    securities = entry["securities"]
    assert isinstance(securities, list)
    cusips = [s["cusip"] for s in securities]
    assert len(set(cusips)) == 2
    assert all(s["class_title"] for s in securities)
