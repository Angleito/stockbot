"""Offline tests for app/sec/offerings.py (no network)."""

from types import SimpleNamespace

import app.sec.offerings as offerings


def _filing(form, filed_at, accession, company="ACME"):
    return SimpleNamespace(form=form, filed_at=filed_at,
                           accession_no=accession, company=company)


def _history(monkeypatch):
    filings = [
        _filing("S-3", "2024-01-10", "s3"),
        _filing("424B5", "2024-02-01", "b5"),
        _filing("EFFECT", "2024-02-05", "eff"),
        _filing("RW", "2024-03-01", "rw"),
    ]
    terms = {
        "s3": {},
        "b5": {"shares": "1,000", "price_per_share": "10.00",
               "offering_type": "Common Stock",
               "underwriters": ["Bank A", "Bank B"]},
        "eff": None,
        "rw": {},
    }
    monkeypatch.setattr(offerings, "list_sec_filings", lambda *a, **k: filings)
    monkeypatch.setattr(offerings, "load_terms", lambda acc: terms[acc])
    return offerings.get_offering_history("ACME")


def test_history_links_and_statuses(monkeypatch):
    out = _history(monkeypatch)
    assert [o.form for o in out] == ["S-3", "424B5", "EFFECT", "RW"]
    assert [o.filed_at for o in out] == ["2024-01-10", "2024-02-01",
                                         "2024-02-05", "2024-03-01"]
    pros = next(o for o in out if o.accession_no == "b5")
    assert pros.source_registration == "s3"
    assert pros.shares == 1000 and pros.price_per_share == 10.0
    assert pros.underwriters == ("Bank A", "Bank B")
    assert next(o for o in out if o.accession_no == "eff").status == "effective"
    assert next(o for o in out if o.accession_no == "rw").status == "withdrawn"


def test_missing_terms_yield_none_fields(monkeypatch):
    out = _history(monkeypatch)
    shelf = next(o for o in out if o.accession_no == "s3")
    assert shelf.shares is None and shelf.price_per_share is None
    assert shelf.gross_proceeds is None and shelf.offering_type is None
    assert shelf.has_warrants is None and shelf.has_convertibles is None
    assert shelf.source_registration is None and shelf.status == "filed"


def test_atm_detected_from_type_text():
    rec = offerings.normalize_offering("a", "424B5", issuer="ACME",
                                       filed_at="2024-02-01", terms={
                                           "offering_type": "At The Market offering"})
    assert rec.is_atm is True
