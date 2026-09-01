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
from typing import Sequence

from ..analytics.portfolio import (
    portfolio_market_value,
    position_market_value,
    position_weight,
    unrealized_gain,
    unrealized_gain_pct,
    valuation_price,
)
from ..domain.market.securities import SecurityResolution, TickerAlias
from ..domain.market.quotes import Quote
from ..domain.portfolio import BrokeragePositionInput, PortfolioSnapshot, Position, local_account_id
from ..robinhood.account import BrokerageAccount, BrokeragePosition, CashBalance
from ..robinhood.adapters import to_position_input, to_quote
from ..robinhood.portfolio import RobinhoodPortfolioProvider
from ..storage import duckdb, ids, parquet

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
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    rows = duckdb.query(
        "SELECT alias_type, alias_value, entity_id, security_id, source, "
        "valid_from, valid_to, known_at, retrieved_at, "
        "dense_rank() OVER (ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST) AS _newest_rank "
        "FROM entity_aliases "
        "WHERE alias_type = 'ticker' AND alias_value = ? AND "
        f"{clause} "
        "AND (valid_from IS NULL OR CAST(valid_from AS TIMESTAMP) <= CAST(? AS TIMESTAMP)) "
        "AND (valid_to IS NULL OR CAST(valid_to AS TIMESTAMP) > CAST(? AS TIMESTAMP)) "
        "ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, "
        "CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST",
        params=[ticker.strip().upper(), param, param, param],
        data_root=data_root,
    )
    if not rows:
        return SecurityResolution(None, None, ticker, False, "unresolved")
    entities = list(dict.fromkeys(row["entity_id"] for row in rows))
    if len(entities) > 1:
        return SecurityResolution(None, None, ticker, False, "ambiguous")
    material_security_ids = set()
    for row in rows:
        if row["_newest_rank"] != 1:
            break  # older instants are historical revisions, not conflicts
        row_security_id = row.get("security_id")
        entity_id = str(row["entity_id"])
        if row_security_id is None and entity_id.startswith("sec:cik:"):
            row_security_id = ids.sec_security_id(int(entity_id[len("sec:cik:"):]))
        material_security_ids.add(str(row_security_id) if row_security_id is not None else None)
    if len(material_security_ids) > 1:
        return SecurityResolution(None, entities[0], ticker, False, "ambiguous")
    newest = rows[0]
    alias = TickerAlias.from_row(newest)
    entity_id = alias.entity_id
    if alias.security_id is not None:
        security_id = alias.security_id
    elif entity_id.startswith("sec:cik:"):
        security_id = ids.sec_security_id(int(entity_id[len("sec:cik:"):]))
    else:
        security_id = None
    return SecurityResolution(security_id, entity_id, ticker, True, "entity_alias")


def build_position(
    raw: BrokeragePositionInput,
    resolution: SecurityResolution,
    quote: Quote | None,
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
        asset_type=raw.asset_type,
    )


def build_portfolio_snapshot(
    *,
    accounts: Sequence[BrokerageAccount],
    positions: Sequence[Position],
    cash_balances: Sequence[CashBalance],
    created_at: datetime,
) -> PortfolioSnapshot:
    """Assemble the deterministic, immutable portfolio snapshot.

    Position weights use ``total_value`` as the denominator (cash included);
    weights are ``None`` when total value is unknown.  Weights are ``None``
    whenever any non-zero position lacks a valuation (total_value is then
    ``None``; zero-quantity positions never block completeness).  Cash is the
    sum of all balances, and only when every account has a non-None balance
    (all-or-nothing completeness); partial or missing balances yield
    ``None``, never an invented partial sum.
    """
    account_ids = {a.account_id for a in accounts}
    balance_ids = {b.account_id for b in cash_balances}
    cash_complete = (
        bool(cash_balances)
        and len(cash_balances) == len(accounts)
        and balance_ids == account_ids
        and all(balance.cash is not None for balance in cash_balances)
    )
    cash = sum(balance.cash for balance in cash_balances) if cash_complete else None
    invested_value = (
        Decimal("0")
        if not positions and cash is not None
        else portfolio_market_value([position.market_value for position in positions])[0]
    )
    total_value = invested_value + cash if invested_value is not None and cash is not None else None
    valuation_complete = all(
        position.market_value is not None or position.quantity == 0
        for position in positions
    )
    if not valuation_complete:
        total_value = None

    snapshot_id = f"portfolio:robinhood:{created_at.isoformat()}"
    account_ids = tuple(local_account_id(account.account_id) for account in accounts)
    built_positions: list[Position] = []
    for position in positions:
        account_id = local_account_id(position.account_id)
        built_positions.append(
            Position(
                position_id=f"{snapshot_id}:{account_id}:{position.ticker}",
                account_id=account_id,
                security_id=position.security_id,
                entity_id=position.entity_id,
                ticker=position.ticker,
                quantity=position.quantity,
                average_cost=position.average_cost,
                market_price=position.market_price,
                market_value=position.market_value,
                unrealized_gain=position.unrealized_gain,
                unrealized_gain_pct=position.unrealized_gain_pct,
                portfolio_weight=position_weight(position.market_value, total_value),
                source=position.source,
                retrieved_at=position.retrieved_at,
                price_type=position.price_type,
                quote_retrieved_at=position.quote_retrieved_at,
                asset_type=position.asset_type,
            )
        )
    built = tuple(built_positions)
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
    asset_type = row.get("asset_type") or "equity"
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
        asset_type=asset_type,
    )


def read_latest_snapshot(*, data_root: Path | None = None) -> PortfolioSnapshot | None:
    """Return the newest persisted snapshot, or None when none exists.

    ``account_ids`` is reconstructed from the persisted positions (the
    snapshot schema carries only account_count) as local opaque
    identifiers; accounts without positions are not recoverable.
    Positions are returned in persisted write order: a snapshot's rows
    are written in one batch, and position_id embeds opaque account ids
    that do not sort by account.
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
        accounts=accounts,
        positions=positions,
        cash_balances=cash_balances,
        created_at=created_at,
    )
    persist_snapshot(snapshot, data_root=data_root)
    return snapshot