"""Read-only Robinhood portfolio provider.

Internal plumbing between the Robinhood MCP client and the portfolio sync
service.  Provider method names are never registered as LLM tools.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from .account import (
    BrokerageAccount,
    BrokeragePosition,
    CashBalance,
    _decimal,
    _first_present,
    normalize_account,
    normalize_cash_balance,
    normalize_position,
)
from .options import MarketSnapshot

_QUOTE_LIST_KEYS = ("quotes", "data", "results", "items", "records")


def _provider_data(payload: Any) -> Any:
    """Unwrap the MCP response envelope to the tool's ``data`` object.

    Live responses carry both ``structured_content.data`` and a text-JSON
    ``content`` block; a bare ``{"data": ...}`` and envelope-less payloads
    (used by tests) are unwrapped/passed through unchanged.
    """
    if isinstance(payload, dict):
        structured = payload.get("structured_content") or payload.get("structuredContent")
        if isinstance(structured, dict) and isinstance(structured.get("data"), dict):
            return structured["data"]
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str) and text.strip():
                    try:
                        parsed = json.loads(text)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(parsed, dict):
                        return parsed.get("data", parsed)
        if isinstance(payload.get("data"), dict):
            return payload["data"]
    return payload


def _rows(payload: Any, *keys: str, wrap: bool = True) -> list[dict[str, Any]] | None:
    """Extract row dicts from a provider payload.

    A bare list is used directly; a bare dict is wrapped as a single row;
    otherwise rows are looked up under the given top-level keys (an empty
    list under a key is a valid empty result).  Returns None when the
    payload is neither a list nor a dict (unrecognized shape).
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [value]
    return [payload] if wrap else None


def _quote_rows(payload: Any) -> list[dict[str, Any]]:
    """Extract quote rows, accepting the common list aliases or a bare list."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _QUOTE_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [value]
    return [payload]


def _quote_retrieved_at(row: dict[str, Any]) -> datetime:
    raw = _first_present(row, "retrieved_at", "retrievedAt", "timestamp", "venue_last_trade_time", "venueLastTradeTime")
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            value = datetime.now(timezone.utc)
    elif isinstance(raw, datetime):
        value = raw
    else:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


class RobinhoodPortfolioProvider:
    """Internal read-only portfolio access over a :class:`RobinhoodClient`.

    Never exposed to the LLM; missing allowlist tools surface loudly as
    :class:`RobinhoodToolError` from the client.
    """

    def __init__(self, client) -> None:
        self._client = client

    def get_accounts(self) -> list[BrokerageAccount]:
        payload = self._client.call_tool("get_accounts", {})
        rows = _rows(_provider_data(payload), "accounts")
        if rows is None:
            raise ValueError(
                "Unexpected get_accounts payload shape: expected a list or an object with an 'accounts' key"
            )
        return [normalize_account(row) for row in rows]

    def get_positions(self, account_id: str) -> list[BrokeragePosition]:
        payload = self._client.call_tool("get_equity_positions", {"account_number": account_id})
        rows = _rows(_provider_data(payload), "positions")
        if rows is None:
            raise ValueError(
                "Unexpected get_equity_positions payload shape: expected a list or an object with a 'positions' key"
            )
        return [normalize_position(row, account_id=account_id) for row in rows]

    def get_cash_balance(self, account_id: str) -> CashBalance:
        payload = self._client.call_tool("get_portfolio", {"account_number": account_id})
        data = _provider_data(payload)
        rows = _rows(data, "portfolios")
        if rows:
            row = rows[0]
        elif isinstance(data, dict):
            row = data
        else:
            raise ValueError(
                "Unexpected get_portfolio payload shape: expected an object or a 'portfolios' list"
            )
        return normalize_cash_balance(row, account_id=account_id)

    def get_scanner_filter_specs(self) -> dict[str, Any]:
        """The scanner filter-type catalog (no parameters)."""
        data = _provider_data(self._client.call_tool("get_scanner_filter_specs", {}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected get_scanner_filter_specs payload shape: expected an object")
        return data

    def get_scans(self) -> list[dict[str, Any]]:
        """The user's saved scanners, one dict per scan."""
        data = _provider_data(self._client.call_tool("get_scans", {}))
        rows = _rows(data, "scans", "results", "items")
        if rows is None:
            raise ValueError(
                "Unexpected get_scans payload shape: expected a list or an object with a 'scans' key"
            )
        return rows

    def run_scan(self, scan_id: str) -> dict[str, Any]:
        """Execute a saved scanner; returns live market results."""
        data = _provider_data(self._client.call_tool("run_scan", {"scan_id": scan_id}))
        if not isinstance(data, dict):
            raise ValueError("Unexpected run_scan payload shape: expected an object")
        return data

    def get_equity_quotes(self, tickers: Sequence[str]) -> dict[str, MarketSnapshot]:
        symbols = [str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()]
        if not symbols:
            return {}
        payload = self._client.call_tool("get_equity_quotes", {"symbols": symbols})
        snapshots: dict[str, MarketSnapshot] = {}
        for row in _quote_rows(_provider_data(payload)):
            quote = row.get("quote") if isinstance(row.get("quote"), dict) else row
            ticker = str(_first_present(quote, "ticker", "symbol") or "").upper()
            if not ticker:
                continue
            snapshots[ticker] = MarketSnapshot(
                ticker=ticker,
                last=_decimal(
                    _first_present(
                        quote,
                        "last",
                        "last_price",
                        "lastPrice",
                        "last_trade_price",
                        "last_non_reg_trade_price",
                        "price",
                    )
                ),
                bid=_decimal(_first_present(quote, "bid", "bid_price", "bidPrice")),
                ask=_decimal(_first_present(quote, "ask", "ask_price", "askPrice")),
                retrieved_at=_quote_retrieved_at(quote),
            )
        return snapshots