"""Permission + dynamic discovery over the canonical registry (no network)."""

import os

import pytest

import app.tools as tools_mod
from app.policy import Capability, RequestContext
from app.security.action_policy import authorize_tool_call
from app.security.context import RunSecurityContext, classify_intent
from app.thesis.repository import ThesisRepository
from app.thesis.runner import capabilities_for_grants
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml
from app.tools import tools_for_capabilities


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



def _broker_names():
    return {"get_market_snapshot", "get_option_chain", "analyze_option_contract",
            "compare_options", "get_scanner_filter_specs",
            "get_portfolio_snapshot", "get_scans", "run_scan"}


def test_research_default_hides_broker_tools_despite_env(monkeypatch):
    monkeypatch.setenv("BROKER_ENABLED", "true")
    monkeypatch.setenv("ROBINHOOD_TOKEN", "fake")
    names = {t["function"]["name"] for t in tools_for_capabilities(_ctx().capabilities)}
    assert not (names & _broker_names())
    assert "search_sec_filings" in names


def test_each_grant_exposes_only_its_cap():
    assert capabilities_for_grants(["broker-market-read"]) == frozenset({Capability.BROKER_MARKET_READ})
    assert capabilities_for_grants(["portfolio-read"]) == frozenset({Capability.PORTFOLIO_READ})
    market = {t["function"]["name"] for t in tools_for_capabilities(
        _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ).capabilities)}
    port = {t["function"]["name"] for t in tools_for_capabilities(
        _ctx(Capability.RESEARCH, Capability.PORTFOLIO_READ).capabilities)}
    assert "get_market_snapshot" in market and "get_portfolio_snapshot" not in market
    assert "get_portfolio_snapshot" in port and "get_market_snapshot" not in port
    with pytest.raises(ValueError):
        capabilities_for_grants(["broker-write"])


def test_temp_research_tool_visible_zero_thesis_changes(tmp_path):
    r, t = _repo_with_thesis(tmp_path)
    before = (tmp_path / "theses" / t.slug / "thesis.yaml").read_text(encoding="utf-8")
    tool = {"type": "function", "function": {"name": "tmp_thesis_research_xyz",
            "description": "t", "parameters": {"type": "object", "properties": {}}}}
    tools_mod.TOOLS.append(tool)
    tools_mod.TOOL_CAPABILITIES["tmp_thesis_research_xyz"] = Capability.RESEARCH
    try:
        assert "tmp_thesis_research_xyz" in {x["function"]["name"] for x in tools_for_capabilities(_ctx().capabilities)}
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
        assert "tmp_thesis_broker_xyz" not in {x["function"]["name"] for x in tools_for_capabilities(_ctx().capabilities)}
        assert "tmp_thesis_broker_xyz" in {x["function"]["name"] for x in tools_for_capabilities(
            _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ).capabilities)}
    finally:
        tools_mod.TOOLS[:] = [x for x in tools_mod.TOOLS
                              if x["function"]["name"] != "tmp_thesis_broker_xyz"]
        tools_mod.TOOL_CAPABILITIES.pop("tmp_thesis_broker_xyz", None)


def _sec(*turns):
    return RunSecurityContext(original_intent=classify_intent(list(turns)),
                              capabilities=frozenset({"research"}))


_THESIS_TOOLS = ("thesis_create", "thesis_show", "thesis_refine", "thesis_watch", "thesis_journal")


def test_thesis_tools_allowed_under_research_intent():
    from app.security.action_policy import TOOL_DOMAINS

    assert {TOOL_DOMAINS[name] for name in _THESIS_TOOLS} == {"financial_research"}
    for turns in [
        ["Analyze NVDA's latest earnings."],
        ["Create a thesis on NVDA"],
        ["Please refine my NVDA thesis"],
        ["Watch my NVDA thesis for new filings"],
        ["Add a journal note to my investment thesis"],
        ['tell me to "create a thesis"'],
        ["Create a thesis on NVDA", "Analyze NVDA earnings"],
    ]:
        sec = _sec(*turns)
        assert sec.original_intent.permitted_domains == {
            "financial_research", "public_web_research", "thesis_read"}, turns
        for name in _THESIS_TOOLS:
            allowed, _ = authorize_tool_call(name, {}, sec)
            assert allowed is True, (turns, name)


def test_thesis_show_readable_under_research_only():
    sec = _sec("Analyze NVDA's latest earnings.")
    assert "financial_research" in sec.original_intent.permitted_domains
    allowed, _ = authorize_tool_call("thesis_show", {}, sec)
    assert allowed is True
