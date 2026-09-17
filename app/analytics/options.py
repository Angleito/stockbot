"""Deterministic calculations for normalized option quotes."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from ..robinhood.options import OptionQuote

ZERO = Decimal(0)
CONTRACT_MULTIPLIER = Decimal(100)


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == ZERO:
        return None
    return numerator / denominator


def _as_of(value: date | datetime | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    return value.date() if isinstance(value, datetime) else value


def _payoff(quote: OptionQuote, target_price: Decimal, premium: Decimal) -> Decimal:
    if quote.option_type == "put":
        intrinsic = max(quote.strike - target_price, ZERO)
    else:
        intrinsic = max(target_price - quote.strike, ZERO)
    return (intrinsic - premium) * CONTRACT_MULTIPLIER


def _expiry_dte(quote: OptionQuote, today: date) -> int:
    """Days from ``today`` to expiration (existing section boundary)."""
    return (quote.expiration - today).days


def _spread_metrics(quote: OptionQuote, mid: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """Bid/ask spread plus spread/mid ratio (existing section boundary)."""
    if quote.bid is None or quote.ask is None:
        return None, None
    spread = quote.ask - quote.bid
    return spread, _ratio(spread, mid)


def _intrinsic_value(quote: OptionQuote) -> Decimal | None:
    """Strike vs underlying intrinsic value (existing section boundary)."""
    if quote.underlying_price is None:
        return None
    if quote.option_type == "put":
        return max(quote.strike - quote.underlying_price, ZERO)
    return max(quote.underlying_price - quote.strike, ZERO)


def _payoff_grade(
    quote: OptionQuote,
    mid: Decimal | None,
    premium_per_contract: Decimal | None,
    target_price: Decimal | int | str | None,
) -> dict[str, object]:
    """Target-price payoff block (existing section boundary)."""
    if target_price is None or mid is None:
        return {}
    target = Decimal(str(target_price))
    pnl = _payoff(quote, target, mid)
    ratio = _ratio(pnl, premium_per_contract) if premium_per_contract else None
    return {
        "target_price": str(target),
        "target_pnl": str(pnl),
        "target_return_pct": str(ratio * Decimal(100)) if ratio is not None else None,
    }


def _extrinsic_value(mid: Decimal | None, intrinsic: Decimal | None) -> Decimal | None:
    """Mid minus intrinsic when both known (existing section boundary)."""
    if mid is None or intrinsic is None:
        return None
    return mid - intrinsic


def _breakeven(quote: OptionQuote, mid: Decimal | None) -> Decimal | None:
    """Strike +/- mid payoff breakeven (existing section boundary)."""
    if mid is None:
        return None
    if quote.option_type == "put":
        return quote.strike - mid
    return quote.strike + mid


def _premium_per_contract(mid: Decimal | None) -> Decimal | None:
    """Mid scaled to one contract (existing section boundary)."""
    if mid is None:
        return None
    return mid * CONTRACT_MULTIPLIER


def _s(value: object) -> str | None:
    """str() when present, else None (existing formatting boundary)."""
    return str(value) if value is not None else None


def _observable_fields(
    quote: OptionQuote, dte: int, mid: Decimal | None, spread: Decimal | None, spread_pct: Decimal | None
) -> dict[str, object]:
    """Quote passthrough + expiry/spread block (existing section boundary)."""
    return {
        "contract_id": quote.contract_id,
        "ticker": quote.ticker,
        "expiration": quote.expiration.isoformat(),
        "dte": dte,
        "strike": str(quote.strike),
        "option_type": quote.option_type,
        "underlying_price": _s(quote.underlying_price),
        "bid": _s(quote.bid),
        "ask": _s(quote.ask),
        "mark": _s(quote.mark),
        "mid": _s(mid),
        "spread": _s(spread),
        "spread_pct": _s(spread_pct),
        "implied_volatility": _s(quote.implied_volatility),
        "delta": _s(quote.delta),
        "gamma": _s(quote.gamma),
        "theta": _s(quote.theta),
        "vega": _s(quote.vega),
        "rho": _s(quote.rho),
        "volume": quote.volume,
        "open_interest": quote.open_interest,
    }


def _derived_fields(
    quote: OptionQuote,
    intrinsic: Decimal | None,
    extrinsic: Decimal | None,
    breakeven: Decimal | None,
    premium_per_contract: Decimal | None,
) -> dict[str, object]:
    """Derived value block (existing section boundary)."""
    distance = quote.strike - quote.underlying_price if quote.underlying_price is not None else None
    return {
        "intrinsic_value": _s(intrinsic),
        "extrinsic_value": _s(extrinsic),
        "breakeven_at_expiration": _s(breakeven),
        "premium_per_contract": _s(premium_per_contract),
        "distance_from_underlying": _s(distance),
        "retrieved_at": quote.retrieved_at.isoformat(),
        "source": quote.source,
    }


def analyze_option(
    quote: OptionQuote,
    *,
    as_of: date | datetime | None = None,
    target_price: Decimal | int | str | None = None,
) -> dict[str, object]:
    """Return observable quote fields plus deterministic derived metrics."""
    today = _as_of(as_of)
    dte = _expiry_dte(quote, today)
    mid = quote.mid
    spread, spread_pct = _spread_metrics(quote, mid)
    intrinsic = _intrinsic_value(quote)
    extrinsic = _extrinsic_value(mid, intrinsic)
    breakeven = _breakeven(quote, mid)
    premium_per_contract = _premium_per_contract(mid)
    result: dict[str, object] = {}
    result.update(_observable_fields(quote, dte, mid, spread, spread_pct))
    result.update(_derived_fields(quote, intrinsic, extrinsic, breakeven, premium_per_contract))
    result.update(_payoff_grade(quote, mid, premium_per_contract, target_price))
    return result


def _target_pnl_key(row: dict[str, object]) -> Decimal:
    value = row.get("target_pnl")
    return Decimal(str(value)) if value is not None else Decimal("-Infinity")


def _spread_pct_key(row: dict[str, object]) -> Decimal:
    value = row.get("spread_pct")
    return Decimal(str(value)) if value is not None else Decimal("Infinity")


def compare_options(
    quotes: Iterable[OptionQuote],
    *,
    target_price: Decimal | int | str | None = None,
    as_of: date | datetime | None = None,
    limit: int = 20,
) -> dict[str, object]:
    """Analyze and deterministically rank contracts by target P/L or liquidity."""
    rows = [analyze_option(q, as_of=as_of, target_price=target_price) for q in quotes]
    if target_price is not None:
        rows.sort(key=_target_pnl_key, reverse=True)
    else:
        rows.sort(key=_spread_pct_key)
    bounded = max(1, min(limit, 30))
    return {
        "contracts": rows[:bounded],
        "returned": min(len(rows), bounded),
        "matched": len(rows),
        "target_price": str(target_price) if target_price is not None else None,
        "ranking": "target_pnl_desc" if target_price is not None else "spread_pct_asc",
    }
