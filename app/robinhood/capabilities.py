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


def _configured_subset(
    supplied: frozenset[str] | None,
    canonical: frozenset[str],
    capability: RobinhoodCapability,
) -> frozenset[str]:
    """Validate an optional configuration can only reduce a capability."""
    if supplied is None:
        return canonical
    configured = frozenset(supplied)
    invalid = configured - canonical
    if invalid:
        names = ", ".join(sorted(invalid))
        raise ValueError(f"Unknown {capability.value} tool configuration: {names}")
    return configured & canonical


def allowed_read_tools(
    *, market: frozenset[str] | None = None,
    account: frozenset[str] | None = None,
) -> frozenset[str]:
    """Union of configured subsets of the canonical read-only registry.

    Configuration never becomes an allowlist of its own: every supplied name
    is verified against the appropriate canonical capability set.
    """
    market_set = _configured_subset(market, MARKET_READ_TOOLS, RobinhoodCapability.MARKET_READ)
    account_set = _configured_subset(account, ACCOUNT_READ_TOOLS, RobinhoodCapability.ACCOUNT_READ)
    return market_set | account_set


def permitted_tools(
    *, market: frozenset[str] | None = None,
    account: frozenset[str] | None = None,
) -> frozenset[str]:
    """Compatibility name for the read-only tool allowlist."""
    return allowed_read_tools(market=market, account=account)
