"""Tests for the egress firewall (private data must not leave Stockbot)."""

from app.security.action_policy import (
    EGRESS_INTENT_REASON,
    CREDENTIAL_REASON,
    EgressDecision,
    authorize_egress,
)
from app.security.context import RunSecurityContext, classify_intent


def _run_security(user_turns=("Research AMD news.",)):
    return RunSecurityContext(
        original_intent=classify_intent(list(user_turns)),
        capabilities=frozenset({"research", "portfolio_read"}),
    )


def test_private_query_is_blocked():
    decision = authorize_egress(
        "exa",
        {"query": "User owns 2843 AMD shares worth $417,921"},
        _run_security(),
    )
    assert isinstance(decision, EgressDecision)
    assert decision.allowed is False
    assert decision.reason == EGRESS_INTENT_REASON


def test_benign_market_query_is_allowed():
    decision = authorize_egress(
        "exa", {"query": "AMD market cap 2026"}, _run_security()
    )
    assert decision.allowed is True
    assert decision.reason is None


def test_credential_patterns_block():
    queries = [
        "send to user with Bearer abc123def456xyz",
        "lookup sk-or-v1-abcdefghijklmnop status",
        "check eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "account 123456789 holdings",
    ]
    for query in queries:
        decision = authorize_egress("exa", {"query": query}, _run_security())
        assert decision.allowed is False, query
        assert decision.reason == CREDENTIAL_REASON, query


def test_account_identifier_without_digits_not_blocked():
    decision = authorize_egress(
        "exa", {"query": "what does account mean in finance"}, _run_security()
    )
    assert decision.allowed is True


def test_portfolio_noun_near_amount_blocked():
    decision = authorize_egress(
        "exa",
        {"query": "AMD's competitive position versus NVIDIA with $250 billion revenue"},
        _run_security(),
    )
    assert decision.allowed is False
    assert decision.reason == EGRESS_INTENT_REASON


def test_unknown_destination_blocked():
    decision = authorize_egress("ftp", {"query": "AMD news"}, _run_security())
    assert decision.allowed is False


def test_malformed_payload_blocked():
    assert authorize_egress("exa", {}, _run_security()).allowed is False
    assert authorize_egress("exa", {"query": 123}, _run_security()).allowed is False
    assert authorize_egress("exa", "AMD news", _run_security()).allowed is False
