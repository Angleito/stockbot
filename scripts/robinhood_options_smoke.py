#!/usr/bin/env python3
"""List Robinhood MCP tools, then optionally invoke a read-only tool."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import tools as stockbot_tools
from app.robinhood import RobinhoodClient
from app.robinhood.auth import OAuthConfig
from app.tool_render import render_tool_result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", nargs="?", help="ticker to inspect after discovery")
    parser.add_argument(
        "--server-url", default=os.getenv("ROBINHOOD_MCP_URL", "https://agent.robinhood.com/mcp/trading")
    )
    parser.add_argument("--type", dest="option_type", choices=("put", "call"), default="put")
    parser.add_argument("--min-dte", type=int, default=180)
    parser.add_argument("--max-dte", type=int, default=365)
    parser.add_argument("--tool", help="optional read-only tool to invoke")
    return parser.parse_args(argv)


def connect(server_url: str) -> RobinhoodClient:
    return RobinhoodClient(server_url, oauth=OAuthConfig(server_url))


def list_tool_names(client: RobinhoodClient) -> list[str]:
    return [str(tool.get("name", "<unknown>")) for tool in client.list_tools()]


def render_tool_names(names: list[str]) -> str:
    return "\n".join(["Available tools:"] + [f"- {name}" for name in names])


def run_chain(client: RobinhoodClient, args: argparse.Namespace) -> str:
    def _smoke_client(*, account_tools: frozenset[str] = frozenset()) -> RobinhoodClient:
        return client

    stockbot_tools._robinhood_client = _smoke_client
    chain = stockbot_tools.get_option_chain(
        args.ticker,
        args.option_type,
        min_dte=args.min_dte,
        max_dte=args.max_dte,
    )
    return render_tool_result(chain)


def _emit_selection(client: RobinhoodClient, args: argparse.Namespace) -> None:
    if args.tool:
        print(render_tool_result(client.call_tool(args.tool)))
    elif args.ticker:
        print(run_chain(client, args))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        client = connect(args.server_url)
        names = list_tool_names(client)
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        print(f"Robinhood smoke failed (unauthenticated or MCP schema unavailable): {exc}", file=sys.stderr)
        return 1
    print(render_tool_names(names))
    _emit_selection(client, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
