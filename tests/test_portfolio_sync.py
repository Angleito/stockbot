"""End-to-end Robinhood portfolio sync tests with a recorded fake client.

Coverage under test:

- the exact read-only call sequence (accounts, per-account positions and
  portfolio, one batched deduplicated quotes call);
- snapshot math (cash sum, invested/total value, weights, zero-quantity
  positions, Decimal market values, deterministic snapshot/position ids);
- fail-explicit behavior on malformed fixture rows;
- the persistence round trip through ``read_latest_snapshot``;
- missing quote prices degrading the snapshot without crashing;
- no OAuth/token data in any persisted row.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.market.securities import SecurityResolution
from app.domain.portfolio import BrokeragePositionInput, PortfolioSnapshot, Position, local_account_id
from app.domain.portfolio.snapshot import build_portfolio_snapshot
from app.domain.portfolio.valuation import build_position
from app.robinhood.portfolio import RobinhoodPortfolioProvider
from app.services.portfolio_sync import (
    persist_snapshot,
    read_latest_snapshot,
    resolve_security,
    sync_robinhood_portfolio,
)
from app.storage import parquet

FIXTURES = Path(__file__).parent / "fixtures" / "robinhood"

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
SNAPSHOT_ID = "portfolio:robinhood:2026-08-25T12:00:00+00:00"
QUOTE_TIME = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _inline_positions(account_number):
    positions = {
        "100000001": [
            {"id": "pos-2", "instrument_id": "instr-aapl", "symbol": "AAPL", "quantity": "0.5",
             "average_buy_price": "150.25"},
            {"id": "pos-3", "instrument_id": "instr-tsla", "symbol": "TSLA", "quantity": "0",
             "average_buy_price": "200.00"},
            {"id": "pos-1", "instrument_id": "instr-wing", "symbol": "WING", "quantity": "10",
             "average_buy_price": "95.50"},
        ],
        "100000002": [
            {"id": "pos-5", "instrument_id": "instr-msft", "symbol": "MSFT", "quantity": "5",
             "average_buy_price": "400.00"},
            {"id": "pos-6", "instrument_id": "instr-wing-2", "symbol": "WING", "quantity": "1",
             "average_buy_price": "100.00"},
        ],
    }
    return {"data": {"positions": positions.get(account_number, [])}}


def _inline_balance(account_number):
    cash = {"100000001": "1234.56", "100000002": "2000.00"}[account_number]
    return {"data": {"cash": cash, "buying_power": {"buying_power": "2500.00"} }}


def _inline_quotes():
    return {"data": {"results": [
        {"quote": {"symbol": "WING", "last_trade_price": "116.84", "bid_price": "116.83",
                   "ask_price": "116.85", "venue_last_trade_time": "2026-08-25T15:00:00Z"}},
        {"quote": {"symbol": "AAPL", "last_trade_price": "160.00", "bid_price": "159.90",
                   "ask_price": "160.10", "venue_last_trade_time": "2026-08-25T15:00:00Z"}},
        {"quote": {"symbol": "TSLA", "last_trade_price": "250.00",
                   "venue_last_trade_time": "2026-08-25T15:00:00Z"}},
        {"quote": {"symbol": "MSFT", "last_trade_price": "405.00",
                   "venue_last_trade_time": "2026-08-25T15:00:00Z"}},
    ]}}


class FakeClient:
    """Records call_tool(name, args) and serves payloads keyed by tool name.

    Payloads may be plain values or callables receiving the arguments dict
    (used for per-account responses, mirroring the real MCP server).
    """

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        payload = self.payloads[name]
        if callable(payload):
            return payload(arguments)
        return payload


def _happy_payloads():
    return {
        "get_accounts": _fixture("accounts.json"),
        "get_equity_positions": lambda args: _inline_positions(args["account_number"]),
        "get_portfolio": lambda args: _inline_balance(args["account_number"]),
        "get_equity_quotes": _inline_quotes(),
    }


def _alias_row(**overrides):
    """One entity_aliases row for WING/sec:cik:0000320193; overrides win."""
    row = {
        "alias_type": "ticker",
        "alias_value": "WING",
        "entity_id": "sec:cik:0000320193",
        "security_id": "sec:equity:0000320193",
        "source": "sec",
        "valid_from": "2026-01-01",
        "known_at": "2026-08-25T00:00:00Z",
        "retrieved_at": "2026-08-25T00:00:00Z",
        "content_hash": "alias-wing",
        "parser_version": "test",
    }
    row.update(overrides)
    return row


def _seed_wing_alias(data_root):
    parquet.write_rows("entities", [{
        "entity_id": "sec:cik:0000320193",
        "name": "Wing Stop Inc",
        "entity_type": "company",
        "source": "sec",
        "known_at": "2026-01-01T00:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "content_hash": "entity-wing",
        "parser_version": "test",
    }], root=data_root / "parquet")
    parquet.write_rows("entity_aliases", [{
        "alias_type": "ticker",
        "alias_value": "WING",
        "entity_id": "sec:cik:0000320193",
        "security_id": "sec:equity:0000320193",
        "source": "sec",
        "valid_from": "2026-01-01",
        "known_at": "2026-01-01T00:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "content_hash": "alias-wing",
        "parser_version": "test",
    }], root=data_root / "parquet")
    parquet.write_rows("securities", [{
        "security_id": "sec:equity:0000320193",
        "entity_id": "sec:cik:0000320193",
        "security_type": "equity-common",
        "ticker": "WING",
        "source": "sec",
        "known_at": "2026-01-01T00:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "content_hash": "security-wing",
        "parser_version": "test",
    }], root=data_root / "parquet")


def _run_sync(data_root, payloads=None):
    client = FakeClient(payloads or _happy_payloads())
    provider = RobinhoodPortfolioProvider(client)
    snapshot = sync_robinhood_portfolio(provider, data_root=data_root, now=NOW)
    return client, provider, snapshot


def test_sync_uses_exact_readonly_call_sequence(data_root):
    client, _, snapshot = _run_sync(data_root)
    assert client.calls == [
        ("get_accounts", {}),
        ("get_equity_positions", {"account_number": "100000001"}),
        ("get_portfolio", {"account_number": "100000001"}),
        ("get_equity_positions", {"account_number": "100000002"}),
        ("get_portfolio", {"account_number": "100000002"}),
        ("get_equity_quotes", {"symbols": ["AAPL", "TSLA", "WING", "MSFT"]}),
    ]
    assert snapshot.snapshot_id == SNAPSHOT_ID


def test_sync_builds_valued_snapshot_with_two_accounts(data_root):
    _seed_wing_alias(data_root)
    _, _, snapshot = _run_sync(data_root)
    assert isinstance(snapshot, PortfolioSnapshot)
    assert snapshot.broker == "robinhood"
    assert snapshot.account_ids == (local_account_id("100000001"), local_account_id("100000002"))
    assert snapshot.created_at == NOW
    assert snapshot.cash == Decimal("3234.56")
    assert snapshot.invested_value == Decimal("3390.24")
    assert snapshot.total_value == Decimal("6624.80")
    assert len(snapshot.positions) == 5
    by_id = {position.position_id: position for position in snapshot.positions}
    wing = by_id[f"{SNAPSHOT_ID}:{local_account_id('100000001')}:WING"]
    assert wing.market_value == Decimal("1168.40")
    assert wing.market_price == Decimal("116.84")
    assert wing.quantity == Decimal("10")
    assert wing.unrealized_gain == Decimal("213.40")
    assert wing.price_type == "last"
    assert wing.quote_retrieved_at == QUOTE_TIME
    assert wing.entity_id == "sec:cik:0000320193"
    assert wing.security_id == "sec:equity:0000320193"
    assert wing.source == "robinhood_mcp"
    aapl = by_id[f"{SNAPSHOT_ID}:{local_account_id('100000001')}:AAPL"]
    assert aapl.market_value == Decimal("80.00")
    msft = by_id[f"{SNAPSHOT_ID}:{local_account_id('100000002')}:MSFT"]
    assert msft.market_value == Decimal("2025.00")
    assert msft.account_id == local_account_id("100000002")
    weights = [position.portfolio_weight for position in snapshot.positions]
    assert all(weight is not None for weight in weights)
    assert snapshot.total_value is not None
    assert sum(weights) == pytest.approx(snapshot.invested_value / snapshot.total_value)


def test_local_account_id_is_opaque_and_deterministic():
    assert local_account_id("100000001") == local_account_id("100000001")
    assert local_account_id("100000001") == "local:91b1e23ddbc8bddc"
    assert "100000001" not in local_account_id("100000001")
    assert local_account_id("100000001") != local_account_id("100000002")


def test_sync_raises_on_malformed_quantity(data_root):
    payloads = _happy_payloads()
    payloads["get_equity_positions"] = _fixture("positions.json")
    client = FakeClient(payloads)
    provider = RobinhoodPortfolioProvider(client)
    with pytest.raises(ValueError, match="quantity"):
        sync_robinhood_portfolio(provider, data_root=data_root, now=NOW)


def test_zero_quantity_position_is_valued_at_zero(data_root):
    _, _, snapshot = _run_sync(data_root)
    tsla = next(position for position in snapshot.positions if position.ticker == "TSLA")
    assert tsla.quantity == Decimal("0")
    assert tsla.market_value == Decimal("0")
    assert tsla.portfolio_weight == Decimal("0")


def test_snapshot_and_positions_are_persisted_once(data_root):
    _seed_wing_alias(data_root)
    client, _, _ = _run_sync(data_root)
    snapshots = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    positions = parquet.read_table("portfolio_positions", root=data_root / "parquet")
    assert snapshots.num_rows == 1
    assert snapshots.column("snapshot_id").to_pylist() == [SNAPSHOT_ID]
    assert snapshots.column("account_count").to_pylist() == [2]
    assert snapshots.column("position_count").to_pylist() == [5]
    assert snapshots.column("priced_position_count").to_pylist() == [5]
    assert snapshots.column("unresolved_position_count").to_pylist() == [3]
    assert snapshots.column("total_value").to_pylist() == [Decimal("6624.8")]
    assert snapshots.column("parser_version").to_pylist() == ["robinhood-mcp-account-v1"]
    assert snapshots.column("calculation_version").to_pylist() == ["portfolio-snapshot-v1"]
    assert positions.num_rows == 5
    assert positions.column("price_type").to_pylist() == ["last"] * 5


def _assert_position_close(actual, expected):
    assert actual.position_id == expected.position_id
    assert actual.account_id == expected.account_id
    assert actual.security_id == expected.security_id
    assert actual.entity_id == expected.entity_id
    assert actual.ticker == expected.ticker
    assert actual.source == expected.source
    assert actual.price_type == expected.price_type
    assert actual.quote_retrieved_at == expected.quote_retrieved_at
    for field in (
        "quantity", "average_cost", "market_price", "market_value",
        "unrealized_gain",
    ):
        # Money and quantity are stored at their exact scale (quantity *
        # price products fit decimal128(38, 14)); restored values equal
        # the live Decimals exactly.
        assert getattr(actual, field) == getattr(expected, field)
    for field in ("unrealized_gain_pct", "portfolio_weight"):
        # Ratios are computed at Python's default context precision (28
        # significant digits) and can carry more fractional digits than
        # the (38, 28) ratio columns hold; the write boundary rounds the
        # overflow, so compare approximately.
        assert getattr(actual, field) == pytest.approx(getattr(expected, field))


def test_read_latest_snapshot_round_trips(data_root):
    snapshot = _run_sync(data_root)[2]
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.snapshot_id == snapshot.snapshot_id
    assert restored.created_at == snapshot.created_at
    assert restored.broker == "robinhood"
    assert restored.account_ids == (local_account_id("100000001"), local_account_id("100000002"))
    assert restored.cash == snapshot.cash
    assert restored.invested_value == snapshot.invested_value
    assert restored.total_value == snapshot.total_value
    assert [position.position_id for position in restored.positions] == [
        position.position_id for position in snapshot.positions
    ]
    for actual, expected in zip(restored.positions, snapshot.positions):
        _assert_position_close(actual, expected)
        assert actual.retrieved_at == restored.created_at



def test_read_latest_snapshot_none_when_empty(data_root):
    assert read_latest_snapshot(data_root=data_root) is None


def test_cash_only_account_survives_round_trip(data_root):
    # Positions only in account A; account B holds cash only.  Before
    # portfolio_accounts, the read side derived account ids from position
    # rows and dropped B.
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={
            "100000001": Decimal("1000"),
            "100000002": Decimal("2000"),
        },
        created_at=NOW,
    )
    persist_snapshot(snapshot, data_root=data_root)
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.account_ids == (
        local_account_id("100000001"),
        local_account_id("100000002"),
    )


def test_empty_portfolio_with_cash_round_trips_account_ids(data_root):
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001"],
        positions=[],
        cash_balances={"100000001": Decimal("5000")},
        created_at=NOW,
    )
    persist_snapshot(snapshot, data_root=data_root)
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.account_ids == (local_account_id("100000001"),)
    assert restored.positions == ()


def test_account_ids_round_trip_preserves_order(data_root):
    account_a = "100000001"
    account_b = "100000002"
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=[account_a, account_b],
        positions=[_position(account_id=account_a), _position(account_id=account_b)],
        cash_balances={account_a: Decimal("1000"), account_b: Decimal("2000")},
        created_at=NOW,
    )
    persist_snapshot(snapshot, data_root=data_root)
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.account_ids == snapshot.account_ids


def test_decimal_round_trip_is_exact(data_root):
    # Values chosen to stress column scales: a small fractional quantity
    # whose price product needs all 14 fractional digits, a large dollar
    # amount, a repeating-fraction percentage, and non-integral decimals
    # whose storage-scale artifacts (trailing zeros) must canonicalize
    # away on read (0.5 -> not 0.5000...).
    position = Position(
        position_id="pos-1",
        account_id="100000001",
        security_id="sec:equity:0000320193",
        entity_id="sec:cik:0000320193",
        ticker="AMD",
        quantity=Decimal("0.00000001"),
        average_cost=Decimal("123456789012.34"),
        market_price=Decimal("123456789012.123456"),
        market_value=Decimal("1234.56789012123456"),
        unrealized_gain=Decimal("0.1"),
        unrealized_gain_pct=Decimal("0.3333333333"),
        portfolio_weight=None,
        source="robinhood_mcp",
        retrieved_at=NOW,
    )
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001"],
        positions=[position],
        cash_balances={"100000001": Decimal("1234.56789012123456")},
        created_at=NOW,
    )
    persist_snapshot(snapshot, data_root=data_root)
    restored = read_latest_snapshot(data_root=data_root)
    assert restored is not None
    assert restored.cash == snapshot.cash
    assert restored.invested_value == snapshot.invested_value
    assert restored.total_value == snapshot.total_value
    (actual,) = restored.positions
    (expected,) = snapshot.positions
    for field in (
        "quantity", "average_cost", "market_price", "market_value",
        "unrealized_gain", "unrealized_gain_pct", "portfolio_weight",
    ):
        assert getattr(actual, field) == getattr(expected, field)


def test_missing_quote_price_degrades_snapshot_but_persists(data_root):
    payloads = _happy_payloads()
    payloads["get_equity_positions"] = lambda args: (
        {"data": {"positions": [{"id": "pos-1", "instrument_id": "instr-wing", "symbol": "WING",
                                 "quantity": "10", "average_buy_price": "95.50"}]}}
        if args["account_number"] == "100000001"
        else {"data": {"positions": []}}
    )
    payloads["get_equity_quotes"] = {
        "data": {"results": [{"quote": {"symbol": "WING", "venue_last_trade_time": "2026-08-25T15:00:00Z"}}]}
    }
    _, _, snapshot = _run_sync(data_root, payloads)
    wing = snapshot.positions[0]
    assert wing.market_price is None
    assert wing.market_value is None
    assert wing.unrealized_gain is None
    assert wing.portfolio_weight is None
    assert wing.price_type is None
    assert wing.quote_retrieved_at == QUOTE_TIME
    assert snapshot.invested_value is None
    assert snapshot.total_value is None
    assert snapshot.cash == Decimal("3234.56")
    snapshots = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    positions = parquet.read_table("portfolio_positions", root=data_root / "parquet")
    assert snapshots.num_rows == 1
    assert snapshots.column("priced_position_count").to_pylist() == [0]
    assert snapshots.column("position_count").to_pylist() == [1]
    assert snapshots.column("total_value").to_pylist() == [None]
    assert positions.num_rows == 1
    assert positions.column("market_price").to_pylist() == [None]
    restored = read_latest_snapshot(data_root=data_root)
    assert restored.invested_value is None
    assert restored.positions[0].market_price is None

def test_partial_pricing_nils_total_and_weights(data_root):
    payloads = _happy_payloads()
    payloads["get_equity_positions"] = lambda args: (
        {"data": {"positions": [{"id": "pos-1", "instrument_id": "instr-wing", "symbol": "WING",
                                 "quantity": "10", "average_buy_price": "95.50"}]}}
        if args["account_number"] == "100000001"
        else {"data": {"positions": [{"id": "pos-5", "instrument_id": "instr-aapl", "symbol": "AAPL",
                                      "quantity": "5", "average_buy_price": "150.25"}]}}
    )
    payloads["get_equity_quotes"] = {
        "data": {"results": [{"quote": {"symbol": "WING", "last_trade_price": "116.84",
                                       "venue_last_trade_time": "2026-08-25T15:00:00Z"}}]}
    }
    _, _, snapshot = _run_sync(data_root, payloads)
    assert snapshot.invested_value == Decimal("1168.40")
    assert snapshot.total_value is None
    assert snapshot.cash == Decimal("3234.56")
    assert all(position.portfolio_weight is None for position in snapshot.positions)


def test_zero_quantity_unpriced_does_not_block_completeness(data_root):
    payloads = _happy_payloads()
    payloads["get_equity_positions"] = lambda args: (
        {"data": {"positions": [{"id": "pos-1", "instrument_id": "instr-wing", "symbol": "WING",
                                 "quantity": "0", "average_buy_price": "95.50"}]}}
        if args["account_number"] == "100000001"
        else {"data": {"positions": [{"id": "pos-5", "instrument_id": "instr-aapl", "symbol": "AAPL",
                                      "quantity": "5", "average_buy_price": "150.25"}]}}
    )
    payloads["get_equity_quotes"] = {
        "data": {"results": [{"quote": {"symbol": "AAPL", "last_trade_price": "160.00",
                                       "venue_last_trade_time": "2026-08-25T15:00:00Z"}}]}
    }
    _, _, snapshot = _run_sync(data_root, payloads)
    assert snapshot.invested_value == Decimal("800.00")
    assert snapshot.total_value is not None
    aapl = next(position for position in snapshot.positions if position.ticker == "AAPL")
    wing = next(position for position in snapshot.positions if position.ticker == "WING")
    assert aapl.portfolio_weight is not None
    assert wing.market_value == Decimal("0")
    assert wing.portfolio_weight == Decimal("0")


def test_empty_accounts_still_persists_empty_snapshot(data_root):
    client = FakeClient({"get_accounts": {"accounts": []}})
    provider = RobinhoodPortfolioProvider(client)
    snapshot = sync_robinhood_portfolio(provider, data_root=data_root, now=NOW)
    assert snapshot.positions == ()
    assert snapshot.cash is None
    assert snapshot.invested_value is None
    assert snapshot.total_value is None
    assert snapshot.account_ids == ()
    assert parquet.count_rows("portfolio_snapshots", root=data_root / "parquet") == 1
    assert parquet.count_rows("portfolio_positions", root=data_root / "parquet") == 0
    assert parquet.count_rows("portfolio_accounts", root=data_root / "parquet") == 0
    assert snapshot.snapshot_id == SNAPSHOT_ID


def test_persisted_rows_contain_no_oauth_data(data_root, monkeypatch):
    captured = []
    real_write_rows = parquet.write_rows

    def spy(name, rows, root=None):
        captured.append((name, list(rows)))
        return real_write_rows(name, rows, root=root)

    monkeypatch.setattr(parquet, "write_rows", spy)
    _run_sync(data_root)
    forbidden = ("token", "oauth", "secret", "access", "refresh", "authorization")
    assert captured
    for name, rows in captured:
        for row in rows:
            for key, value in row.items():
                assert not any(part in key.lower() for part in forbidden), (
                    f"{name}.{key} is a forbidden column"
                )
                assert not any(part in str(value).lower() for part in forbidden), (
                    f"{name}.{key} carries forbidden data"
                )


def test_persisted_rows_never_contain_raw_account_ids(data_root):
    _seed_wing_alias(data_root)
    _run_sync(data_root)
    snapshots = parquet.read_table("portfolio_snapshots", root=data_root / "parquet")
    positions = parquet.read_table("portfolio_positions", root=data_root / "parquet")
    accounts = parquet.read_table("portfolio_accounts", root=data_root / "parquet")
    for table in (snapshots, positions, accounts):
        for column_index in range(table.num_columns):
            for cell in table.column(column_index).to_pylist():
                if cell is not None:
                    assert "100000001" not in str(cell) and "100000002" not in str(cell), (
                        f"raw broker account id leaked into persisted cell: {cell!r}"
                    )
    assert positions.column("account_id").to_pylist() == [
        local_account_id("100000001"),
        local_account_id("100000001"),
        local_account_id("100000001"),
        local_account_id("100000002"),
        local_account_id("100000002"),
    ]
    assert accounts.column("account_id").to_pylist() == [
        local_account_id("100000001"),
        local_account_id("100000002"),
    ]


def test_sync_without_explicit_now_uses_utc_now(data_root, monkeypatch):
    class FrozenClock(datetime):
        """datetime subclass freezing now(); isinstance checks still pass."""

        calls = 0

        @staticmethod
        def now(tz=None):
            FrozenClock.calls += 1
            return FrozenClock(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    import app.services.portfolio_sync as portfolio_sync

    monkeypatch.setattr(portfolio_sync, "datetime", FrozenClock)
    client = FakeClient(_happy_payloads())
    snapshot = sync_robinhood_portfolio(
        RobinhoodPortfolioProvider(client), data_root=data_root
    )
    assert snapshot.created_at == NOW
    assert snapshot.snapshot_id == SNAPSHOT_ID


# ---------------------------------------------------------------------------
# resolve_security / build_position
# ---------------------------------------------------------------------------


def test_resolve_security_alias_learned_after_as_of_unresolved(data_root):
    as_of = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    parquet.write_rows("entity_aliases", [
        _alias_row(known_at="2026-08-26T00:00:00Z", retrieved_at="2026-08-26T00:00:00Z", content_hash="learned-later"),
    ], root=data_root / "parquet")
    late = resolve_security("WING", as_of=as_of, data_root=data_root)
    assert late.resolved is False
    assert late.resolution_method == "unresolved"
    parquet.write_rows("entity_aliases", [
        _alias_row(known_at="2026-08-25T00:00:00Z", retrieved_at="2026-08-25T00:00:00Z", source="control", content_hash="knowable"),
    ], root=data_root / "parquet")
    known = resolve_security("WING", as_of=as_of, data_root=data_root)
    assert known.resolved is True
    assert known.resolution_method == "entity_alias"
    assert known.entity_id == "sec:cik:0000320193"


def test_resolve_security_expired_alias_unresolved(data_root):
    parquet.write_rows("entity_aliases", [
        _alias_row(valid_from="2026-01-01", valid_to="2026-08-24", known_at="2026-08-01T00:00:00Z", retrieved_at="2026-08-01T00:00:00Z", content_hash="expired"),
        _alias_row(valid_from="2026-01-01", valid_to="2026-08-25", known_at="2026-08-02T00:00:00Z", retrieved_at="2026-08-02T00:00:00Z", source="control", content_hash="boundary"),
    ], root=data_root / "parquet")
    as_of = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    expired = resolve_security("WING", as_of=as_of, data_root=data_root)
    assert expired.resolved is False
    assert expired.resolution_method == "unresolved"
    # Half-open boundary: a date-only valid_to is midnight, so
    # valid_to="2026-08-25" is already expired at 00:00 on the 25th.
    boundary = resolve_security(
        "WING", as_of=datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc), data_root=data_root
    )
    assert boundary.resolved is False
    assert boundary.resolution_method == "unresolved"


def test_resolve_security_ambiguous_ticker(data_root):
    parquet.write_rows("entity_aliases", [
        _alias_row(entity_id="sec:cik:0000320193", security_id="sec:equity:0000320193", content_hash="alias-a"),
        _alias_row(entity_id="sec:cik:0000999999", security_id="sec:equity:0000999999", source="control", content_hash="alias-b"),
    ], root=data_root / "parquet")
    resolution = resolve_security(
        "WING", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root
    )
    assert resolution.resolved is False
    assert resolution.resolution_method == "ambiguous"
    assert resolution.entity_id is None
    assert resolution.security_id is None


def test_resolve_security_same_entity_multiple_rows_resolves(data_root):
    parquet.write_rows("entity_aliases", [
        _alias_row(security_id=None, known_at="2026-08-01T00:00:00Z", retrieved_at="2026-08-01T00:00:00Z", content_hash="older"),
        _alias_row(security_id="sec:equity:0000320193", known_at="2026-08-02T00:00:00Z", retrieved_at="2026-08-02T00:00:00Z", source="control", content_hash="newer"),
    ], root=data_root / "parquet")
    resolution = resolve_security(
        "WING", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root
    )
    assert resolution.resolved is True
    assert resolution.resolution_method == "entity_alias"
    assert resolution.entity_id == "sec:cik:0000320193"
    assert resolution.security_id == "sec:equity:0000320193"


def test_build_position_passes_asset_type():
    raw = BrokeragePositionInput(
        position_id="pos-1",
        account_id="acc-1",
        ticker="WING",
        provider_instrument_id="instr-1",
        quantity=Decimal("10"),
        average_cost=Decimal("95.50"),
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        source="robinhood_mcp",
        asset_type="option",
    )
    position = build_position(
        raw,
        SecurityResolution(None, None, "WING", False, "unresolved"),
        None,
    )
    assert position.asset_type == "option"


def _position(
    market_value: Decimal | None = Decimal("500"),
    *,
    account_id: str = "100000001",
) -> Position:
    return Position(
        position_id="pos-1",
        account_id=account_id,
        security_id="sec:equity:0000320193",
        entity_id="sec:cik:0000320193",
        ticker="AMD",
        quantity=Decimal("1"),
        average_cost=Decimal("400"),
        market_price=market_value,
        market_value=market_value,
        unrealized_gain=Decimal("100") if market_value is not None else None,
        unrealized_gain_pct=Decimal("0.25") if market_value is not None else None,
        portfolio_weight=None,
        source="robinhood_mcp",
        retrieved_at=NOW,
    )


def test_cash_complete_multi_account_sums():
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={"100000001": Decimal("1000"), "100000002": Decimal("2000")},
        created_at=NOW,
    )
    assert snapshot.cash == Decimal("3000")
    assert snapshot.invested_value == Decimal("500")
    assert snapshot.total_value == Decimal("3500")


def test_partial_cash_nils_total_and_weights():
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={"100000001": Decimal("1000"), "100000002": None},
        created_at=NOW,
    )
    assert snapshot.cash is None
    assert snapshot.total_value is None
    assert all(position.portfolio_weight is None for position in snapshot.positions)


def test_missing_balance_nils_total():
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={"100000001": Decimal("1000")},
        created_at=NOW,
    )
    assert snapshot.cash is None
    assert snapshot.total_value is None


def test_duplicate_balance_for_one_account_incomplete():
    # The mapping signature cannot represent duplicate balances (the
    # provider yields one CashBalance per account), so this collapses to a
    # missing-balance case and stays incomplete.
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={"100000001": Decimal("1000"), "100000001": Decimal("2000")},
        created_at=NOW,
    )
    assert snapshot.cash is None
    assert snapshot.total_value is None


def test_mismatched_balance_account_ids_incomplete():
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001", "100000002"],
        positions=[_position()],
        cash_balances={"100000001": Decimal("1000"), "100000003": Decimal("2000")},
        created_at=NOW,
    )
    assert snapshot.cash is None
    assert snapshot.total_value is None


def test_cash_only_portfolio_has_valid_totals():
    snapshot = build_portfolio_snapshot(
        broker="robinhood",
        account_ids=["100000001"],
        positions=[],
        cash_balances={"100000001": Decimal("5000")},
        created_at=NOW,
    )
    assert snapshot.invested_value == Decimal("0")
    assert snapshot.cash == Decimal("5000")
    assert snapshot.total_value == Decimal("5000")
    assert snapshot.positions == ()


def test_snapshot_builder_is_provider_neutral():
    snapshot = build_portfolio_snapshot(
        broker="testbroker",
        account_ids=["acct"],
        positions=[],
        cash_balances={"acct": Decimal("100")},
        created_at=NOW,
    )
    assert snapshot.broker == "testbroker"
    assert snapshot.snapshot_id == "portfolio:testbroker:2026-08-25T12:00:00+00:00"


# ---------------------------------------------------------------------------
# Provider-level extraction behavior
# ---------------------------------------------------------------------------


class TestProvider:
    def test_get_accounts_accepts_bare_list(self):
        client = FakeClient({"get_accounts": [{"id": "acc-1", "type": "individual"}]})
        accounts = RobinhoodPortfolioProvider(client).get_accounts()
        assert [account.account_id for account in accounts] == ["acc-1"]

    def test_get_accounts_wraps_bare_object(self):
        client = FakeClient({"get_accounts": {"id": "acc-1", "type": "individual"}})
        accounts = RobinhoodPortfolioProvider(client).get_accounts()
        assert [account.account_id for account in accounts] == ["acc-1"]

    def test_get_accounts_unknown_shape_raises(self):
        client = FakeClient({"get_accounts": "unexpected"})
        with pytest.raises(ValueError, match="get_accounts"):
            RobinhoodPortfolioProvider(client).get_accounts()

    def test_get_accounts_unwraps_structured_content_envelope(self):
        client = FakeClient({"get_accounts": _fixture("accounts.json")})
        accounts = RobinhoodPortfolioProvider(client).get_accounts()
        assert [account.account_id for account in accounts] == ["100000001", "100000002"]
        assert {account.account_type for account in accounts} == {"individual"}

    def test_get_accounts_falls_back_to_content_text_json(self):
        payload = {"content": [{"type": "text", "text": json.dumps({
            "data": {"accounts": [{"account_number": "c-1", "type": "margin",
                                   "brokerage_account_type": "individual", "state": "active"}]}
        })}]}
        client = FakeClient({"get_accounts": payload})
        accounts = RobinhoodPortfolioProvider(client).get_accounts()
        assert [account.account_id for account in accounts] == ["c-1"]

    def test_get_positions_normalizes_rows(self):
        client = FakeClient({"get_equity_positions": {"positions": [
            {"id": "p", "account_id": "acc-1", "ticker": "wing", "quantity": "0.5"},
        ]}})
        positions = RobinhoodPortfolioProvider(client).get_positions("acc-1")
        assert positions[0].ticker == "WING"
        assert positions[0].account_id == "acc-1"
        assert positions[0].quantity == Decimal("0.5")
        assert client.calls == [("get_equity_positions", {"account_number": "acc-1"})]

    def test_get_cash_balance_accepts_bare_object(self):
        client = FakeClient({"get_portfolio": {"account_id": "acc-1", "cash": "100.00"}})
        balance = RobinhoodPortfolioProvider(client).get_cash_balance("acc-1")
        assert balance.cash == Decimal("100.00")
        assert balance.account_id == "acc-1"

    def test_get_cash_balance_unwraps_nested_buying_power(self):
        client = FakeClient({"get_portfolio": {
            "data": {"cash": "100.00", "buying_power": {"buying_power": "250.00"}}
        }})
        balance = RobinhoodPortfolioProvider(client).get_cash_balance("acc-1")
        assert balance.cash == Decimal("100.00")
        assert balance.buying_power == Decimal("250.00")
        assert balance.account_id == "acc-1"
        assert client.calls == [("get_portfolio", {"account_number": "acc-1"})]

    def test_get_equity_quotes_merges_aliases_and_skips_missing_ticker(self):
        client = FakeClient({"get_equity_quotes": {"results": [
            {"symbol": "wing", "last": "116.84", "bid_price": "116.83", "askPrice": "116.85"},
            {"ticker": "AAPL", "price": "160.00"},
            {"last": "10.00"},
        ]}})
        quotes = RobinhoodPortfolioProvider(client).get_equity_quotes(["wing", "AAPL"])
        assert list(quotes) == ["WING", "AAPL"]
        assert quotes["WING"].last == Decimal("116.84")
        assert quotes["WING"].bid == Decimal("116.83")
        assert quotes["WING"].ask == Decimal("116.85")
        assert quotes["AAPL"].last == Decimal("160.00")
        assert client.calls == [("get_equity_quotes", {"symbols": ["WING", "AAPL"]})]

    def test_get_equity_quotes_digs_into_result_quote_objects(self):
        client = FakeClient({"get_equity_quotes": {
            "data": {"results": [
                {"quote": {"symbol": "WING", "last_trade_price": "116.84",
                           "bid_price": "116.83", "ask_price": "116.85",
                           "venue_last_trade_time": "2026-08-25T15:00:00Z"}},
            ]}
        }})
        quotes = RobinhoodPortfolioProvider(client).get_equity_quotes(["WING"])
        assert quotes["WING"].last == Decimal("116.84")
        assert quotes["WING"].bid == Decimal("116.83")
        assert quotes["WING"].ask == Decimal("116.85")
        assert quotes["WING"].retrieved_at == QUOTE_TIME

    def test_get_equity_quotes_accepts_bare_list(self):
        client = FakeClient({"get_equity_quotes": [
            {"symbol": "WING", "last": "116.84"},
        ]})
        quotes = RobinhoodPortfolioProvider(client).get_equity_quotes(["WING"])
        assert quotes["WING"].last == Decimal("116.84")

    def test_get_equity_quotes_empty_tickers_skips_call(self):
        client = FakeClient({})
        quotes = RobinhoodPortfolioProvider(client).get_equity_quotes([])
        assert quotes == {}
        assert client.calls == []


class TestScanProvider:
    def test_get_scanner_filter_specs_unwraps_envelope(self):
        client = FakeClient({"get_scanner_filter_specs": _fixture("scan_specs.json")})
        data = RobinhoodPortfolioProvider(client).get_scanner_filter_specs()
        assert client.calls == [("get_scanner_filter_specs", {})]
        assert data["filter_specs"][0]["filter_type"] == "FILTER_TYPE_INSTRUMENT_TYPE"

    def test_get_scans_returns_rows(self):
        client = FakeClient({"get_scans": _fixture("scans.json")})
        scans = RobinhoodPortfolioProvider(client).get_scans()
        assert client.calls == [("get_scans", {})]
        assert [scan["id"] for scan in scans] == ["scan-rsi-1", "scan-cortex-2"]
        assert scans[1]["cortex_managed"] is True

    def test_run_scan_passes_scan_id(self):
        client = FakeClient({"run_scan": _fixture("scan_results.json")})
        data = RobinhoodPortfolioProvider(client).run_scan("scan-rsi-1")
        assert client.calls == [("run_scan", {"scan_id": "scan-rsi-1"})]
        assert data["total"] == 3
        assert data["results"][0]["ticker"] == "WING"
