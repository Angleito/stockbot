"""Unit tests for scripts/verify_tool_health.check_handler (no network)."""
from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from app import tools as tools_mod
from app.policy import Capability, RequestContext
from scripts import verify_tool_health as vth
from scripts.verify_tool_health import (
    _evaluate_envelope,
    _handler_worker,
    check_handler,
)


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

def test_handler_worker_execute_raise_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    caps = [c.value for c in ctx.capabilities]

    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(vth, "execute_tool", _boom)
    reader, writer = multiprocessing.Pipe(duplex=False)
    try:
        _handler_worker(writer, "thesis_create", {"user_thesis": "x"}, ctx.principal_id, caps, str(tmp_path))
        assert reader.poll(5)
        msg = reader.recv()
    finally:
        reader.close()
    assert isinstance(msg, dict)
    assert msg.get("worker_ok") is False
    assert "RuntimeError" in str(msg.get("reason"))


def test_handler_worker_setup_raise_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    caps = [c.value for c in ctx.capabilities]

    def _bad_swaps(_name: str) -> object:
        raise RuntimeError("seam boom")

    monkeypatch.setattr(vth, "_handler_swaps", _bad_swaps)
    reader, writer = multiprocessing.Pipe(duplex=False)
    try:
        _handler_worker(writer, "thesis_create", {"user_thesis": "x"}, ctx.principal_id, caps, str(tmp_path))
        assert reader.poll(5)
        msg = reader.recv()
    finally:
        reader.close()
    assert isinstance(msg, dict)
    assert msg.get("worker_ok") is False
    assert "setup" in str(msg.get("reason")).lower()


def test_handler_worker_unsendable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    caps = [c.value for c in ctx.capabilities]

    def _unsendable(*args: object, **kwargs: object) -> object:
        # ponytail: lambda reliably fails send pickling; object() pickles fine
        return {"ok": True, "payload": lambda: None}

    monkeypatch.setattr(vth, "execute_tool", _unsendable)
    reader, writer = multiprocessing.Pipe(duplex=False)
    try:
        _handler_worker(writer, "thesis_create", {"user_thesis": "x"}, ctx.principal_id, caps, str(tmp_path))
        assert reader.poll(5)
        msg = reader.recv()
    finally:
        reader.close()
    assert isinstance(msg, dict)
    assert msg.get("worker_ok") is False
    assert "sendable" in str(msg.get("reason"))


def test_evaluate_envelope_structured_error_pass_and_worker_fail() -> None:
    assert _evaluate_envelope({"worker_ok": True, "result": {"error": "data unavailable", "error_type": "upstream"}}) is None
    reason = _evaluate_envelope({"worker_ok": False, "reason": "execute_tool raised RuntimeError: boom"})
    assert isinstance(reason, str)
    assert reason.startswith("handler worker failed")


def test_check_handler_timeout_reaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    before = {p.pid for p in multiprocessing.active_children()}
    monkeypatch.setattr(vth, "HANDLER_CALL_TIMEOUT_S", 0)
    reason = check_handler("thesis_create", {"user_thesis": "NVDA AI demand stays strong."}, ctx)
    assert reason == "timed out after 0s"
    assert {p.pid for p in multiprocessing.active_children()} <= before
    monkeypatch.undo()
    assert check_handler("thesis_create", {"user_thesis": "NVDA AI demand stays strong."}, _ctx(tmp_path)) is None
