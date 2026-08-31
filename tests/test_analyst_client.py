"""Unit tests for the analyst-consensus (Yahoo Finance) and index-weight
(Slickcharts) data sources.

Deterministic and offline: all HTTP is mocked against sanitized fixtures in
tests/fixtures/yahoo/ and tests/fixtures/slickcharts/.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from app import analyst_client
from app import tool_render
from app.tools import execute_tool
from app.policy import LOCAL_CONTEXT

YAHOO_FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
SLICK_FIXTURES = Path(__file__).parent / "fixtures" / "slickcharts"


def _load_yahoo(name: str) -> dict:
    return json.loads((YAHOO_FIXTURES / name).read_text())


def _response(payload=None, text: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = text
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _fake_session(*responses: MagicMock):
    """Session whose get() returns responses in order, last one repeated."""
    calls = {"i": 0}

    def get(url, **kwargs):
        idx = min(calls["i"], len(responses) - 1)
        calls["i"] += 1
        return responses[idx]

    session = MagicMock()
    session.get.side_effect = get
    return session


class FakeCache:
    """In-memory stand-in for app.cache that records accesses."""

    def __init__(self):
        self.data = {}
        self.gets = 0
        self.sets = 0

    def get(self, key, ttl=None):
        self.gets += 1
        return self.data.get(key)

    def set(self, key, value):
        self.sets += 1
        self.data[key] = value


@pytest.fixture
def fake_cache(monkeypatch):
    cache = FakeCache()
    monkeypatch.setattr(analyst_client, "cache", cache)
    return cache


@pytest.fixture
def yahoo_session(monkeypatch):
    payload = _load_yahoo("quoteSummary_NVDA.json")
    monkeypatch.setattr(
        analyst_client,
        "_ensure_session",
        lambda: _fake_session(_response(payload=payload)),
    )
    monkeypatch.setattr(analyst_client, "_get_crumb", lambda: "crumb-1")


def test_get_analyst_estimates_normalized(yahoo_session, fake_cache):
    result = analyst_client.get_analyst_estimates("NVDA")

    assert result["ticker"] == "NVDA"
    assert result["source"].startswith("Yahoo")
    assert result["quote"]["price"] == 213.05
    assert result["price_targets"]["median"] == 300.0
    assert result["price_targets"]["mean"] == pytest.approx(304.72882)
    assert result["price_targets"]["high"] == 500.0
    assert result["price_targets"]["low"] == 180.0
    assert result["price_targets"]["num_analysts"] == 59
    assert result["price_targets"]["recommendation"] == "Strong Buy"
    assert result["valuation"]["forward_pe"] == pytest.approx(16.336937)

    periods = {r["period"]: r for r in result["forward_estimates"]}
    cq = periods["current_quarter"]
    assert cq["period_end_date"] == "2026-07-31"
    assert cq["eps_avg"] == pytest.approx(2.09161)
    assert cq["eps_growth_pct"] == pytest.approx(99.2)
    assert cq["revenue_avg"] == pytest.approx(92_176_624_640)
    assert cq["revenue_growth_pct"] == pytest.approx(97.2)
    assert cq["eps_revision"]["current"] == pytest.approx(2.09161)
    assert cq["eps_revision"]["days60_ago"] == pytest.approx(2.07667)

    ny = periods["next_fiscal_year"]
    assert ny["period_end_date"] == "2028-01-31"
    assert ny["eps_avg"] == pytest.approx(13.041)

    assert fake_cache.sets == 1
    assert fake_cache.gets == 1


def test_get_analyst_estimates_cache_hit(yahoo_session, fake_cache):
    fake_cache.data["analyst_estimates:NVDA"] = {"cached": True}
    result = analyst_client.get_analyst_estimates("NVDA")
    assert result == {"cached": True}


def test_get_analyst_estimates_crumb_refresh(monkeypatch, fake_cache):
    payload = _load_yahoo("quoteSummary_NVDA.json")
    session = _fake_session(
        _response(status=401),  # stale crumb -> refresh + retry
        _response(payload=payload),
    )
    monkeypatch.setattr(analyst_client, "_ensure_session", lambda: session)
    crumb_calls = {"n": 0}

    def crumb():
        crumb_calls["n"] += 1
        return f"crumb-{crumb_calls['n']}"

    monkeypatch.setattr(analyst_client, "_get_crumb", crumb)
    result = analyst_client.get_analyst_estimates("NVDA")
    assert result["price_targets"]["median"] == 300.0
    assert crumb_calls["n"] == 2
    assert session.get.call_count == 2


def test_get_analyst_estimates_missing_ticker():
    result = analyst_client.get_analyst_estimates("")
    assert "error" in result


def test_get_analyst_estimates_network_error(monkeypatch, fake_cache):
    def boom(url, **kwargs):
        raise requests.ConnectionError("no network")

    session = MagicMock()
    session.get.side_effect = boom
    monkeypatch.setattr(analyst_client, "_ensure_session", lambda: session)
    result = analyst_client.get_analyst_estimates("NVDA")
    assert "error" in result
    assert "NVDA" in result["error"]


def test_get_sp500_weight(monkeypatch, fake_cache):
    html = (SLICK_FIXTURES / "sp500_trimmed.html").read_text()
    monkeypatch.setattr(
        analyst_client,
        "_ensure_session",
        lambda: _fake_session(_response(text=html)),
    )
    result = analyst_client.get_sp500_weight("NVDA")
    assert result["ticker"] == "NVDA"
    assert result["company"] == "NVIDIA Corp"
    assert result["rank"] == 1
    assert result["weight_pct"] == pytest.approx(7.40)
    assert fake_cache.sets == 1


def test_get_sp500_weight_symbol_case(monkeypatch, fake_cache):
    html = (SLICK_FIXTURES / "sp500_trimmed.html").read_text()
    monkeypatch.setattr(
        analyst_client,
        "_ensure_session",
        lambda: _fake_session(_response(text=html)),
    )
    result = analyst_client.get_sp500_weight("nvda")
    assert result["ticker"] == "NVDA"
    assert result["weight_pct"] == pytest.approx(7.40)


def test_get_sp500_weight_not_in_index(monkeypatch, fake_cache):
    html = (SLICK_FIXTURES / "sp500_trimmed.html").read_text()
    monkeypatch.setattr(
        analyst_client,
        "_ensure_session",
        lambda: _fake_session(_response(text=html)),
    )
    result = analyst_client.get_sp500_weight("ZZZZ")
    assert "error" in result
    assert "not found" in result["error"]


def test_get_sp500_weight_http_403(monkeypatch, fake_cache):
    monkeypatch.setattr(
        analyst_client,
        "_ensure_session",
        lambda: _fake_session(_response(status=403)),
    )
    result = analyst_client.get_sp500_weight("NVDA")
    assert "error" in result
    assert "403" in result["error"]


def test_execute_tool_dispatch(monkeypatch, fake_cache):
    payload = _load_yahoo("quoteSummary_NVDA.json")
    html = (SLICK_FIXTURES / "sp500_trimmed.html").read_text()
    yahoo_session = _fake_session(_response(payload=payload))
    slick_session = _fake_session(_response(text=html))

    def session_for(url, **kwargs):
        if "yahoo" in url:
            return yahoo_session.get(url, **kwargs)
        return slick_session.get(url, **kwargs)

    session = MagicMock()
    session.get.side_effect = session_for
    monkeypatch.setattr(analyst_client, "_ensure_session", lambda: session)
    monkeypatch.setattr(analyst_client, "_get_crumb", lambda: "crumb-1")

    result = execute_tool("get_analyst_estimates", {"ticker": "NVDA"}, "model-x", context=LOCAL_CONTEXT)
    assert result["price_targets"]["median"] == 300.0
    result = execute_tool("get_sp500_weight", {"ticker": "NVDA"}, "model-x", context=LOCAL_CONTEXT)
    assert result["weight_pct"] == pytest.approx(7.40)


def test_render_analyst_estimates(yahoo_session, fake_cache):
    result = analyst_client.get_analyst_estimates("NVDA")
    text = tool_render.render_tool_result(result)
    assert "NVDA analyst consensus" in text
    assert "median 300.0" in text
    assert "current_quarter" in text
    assert "EPS revision" in text


def test_render_sp500_weight():
    result = {
        "ticker": "NVDA",
        "as_of": "2026-08-25T12:00:00Z",
        "source": "Slickcharts S&P 500 constituents",
        "company": "NVIDIA Corp",
        "rank": 1,
        "weight_pct": 7.4,
        "note": "test note",
    }
    text = tool_render.render_tool_result(result)
    assert "7.4% of index market cap" in text
    assert "test note" in text