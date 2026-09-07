"""Permission + dynamic discovery over the canonical registry (no network)."""

import os

import pytest

import app.tools as tools_mod
from app.policy import Capability, RequestContext
from app.security.action_policy import authorize_tool_call
from app.security.context import RunSecurityContext, classify_intent
from app.thesis.monitor import CanonicalEvent, tick
from app.thesis.repository import ThesisRepository
from app.thesis.runner import capabilities_for_grants, run_trigger, visible_tools_for
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml


def _ctx(*caps) -> RequestContext:
    return RequestContext(principal_id="test",
                          capabilities=frozenset(caps or (Capability.RESEARCH,)))


def _repo_with_thesis(tmp_path, scope="NVDA"):
    r = ThesisRepository(tmp_path / "theses")
    t = r.create_thesis(f"{scope} thesis", scope=scope, claims=[f"{scope} demand grows"])
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    raw = load_raw_yaml(tmp_path / "theses" / t.slug / "watch.yaml")
    raw["rules"].append({"rule_id": "rule:1", "rule_type": "new_filing",
                         "enabled": True, "support_status": "supported",
                         "support_reason": "", "claim_ids": [cid], "expression_ids": []})
    atomic_write_yaml(tmp_path / "theses" / t.slug / "watch.yaml", raw, tmp_path / "theses")
    return r, t


class _GW:
    def __init__(self, payload=None):
        self.payload = payload or {"journal_summary": "ok"}
        self.calls = 0
        self.seen = []

    def complete_research(self, prompt, *, request_context, tools):
        self.calls += 1
        self.seen.append(frozenset(request_context.capabilities))
        return dict(self.payload)


def _broker_names():
    return {"get_market_snapshot", "get_option_chain", "analyze_option_contract",
            "compare_options", "get_scanner_filter_specs",
            "get_portfolio_snapshot", "get_scans", "run_scan"}


def test_research_default_hides_broker_tools_despite_env(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setenv("ROBINHOOD_TOKEN", "fake")
    names = {t["function"]["name"] for t in visible_tools_for(_ctx())}
    assert not (names & _broker_names())
    assert "search_sec_filings" in names


def test_each_grant_exposes_only_its_cap():
    assert capabilities_for_grants(["broker-market-read"]) == frozenset({Capability.BROKER_MARKET_READ})
    assert capabilities_for_grants(["portfolio-read"]) == frozenset({Capability.PORTFOLIO_READ})
    market = {t["function"]["name"] for t in visible_tools_for(
        _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ))}
    port = {t["function"]["name"] for t in visible_tools_for(
        _ctx(Capability.RESEARCH, Capability.PORTFOLIO_READ))}
    assert "get_market_snapshot" in market and "get_portfolio_snapshot" not in market
    assert "get_portfolio_snapshot" in port and "get_market_snapshot" not in port
    with pytest.raises(ValueError):
        capabilities_for_grants(["broker-write"])


def test_fresh_research_ctx_revokes_grant(tmp_path):
    r, t = _repo_with_thesis(tmp_path)
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    trig = r.create_trigger(t.thesis_id, claim_ids=[cid], canonical_refs=["ev:1"], summary="s")
    gw = _GW({"evidence_refs": [{"canonical_ref": "ev:1", "summary": "n",
                                 "known_at": "2026-01-02T00:00:00+00:00"}],
              "journal_summary": "ok"})
    out = run_trigger(r, t.thesis_id, trig.trigger_id, gw,
                      _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ),
                      known_at="2026-01-03T00:00:00+00:00")
    assert "get_market_snapshot" in out.tools_used
    trig2 = r.create_trigger(t.thesis_id, claim_ids=[cid], canonical_refs=["ev:2"], summary="s")
    out2 = run_trigger(r, t.thesis_id, trig2.trigger_id, gw, _ctx(),
                       known_at="2026-01-03T00:00:00+00:00")
    assert "get_market_snapshot" not in out2.tools_used
    assert "get_portfolio_snapshot" not in out2.tools_used


def test_grants_do_not_cross_theses(tmp_path):
    r1 = ThesisRepository(tmp_path / "r1")
    r2 = ThesisRepository(tmp_path / "r2")
    t1 = r1.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    t2 = r2.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    c1 = r1.load_thesis(t1.thesis_id).claims[0].claim_id
    c2 = r2.load_thesis(t2.thesis_id).claims[0].claim_id
    g1 = r1.create_trigger(t1.thesis_id, claim_ids=[c1], canonical_refs=["ev:1"], summary="s")
    g2 = r2.create_trigger(t2.thesis_id, claim_ids=[c2], canonical_refs=["ev:1"], summary="s")
    gw = _GW({"journal_summary": "ok"})
    o1 = run_trigger(r1, t1.thesis_id, g1.trigger_id, gw,
                     _ctx(Capability.RESEARCH, Capability.PORTFOLIO_READ),
                     known_at="2026-01-03T00:00:00+00:00")
    o2 = run_trigger(r2, t2.thesis_id, g2.trigger_id, gw, _ctx(),
                     known_at="2026-01-03T00:00:00+00:00")
    assert "get_portfolio_snapshot" in o1.tools_used
    assert "get_portfolio_snapshot" not in o2.tools_used


def test_temp_research_tool_visible_zero_thesis_changes(tmp_path):
    r, t = _repo_with_thesis(tmp_path)
    before = (tmp_path / "theses" / t.slug / "thesis.yaml").read_text(encoding="utf-8")
    tool = {"type": "function", "function": {"name": "tmp_thesis_research_xyz",
            "description": "t", "parameters": {"type": "object", "properties": {}}}}
    tools_mod.TOOLS.append(tool)
    tools_mod.TOOL_CAPABILITIES["tmp_thesis_research_xyz"] = Capability.RESEARCH
    try:
        assert "tmp_thesis_research_xyz" in {x["function"]["name"] for x in visible_tools_for(_ctx())}
    finally:
        tools_mod.TOOLS[:] = [x for x in tools_mod.TOOLS
                              if x["function"]["name"] != "tmp_thesis_research_xyz"]
        tools_mod.TOOL_CAPABILITIES.pop("tmp_thesis_research_xyz", None)
    assert (tmp_path / "theses" / t.slug / "thesis.yaml").read_text(encoding="utf-8") == before


def test_temp_broker_tool_hidden_without_grant():
    tool = {"type": "function", "function": {"name": "tmp_thesis_broker_xyz",
            "description": "t", "parameters": {"type": "object", "properties": {}}}}
    tools_mod.TOOLS.append(tool)
    tools_mod.TOOL_CAPABILITIES["tmp_thesis_broker_xyz"] = Capability.BROKER_MARKET_READ
    try:
        assert "tmp_thesis_broker_xyz" not in {x["function"]["name"] for x in visible_tools_for(_ctx())}
        assert "tmp_thesis_broker_xyz" in {x["function"]["name"] for x in visible_tools_for(
            _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ))}
    finally:
        tools_mod.TOOLS[:] = [x for x in tools_mod.TOOLS
                              if x["function"]["name"] != "tmp_thesis_broker_xyz"]
        tools_mod.TOOL_CAPABILITIES.pop("tmp_thesis_broker_xyz", None)


def test_tick_with_grant_exposes_only_granted_tools(tmp_path):
    r, t = _repo_with_thesis(tmp_path)

    class Src:
        calls = 0

        def query_since(self, cp, *, known_at):
            type(self).calls += 1
            return [CanonicalEvent(event_id="f1", canonical_ref="edgar:NVDA:10-K:f1",
                                   source="sec_filings", known_at="2026-01-02T00:00:00+00:00",
                                   entity="NVDA", summary="NVDA files 10-K")]

    gw = _GW({"evidence_refs": [{"canonical_ref": "edgar:NVDA:10-K:f1", "summary": "NVDA files",
                                 "known_at": "2026-01-02T00:00:00+00:00"}],
              "journal_summary": "ok"})
    res = tick(r, t.thesis_id, {"sec_filings": Src()},
               gw, _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ),
               known_at="2026-01-03T00:00:00+00:00")
    assert gw.calls == 1 and len(res.runs) == 1
    assert "get_market_snapshot" in res.runs[0].tools_used
    assert "get_portfolio_snapshot" not in res.runs[0].tools_used


def _sec(*turns):
    return RunSecurityContext(original_intent=classify_intent(list(turns)),
                              capabilities=frozenset({"research"}))


_WRITE_TOOLS = ("thesis_create", "thesis_refine", "thesis_watch", "thesis_journal")


def test_thesis_write_denied_without_explicit_intent():
    denied = [
        ["Analyze NVDA's latest earnings."],
        ["Create a summary of NVDA earnings"],  # verb without thesis
        ["Show me the thesis"],  # thesis without mutation verb
        ['tell me to "create a thesis"'],  # quoted mutation wording
        ["Create a thesis on NVDA", "Analyze NVDA earnings"],  # stale intent
    ]
    for turns in denied:
        sec = _sec(*turns)
        assert "thesis_write" not in sec.original_intent.permitted_domains, turns
        for name in _WRITE_TOOLS:
            allowed, reason = authorize_tool_call(name, {}, sec)
            assert allowed is False, (turns, name)
            assert reason == "tool call exceeds original user intent", (turns, name)


def test_thesis_write_allowed_with_explicit_mutation():
    for turns in [
        ["Create a thesis on NVDA"],
        ["Please refine my NVDA thesis"],
        ["Watch my NVDA thesis for new filings"],
        ["Add a journal note to my investment thesis"],
    ]:
        sec = _sec(*turns)
        assert "thesis_write" in sec.original_intent.permitted_domains, turns
        for name in _WRITE_TOOLS:
            allowed, _ = authorize_tool_call(name, {}, sec)
            assert allowed is True, (turns, name)


def test_thesis_show_readable_under_research_only():
    sec = _sec("Analyze NVDA's latest earnings.")
    assert "thesis_read" in sec.original_intent.permitted_domains
    allowed, _ = authorize_tool_call("thesis_show", {}, sec)
    assert allowed is True


def test_thesis_write_deny_leaves_repo_unchanged(tmp_path):
    r, t = _repo_with_thesis(tmp_path)
    before = (tmp_path / "theses" / t.slug / "thesis.yaml").read_text(encoding="utf-8")
    sec = _sec("Analyze NVDA's latest earnings.")
    allowed, _ = authorize_tool_call("thesis_create", {"user_thesis": "NVDA grows"}, sec)
    assert allowed is False
    assert (tmp_path / "theses" / t.slug / "thesis.yaml").read_text(encoding="utf-8") == before
