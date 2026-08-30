#!/usr/bin/env python3
"""Discover and classify Robinhood MCP tools without invoking any of them.

Workflow:
    python scripts/robinhood_tools.py            # classified listing + review candidates
    python scripts/robinhood_tools.py --json     # full tool list as JSON (pipeable)

Review the "account-related candidates to review" section at the end of the
output: those names match the account vocabulary (accounts/positions/
portfolio/balance/transaction/buying/cash) but are not yet in
ACCOUNT_READ_TOOLS.  Only after reviewing each candidate's input schema
should you add it to ACCOUNT_READ_TOOLS in app/robinhood/capabilities.py —
and only if it is genuinely read-only.  Discovery itself never invokes a
tool; trading/write tools stay on the deny list regardless of discovery.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_robinhood_mcp_url
from app.robinhood import RobinhoodClient
from app.robinhood.auth import OAuthConfig
from app.robinhood.capabilities import (
    ACCOUNT_READ_TOOLS,
    RobinhoodCapability,
    is_blocked,
    tool_capability,
)

ACCOUNT_KEYWORDS = (
    "accounts", "positions", "portfolio", "balance",
    "transaction", "buying", "cash",
)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _classify(name: str) -> str:
    capability = tool_capability(name)
    if capability is RobinhoodCapability.MARKET_READ:
        return "MARKET_READ"
    if capability is RobinhoodCapability.ACCOUNT_READ:
        return "ACCOUNT_READ"
    if is_blocked(name):
        return "BLOCKED"
    return "UNKNOWN"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=get_robinhood_mcp_url())
    parser.add_argument(
        "--json", action="store_true",
        help="dump the full discovered tool list as JSON",
    )
    args = parser.parse_args()
    try:
        client = RobinhoodClient(args.server_url, oauth=OAuthConfig(args.server_url))
        tools = client.list_tools()
    except Exception as exc:
        print(
            f"Robinhood tool discovery failed (unauthenticated or MCP schema unavailable): {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(json.dumps(tools, indent=2, sort_keys=True))
        return 0

    names = [str(tool.get("name", "<unknown>")) for tool in tools]
    for tool in tools:
        name = str(tool.get("name", "<unknown>"))
        description = str(tool.get("description") or "")
        schema = tool.get("input_schema") or tool.get("inputSchema") or {}
        print(f"- {name}")
        print(f"  capability: {_classify(name)}")
        print(f"  description: {_truncate(description, 200)}")
        print(f"  input_schema: {_truncate(json.dumps(schema, sort_keys=True), 400)}")

    counts = {label: 0 for label in ("MARKET_READ", "ACCOUNT_READ", "BLOCKED", "UNKNOWN")}
    for name in names:
        counts[_classify(name)] += 1
    candidates = sorted({
        name
        for name in names
        if any(keyword in name.lower() for keyword in ACCOUNT_KEYWORDS)
        and name not in ACCOUNT_READ_TOOLS
    })

    print()
    print(f"tools discovered: {len(names)}")
    for label in ("MARKET_READ", "ACCOUNT_READ", "BLOCKED", "UNKNOWN"):
        print(f"  {label}: {counts[label]}")
    print("account-related candidates to review (not yet in ACCOUNT_READ_TOOLS):")
    if candidates:
        for name in candidates:
            print(f"  {name}")
    else:
        print("  (none - every account-related name is allowlisted)")
    print("Discovery only: no tool was invoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())