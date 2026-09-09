"""Offline regression tests for XBRL concept matching (all-tokens + ambiguity)."""

import pandas as pd
import pytest

from app import edgar_client


class _FakeFacts:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self._rows)


class _FakeCompany:
    rows: list[dict[str, object]] = []

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.name = "Fake Corp"
        self.cik = "0000000001"
        self.sic_description = "Fake Industry"

    def get_facts(self) -> _FakeFacts:
        return _FakeFacts(type(self).rows)


class _FakeCache:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def get(self, key: str, ttl: float | None = None) -> object | None:
        return self.store.get(key)

    def set(self, key: str, value: object) -> None:
        self.store[key] = value


@pytest.fixture(autouse=True)
def fake_edgar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(edgar_client, "Company", _FakeCompany)
    monkeypatch.setattr(edgar_client, "cache", _FakeCache())
    monkeypatch.setattr(edgar_client, "_ensure_init", lambda: None)


def _row(concept: str, value: float = 1000) -> dict[str, object]:
    return {"concept": concept, "value": value, "period_end": "2026-08-01"}


def test_total_revenue_requires_all_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FakeCompany, "rows", [
        _row("us-gaap:TotalAssets"),
        _row("us-gaap:RevenueFromContractWithCustomer"),
        _row("us-gaap:TotalRevenues"),
    ])
    result = edgar_client.get_xbrl_facts("FAKE", "Revenue Total")
    assert "error" not in result
    matching = result["matching_concepts"]
    assert isinstance(matching, list)
    assert len(matching) >= 1
    assert all(r["concept"] == "us-gaap:TotalRevenues" for r in matching)


def test_ambiguous_concepts_return_no_data_without_mixing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_FakeCompany, "rows", [
        _row("us-gaap:TotalRevenues"),
        _row("us-gaap:TotalRevenuesNet"),
    ])
    result = edgar_client.get_xbrl_facts("FAKE", "Revenue Total")
    assert "error" in result
    assert "ambiguous" in str(result["error"])
    assert "matching_concepts" not in result
