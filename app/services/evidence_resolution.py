"""Deterministic entity linking for evidence claims.

Sole linking path is ``resolve_ticker_aliases`` — no second implementation.
Name matching is an exact case-insensitive warehouse lookup that maps to a
ticker, then resolves through the same ticker path. Never guesses:
unresolved/ambiguous keep IDs None.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.domain.market.identity import resolve_ticker_aliases
from app.domain.market.securities import SecurityResolution, TickerAlias


def resolve_subject(
    *,
    ticker: str | None,
    name: str | None,
    aliases_by_ticker: Callable[[str], Sequence[TickerAlias]],
    name_to_ticker: Callable[[str], str | None],
    as_of: datetime,
) -> SecurityResolution:
    """Resolve a claim subject (or object) to Stockbot identity."""
    if ticker is not None and ticker.strip():
        t = ticker.strip().upper()
        return resolve_ticker_aliases(t, aliases_by_ticker(t), as_of=as_of)
    if name is not None and name.strip():
        mapped = name_to_ticker(name.strip())
        if not mapped or not mapped.strip():
            return SecurityResolution(None, None, name.strip(), False, "unresolved")
        t = mapped.strip().upper()
        return resolve_ticker_aliases(t, aliases_by_ticker(t), as_of=as_of)
    return SecurityResolution(None, None, (ticker or name or "").strip(), False, "unresolved")


def warehouse_aliases_fn(
    as_of: datetime, data_root: Optional[Path] = None
) -> Callable[[str], Sequence[TickerAlias]]:
    """Aliases lookup bound to an as-of instant (PIT visibility)."""
    from app.storage import duckdb

    def _lookup(ticker: str) -> Sequence[TickerAlias]:
        return duckdb.ticker_alias_candidates(ticker, as_of, data_root=data_root)

    return _lookup


def warehouse_name_to_ticker(
    name: str, data_root: Optional[Path] = None
) -> str | None:
    """Exact case-insensitive name → ticker; None on 0 or 2+ tickers.

    Matches warehouse ``entities.name`` and ``entity_aliases.alias_value``,
    then maps matched entities to distinct tickers via ``securities`` +
    ticker aliases. Add explicit alias rows via normal ingestion when a
    common name misses both (claim stays unresolved — correct per spec).
    """
    from app.storage import duckdb

    cleaned = (name or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.casefold()
    try:
        entity_rows = duckdb.query("SELECT entity_id, name FROM entities", data_root=data_root)
    except Exception:
        return None
    entity_ids: set[str] = set()
    for row in entity_rows:
        if isinstance(row.get("name"), str) and row["name"].casefold() == lowered:
            entity_ids.add(str(row["entity_id"]))
    try:
        alias_rows = duckdb.query(
            "SELECT alias_value, entity_id FROM entity_aliases", data_root=data_root
        )
    except Exception:
        alias_rows = []
    for row in alias_rows:
        if isinstance(row.get("alias_value"), str) and row["alias_value"].casefold() == lowered:
            entity_ids.add(str(row["entity_id"]))
    if not entity_ids:
        return None
    tickers: set[str] = set()
    for eid in entity_ids:
        try:
            sec_rows = duckdb.query(
                "SELECT ticker FROM securities WHERE entity_id = ?",
                params=[eid],
                data_root=data_root,
            )
        except Exception:
            sec_rows = []
        for row in sec_rows:
            if row.get("ticker"):
                tickers.add(str(row["ticker"]).strip().upper())
        try:
            alias_tickers = duckdb.query(
                "SELECT alias_value FROM entity_aliases WHERE entity_id = ? AND alias_type = 'ticker'",
                params=[eid],
                data_root=data_root,
            )
        except Exception:
            alias_tickers = []
        for row in alias_tickers:
            if row.get("alias_value"):
                tickers.add(str(row["alias_value"]).strip().upper())
    if len(tickers) != 1:
        return None
    return next(iter(tickers))


def resolve_subject_with_warehouse(
    *,
    ticker: str | None,
    name: str | None,
    as_of: datetime,
    data_root: Optional[Path] = None,
) -> SecurityResolution:
    """Warehouse-backed resolve_subject (what the gateway path uses)."""
    return resolve_subject(
        ticker=ticker,
        name=name,
        aliases_by_ticker=warehouse_aliases_fn(as_of, data_root),
        name_to_ticker=lambda n: warehouse_name_to_ticker(n, data_root),
        as_of=as_of,
    )
