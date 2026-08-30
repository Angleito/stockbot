import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.portfolio import PortfolioSnapshot, Position
from app.robinhood.account import (
    normalize_account,
    normalize_cash_balance,
    normalize_position,
    to_json_dict,
)


FIXTURES = Path(__file__).parent / "fixtures" / "robinhood"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_decimal_parsing_from_string_and_numeric_payloads():
    from_string = normalize_cash_balance({"account_id": "a", "cash": "1234.56"})
    from_number = normalize_cash_balance({"account_id": "a", "cash": 1234.56})
    assert from_string.cash == Decimal("1234.56")
    assert from_number.cash == Decimal("1234.56")
    position = normalize_position({"id": "p", "account_id": "a", "ticker": "WING", "quantity": 10})
    assert position.quantity == Decimal("10")


def test_missing_cost_basis_is_none():
    position = normalize_position({"id": "p", "account_id": "a", "ticker": "WING", "quantity": "1"})
    assert position.average_cost is None


def test_fractional_shares():
    position = normalize_position({"id": "p", "accountId": "a", "symbol": "aapl", "quantity": "0.5"})
    assert position.ticker == "AAPL"
    assert position.quantity == Decimal("0.5")


def test_zero_quantity_is_valid():
    position = normalize_position({"id": "p", "account_id": "a", "ticker": "WING", "quantity": "0"})
    assert position.quantity == Decimal("0")


@pytest.mark.parametrize("quantity", ["abc", None, ""])
def test_malformed_or_missing_quantity_raises(quantity):
    payload = {"id": "p", "account_id": "a", "ticker": "WING"}
    if quantity is not None:
        payload["quantity"] = quantity
    with pytest.raises(ValueError, match="quantity"):
        normalize_position(payload)


def test_missing_ticker_raises():
    with pytest.raises(ValueError, match="ticker"):
        normalize_position({"id": "p", "account_id": "a", "quantity": "1"})


def test_blank_ticker_raises():
    with pytest.raises(ValueError, match="ticker"):
        normalize_position({"id": "p", "account_id": "a", "ticker": "", "quantity": "1"})


def test_missing_account_id_raises():
    with pytest.raises(ValueError, match="account_id"):
        normalize_account({"type": "individual"})
    with pytest.raises(ValueError, match="account_id"):
        normalize_cash_balance({"cash": "100.00"})
    with pytest.raises(ValueError, match="account_id"):
        normalize_position({"id": "p", "ticker": "WING", "quantity": "1"})


def test_position_account_id_kwarg_wins():
    position = normalize_position(
        {"id": "p", "account_id": "ignored", "ticker": "WING", "quantity": "1"}, account_id="kwarg-id"
    )
    assert position.account_id == "kwarg-id"


def test_accounts_fixture_returns_multiple_accounts():
    payload = _fixture("accounts.json")["structured_content"]["data"]
    accounts = [normalize_account(item) for item in payload["accounts"]]
    assert len(accounts) == 2
    assert {account.account_id for account in accounts} == {"100000001", "100000002"}
    assert {account.account_type for account in accounts} == {"individual"}
    assert {account.status for account in accounts} == {"active"}


def test_balances_fixture_matches_real_get_portfolio_shape():
    payload = _fixture("balances.json")["structured_content"]["data"]
    balance = normalize_cash_balance(payload, account_id="100000001")
    assert balance.account_id == "100000001"
    assert balance.cash == Decimal("0")
    assert balance.buying_power == Decimal("0.0000")
    assert balance.withdrawable_cash is None


def test_positions_fixture_normalizes_rows():
    payload = _fixture("positions.json")["structured_content"]["data"]
    wing, aapl, tsla = (normalize_position(item, account_id="acc-123") for item in payload["positions"][:3])
    assert wing.ticker == "WING"
    assert wing.quantity == Decimal("10")
    assert wing.average_cost == Decimal("95.50")
    assert wing.provider_instrument_id == "instr-wing"
    assert aapl.ticker == "AAPL"
    assert aapl.quantity == Decimal("0.5")
    assert aapl.average_cost == Decimal("150.25")
    assert aapl.provider_instrument_id == "instr-aapl"
    assert tsla.quantity == Decimal("0")
    assert tsla.average_cost == Decimal("200.00")


def test_positions_fixture_has_malformed_row_that_raises():
    payload = _fixture("positions.json")["structured_content"]["data"]
    with pytest.raises(ValueError, match="quantity"):
        normalize_position(payload["positions"][3], account_id="acc-123")


def test_alias_merging_snake_and_camel_normalize_identically():
    snake = normalize_cash_balance(
        {"account_id": "acc-1", "cash": "100.00", "buying_power": "200.00", "withdrawable_cash": "50.00"}
    )
    camel = normalize_cash_balance(
        {"accountId": "acc-1", "cashAvailable": "100.00", "buyingPowerAvailable": "200.00", "withdrawableAmount": "50.00"}
    )
    assert snake.cash == camel.cash == Decimal("100.00")
    assert snake.buying_power == camel.buying_power == Decimal("200.00")
    assert snake.withdrawable_cash == camel.withdrawable_cash == Decimal("50.00")

    snake_account = normalize_account({"id": "acc-1", "type": "individual", "status": "active"})
    camel_account = normalize_account({"account_id": "acc-1", "account_type": "individual", "state": "active"})
    assert (snake_account.account_type, snake_account.status) == (camel_account.account_type, camel_account.status)


def test_explicit_retrieved_at_is_honored():
    when = datetime(2026, 8, 25, 15, 0)
    account = normalize_account({"id": "acc-1"}, retrieved_at=when)
    assert account.retrieved_at == when.replace(tzinfo=timezone.utc)
    assert account.retrieved_at.tzinfo == timezone.utc


def test_naive_payload_timestamp_becomes_utc():
    position = normalize_position(
        {"id": "p", "account_id": "a", "ticker": "WING", "quantity": "1", "retrieved_at": "2026-08-25T15:00:00"}
    )
    assert position.retrieved_at == datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def test_z_suffix_timestamp_normalizes():
    balance = normalize_cash_balance({"account_id": "a", "cash": "1.00", "retrievedAt": "2026-08-25T15:00:00Z"})
    assert balance.retrieved_at == datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


def test_missing_retrieved_at_defaults_to_now_utc():
    before = datetime.now(timezone.utc)
    balance = normalize_cash_balance({"account_id": "a", "cash": "1.00"})
    after = datetime.now(timezone.utc)
    assert isinstance(balance.retrieved_at, datetime)
    assert balance.retrieved_at.tzinfo == timezone.utc
    assert before <= balance.retrieved_at <= after


def test_source_defaults_to_robinhood_mcp():
    assert normalize_account({"id": "a"}).source == "robinhood_mcp"
    assert normalize_cash_balance({"account_id": "a"}).source == "robinhood_mcp"
    assert normalize_position({"id": "p", "account_id": "a", "ticker": "WING", "quantity": "1"}).source == "robinhood_mcp"


def test_malformed_monetary_values_are_none_never_zero():
    balance = normalize_cash_balance({"account_id": "a", "cash": "abc", "buying_power": "", "withdrawable_cash": "oops"})
    assert balance.cash is None
    assert balance.buying_power is None
    assert balance.withdrawable_cash is None
    valid_zero = normalize_cash_balance({"account_id": "a", "cash": 0})
    assert valid_zero.cash == Decimal("0")


def test_non_string_instrument_id_is_none():
    position = normalize_position(
        {"id": "p", "account_id": "a", "ticker": "WING", "quantity": "1", "instrument_id": 123}
    )
    assert position.provider_instrument_id is None


def test_domain_models_are_frozen_and_hold_values():
    position = Position(
        position_id="p",
        account_id="a",
        security_id=None,
        entity_id=None,
        ticker="WING",
        quantity=Decimal("0.5"),
        average_cost=Decimal("95.50"),
        market_price=None,
        market_value=None,
        unrealized_gain=None,
        unrealized_gain_pct=None,
        portfolio_weight=None,
        source="robinhood_mcp",
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    assert position.quantity == Decimal("0.5")
    assert position.average_cost == Decimal("95.50")
    with pytest.raises(FrozenInstanceError):
        position.ticker = "AAPL"

    snapshot = PortfolioSnapshot(
        snapshot_id="snap-1",
        created_at=datetime(2026, 8, 25, 15, 1, tzinfo=timezone.utc),
        broker="robinhood",
        account_ids=("a",),
        cash=Decimal("1234.56"),
        invested_value=None,
        total_value=None,
        positions=(position,),
    )
    assert snapshot.account_ids == ("a",)
    assert snapshot.cash == Decimal("1234.56")
    assert snapshot.positions[0] is position
    with pytest.raises(FrozenInstanceError):
        snapshot.cash = Decimal("0")


def test_to_json_dict_serializes_decimals_and_datetimes():
    data = to_json_dict(normalize_cash_balance({"account_id": "a", "cash": "1234.56", "retrieved_at": "2026-08-25T15:00:00Z"}))
    assert data["cash"] == "1234.56"
    assert data["retrieved_at"] == "2026-08-25T15:00:00+00:00"
    assert data["source"] == "robinhood_mcp"