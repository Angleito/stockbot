"""Provider-neutral Robinhood option and quote models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    return next((payload[key] for key in keys if key in payload and payload[key] is not None), None)


def _date(value: Any) -> date | None:
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
    retrieved_at: datetime = datetime.min.replace(tzinfo=timezone.utc)
    source: str = "robinhood_mcp"

    @property
    def mid(self) -> Decimal | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal("2")
        return self.mark


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def to_json_dict(value: Any) -> dict[str, Any]:
    """Serialize a normalized model without leaking provider payloads."""
    data = asdict(value)
    return {key: _json_value(item) for key, item in data.items()}


def normalize_option_quote(payload: dict[str, Any], *, ticker: str = "") -> OptionQuote:
    """Normalize common provider aliases while keeping absent values nullable."""
    expiration = _date(_first_present(payload, "expiration", "expiration_date", "expirationDate"))
    strike = _decimal(_first_present(payload, "strike", "strike_price", "strikePrice"))
    if expiration is None or strike is None:
        raise ValueError("Option response is missing expiration or strike")
    option_type = str(
        _first_present(payload, "option_type", "type", "optionType") or ""
    ).lower()
    if option_type in {"p", "put"}:
        option_type = "put"
    elif option_type in {"c", "call"}:
        option_type = "call"
    else:
        raise ValueError("Option response is missing a supported option type")

    def integer(name: str, *aliases: str) -> int | None:
        raw = next((payload.get(key) for key in (name, *aliases) if payload.get(key) is not None), None)
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    retrieved = _first_present(payload, "retrieved_at", "retrievedAt", "updated_at", "updatedAt")
    if isinstance(retrieved, str):
        retrieved_at = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
    elif isinstance(retrieved, datetime):
        retrieved_at = retrieved
    else:
        retrieved_at = datetime.now(timezone.utc)
    if retrieved_at.tzinfo is None:
        retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)

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
        volume=integer("volume"),
        open_interest=integer("open_interest", "openInterest"),
        retrieved_at=retrieved_at,
        source=str(payload.get("source") or "robinhood_mcp"),
    )
