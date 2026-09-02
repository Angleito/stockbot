"""Portfolio synchronization service.

Resolves positions to Stockbot identities, values them against Robinhood
quotes, builds immutable portfolio snapshots, and persists/reads them from
the versioned Parquet datasets.  Never exposes provider internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..domain.market.identity import resolve_ticker_aliases
from ..domain.market.securities import SecurityResolution
from ..domain.portfolio import PortfolioSnapshot
from ..domain.portfolio.snapshot import build_portfolio_snapshot
from ..domain.portfolio.valuation import build_position
from ..robinhood.account import BrokerageAccount, BrokeragePosition, CashBalance
from ..robinhood.adapters import to_position_input, to_quote
from ..robinhood.portfolio import RobinhoodPortfolioProvider
from ..storage import duckdb, mappers, parquet

SNAPSHOT_SOURCE = "robinhood_mcp"
PARSER_VERSION = "robinhood-mcp-account-v1"
CALCULATION_VERSION = "portfolio-snapshot-v1"


def resolve_security(
    ticker: str,
    *,
    provider_instrument_id: str | None = None,
    as_of: datetime | None = None,
    data_root: Path | None = None,
) -> SecurityResolution:
    """Resolve a ticker to a Stockbot security/entity identity.

    ``as_of`` must be a timezone-aware datetime; ``date`` and naive values
    are rejected at runtime (TypeError/ValueError), and aware values are
    normalized to UTC (timestamp precision).
    ``provider_instrument_id`` is preserved on the position but is NOT used
    for resolution: Robinhood instrument IDs are not bridged to Stockbot
    identities yet.  Resolution is point-in-time over entity_aliases: an
    alias is visible only when ``known_at <= as_of`` (timestamp precision)
    AND its half-open validity interval ``[valid_from, valid_to)`` contains
    ``as_of`` (``NULL`` bounds are unbounded; date-only values are midnight,
    so ``valid_to="2026-08-25"`` is expired on the 25th).  Multiple active
    entities for one ticker resolve as ``"ambiguous"`` — never
    source/order-wins.  Rows tied at the newest known_at/retrieved_at instant that carry
    conflicting resolved security ids for one entity also resolve as
    ``"ambiguous"`` — never an arbitrary row pick.  Older revisions never
    create ambiguity; the newest record wins.  No mappings are ever invented: unknown
    tickers resolve to ``resolved=False``.
    """
    del provider_instrument_id
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif not isinstance(as_of, datetime):
        raise TypeError(f"as_of must be a timezone-aware datetime, got {type(as_of).__name__}")
    elif as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    aliases = duckdb.ticker_alias_candidates(ticker, as_of, data_root=data_root)
    return resolve_ticker_aliases(ticker, aliases, as_of=as_of)


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def persist_snapshot(
    snapshot: PortfolioSnapshot, *, data_root: Path | None = None
) -> None:
    """Persist an immutable snapshot (idempotent: a rerun writes 0 rows).

    Never writes OAuth/token or raw provider payload data or raw broker account identifiers.
    """
    parquet_root = Path(data_root) / "parquet" if data_root else None
    parquet.write_rows(
        "portfolio_snapshots",
        [{
            "snapshot_id": snapshot.snapshot_id,
            "broker": snapshot.broker,
            "created_at": snapshot.created_at.isoformat(),
            "cash": _float(snapshot.cash),
            "invested_value": _float(snapshot.invested_value),
            "total_value": _float(snapshot.total_value),
            "account_count": len(snapshot.account_ids),
            "position_count": len(snapshot.positions),
            "priced_position_count": sum(
                1 for position in snapshot.positions if position.market_value is not None
            ),
            "unresolved_position_count": sum(
                1 for position in snapshot.positions if position.entity_id is None
            ),
            "source": SNAPSHOT_SOURCE,
            "parser_version": PARSER_VERSION,
            "calculation_version": CALCULATION_VERSION,
        }],
        root=parquet_root,
    )
    parquet.write_rows(
        "portfolio_positions",
        [{
            "snapshot_id": snapshot.snapshot_id,
            "position_id": position.position_id,
            "account_id": position.account_id,
            "security_id": position.security_id,
            "entity_id": position.entity_id,
            "ticker": position.ticker,
            "quantity": float(position.quantity),
            "average_cost": _float(position.average_cost),
            "market_price": _float(position.market_price),
            "price_type": position.price_type,
            "market_value": _float(position.market_value),
            "unrealized_gain": _float(position.unrealized_gain),
            "unrealized_gain_pct": _float(position.unrealized_gain_pct),
            "portfolio_weight": _float(position.portfolio_weight),
            "source": position.source,
            "quote_retrieved_at": (
                position.quote_retrieved_at.isoformat() if position.quote_retrieved_at else None
            ),
            "asset_type": position.asset_type,
        } for position in snapshot.positions],
        root=parquet_root,
    )
    parquet.write_rows(
        "portfolio_accounts",
        [{"snapshot_id": snapshot.snapshot_id, "account_id": account_id}
         for account_id in snapshot.account_ids],
        root=parquet_root,
    )


def read_latest_snapshot(*, data_root: Path | None = None) -> PortfolioSnapshot | None:
    """Return the newest persisted snapshot, or None when none exists.

    ``account_ids`` comes from ``portfolio_accounts`` in write order (one
    write batch = one part file); for legacy snapshots predating that
    dataset it is reconstructed from the persisted positions as local
    opaque identifiers, in which case accounts without positions are not
    recoverable.  Positions are returned in persisted write order: a
    snapshot's rows are written in one batch, and position_id embeds
    opaque account ids that do not sort by account.
    """
    rows = duckdb.query(
        "SELECT * FROM portfolio_snapshots ORDER BY created_at DESC LIMIT 1",
        data_root=data_root,
    )
    if not rows:
        return None
    row = rows[0]
    snapshot_id = str(row["snapshot_id"])
    created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    position_rows = duckdb.query(
        "SELECT * FROM portfolio_positions WHERE snapshot_id = ?",
        params=[snapshot_id],
        data_root=data_root,
    )
    positions = tuple(
        mappers.position_from_row(position_row, created_at) for position_row in position_rows
    )
    account_rows = duckdb.query(
        "SELECT account_id FROM portfolio_accounts WHERE snapshot_id = ?",
        params=[snapshot_id],
        data_root=data_root,
    )
    account_ids = [str(row["account_id"]) for row in account_rows]
    if not account_ids:
        # Legacy snapshots predate portfolio_accounts: fall back to the
        # position-derived reconstruction (accounts without positions are
        # unrecoverable there, same as before this change).
        for position in positions:
            if position.account_id not in account_ids:
                account_ids.append(position.account_id)
    return PortfolioSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        broker=str(row["broker"]),
        account_ids=tuple(account_ids),
        cash=Decimal(str(row["cash"])) if row.get("cash") is not None else None,
        invested_value=Decimal(str(row["invested_value"])) if row.get("invested_value") is not None else None,
        total_value=Decimal(str(row["total_value"])) if row.get("total_value") is not None else None,
        positions=positions,
    )


def sync_robinhood_portfolio(
    provider: RobinhoodPortfolioProvider,
    *,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> PortfolioSnapshot:
    """Full read-only portfolio sync: accounts, positions, cash, one batched
    quote call, identity resolution, valuation, persistence."""
    created_at = now or datetime.now(timezone.utc)
    accounts = provider.get_accounts()
    raw_positions: list[BrokeragePosition] = []
    cash_balances: list[CashBalance] = []
    for account in accounts:
        raw_positions.extend(provider.get_positions(account.account_id))
        cash_balances.append(provider.get_cash_balance(account.account_id))
    tickers = list(dict.fromkeys(position.ticker for position in raw_positions))
    quotes = provider.get_equity_quotes(tickers)
    positions = [
        build_position(
            to_position_input(raw),
            resolve_security(
                raw.ticker,
                provider_instrument_id=raw.provider_instrument_id,
                as_of=created_at,
                data_root=data_root,
            ),
            to_quote(quotes[raw.ticker]) if raw.ticker in quotes else None,
        )
        for raw in raw_positions
    ]
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=[account.account_id for account in accounts],
        positions=positions,
        cash_balances={balance.account_id: balance.cash for balance in cash_balances},
        created_at=created_at,
    )
    persist_snapshot(snapshot, data_root=data_root)
    return snapshot