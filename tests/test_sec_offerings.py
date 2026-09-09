"""Offline tests for app/sec/offerings.py (no network)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import app.sec.offerings as offerings


def _filing(form: str, filed_at: str, accession: str, company: str = "ACME") -> SimpleNamespace:
    return SimpleNamespace(form=form, filed_at=filed_at,
                           accession_no=accession, company=company)


def _history(monkeypatch: pytest.MonkeyPatch):
    filings = [
        _filing("S-3", "2024-01-10", "s3"),
        _filing("424B5", "2024-02-01", "b5"),
        _filing("EFFECT", "2024-02-05", "eff"),
        _filing("RW", "2024-03-01", "rw"),
    ]
    terms: dict[str, dict[str, str | list[str]] | None] = {
        "s3": {},
        "b5": {"shares": "1,000", "price_per_share": "10.00",
               "offering_type": "Common Stock",
               "underwriters": ["Bank A", "Bank B"]},
        "eff": None,
        "rw": {},
    }

    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return filings

    def _fake_terms(accession_no: str) -> dict[str, str | list[str]] | None:
        return terms[accession_no]

    monkeypatch.setattr(offerings, "list_sec_filings", _fake_list)
    monkeypatch.setattr(offerings, "load_terms", _fake_terms)
    return offerings.get_offering_history("ACME")


def test_history_links_and_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_missing_terms_yield_none_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _history(monkeypatch)
    shelf = next(o for o in out if o.accession_no == "s3")
    assert shelf.shares is None and shelf.price_per_share is None
    assert shelf.gross_proceeds is None and shelf.offering_type is None
    assert shelf.has_warrants is None and shelf.has_convertibles is None
    assert shelf.source_registration is None and shelf.status == "filed"

def test_terms_forms_skips_unconsumed_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    filings = [_filing("S-3", "2024-01-10", "s3"),
               _filing("424B5", "2024-02-01", "b5")]
    fetched: list[str] = []

    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return filings

    def _fake_terms(accession_no: str) -> dict[str, str]:
        fetched.append(accession_no)
        return {"shares": "1,000"}

    monkeypatch.setattr(offerings, "list_sec_filings", _fake_list)
    monkeypatch.setattr(offerings, "load_terms", _fake_terms)
    out = offerings.get_offering_history("ACME", terms_forms={"424B5"})
    assert fetched == ["b5"]
    assert [o.accession_no for o in out] == ["s3", "b5"]
    assert next(o for o in out if o.accession_no == "s3").shares is None


def test_atm_detected_from_type_text() -> None:
    rec = offerings.normalize_offering("a", "424B5", issuer="ACME",
                                       filed_at="2024-02-01", terms={
                                           "offering_type": "At The Market offering"})
    assert rec.is_atm is True


def test_store_offering_both_directions_registration_not_issuance(tmp_path: Path) -> None:
    from app.sec.store import query_offerings, store_offering

    assert store_offering({
        "accession": "0000000000-25-000017", "form": "S-3",
        "filer_cik": 320193, "filer_name": "Issuer Inc",
        "registrant_cik": 320193, "registrant_name": "Issuer Inc",
        "security_title": "Common Stock", "known_at": "2024-06-01",
    }, root=tmp_path) == 1
    by_filer = query_offerings(filer_cik=320193, root=tmp_path)
    assert by_filer[0]["form"] == "S-3"
    by_registrant = query_offerings(registrant="Issuer Inc", root=tmp_path)
    assert [r["accession"] for r in by_registrant] == ["0000000000-25-000017"]
    # A shelf registration records proposed terms only; nothing here claims issuance.
    assert "issued" not in str(by_filer[0]).lower()
