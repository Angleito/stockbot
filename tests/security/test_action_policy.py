"""Tests for intent classification and the action firewall."""

import pytest

from app.policy import Capability
from app.security.action_policy import TOOL_DOMAINS, authorize_tool_call
from app.security.context import RunSecurityContext, SessionAuthorization, classify_intent
from app.tools import PORTFOLIO_AUTHORIZED_TOOLS, TOOL_CAPABILITIES


def _run_security(user_turns):
    return RunSecurityContext(
        original_intent=classify_intent(user_turns),
        capabilities=frozenset({"research", "portfolio_read"}),
    )


# -- classify_intent ---------------------------------------------------------

def test_classify_intent_research_only():
    intent = classify_intent(["What's the latest AMD news?"])
    assert intent.request == "What's the latest AMD news?"
    assert intent.permitted_domains == {"financial_research", "public_web_research"}


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
        assert intent.permitted_domains == {"financial_research", "public_web_research"}, turns


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
    from app.security.context_gateway import TOOL_ENVELOPES

    portfolio = {
        name for name, domain in TOOL_DOMAINS.items() if domain == "portfolio_read"
    }
    for name in portfolio:
        assert name in TOOL_ENVELOPES
        assert TOOL_ENVELOPES[name].sensitivity.value == "private"
