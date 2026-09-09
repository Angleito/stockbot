"""Offline tests for reporting-regime detection (no network)."""

from types import SimpleNamespace
from typing import NoReturn

import pytest

from app.sec import foreign
from app.sec.foreign import reporting_regime


def _filings(*forms: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(form=f, accession_no=f"a{i}")
            for i, f in enumerate(forms)]


def test_20f_history_is_foreign_20f(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return _filings("20-F", "6-K")

    monkeypatch.setattr(foreign, "list_sec_filings", _fake_list)
    out = reporting_regime("AAA")
    assert out["regime"] == "foreign-20F"
    assert out["evidence_forms"] == ["20-F", "6-K"]


def test_40f_beats_20f(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return _filings("20-F", "40-F")

    monkeypatch.setattr(foreign, "list_sec_filings", _fake_list)
    assert reporting_regime("AAA")["regime"] == "foreign-40F"


def test_10k_is_domestic(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return _filings("10-K", "10-Q", "8-K")

    monkeypatch.setattr(foreign, "list_sec_filings", _fake_list)
    assert reporting_regime("AAA")["regime"] == "domestic"


def test_empty_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_list(*args: object, **kwargs: object) -> list[SimpleNamespace]:
        return []

    monkeypatch.setattr(foreign, "list_sec_filings", _fake_list)
    out = reporting_regime("AAA")
    assert out["regime"] == "unknown"
    assert out["evidence_forms"] == []


def test_failure_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("offline")

    monkeypatch.setattr(foreign, "list_sec_filings", boom)
    assert reporting_regime("AAA")["regime"] == "unknown"


def test_generic_remainder_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[object] = []

    class FakeCompany:
        def get_filings(self, **kwargs: object) -> list[SimpleNamespace]:
            received.append(kwargs.get("form"))
            return []

    def _fake_get_company(ticker_or_cik: str) -> FakeCompany:
        return FakeCompany()

    monkeypatch.setattr("app.sec.filings.get_company", _fake_get_company)
    import app.sec.filings as filings_mod

    forms = ["20-F", "6-K", "40-F", "F-3", "25", "15-12B", "D",
             "SD", "CORRESP", "UPLOAD"]
    for form in forms:
        assert filings_mod.list_sec_filings("AAA", forms=[form]) == []
    assert received == [[f] for f in forms]
