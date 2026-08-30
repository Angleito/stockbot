"""Provider-neutral Robinhood brokerage account, cash, and position models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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


def _unwrap_nested(payload: dict[str, Any], outer_keys: tuple[str, ...], inner_keys: tuple[str, ...]) -> Any:
    """Return a value, descending one level when the outer value is a dict.

    Robinhood returns some scalars as nested objects (e.g. buying_power is
    ``{"buying_power": "0.0000", ...}``); malformed or missing values stay
    ``None`` and are never estimated.
    """
    value = _first_present(payload, *outer_keys)
    if isinstance(value, dict):
        value = _first_present(value, *inner_keys)
    return value


def _coerce_retrieved_at(payload: dict[str, Any], explicit: datetime | None) -> datetime:
    if explicit is not None:
        value = explicit
    else:
        retrieved = _first_present(payload, "retrieved_at", "retrievedAt", "updated_at", "updatedAt")
        if isinstance(retrieved, str):
            value = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        elif isinstance(retrieved, datetime):
            value = retrieved
        else:
            value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class BrokerageAccount:
    account_id: str
    account_type: str | None
    status: str | None
    retrieved_at: datetime
    source: str = "robinhood_mcp"


@dataclass(frozen=True)
class CashBalance:
    account_id: str
    cash: Decimal | None
    buying_power: Decimal | None
    withdrawable_cash: Decimal | None
    retrieved_at: datetime
    source: str = "robinhood_mcp"


@dataclass(frozen=True)
class BrokeragePosition:
    position_id: str
    account_id: str
    ticker: str
    provider_instrument_id: str | None
    quantity: Decimal
    average_cost: Decimal | None
    retrieved_at: datetime
    source: str = "robinhood_mcp"


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def to_json_dict(value: Any) -> dict[str, Any]:
    """Serialize a normalized model without leaking provider payloads."""
    data = asdict(value)
    return {key: _json_value(item) for key, item in data.items()}


def normalize_account(payload: dict[str, Any], *, retrieved_at: datetime | None = None) -> BrokerageAccount:
    """Normalize a get_accounts entry while keeping absent values nullable."""
    account_id = _first_present(payload, "account_id", "id", "accountNumber", "account_number")
    if account_id is None:
        raise ValueError("Account response is missing account_id")
    account_type = _first_present(payload, "brokerage_account_type", "type", "account_type", "accountType")
    status = _first_present(payload, "status", "state")
    return BrokerageAccount(
        account_id=str(account_id),
        account_type=str(account_type) if account_type is not None else None,
        status=str(status) if status is not None else None,
        retrieved_at=_coerce_retrieved_at(payload, retrieved_at),
    )


def normalize_cash_balance(
    payload: dict[str, Any], *, account_id: str | None = None, retrieved_at: datetime | None = None
) -> CashBalance:
    """Normalize a get_portfolio entry while keeping absent values nullable."""
    account_id = account_id or _first_present(payload, "account_id", "id", "accountNumber", "account_number", "accountId")
    if account_id is None:
        raise ValueError("Account response is missing account_id")
    return CashBalance(
        account_id=str(account_id),
        cash=_decimal(_first_present(payload, "cash", "cash_available", "cashAvailable", "available_cash", "availableCash")),
        buying_power=_decimal(
            _unwrap_nested(
                payload,
                ("buying_power", "buyingPower", "buying_power_available", "buyingPowerAvailable"),
                ("buying_power", "buyingPower", "value"),
            )
        ),
        withdrawable_cash=_decimal(
            _first_present(
                payload,
                "withdrawable_cash",
                "withdrawableCash",
                "withdrawable_amount",
                "withdrawableAmount",
            )
        ),
        retrieved_at=_coerce_retrieved_at(payload, retrieved_at),
    )


def normalize_position(
    payload: dict[str, Any], *, account_id: str | None = None, retrieved_at: datetime | None = None
) -> BrokeragePosition:
    """Normalize a get_equity_positions entry while keeping absent values nullable."""
    position_id = str(_first_present(payload, "position_id", "id", "positionId") or "")
    account_id = account_id or _first_present(payload, "account_id", "accountId", "account_number", "accountNumber")
    if account_id is None:
        raise ValueError("Position response is missing account_id")
    ticker = str(_first_present(payload, "ticker", "symbol", "instrument_symbol", "instrumentSymbol") or "").upper()
    if not ticker:
        raise ValueError("Position response is missing ticker")
    instrument = _first_present(payload, "instrument_id", "instrumentId", "instrument")
    quantity = _decimal(_first_present(payload, "quantity", "qty", "shares", "available_quantity", "availableQuantity"))
    if quantity is None:
        raise ValueError("Position response is missing or malformed quantity")
    return BrokeragePosition(
        position_id=position_id,
        account_id=str(account_id),
        ticker=ticker,
        provider_instrument_id=instrument if isinstance(instrument, str) else None,
        quantity=quantity,
        average_cost=_decimal(
            _first_present(
                payload,
                "average_cost",
                "averageCost",
                "average_buy_price",
                "averageBuyPrice",
                "cost_basis",
                "costBasis",
                "avg_price",
                "avgPrice",
            )
        ),
        retrieved_at=_coerce_retrieved_at(payload, retrieved_at),
    )