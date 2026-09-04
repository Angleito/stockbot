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


def _load():
    return json.loads(_FIX.read_text())


def _by_form(entries, form):
    return next(e for e in entries if e["form"] == form)


def _person(entry, i=0):
    p = entry["persons"][i]
    return BeneficialOwnership(
        filer_name=p["name"], filer_cik=None, issuer=entry["issuer"],
        form=entry["form"], filed_at=entry["filed_at"],
        accession_no=entry["accession_no"], shares=p["shares"],
        percent=p["percent"], sole_voting=p.get("sole_voting"),
        shared_voting=p.get("shared_voting"),
        sole_dispositive=p.get("sole_dispositive"),
        shared_dispositive=p.get("shared_dispositive"),
    )


def test_accessions_stable():
    entries = _load()
    accs = [e["accession_no"] for e in entries]
    assert all(_ACC_RE.match(a) for a in accs)
    assert len(set(accs)) == len(accs)


def test_amendments_linked():
    entries = _load()
    d13, d13a = _by_form(entries, "SC 13D"), _by_form(entries, "SC 13D/A")
    assert d13a["amendment_of"] == d13["accession_no"]
    s1, s1a = _by_form(entries, "S-1"), _by_form(entries, "S-1/A")
    assert s1a["amendment_of"] == s1["accession_no"]
    s3, b5 = _by_form(entries, "S-3"), _by_form(entries, "424B5")
    assert b5["source_registration"] == s3["accession_no"]


def test_pit_excludes_later_amendment():
    entries = _load()
    as_of = "2024-04-01"
    visible = [e for e in entries if e["filed_at"] <= as_of]
    accs = {e["accession_no"] for e in visible}
    d13 = _by_form(entries, "SC 13D")
    d13a = _by_form(entries, "SC 13D/A")
    assert d13["accession_no"] in accs
    assert d13a["accession_no"] not in accs


def test_diff_ownership_deterministic():
    entries = _load()
    prev = _person(_by_form(entries, "SC 13D"))
    curr = _person(_by_form(entries, "SC 13D/A"))
    ev = ownership.diff_ownership(prev, curr)
    assert ev.share_change == 500000
    assert ev.percent_change == ownership.diff_ownership(prev, curr).percent_change
    assert abs(ev.percent_change - 0.6) < 1e-9


def test_dilution_profile_quantified_and_unknown():
    entries = _load()
    s1 = _by_form(entries, "S-1")
    got = dilution.dilution_profile(
        existing_shares=s1["terms"]["existing_shares"],
        new_shares=s1["terms"]["shares"],
        source_accessions=(s1["accession_no"],),
    )
    assert isinstance(got["dilution_pct"], float) and got["dilution_pct"] > 0
    missing = _by_form(entries, "EFFECT")
    assert missing["terms"] is None
    nq = dilution.dilution_profile()
    assert nq["dilution_pct"] == "not_quantifiable"


def test_insider_kinds_purchase_and_other():
    entries = _load()
    f4 = _by_form(entries, "4")
    kinds = {insider.classify_transaction(t["code"]) for t in f4["transactions"]}
    assert "open_market_purchase" in kinds
    assert "other" in kinds  # unknown code -> non-bearish 'other', never bearish default


def test_unknown_markers_never_raise():
    entries = _load()
    null_terms = [e for e in entries if "terms" in e and e["terms"] is None]
    assert null_terms
    for e in null_terms:
        out = dilution.dilution_profile()  # null terms -> no inputs, not a crash
        assert out["dilution_pct"] == "not_quantifiable"
        assert out["fully_diluted_shares"] == "not_quantifiable"


def test_8k_items_parse_with_bankruptcy():
    entries = _load()
    e8k = next(e for e in entries if e["form"] == "8-K")
    events = events8k.parse_8k_events(e8k["accession_no"], e8k["items"])
    assert len(events) >= 2
    assert any(e.item_number == "1.03" for e in events)
