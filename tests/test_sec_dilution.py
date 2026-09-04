"""Offline tests for app/sec/dilution.py (no network)."""

from types import SimpleNamespace

import app.sec.dilution as dilution


def test_exact_dilution_math():
    out = dilution.dilution_profile(existing_shares=100, new_shares=25)
    assert out["dilution_pct"] == 20.0
    assert out["fully_diluted_shares"] == 125


def test_missing_or_zero_existing_not_quantifiable():
    assert dilution.dilution_profile(existing_shares=None,
                                     new_shares=25)["dilution_pct"] == "not_quantifiable"
    assert dilution.dilution_profile(existing_shares=0,
                                     new_shares=25)["dilution_pct"] == "not_quantifiable"
    assert dilution.dilution_profile(existing_shares=100,
                                     new_shares=None)["dilution_pct"] == "not_quantifiable"


def test_inputs_formulas_accessions_echoed():
    out = dilution.dilution_profile(existing_shares=100, new_shares=25,
                                    market_cap=1000, atm_size=100,
                                    source_accessions=["a1"])
    assert out["inputs"]["existing_shares"] == 100
    assert out["inputs"]["new_shares"] == 25
    assert out["formulas"]["dilution_pct"] == \
        "dilution_pct = new_shares / (existing_shares + new_shares) * 100"
    assert out["formulas"]["atm_pct_of_market_cap"] == \
        "atm_pct_of_market_cap = atm_size / market_cap * 100"
    assert out["formulas"]["fully_diluted_shares"] == \
        "fully_diluted_shares = existing + new + convertible + warrant"
    assert out["atm_pct_of_market_cap"] == 10.0
    assert out["source_accessions"] == ("a1",)


def test_profile_sums_known_shares_only(monkeypatch):
    history = [SimpleNamespace(shares=100, accession_no="a1"),
               SimpleNamespace(shares=None, accession_no="a2"),
               SimpleNamespace(shares=50, accession_no="a3")]
    monkeypatch.setattr(dilution, "get_offering_history",
                        lambda *a, **k: history)
    monkeypatch.setattr(dilution, "get_fundamentals",
                        lambda t, m, as_of=None: {"shares_outstanding": 850.0})
    out = dilution.get_dilution_profile("ACME")
    assert out["inputs"]["new_shares"] == 150
    assert out["source_accessions"] == ("a1", "a3")
    assert out["dilution_pct"] == 150 / 1000 * 100
