"""Tests for intent classification and the action firewall."""

import pytest

from app.security.action_policy import TOOL_DOMAINS, authorize_tool_call
from app.security.context import (
    RunSecurityContext,
    SessionAuthorization,
    classify_intent,
)
from app.tools import PORTFOLIO_AUTHORIZED_TOOLS, TOOL_CAPABILITIES


def _run_security(user_turns: list[str]) -> RunSecurityContext:
    return RunSecurityContext(
        original_intent=classify_intent(user_turns),
        capabilities=frozenset({"research", "portfolio_read"}),
    )


# -- classify_intent ---------------------------------------------------------

def test_classify_intent_research_only():
    intent = classify_intent(["What's the latest AMD news?"])
    assert intent.request == "What's the latest AMD news?"
    assert intent.permitted_domains == {"financial_research", "public_web_research", "thesis_read"}


def test_classify_intent_portfolio_phrases():
    # History never authorizes portfolio access — only explicit session
    # approval adds `portfolio_read`. Even portfolio phrasing stays base-only.
    for turns in [
        ["What is AMD's cash balance?"],
        ["What is AMD's competitive position?"],
        ["Show portfolio"],
        ["What is AMD EPS?"],
        ["What's the latest AMD news?"],
        ["Show my account"],
        ["How does today's AMD news affect my portfolio?"],
        ["What news could affect my AMD position?"],
        ["how does that affect me?"],
    ]:
        intent = classify_intent(turns)
        assert intent.permitted_domains == {"financial_research", "public_web_research", "thesis_read"}, turns


def test_classify_intent_accumulates_across_turns():
    # History-alone-grants-nothing: no combination of user turns authorizes
    # portfolio access.
    for turns in [
        ["What's the latest AMD news?", "how does that affect me?"],
        ["Show my account", "what is AMD EPS?"],
        ["What's the latest AMD news?", "what is AMD EPS?"],
        ["Show my portfolio", "Research AMD news"],
    ]:
        intent = classify_intent(turns)
        assert "portfolio_read" not in intent.permitted_domains, turns


def test_classify_intent_request_is_last_turn():
    intent = classify_intent(["First question", "Second question"])
    assert intent.request == "Second question"


def test_classify_intent_benign_first_person_stays_research():
    for turns in [
        ["Tell me about AMD"],
        ["Show me the latest AMD news"],
        ["What if I buy AMD stock?"],
        ["Is AMD a good company?"],
    ]:
        intent = classify_intent(turns)
        assert "portfolio_read" not in intent.permitted_domains, turns


# -- authorize_tool_call -----------------------------------------------------

def test_research_intent_denies_portfolio_tools():
    run_security = _run_security(["Research AMD news."])
    allowed, reason = authorize_tool_call(
        "get_portfolio_snapshot", {}, run_security
    )
    assert allowed is False


def test_portfolio_intent_allows_portfolio_tools():
    # `portfolio_read` lives only in the explicit session grant, modeled here
    # by direct assignment — the same mechanism the approval callback uses.
    run_security = _run_security(["How does today's AMD news affect my portfolio?"])
    allowed, _ = authorize_tool_call("get_portfolio_snapshot", {}, run_security)
    assert allowed is False
    run_security.authorization = SessionAuthorization(portfolio_read=True)
    allowed, reason = authorize_tool_call("get_portfolio_snapshot", {}, run_security)
    assert allowed is True
    assert reason == ""


def test_research_intent_allows_research_and_web_tools():
    run_security = _run_security(["Research AMD news."])
    for name in ("get_xbrl_facts", "search_web", "query_finra", "get_market_snapshot"):
        allowed, _ = authorize_tool_call(name, {}, run_security)
        assert allowed is True, name


def test_unknown_tool_is_denied():
    run_security = _run_security(["Research AMD news."])
    allowed, _ = authorize_tool_call("some_new_tool", {}, run_security)
    assert allowed is False


def test_intent_ignores_assistant_and_tool_content():
    # Only USER turns feed the classifier: a hostile assistant/tool echo
    # cannot expand the intent.
    run_security = _run_security(["Research AMD news."])
    assert "portfolio_read" not in run_security.original_intent.permitted_domains


# -- parity ------------------------------------------------------------------

def test_tool_domains_cover_all_registered_tools():
    assert set(TOOL_DOMAINS) == set(TOOL_CAPABILITIES)


def test_tool_domains_portfolio_set_matches_capabilities():
    portfolio_domain_tools = {
        name for name, domain in TOOL_DOMAINS.items() if domain == "portfolio_read"
    }
    assert portfolio_domain_tools == PORTFOLIO_AUTHORIZED_TOOLS


def test_portfolio_domain_tools_are_exactly_the_private_tools():
    from app.security.context import Sensitivity
    from app.security.context_gateway import TOOL_ENVELOPES

    private_tools = {
        name
        for name, envelope in TOOL_ENVELOPES.items()
        if envelope.sensitivity is Sensitivity.PRIVATE
    }
    portfolio_tools = {
        name for name, domain in TOOL_DOMAINS.items() if domain == "portfolio_read"
    }
    assert private_tools == portfolio_tools

# ---------------------------------------------------------------------------
# Source-policy denial: SEC-only allowlist pattern. Behavior asserts against
# the kernel gates (is_sec_tool + _dispatch_check_domain + runner guard),
# not prompt text. No prod edits; future combos reuse this pattern.
# ---------------------------------------------------------------------------

def _sec_only_denies(name: str) -> bool:
    from app.research.agents.source_agent import is_sec_tool
    return not is_sec_tool(name)


def test_sec_only_denies_finra_web_market_analyst() -> None:
    for name in ("query_finra", "get_short_interest", "get_finra_datapoints",
                 "search_web", "get_market_snapshot", "get_option_chain",
                 "get_analyst_estimates", "get_sp500_weight"):
        assert _sec_only_denies(name) is True, name


def test_sec_only_allows_sec_suite() -> None:
    from app.research.agents.source_agent import is_sec_tool
    for name in ("search_sec_filings", "find_sec_entities", "list_sec_filings",
                 "get_sec_filing", "list_sec_documents", "get_sec_document",
                 "search_sec_relationships", "get_sec_search_coverage",
                 "diff_sec_filings", "get_material_events"):
        assert is_sec_tool(name) is True, name


def test_service_dispatch_check_denies_outside_sec() -> None:
    from app.research.models import Job
    from app.research.service import _dispatch_check_domain
    sec_job = Job(job_id="job:test", session_id="s", wave_id=1, parent_job_id=None,
                  job_type="source_agent", owner="t", source_domain="SEC")
    for denied in ("query_finra", "search_web", "get_market_snapshot", "get_analyst_estimates"):
        try:
            _dispatch_check_domain(sec_job, "job:test", denied)
        except ValueError as exc:
            assert "outside SEC domain" in str(exc), (denied, exc)
        else:
            raise AssertionError(f"expected denial for {denied}")
    _dispatch_check_domain(sec_job, "job:test", "search_sec_filings")


def test_allowlist_combo_pattern_future_sources() -> None:
    # Pattern pin for future combos: allowlist membership decides, not naming.
    from app.research.agents.source_agent import SEC_TOOLS, is_sec_tool
    assert "search_sec_filings" in SEC_TOOLS
    assert "search_web" not in SEC_TOOLS
    assert is_sec_tool("search_sec_filings") is True
    assert is_sec_tool("search_web") is False


def test_session_source_policy_denies_via_authorize_and_filter() -> None:
    from app.security.action_policy import filter_allowed_tools, source_denied_reason
    sec_only: dict[str, object] = {"allowed": ["SEC"], "denied": [], "mode": "allowlist"}
    for denied in ("query_finra", "get_short_interest", "search_web",
                   "get_market_snapshot", "get_analyst_estimates"):
        reason = source_denied_reason(denied, sec_only)
        assert reason is not None and "POLICY_DENIED" in reason, denied
    assert source_denied_reason("search_sec_filings", sec_only) is None
    assert "search_sec_filings" in filter_allowed_tools(
        ["search_sec_filings", "search_web", "query_finra"], sec_only)
    assert "search_web" not in filter_allowed_tools(
        ["search_sec_filings", "search_web", "query_finra"], sec_only)
    assert "get_short_interest" not in filter_allowed_tools(
        ["get_short_interest", "get_sec_document"], sec_only)


def test_authorize_tool_call_gates_call_tool_inner() -> None:
    run_security = _run_security(["Research NVDA filings."])
    run_security.source_policy = {"allowed": ["SEC"], "denied": [], "mode": "allowlist"}
    allowed, _ = authorize_tool_call("search_sec_filings", {}, run_security)
    assert allowed is True
    for inner in ("query_finra", "search_web", "get_market_snapshot", "get_analyst_estimates"):
        allowed_inner, reason = authorize_tool_call("call_tool", {"name": inner}, run_security)
        assert allowed_inner is False, inner
        assert "POLICY_DENIED" in reason, (inner, reason)
    # browse_tools itself stays listed; the inner target is what gets gated.
    allowed_browse, _ = authorize_tool_call("browse_tools", {}, run_security)
    assert allowed_browse is True


def test_context_gates_allowlist_combos() -> None:
    from app.policy import Capability, RequestContext, context_allows_tool, scoped_context
    base = RequestContext("test", frozenset({Capability.RESEARCH}))
    sec_only = scoped_context(base, {"allowed": ["SEC"], "denied": [], "mode": "allowlist"})
    assert context_allows_tool(sec_only, "search_sec_filings") is True
    for denied in ("query_finra", "search_web", "get_market_snapshot", "get_analyst_estimates"):
        assert context_allows_tool(sec_only, denied) is False, denied
    open_all = scoped_context(base, {"allowed": [], "denied": [], "mode": "all"})
    assert context_allows_tool(open_all, "search_web") is True


def test_job_source_gate_and_budgets() -> None:
    from app.research import jobs as _jobs
    from app.research import session as _session
    sess = _session.create_session("q?", "o", as_of="2025-06-30T00:00:00+00:00")
    assert sess.source_policy.get("mode") == "allowlist"
    _jobs.check_source_allowed(sess, "SEC")
    _jobs.check_source_allowed(sess, None)
    with pytest.raises(ValueError, match="policy_denied"):
        _jobs.check_source_allowed(sess, "FINRA")
    assert _jobs.job_tool_budget(sess, "scout") is None  # §1 unlimited default
    # No default deadline: research is unlimited unless a deadline is configured.
    assert _jobs.job_deadline_seconds(sess) is None
    configured = _session.create_session("q?", "o", as_of="2025-06-30T00:00:00+00:00",
                                         budget={"deadline_seconds": 600})
    assert _jobs.job_deadline_seconds(configured) == 600
    assert _jobs.job_token_budget(sess) is None
