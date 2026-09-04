"""Action firewall and egress policy. stdlib only.

Every tool call is gated against the run's original intent (permitted
domains are derived from USER TURNS ONLY — external evidence can never
expand them). Egress to external destinations (Exa) is blocked when the
payload carries credential or portfolio-context patterns."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..redact import _ACCOUNT_ID_RE, _BEARER_RE, _JWT_RE, _SK_OR_V1_RE
from .context import RunSecurityContext

# Keep in sync with TOOL_CAPABILITIES (parity test asserts the portfolio set).
TOOL_DOMAINS: dict[str, str] = {
    # SEC filing data.
    "get_fundamentals": "financial_research",
    "list_sec_filings": "financial_research",
    "get_sec_filing": "financial_research",
    "list_sec_documents": "financial_research",
    "get_sec_document": "financial_research",
    "diff_sec_filings": "financial_research",
    "get_material_events": "financial_research",
    "get_beneficial_ownership": "financial_research",
    "get_ownership_changes": "financial_research",
    "get_insider_activity": "financial_research",
    "get_planned_insider_sales": "financial_research",
    "get_offering_history": "financial_research",
    "get_dilution_profile": "financial_research",
    "get_institutional_ownership": "financial_research",
    "get_governance_events": "financial_research",
    "get_transaction_status": "financial_research",
    "get_short_pressure_profile": "financial_research",
    "search_tools": "financial_research",
    "get_recent_ownership_filings": "financial_research",
    "diff_risk_factors": "financial_research",
    "get_financial_statements": "financial_research",
    "get_xbrl_facts": "financial_research",
    "get_obligations": "financial_research",
    "get_valuation_metrics": "financial_research",
    # FINRA public market data.
    "list_finra_datasets": "financial_research",
    "describe_finra_dataset": "financial_research",
    "get_finra_datapoints": "financial_research",
    "query_finra": "financial_research",
    "get_short_interest": "financial_research",
    "get_short_interest_leaderboard": "financial_research",
    "get_reg_sho_volume": "financial_research",
    "get_threshold_securities": "financial_research",
    # Analyst consensus data.
    "get_analyst_estimates": "financial_research",
    "get_sp500_weight": "financial_research",
    # Robinhood market data.
    "get_market_snapshot": "financial_research",
    "get_option_chain": "financial_research",
    "analyze_option_contract": "financial_research",
    "compare_options": "financial_research",
    "get_scanner_filter_specs": "financial_research",
    # Exa web research.
    "search_web": "public_web_research",
    # Robinhood portfolio data (private).
    "evaluate_mandate": "portfolio_read",
    "get_portfolio_snapshot": "portfolio_read",
    "get_scans": "portfolio_read",
    "run_scan": "portfolio_read",
}

EGRESS_INTENT_REASON = "PRIVATE -> EXTERNAL egress"
EGRESS_AFTER_PRIVATE_REASON = "PRIVATE -> EXTERNAL egress after private context"
CREDENTIAL_REASON = "credential pattern"

_CREDENTIAL_PATTERNS = (
    ("bearer", _BEARER_RE),
    ("sk_or_v1", _SK_OR_V1_RE),
    ("jwt", _JWT_RE),
    ("account_id", _ACCOUNT_ID_RE),
)

_POSSESSIVE_OWN_RE = re.compile(
    r"\b(?:my|i|you|user|we)\s+(?:own\w*|have\w*|hold\w*|held)\b",
    re.IGNORECASE,
)
_PORTFOLIO_NOUN_RE = re.compile(
    r"\b(?:portfolio|position|holdings|balance|account)\b", re.IGNORECASE
)
# Digit-runs (4+) or "$" amounts — the numeric side of portfolio context.
_AMOUNT_TOKEN_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d{4,}\b")

_EGRESS_TOKEN_WINDOW = 12


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str | None


def authorize_tool_call(
    name: str, arguments: dict, run_security: RunSecurityContext
) -> tuple[bool, str]:
    """Gate one tool call against intent plus the explicit session grant."""
    domain = TOOL_DOMAINS.get(name)
    if domain == "portfolio_read":
        if run_security.authorization.portfolio_read:
            return True, ""
        return False, "portfolio access is not authorized for this session"
    if domain is not None and domain in run_security.original_intent.permitted_domains:
        return True, ""
    return False, "tool call exceeds original user intent"


def _portfolio_context(query: str) -> bool:
    """Portfolio-context patterns: possessive-own phrasing, or a portfolio
    noun within 12 tokens of a digit-run or dollar amount."""
    if _POSSESSIVE_OWN_RE.search(query):
        return True
    tokens = query.split()
    nouns = [i for i, token in enumerate(tokens) if _PORTFOLIO_NOUN_RE.search(token)]
    amounts = [i for i, token in enumerate(tokens) if _AMOUNT_TOKEN_RE.search(token)]
    return any(
        abs(noun_index - amount_index) <= _EGRESS_TOKEN_WINDOW
        for noun_index in nouns
        for amount_index in amounts
    )


def private_pattern_hit(text: str) -> str | None:
    """First private-data pattern hit in arbitrary text: a credential match
    wins, then portfolio-context phrasing. None when the text is clean."""
    for _name, pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return CREDENTIAL_REASON
    if _portfolio_context(text):
        return EGRESS_INTENT_REASON
    return None


def authorize_egress(
    destination: str, payload: object, run_security: RunSecurityContext
) -> EgressDecision:
    """Block private data from leaving Stockbot to external destinations.

    Ordering invariant: once ANY private tool result has been ALLOWED into
    model context this run, all further egress is blocked regardless of the
    payload. The full serialized payload (query and every filter) is then
    scanned for credential and portfolio-context patterns.
    """
    if destination != "exa":
        return EgressDecision(False, f"unknown egress destination: {destination}")
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        return EgressDecision(False, "malformed egress payload")
    if "private" in run_security.data_labels:
        return EgressDecision(False, EGRESS_AFTER_PRIVATE_REASON)
    hit = private_pattern_hit(json.dumps(payload, sort_keys=True))
    if hit:
        return EgressDecision(False, hit)
    return EgressDecision(True, None)
