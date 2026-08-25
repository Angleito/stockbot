#!/usr/bin/env python3
"""Authenticate against the configured Robinhood MCP OAuth server."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_robinhood_mcp_url
from app.robinhood import RobinhoodClient
from app.robinhood.auth import OAuthConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=get_robinhood_mcp_url())
    args = parser.parse_args()
    client = RobinhoodClient(args.server_url, oauth=OAuthConfig(args.server_url))
    tools = client.list_tools()
    print(f"Robinhood MCP authenticated; discovered {len(tools)} tools.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
