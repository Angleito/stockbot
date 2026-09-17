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
    "accounts",
    "positions",
    "portfolio",
    "balance",
    "transaction",
    "buying",
    "cash",
)

SUMMARY_LABELS = ("MARKET_READ", "ACCOUNT_READ", "BLOCKED", "UNKNOWN")


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=get_robinhood_mcp_url())
    parser.add_argument(
        "--json",
        action="store_true",
        help="dump the full discovered tool list as JSON",
    )
    return parser.parse_args(argv)


def connect(server_url: str) -> list[dict[str, object]]:
    client = RobinhoodClient(server_url, oauth=OAuthConfig(server_url))
    return client.list_tools()


def tool_names(tools: list[dict[str, object]]) -> list[str]:
    return [str(tool.get("name", "<unknown>")) for tool in tools]


def _tool_schema(tool: dict[str, object]) -> dict[str, object]:
    schema_raw = tool.get("input_schema") or tool.get("inputSchema")
    return schema_raw if isinstance(schema_raw, dict) else {}


def render_tool(tool: dict[str, object]) -> str:
    name = str(tool.get("name", "<unknown>"))
    description = str(tool.get("description") or "")
    return "\n".join(
        (
            f"- {name}",
            f"  capability: {_classify(name)}",
            f"  description: {_truncate(description, 200)}",
            f"  input_schema: {_truncate(json.dumps(_tool_schema(tool), sort_keys=True), 400)}",
        )
    )


def render_text(tools: list[dict[str, object]]) -> str:
    names = tool_names(tools)
    counts = {label: 0 for label in SUMMARY_LABELS}
    for name in names:
        counts[_classify(name)] += 1
    lines = [render_tool(tool) for tool in tools]
    lines += ["", f"tools discovered: {len(names)}"]
    lines += [f"  {label}: {counts[label]}" for label in SUMMARY_LABELS]
    candidates = find_candidates(names)
    lines.append("account-related candidates to review (not yet in ACCOUNT_READ_TOOLS):")
    if candidates:
        lines += [f"  {name}" for name in candidates]
    else:
        lines.append("  (none - every account-related name is allowlisted)")
    lines.append("Discovery only: no tool was invoked.")
    return "\n".join(lines)


def render_json(tools: list[dict[str, object]]) -> str:
    return json.dumps(tools, indent=2, sort_keys=True)


def find_candidates(names: list[str]) -> list[str]:
    return sorted(
        {
            name
            for name in names
            if any(keyword in name.lower() for keyword in ACCOUNT_KEYWORDS) and name not in ACCOUNT_READ_TOOLS
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tools = connect(args.server_url)
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        print(
            f"Robinhood tool discovery failed (unauthenticated or MCP schema unavailable): {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        print(render_json(tools))
        return 0

    print(render_text(tools))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
