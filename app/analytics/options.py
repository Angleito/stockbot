"""Deterministic calculations for normalized option quotes."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from ..robinhood.options import OptionQuote

ZERO = Decimal("0")
CONTRACT_MULTIPLIER = Decimal("100")


def _as_of(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    return value.date() if isinstance(value, datetime) else value


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator in (None, ZERO):
        return None
    return numerator / denominator


def _payoff(quote: OptionQuote, target_price: Decimal, premium: Decimal) -> Decimal:
    if quote.option_type == "put":
        intrinsic = max(quote.strike - target_price, ZERO)
    else:
        intrinsic = max(target_price - quote.strike, ZERO)
    return (intrinsic - premium) * CONTRACT_MULTIPLIER


def analyze_option(
    quote: OptionQuote,
    *,
    as_of: date | datetime | None = None,
    target_price: Decimal | int | str | None = None,
) -> dict[str, Any]:
    """Return observable quote fields plus deterministic derived metrics."""
    today = _as_of(as_of)
    dte = (quote.expiration - today).days
    mid = quote.mid
    spread = quote.ask - quote.bid if quote.bid is not None and quote.ask is not None else None
    spread_pct = _ratio(spread, mid)
    intrinsic = None
    if quote.underlying_price is not None:
        intrinsic = (
            max(quote.strike - quote.underlying_price, ZERO)
            if quote.option_type == "put"
            else max(quote.underlying_price - quote.strike, ZERO)
        )
    extrinsic = mid - intrinsic if mid is not None and intrinsic is not None else None
    breakeven = None
    if mid is not None:
        breakeven = quote.strike - mid if quote.option_type == "put" else quote.strike + mid
    premium_per_contract = mid * CONTRACT_MULTIPLIER if mid is not None else None
    result: dict[str, Any] = {
        "contract_id": quote.contract_id,
        "ticker": quote.ticker,
        "expiration": quote.expiration.isoformat(),
        "dte": dte,
        "strike": str(quote.strike),
        "option_type": quote.option_type,
        "underlying_price": str(quote.underlying_price) if quote.underlying_price is not None else None,
        "bid": str(quote.bid) if quote.bid is not None else None,
        "ask": str(quote.ask) if quote.ask is not None else None,
        "mark": str(quote.mark) if quote.mark is not None else None,
        "mid": str(mid) if mid is not None else None,
        "spread": str(spread) if spread is not None else None,
        "spread_pct": str(spread_pct) if spread_pct is not None else None,
        "implied_volatility": str(quote.implied_volatility) if quote.implied_volatility is not None else None,
        "delta": str(quote.delta) if quote.delta is not None else None,
        "gamma": str(quote.gamma) if quote.gamma is not None else None,
        "theta": str(quote.theta) if quote.theta is not None else None,
        "vega": str(quote.vega) if quote.vega is not None else None,
        "rho": str(quote.rho) if quote.rho is not None else None,
        "volume": quote.volume,
        "open_interest": quote.open_interest,
        "intrinsic_value": str(intrinsic) if intrinsic is not None else None,
        "extrinsic_value": str(extrinsic) if extrinsic is not None else None,
        "breakeven_at_expiration": str(breakeven) if breakeven is not None else None,
        "premium_per_contract": str(premium_per_contract) if premium_per_contract is not None else None,
        "distance_from_underlying": (
            str(quote.strike - quote.underlying_price)
            if quote.underlying_price is not None
            else None
        ),
        "retrieved_at": quote.retrieved_at.isoformat(),
        "source": quote.source,
    }
    if target_price is not None and mid is not None:
        target = Decimal(str(target_price))
        pnl = _payoff(quote, target, mid)
        result["target_price"] = str(target)
        result["target_pnl"] = str(pnl)
        result["target_return_pct"] = str(_ratio(pnl, premium_per_contract) * Decimal("100")) if premium_per_contract else None
    return result


def compare_options(
    quotes: Iterable[OptionQuote],
    *,
    target_price: Decimal | int | str | None = None,
    as_of: date | datetime | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Analyze and deterministically rank contracts by target P/L or liquidity."""
    rows = [analyze_option(q, as_of=as_of, target_price=target_price) for q in quotes]
    if target_price is not None:
        rows.sort(key=lambda row: Decimal(row["target_pnl"]) if row.get("target_pnl") is not None else Decimal("-Infinity"), reverse=True)
    else:
        rows.sort(key=lambda row: Decimal(row["spread_pct"]) if row.get("spread_pct") is not None else Decimal("Infinity"))
    bounded = max(1, min(int(limit), 30))
    return {
        "contracts": rows[:bounded],
        "returned": min(len(rows), bounded),
        "matched": len(rows),
        "target_price": str(target_price) if target_price is not None else None,
        "ranking": "target_pnl_desc" if target_price is not None else "spread_pct_asc",
    }
