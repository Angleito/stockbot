"""Provider-neutral Robinhood option and quote models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from .account import _decimal, _first_present


def _date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    retrieved_at: datetime
    source: str = "robinhood_mcp"

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal(2)
        return None


@dataclass(frozen=True)
class OptionQuote:
    contract_id: str
    ticker: str
    expiration: date
    strike: Decimal
    option_type: str
    underlying_price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    mark: Decimal | None = None
    implied_volatility: Decimal | None = None
    delta: Decimal | None = None
    gamma: Decimal | None = None
    theta: Decimal | None = None
    vega: Decimal | None = None
    rho: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    source: str = "robinhood_mcp"

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal(2)
        return self.mark


def _coerce_option_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float, Decimal, str)):
        try:
            return int(raw)
        except TypeError, ValueError:
            return None
    return None


def _option_integer(payload: Mapping[str, object], name: str, *aliases: str) -> int | None:
    raw: object = next((payload.get(key) for key in (name, *aliases) if payload.get(key) is not None), None)
    if raw is None or raw == "":
        return None
    return _coerce_option_int(raw)


def _normalize_option_type(payload: Mapping[str, object]) -> str:
    option_type = str(_first_present(payload, "option_type", "type", "optionType") or "").lower()
    if option_type in {"p", "put"}:
        return "put"
    if option_type in {"c", "call"}:
        return "call"
    raise ValueError("Option response is missing a supported option type")


def _option_expiry_strike(payload: Mapping[str, object]) -> tuple[date, Decimal]:
    expiration = _date(_first_present(payload, "expiration", "expiration_date", "expirationDate"))
    strike = _decimal(_first_present(payload, "strike", "strike_price", "strikePrice"))
    if expiration is None or strike is None:
        raise ValueError("Option response is missing expiration or strike")
    return expiration, strike


def _option_retrieved_at(payload: Mapping[str, object]) -> datetime:
    retrieved = _first_present(payload, "retrieved_at", "retrievedAt", "updated_at", "updatedAt")
    if isinstance(retrieved, str):
        retrieved_at = datetime.fromisoformat(retrieved)
    elif isinstance(retrieved, datetime):
        retrieved_at = retrieved
    else:
        retrieved_at = datetime.now(UTC)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=UTC)
    return retrieved_at


def normalize_option_quote(payload: Mapping[str, object], *, ticker: str = "") -> OptionQuote:
    """Normalize common provider aliases while keeping absent values nullable."""
    expiration, strike = _option_expiry_strike(payload)
    option_type = _normalize_option_type(payload)
    retrieved_at = _option_retrieved_at(payload)

    return OptionQuote(
        contract_id=str(payload.get("contract_id") or payload.get("id") or payload.get("instrument_id") or ""),
        ticker=ticker or str(payload.get("ticker") or payload.get("symbol") or "").upper(),
        expiration=expiration,
        strike=strike,
        option_type=option_type,
        underlying_price=_decimal(_first_present(payload, "underlying_price", "underlyingPrice")),
        bid=_decimal(_first_present(payload, "bid", "bid_price", "bidPrice")),
        ask=_decimal(_first_present(payload, "ask", "ask_price", "askPrice")),
        mark=_decimal(
            _first_present(
                payload,
                "mark",
                "mark_price",
                "markPrice",
                "adjusted_mark_price",
                "adjustedMarkPrice",
            )
        ),
        implied_volatility=_decimal(_first_present(payload, "implied_volatility", "impliedVolatility", "iv")),
        delta=_decimal(payload.get("delta")),
        gamma=_decimal(payload.get("gamma")),
        theta=_decimal(payload.get("theta")),
        vega=_decimal(payload.get("vega")),
        rho=_decimal(payload.get("rho")),
        volume=_option_integer(payload, "volume"),
        open_interest=_option_integer(payload, "open_interest", "openInterest"),
        retrieved_at=retrieved_at,
        source=str(payload.get("source") or "robinhood_mcp"),
    )
