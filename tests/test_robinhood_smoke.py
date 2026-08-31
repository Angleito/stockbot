"""Opt-in live smoke test against the Robinhood MCP server.

Run with:
    RUN_ROBINHOOD_SMOKE=1 venv/bin/pytest -m robinhood_smoke -q

Requires prior OAuth state (scripts/robinhood_login.py). Skipped automatically
unless RUN_ROBINHOOD_SMOKE=1.

Verifies the production agent contract end-to-end: tool discovery is
reachable, every discovered read tool is covered by the allowlist (drift
guard tying discovery to app/robinhood/capabilities.py), every
trading-looking tool is deny-listed, and the full read-only portfolio sync
persists a snapshot that round-trips through read_latest_snapshot with no
OAuth/token material in any persisted row, and no raw broker account
identifier is printed or persisted.  No trading/write tool is ever
invoked.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from app.config import get_robinhood_mcp_url
from app.domain.portfolio import PortfolioSnapshot
from app.robinhood import capabilities
from app.robinhood.auth import OAuthConfig
from app.robinhood.client import RobinhoodClient
from app.robinhood.portfolio import RobinhoodPortfolioProvider
from app.services.portfolio_sync import (
    read_latest_snapshot,
    sync_robinhood_portfolio,
)
from app.storage import parquet

pytestmark = [
    pytest.mark.robinhood_smoke,
    pytest.mark.skipif(
        os.getenv("RUN_ROBINHOOD_SMOKE") != "1",
        reason="opt-in live Robinhood smoke; set RUN_ROBINHOOD_SMOKE=1",
    ),
]

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
TRADING_KEYWORDS = (
    "order", "trade", "place", "submit", "cancel", "replace", "modify",
    "exercise", "withdraw", "deposit", "transfer",
)
FORBIDDEN_COLUMNS = ("token", "oauth", "secret", "access", "refresh", "authorization")


def test_robinhood_smoke_discovery_and_portfolio_sync(tmp_path):
    data_root = tmp_path / "data"
    client = RobinhoodClient(
        get_robinhood_mcp_url(),
        oauth=OAuthConfig(get_robinhood_mcp_url()),
        market_tools=capabilities.MARKET_READ_TOOLS,
        account_tools=capabilities.ACCOUNT_READ_TOOLS,
    )

    tools = client.list_tools()
    assert len(tools) > 0
    names = [str(tool.get("name", "")) for tool in tools]
    for name in names:
        if capabilities.tool_capability(name) is not None:
            assert (
                name in capabilities.MARKET_READ_TOOLS
                or name in capabilities.ACCOUNT_READ_TOOLS
            ), f"classified tool {name!r} is missing from the allowlists"
        if any(keyword in name.lower() for keyword in TRADING_KEYWORDS):
            assert capabilities.is_blocked(name), f"trading-looking tool {name!r} is not blocked"

    provider = RobinhoodPortfolioProvider(client)
    accounts = provider.get_accounts()
    if accounts:
        first = accounts[0]
        positions = provider.get_positions(first.account_id)
        provider.get_cash_balance(first.account_id)
        tickers = list(dict.fromkeys(position.ticker for position in positions))
        provider.get_equity_quotes(tickers)
    provider.get_scanner_filter_specs()
    provider.get_scans()

    snapshot = sync_robinhood_portfolio(provider, data_root=data_root, now=NOW)
    assert isinstance(snapshot, PortfolioSnapshot)
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.snapshot_id == snapshot.snapshot_id
    assert restored.created_at == snapshot.created_at
    assert restored.broker == snapshot.broker == "robinhood"
    for field in ("cash", "invested_value", "total_value"):
        expected = getattr(snapshot, field)
        actual = getattr(restored, field)
        if expected is None:
            assert actual is None
        else:
            assert actual == pytest.approx(expected)
    assert [position.ticker for position in restored.positions] == [
        position.ticker for position in snapshot.positions
    ]
    assert [position.account_id for position in restored.positions] == [
        position.account_id for position in snapshot.positions
    ]

    for table_name in ("portfolio_snapshots", "portfolio_positions"):
        table = parquet.read_table(table_name, root=data_root / "parquet")
        for column in table.column_names:
            assert not any(part in column.lower() for part in FORBIDDEN_COLUMNS), (
                f"{table_name}.{column} is a forbidden column"
            )
        for account in accounts:
            raw_account_id = account.account_id
            assert not any(
                raw_account_id in str(cell)
                for column in table.column_names
                for cell in table.column(column).to_pylist()
                if cell is not None
            ), f"persisted {table_name} leaks raw broker account id"

    print("Robinhood smoke passed")
    print(f"  accounts: {len(accounts)}")
    print(f"  positions: {len(snapshot.positions)}")
    print("  portfolio sync: OK")
