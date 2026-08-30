"""Read-only capability policy for Robinhood MCP tools."""

from __future__ import annotations

from enum import Enum


class RobinhoodCapability(Enum):
    MARKET_READ = "market_read"
    ACCOUNT_READ = "account_read"


MARKET_READ_TOOLS: frozenset[str] = frozenset({
    "get_equity_quotes", "get_equity_fundamentals", "get_option_chains",
    "get_option_instruments", "get_option_quotes", "get_option_historicals",
    "get_scanner_filter_specs",
})

ACCOUNT_READ_TOOLS: frozenset[str] = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions",
    "get_scans", "run_scan",
})

# Deny layer applied before the allowlist. It covers trading and money movement
# even if a caller accidentally attempts to configure one of those tools.
BLOCKED_KEYWORDS: tuple[str, ...] = (
    "order", "trade", "place", "submit", "cancel", "replace", "modify",
    "exercise", "withdraw", "deposit", "transfer",
)

BLOCKED_TOOLS: frozenset[str] = frozenset({
    "review_equity_order",
    "place_equity_order",
    "cancel_equity_order",
    "replace_equity_order",
    "place_option_order",
    "withdraw",
    "deposit",
    "transfer",
})


def is_blocked(name: str) -> bool:
    """Return True when the tool name matches either deny layer."""
    lowered = name.lower()
    return lowered in BLOCKED_TOOLS or any(
        keyword in lowered for keyword in BLOCKED_KEYWORDS
    )


def tool_capability(name: str) -> RobinhoodCapability | None:
    """Classify an explicitly allowlisted read tool; reject everything else."""
    if not isinstance(name, str) or not name or is_blocked(name):
        return None
    if name in MARKET_READ_TOOLS:
        return RobinhoodCapability.MARKET_READ
    if name in ACCOUNT_READ_TOOLS:
        return RobinhoodCapability.ACCOUNT_READ
    return None


def allowed_read_tools(
    *, market: frozenset[str] | None = None,
    account: frozenset[str] | None = None,
) -> frozenset[str]:
    """Union of the explicitly allowlisted market and account read tools."""
    market_set = MARKET_READ_TOOLS if market is None else frozenset(market)
    account_set = ACCOUNT_READ_TOOLS if account is None else frozenset(account)
    return market_set | account_set


def permitted_tools(
    *, market: frozenset[str] | None = None,
    account: frozenset[str] | None = None,
) -> frozenset[str]:
    """Compatibility name for the read-only tool allowlist."""
    return allowed_read_tools(market=market, account=account)
