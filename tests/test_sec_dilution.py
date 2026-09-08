"""Offline tests for app/sec/dilution.py (no network)."""

from types import SimpleNamespace

import pytest

import app.sec.dilution as dilution


def test_exact_dilution_math() -> None:
    out = dilution.dilution_profile(existing_shares=100, new_shares=25)
    assert out["dilution_pct"] == 20.0
    assert out["fully_diluted_shares"] == 125


def test_missing_or_zero_existing_not_quantifiable() -> None:
    assert dilution.dilution_profile(existing_shares=None,
                                     new_shares=25)["dilution_pct"] == "not_quantifiable"
    assert dilution.dilution_profile(existing_shares=0,
                                     new_shares=25)["dilution_pct"] == "not_quantifiable"
    assert dilution.dilution_profile(existing_shares=100,
                                     new_shares=None)["dilution_pct"] == "not_quantifiable"


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def test_inputs_formulas_accessions_echoed() -> None:
    out = dilution.dilution_profile(existing_shares=100, new_shares=25,
                                    market_cap=1000, atm_size=100,
                                    source_accessions=["a1"])
    inputs = _as_dict(out["inputs"])
    assert inputs["existing_shares"] == 100
    assert inputs["new_shares"] == 25
    formulas = _as_dict(out["formulas"])
    assert formulas["dilution_pct"] == \
        "dilution_pct = new_shares / (existing_shares + new_shares) * 100"
    assert formulas["atm_pct_of_market_cap"] == \
        "atm_pct_of_market_cap = atm_size / market_cap * 100"
    assert formulas["fully_diluted_shares"] == \
        "fully_diluted_shares = existing + new + convertible + warrant"
    assert out["atm_pct_of_market_cap"] == 10.0
    assert out["source_accessions"] == ("a1",)


def test_profile_observes_without_computing(monkeypatch: pytest.MonkeyPatch) -> None:
    history = [SimpleNamespace(form="424B5", shares=100, accession_no="a1"),
               SimpleNamespace(form="S-3", shares=1000, accession_no="a2"),
               SimpleNamespace(form="S-3/A", shares=1000, accession_no="a3"),
               SimpleNamespace(form="424B5", shares=50, accession_no="a4"),
               SimpleNamespace(form="S-8", shares=999, accession_no="a5"),
               SimpleNamespace(form="424B5", shares=None, accession_no="a6")]

    def _fake_history(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return history

    def _fake_fundamentals(ticker: str, metric: str, as_of: object = None) -> dict[str, float]:
        return {"shares_outstanding": 850.0}

    monkeypatch.setattr(dilution, "get_offering_history", _fake_history)
    monkeypatch.setattr(dilution, "get_fundamentals", _fake_fundamentals)
    out = dilution.get_dilution_profile("ACME")
    assert out["dilution_pct"] == "not_quantifiable"
    assert _as_dict(out["inputs"])["new_shares"] is None
    assert out["sum_of_disclosed_share_counts"] == 150
    assert out["offering_accessions"] == ("a1", "a4")
    assert out["registration_accessions"] == ("a2", "a3", "a5")
    assert out["registered_capacity"] == "not_quantifiable"
    assert out["source_accessions"] == ("a1", "a4", "a2", "a3", "a5")
    assert out["fully_diluted_shares"] == "not_quantifiable"
    note = out["note"]
    assert isinstance(note, str)
    assert "424B share counts are summed across disclosures without deduplication. This is not confirmed issuance and must not be interpreted as incremental dilution." in note
    assert "offering_shares_disclosed" not in out
