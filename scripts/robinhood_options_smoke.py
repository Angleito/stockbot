#!/usr/bin/env python3.14
"""List Robinhood MCP tools, then optionally invoke a read-only tool."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.robinhood import RobinhoodClient
from app.robinhood.auth import OAuthConfig
from app.tool_render import render_tool_result
from app import tools as stockbot_tools


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", help="ticker to inspect after discovery")
    parser.add_argument("--server-url", default=os.getenv("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading"))
    parser.add_argument("--type", dest="option_type", choices=("put", "call"), default="put")
    parser.add_argument("--min-dte", type=int, default=180)
    parser.add_argument("--max-dte", type=int, default=365)
    parser.add_argument("--tool", help="optional read-only tool to invoke")
    args = parser.parse_args()
    try:
        client = RobinhoodClient(args.server_url, oauth=OAuthConfig(args.server_url))
        tools = client.list_tools()
    except Exception as exc:
        print(f"Robinhood smoke failed (unauthenticated or MCP schema unavailable): {exc}", file=sys.stderr)
        return 1
    print("Available tools:")
    for tool in tools:
        print(f"- {tool.get('name', '<unknown>')}")
    if args.tool:
        print(render_tool_result(client.call_tool(args.tool)))
    elif args.ticker:
        stockbot_tools._robinhood_client = lambda: client
        print(render_tool_result(stockbot_tools.get_market_snapshot(args.ticker)))
        chain = stockbot_tools.get_option_chain(
            args.ticker,
            args.option_type,
            min_dte=args.min_dte,
            max_dte=args.max_dte,
        )
        print(render_tool_result(chain))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
