"""Unit tests for scripts/verify_tool_health.check_handler (no network)."""
from __future__ import annotations

import os
from pathlib import Path

from app import tools as tools_mod
from app.policy import Capability, RequestContext
from scripts.verify_tool_health import check_handler


def _ctx(tmp_path: Path) -> RequestContext:
    return RequestContext("handler-test", frozenset({Capability.RESEARCH}), data_root=tmp_path)


def test_check_handler_local_pass(tmp_path: Path) -> None:
    assert check_handler("thesis_create", {"user_thesis": "NVDA AI demand stays strong."}, _ctx(tmp_path)) is None


def test_check_handler_web_pass(tmp_path: Path) -> None:
    assert check_handler("search_web", {"query": "Apple"}, _ctx(tmp_path)) is None


def test_check_handler_unknown_tool_fails_closed(tmp_path: Path) -> None:
    reason = check_handler("no_such_tool", {}, _ctx(tmp_path))
    assert reason == "no deterministic provider seam"
    assert f"handler: {reason}".startswith("handler:")


def test_check_handler_restores_seams(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    before = {
        "sec_list": tools_mod.sec.list_sec_filings,
        "finra_query": tools_mod.finra_client.query_dataset,
        "exa_search": tools_mod.exa_client.search,
        "analyst_est": tools_mod.analyst_client.get_analyst_estimates,
        "env": os.environ.get("GOOGLE_DATA_ENABLED"),
    }
    cases: list[tuple[str, dict[str, object]]] = [
        ("list_sec_filings", {"identifier": "AAPL"}),
        ("query_finra", {"dataset": "otcMarket/consolidatedShortInterest"}),
        ("search_web", {"query": "Apple"}),
        ("get_analyst_estimates", {"ticker": "AAPL"}),
        ("get_trend_evidence", {}),
        ("investigate_social_arbitrage_candidate", {"term": "Stanley"}),
    ]
    for name, fixture in cases:
        assert check_handler(name, dict(fixture), ctx) is None
    assert tools_mod.sec.list_sec_filings is before["sec_list"]
    assert tools_mod.finra_client.query_dataset is before["finra_query"]
    assert tools_mod.exa_client.search is before["exa_search"]
    assert tools_mod.analyst_client.get_analyst_estimates is before["analyst_est"]
    assert os.environ.get("GOOGLE_DATA_ENABLED") == before["env"]
