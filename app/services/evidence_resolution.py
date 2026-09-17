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


def warehouse_aliases_fn(as_of: datetime, data_root: Path | None = None) -> Callable[[str], Sequence[TickerAlias]]:
    """Aliases lookup bound to an as-of instant (PIT visibility)."""
    from app.storage import duckdb

    def _lookup(ticker: str) -> Sequence[TickerAlias]:
        return duckdb.ticker_alias_candidates(ticker, as_of, data_root=data_root)

    return _lookup


def _clean_lookup_name(name: object) -> tuple[str, str] | None:
    """(cleaned, casefolded) lookup name; None when blank."""
    cleaned = (name or "").strip() if isinstance(name, str) else ""
    if not cleaned:
        return None
    return cleaned, cleaned.casefold()


def _matching_ids(rows: list[dict[str, object]], value_key: str, id_key: str, lowered: str) -> set[str]:
    out: set[str] = set()
    for row in rows:
        value = row.get(value_key)
        if isinstance(value, str) and value.casefold() == lowered:
            out.add(str(row[id_key]))
    return out


def _entity_table_ids(lowered: str, data_root: Path | None = None) -> set[str]:
    from app.storage import duckdb

    try:
        rows: list[dict[str, object]] = duckdb.query("SELECT entity_id, name FROM entities", data_root=data_root)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return set()
    return _matching_ids(rows, "name", "entity_id", lowered)


def _entity_alias_ids(lowered: str, data_root: Path | None = None) -> set[str]:
    from app.storage import duckdb

    try:
        rows = duckdb.query("SELECT alias_value, entity_id FROM entity_aliases", data_root=data_root)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return set()
    return _matching_ids(rows, "alias_value", "entity_id", lowered)


def _entity_ids_for_name(lowered: str, data_root: Path | None = None) -> set[str]:
    """Entity ids matching entities.name or entity_aliases.alias_value exactly."""
    return _entity_table_ids(lowered, data_root) | _entity_alias_ids(lowered, data_root)


def _entity_query_rows(sql: str, eid: str, data_root: Path | None = None) -> list[dict[str, object]]:
    from app.storage import duckdb

    try:
        return duckdb.query(sql, params=[eid], data_root=data_root)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _security_tickers(sec: list[dict[str, object]]) -> set[str]:
    return {str(r["ticker"]).strip().upper() for r in sec if r.get("ticker")}


def _alias_tickers(als: list[dict[str, object]]) -> set[str]:
    return {str(r["alias_value"]).strip().upper() for r in als if r.get("alias_value")}


def _tickers_for_entity(eid: str, data_root: Path | None = None) -> set[str]:
    sec = _entity_query_rows("SELECT ticker FROM securities WHERE entity_id = ?", eid, data_root)
    als = _entity_query_rows(
        "SELECT alias_value FROM entity_aliases WHERE entity_id = ? AND alias_type = 'ticker'", eid, data_root
    )
    return _security_tickers(sec) | _alias_tickers(als)


def _tickers_for_entities(entity_ids: set[str], data_root: Path | None = None) -> set[str]:
    """Distinct tickers for entities via securities + ticker aliases."""
    tickers: set[str] = set()
    for eid in entity_ids:
        tickers |= _tickers_for_entity(eid, data_root)
    return tickers


def warehouse_name_to_ticker(name: str, data_root: Path | None = None) -> str | None:
    """Exact case-insensitive name → ticker; None on 0 or 2+ tickers.

    Matches warehouse ``entities.name`` and ``entity_aliases.alias_value``,
    then maps matched entities to distinct tickers via ``securities`` +
    ticker aliases. Add explicit alias rows via normal ingestion when a
    common name misses both (claim stays unresolved — correct per spec).
    """
    cleaned = _clean_lookup_name(name)
    if cleaned is None:
        return None
    _, lowered = cleaned
    entity_ids = _entity_ids_for_name(lowered, data_root)
    if not entity_ids:
        return None
    tickers = _tickers_for_entities(entity_ids, data_root)
    if len(tickers) != 1:
        return None
    return next(iter(tickers))


def resolve_subject_with_warehouse(
    *,
    ticker: str | None,
    name: str | None,
    as_of: datetime,
    data_root: Path | None = None,
) -> SecurityResolution:
    """Warehouse-backed resolve_subject (what the gateway path uses)."""
    return resolve_subject(
        ticker=ticker,
        name=name,
        aliases_by_ticker=warehouse_aliases_fn(as_of, data_root),
        name_to_ticker=lambda n: warehouse_name_to_ticker(n, data_root),
        as_of=as_of,
    )
