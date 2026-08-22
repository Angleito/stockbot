"""Offline tests for the corrected shares_outstanding fundamental.

The metric was renamed from 'shares_float' because the value is SEC-reported
shares outstanding, not public float. The old name remains a deprecated alias
that returns the same value with an explicit note.
"""

import pandas as pd
import pytest

from app import edgar_client
from app.tools import TOOLS


class _FakeFacts:
    def to_dataframe(self):
        return pd.DataFrame([
            {"concept": "dei:EntityCommonStockSharesOutstanding", "value": 1000, "period_end": "2026-08-01"},
        ])


class _FakeCompany:
    def __init__(self, ticker):
        self.ticker = ticker
        self.name = "Fake Corp"
        self.cik = "0000000001"
        self.sic_description = "Fake Industry"

    def get_facts(self):
        return _FakeFacts()


class _FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, ttl=None):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


@pytest.fixture(autouse=True)
def fake_edgar(monkeypatch):
    monkeypatch.setattr(edgar_client, "Company", _FakeCompany)
    monkeypatch.setattr(edgar_client, "cache", _FakeCache())


def test_shares_outstanding_is_sec_shares_not_float():
    result = edgar_client.get_fundamentals("FAKE", "shares_outstanding")
    assert result["shares_outstanding"] == 1000
    assert result["as_of"] == "2026-08-01"
    assert "not public float" in result["note"]


def test_shares_float_alias_returns_shares_outstanding():
    result = edgar_client.get_fundamentals("FAKE", "shares_float")
    assert result["shares_outstanding"] == 1000
    assert "not public float" in result["note"]


def test_tool_schema_offers_shares_outstanding_not_shares_float():
    schema = next(item for item in TOOLS if item["function"]["name"] == "get_fundamentals")
    enum = schema["function"]["parameters"]["properties"]["metric"]["enum"]
    assert "shares_outstanding" in enum
    assert "shares_float" not in enum
    assert "not public float" in schema["function"]["description"]