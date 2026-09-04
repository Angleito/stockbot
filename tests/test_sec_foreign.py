"""Offline tests for reporting-regime detection (no network)."""

from types import SimpleNamespace

from app.sec import foreign
from app.sec.foreign import reporting_regime


def _filings(*forms):
    return [SimpleNamespace(form=f, accession_no=f"a{i}")
            for i, f in enumerate(forms)]


def test_20f_history_is_foreign_20f(monkeypatch):
    monkeypatch.setattr(foreign, "list_sec_filings",
                        lambda *a, **k: _filings("20-F", "6-K"))
    out = reporting_regime("AAA")
    assert out["regime"] == "foreign-20F"
    assert out["evidence_forms"] == ["20-F", "6-K"]


def test_40f_beats_20f(monkeypatch):
    monkeypatch.setattr(foreign, "list_sec_filings",
                        lambda *a, **k: _filings("20-F", "40-F"))
    assert reporting_regime("AAA")["regime"] == "foreign-40F"


def test_10k_is_domestic(monkeypatch):
    monkeypatch.setattr(foreign, "list_sec_filings",
                        lambda *a, **k: _filings("10-K", "10-Q", "8-K"))
    assert reporting_regime("AAA")["regime"] == "domestic"


def test_empty_is_unknown(monkeypatch):
    monkeypatch.setattr(foreign, "list_sec_filings",
                        lambda *a, **k: [])
    out = reporting_regime("AAA")
    assert out["regime"] == "unknown"
    assert out["evidence_forms"] == []


def test_failure_is_unknown(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr(foreign, "list_sec_filings", boom)
    assert reporting_regime("AAA")["regime"] == "unknown"


def test_generic_remainder_passes_through(monkeypatch):
    received = []

    class FakeCompany:
        def get_filings(self, **kwargs):
            received.append(kwargs.get("form"))
            return []

    monkeypatch.setattr("app.sec.filings.get_company",
                        lambda ticker: FakeCompany())
    import app.sec.filings as filings_mod

    forms = ["20-F", "6-K", "40-F", "F-3", "25", "15-12B", "D",
             "SD", "CORRESP", "UPLOAD"]
    for form in forms:
        assert filings_mod.list_sec_filings("AAA", forms=[form]) == []
    assert received == [[f] for f in forms]
