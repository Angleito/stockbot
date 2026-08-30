"""Portfolio synchronization service.

Resolves positions to Stockbot identities, values them against Robinhood
quotes, builds immutable portfolio snapshots, and persists/reads them from
the versioned Parquet datasets.  Never exposes provider internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence

from ..analytics.portfolio import (
    portfolio_market_value,
    position_market_value,
    position_weight,
    unrealized_gain,
    unrealized_gain_pct,
    valuation_price,
)
from ..domain.portfolio import PortfolioSnapshot, Position
from ..robinhood.account import BrokerageAccount, BrokeragePosition, CashBalance
from ..robinhood.options import MarketSnapshot
from ..robinhood.portfolio import RobinhoodPortfolioProvider
from ..storage import duckdb, ids, parquet

SNAPSHOT_SOURCE = "robinhood_mcp"
PARSER_VERSION = "robinhood-mcp-account-v1"
CALCULATION_VERSION = "portfolio-snapshot-v1"


@dataclass(frozen=True)
class SecurityResolution:
    security_id: str | None
    entity_id: str | None
    ticker: str
    resolved: bool
    resolution_method: str


def resolve_security(
    ticker: str,
    *,
    provider_instrument_id: str | None = None,
    as_of: date | None = None,
    data_root: Path | None = None,
) -> SecurityResolution:
    """Resolve a ticker to a Stockbot security/entity identity.

    ``provider_instrument_id`` is preserved on the position but is NOT used
    for resolution: Robinhood instrument IDs are not bridged to Stockbot
    identities yet.  Resolution is point-in-time over entity_aliases
    (newest-wins per ticker, knowable on/before ``as_of``); an alias with a
    later ``known_at`` can never retroactively resolve an earlier snapshot.
    No mappings are ever invented: unknown tickers resolve to
    ``resolved=False``.
    """
    del provider_instrument_id
    as_of = as_of or datetime.now(timezone.utc).date()
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    rows = duckdb.query(
        "SELECT alias_value, entity_id FROM entity_aliases "
        "WHERE alias_type = 'ticker' AND alias_value = ? AND "
        f"{clause} "
        "QUALIFY row_number() OVER (PARTITION BY alias_value "
        "ORDER BY known_at DESC, retrieved_at DESC) = 1",
        params=[ticker.strip().upper(), param],
        data_root=data_root,
    )
    if not rows:
        return SecurityResolution(None, None, ticker, False, "unresolved")
    entity_id = str(rows[0]["entity_id"])
    if entity_id.startswith("sec:cik:"):
        security_id = ids.sec_security_id(int(entity_id[len("sec:cik:"):]))
    else:
        security_id = None
    return SecurityResolution(security_id, entity_id, ticker, True, "entity_alias")


def build_position(
    raw: BrokeragePosition,
    resolution: SecurityResolution,
    quote: MarketSnapshot | None,
) -> Position:
    """Build a valued position; portfolio_weight is None here (computed by
    the snapshot builder once the invested total is known)."""
    valuation = valuation_price(quote) if quote else {"price": None, "price_type": None}
    market_price = Decimal(valuation["price"]) if valuation["price"] is not None else None
    market_value = position_market_value(raw.quantity, market_price)
    gain = unrealized_gain(market_value, raw.average_cost, raw.quantity)
    cost_basis = raw.average_cost * raw.quantity if raw.average_cost is not None else None
    return Position(
        position_id=raw.position_id,
        account_id=raw.account_id,
        security_id=resolution.security_id,
        entity_id=resolution.entity_id,
        ticker=raw.ticker,
        quantity=raw.quantity,
        average_cost=raw.average_cost,
        market_price=market_price,
        market_value=market_value,
        unrealized_gain=gain,
        unrealized_gain_pct=unrealized_gain_pct(gain, cost_basis),
        portfolio_weight=None,
        source=raw.source,
        retrieved_at=raw.retrieved_at,
        price_type=valuation["price_type"],
        quote_retrieved_at=quote.retrieved_at if quote else None,
    )


def build_portfolio_snapshot(
    *,
    accounts: Sequence[BrokerageAccount],
    positions: Sequence[Position],
    cash_balances: Sequence[CashBalance],
    created_at: datetime,
) -> PortfolioSnapshot:
    """Assemble the deterministic, immutable portfolio snapshot.

    Position weights use ``invested_value`` as the denominator (cash is
    carried on the snapshot, not per position).  Cash is the sum of non-None
    balances and is never invented as zero.
    """
    invested_value, _, _ = portfolio_market_value([position.market_value for position in positions])
    cash_values = [balance.cash for balance in cash_balances if balance.cash is not None]
    cash = sum(cash_values) if cash_values else None
    total_value = invested_value + cash if invested_value is not None and cash is not None else None

    snapshot_id = f"portfolio:robinhood:{created_at.isoformat()}"
    account_ids = tuple(account.account_id for account in accounts)
    built = tuple(
        Position(
            position_id=f"{snapshot_id}:{position.account_id}:{position.ticker}",
            account_id=position.account_id,
            security_id=position.security_id,
            entity_id=position.entity_id,
            ticker=position.ticker,
            quantity=position.quantity,
            average_cost=position.average_cost,
            market_price=position.market_price,
            market_value=position.market_value,
            unrealized_gain=position.unrealized_gain,
            unrealized_gain_pct=position.unrealized_gain_pct,
            portfolio_weight=position_weight(position.market_value, invested_value),
            source=position.source,
            retrieved_at=position.retrieved_at,
            price_type=position.price_type,
            quote_retrieved_at=position.quote_retrieved_at,
        )
        for position in positions
    )
    return PortfolioSnapshot(
        snapshot_id=snapshot_id,
        created_at=created_at,
        broker="robinhood",
        account_ids=account_ids,
        cash=cash,
        invested_value=invested_value,
        total_value=total_value,
        positions=built,
    )


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def persist_snapshot(
    snapshot: PortfolioSnapshot, *, data_root: Path | None = None
) -> None:
    """Persist an immutable snapshot (idempotent: a rerun writes 0 rows).

    Never writes OAuth/token or raw provider payload data.
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
        } for position in snapshot.positions],
        root=parquet_root,
    )


def _position_from_row(row: dict, retrieved_at: datetime) -> Position:
    """Rebuild a position from a persisted row.

    ``retrieved_at`` is not persisted in the position schema; it is
    reconstructed as the snapshot's ``created_at`` (the position was
    retrieved during the sync that created the snapshot).
    """
    numeric = {
        key: Decimal(str(row[key])) if row[key] is not None else None
        for key in (
            "quantity",
            "average_cost",
            "market_price",
            "market_value",
            "unrealized_gain",
            "unrealized_gain_pct",
            "portfolio_weight",
        )
    }
    quote_retrieved_at = row.get("quote_retrieved_at")
    return Position(
        position_id=str(row["position_id"]),
        account_id=str(row["account_id"]),
        security_id=str(row["security_id"]) if row.get("security_id") else None,
        entity_id=str(row["entity_id"]) if row.get("entity_id") else None,
        ticker=str(row["ticker"]),
        quantity=numeric["quantity"],
        average_cost=numeric["average_cost"],
        market_price=numeric["market_price"],
        market_value=numeric["market_value"],
        unrealized_gain=numeric["unrealized_gain"],
        unrealized_gain_pct=numeric["unrealized_gain_pct"],
        portfolio_weight=numeric["portfolio_weight"],
        source=str(row["source"]),
        retrieved_at=retrieved_at,
        price_type=str(row["price_type"]) if row.get("price_type") else None,
        quote_retrieved_at=(
            datetime.fromisoformat(quote_retrieved_at.replace("Z", "+00:00"))
            if quote_retrieved_at else None
        ),
    )


def read_latest_snapshot(*, data_root: Path | None = None) -> PortfolioSnapshot | None:
    """Return the newest persisted snapshot, or None when none exists.

    ``account_ids`` is reconstructed from the persisted positions (the
    snapshot schema carries only account_count); accounts without positions
    are not recoverable.
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
        "SELECT * FROM portfolio_positions WHERE snapshot_id = ? ORDER BY position_id",
        params=[snapshot_id],
        data_root=data_root,
    )
    positions = tuple(
        _position_from_row(position_row, created_at) for position_row in position_rows
    )
    account_ids: list[str] = []
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
            raw,
            resolve_security(
                raw.ticker,
                provider_instrument_id=raw.provider_instrument_id,
                data_root=data_root,
            ),
            quotes.get(raw.ticker),
        )
        for raw in raw_positions
    ]
    snapshot = build_portfolio_snapshot(
        accounts=accounts,
        positions=positions,
        cash_balances=cash_balances,
        created_at=created_at,
    )
    persist_snapshot(snapshot, data_root=data_root)
    return snapshot