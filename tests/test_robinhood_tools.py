import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app import tools
from app.domain.portfolio import PortfolioSnapshot, Position
from app.services.portfolio_research import PortfolioResearchPosition
from app.tool_render import render_tool_result


FIXTURES = Path(__file__).parent / "fixtures" / "robinhood"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeRobinhood:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {
            "get_equity_quotes": _fixture("equity_quotes.json"),
            "get_option_chains": _fixture("option_chains.json"),
            "get_option_instruments": _fixture("option_instruments.json"),
            "get_option_quotes": _fixture("option_quotes.json"),
            "get_accounts": _fixture("accounts.json"),
            "get_portfolio": _fixture("balances.json"),
            "get_equity_positions": _fixture("positions.json"),
            "get_scanner_filter_specs": _fixture("scan_specs.json"),
            "get_scans": _fixture("scans.json"),
            "run_scan": _fixture("scan_results.json"),
        }[name]


def test_market_snapshot_uses_compact_observed_fields(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    result = tools.get_market_snapshot("wing")
    assert result["ticker"] == "WING"
    assert result["last"] == "116.84"
    assert result["source"] == "robinhood_mcp"


def test_option_chain_is_normalized_and_bounded(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    result = tools.get_option_chain("WING", "put", limit=1)
    assert result["returned"] == 1
    assert result["contracts"][0]["contract_id"] == "wing-put-80"
    assert result["contracts"][0]["delta"] == "-0.12"
    assert [name for name, _ in fake.calls] == [
        "get_option_chains", "get_option_instruments", "get_option_quotes"
    ]


def test_compare_options_returns_deterministic_analysis(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    result = tools.compare_robinhood_options("WING", "put", 80)
    assert result["result_type"] == "option_comparison"
    assert result["ranking"] == "target_pnl_desc"
    assert all("target_pnl" in row for row in result["contracts"])


def test_tool_schemas_have_dispatchers():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    robinhood_names = {
        "get_market_snapshot", "get_option_chain", "analyze_option_contract",
        "compare_options", "get_portfolio_snapshot",
        "get_scanner_filter_specs", "get_scans", "run_scan",
    }
    assert robinhood_names <= names
    assert robinhood_names <= set(tools._ROBINHOOD_HANDLERS)
    assert "place_option_order" not in names


def test_no_trading_tool_names():
    banned = ("order", "place", "submit", "cancel", "replace", "withdraw", "deposit", "transfer", "trade")
    names = [entry["function"]["name"] for entry in tools.TOOLS]
    assert len(names) >= 24
    for name in names:
        for token in banned:
            assert token not in name.lower(), f"{name} contains banned token {token}"


def test_scan_read_handlers_are_bounded(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    specs = tools._get_scanner_filter_specs({}, model="test")
    assert specs["result_type"] == "scan_specs"
    assert specs["count"] == 3
    assert "structured_content" not in specs
    scans = tools._get_scans({}, model="test")
    assert scans["result_type"] == "scan_list"
    assert scans["count"] == 2
    assert scans["scans"][1]["cortex_managed"] is True
    results = tools._run_scan({"scan_id": "scan-rsi-1"}, model="test")
    assert results["result_type"] == "scan_results"
    assert results["total"] == 3
    assert results["rows"][0]["ticker"] == "WING"
    assert results["live"] is True
    assert fake.calls == [
        ("get_scanner_filter_specs", {}),
        ("get_scans", {}),
        ("run_scan", {"scan_id": "scan-rsi-1"}),
    ]


def test_run_scan_limit_is_capped(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    result = tools._run_scan({"scan_id": "scan-rsi-1", "limit": 999}, model="test")
    assert len(result["rows"]) <= 25


def test_scan_renderers_are_bounded_markdown():
    fake = FakeRobinhood()
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
        list_result = tools._get_scans({}, model="test")
        results_result = tools._run_scan({"scan_id": "scan-rsi-1"}, model="test")
    for result in (list_result, results_result):
        rendered = render_tool_result(result, max_bytes=4096)
        assert rendered
        assert "structured_content" not in rendered
        assert "robinhood_mcp" in rendered


def _hand_built_snapshot() -> PortfolioSnapshot:
    resolved = Position(
        position_id="snap-1:100000001:WING",
        account_id="100000001",
        security_id="sec:equity:0000320193",
        entity_id="sec:cik:0000320193",
        ticker="WING",
        quantity=Decimal("10"),
        average_cost=Decimal("95.50"),
        market_price=Decimal("116.84"),
        market_value=Decimal("1168.40"),
        unrealized_gain=Decimal("213.40"),
        unrealized_gain_pct=Decimal("0.2234554973821989528795811518"),
        portfolio_weight=Decimal("0.75"),
        source="robinhood_mcp",
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        price_type="last",
        quote_retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    unresolved = Position(
        position_id="snap-1:100000001:ZZZZ",
        account_id="100000001",
        security_id=None,
        entity_id=None,
        ticker="ZZZZ",
        quantity=Decimal("5"),
        average_cost=Decimal("10.00"),
        market_price=Decimal("12.00"),
        market_value=Decimal("60.00"),
        unrealized_gain=Decimal("10.00"),
        unrealized_gain_pct=Decimal("0.2"),
        portfolio_weight=Decimal("0.25"),
        source="robinhood_mcp",
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        price_type="last",
        quote_retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )
    return PortfolioSnapshot(
        snapshot_id="portfolio:robinhood:2026-08-25T12:00:00+00:00",
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        broker="robinhood",
        account_ids=("100000001",),
        cash=Decimal("1234.56"),
        invested_value=Decimal("1228.40"),
        total_value=Decimal("2462.96"),
        positions=(resolved, unresolved),
    )


def _hand_built_research(snapshot: PortfolioSnapshot) -> list[PortfolioResearchPosition]:
    resolved = snapshot.positions[0]
    unresolved = snapshot.positions[1]
    return [
        PortfolioResearchPosition(
            position=resolved,
            latest_sec_metrics={
                "Revenue": {"value": Decimal("1000000"), "period_end": "2026-06-30"},
                "NetIncomeLoss": {"value": Decimal("200000"), "period_end": "2026-06-30"},
                "CashAndCashEquivalents": {"value": Decimal("300000"), "period_end": "2026-06-30"},
                "LongTermDebt": {"value": Decimal("400000"), "period_end": "2026-06-30"},
                "EntityCommonStockSharesOutstanding": {"value": Decimal("500000"), "period_end": "2026-07-01"},
            },
            latest_finra_metrics={
                "short_position": Decimal("100"),
                "prev_position": Decimal("90"),
                "short_interest_change": Decimal("10"),
                "short_interest_change_pct": Decimal("0.1111111111111111111111111111"),
                "days_to_cover": Decimal("1.5"),
                "settlement_date": "2026-08-14",
                "avg_daily_volume": Decimal("10000"),
                "known_at": "2026-08-17T12:00:00Z",
            },
            research_data_freshness={
                "as_of": "2026-08-25",
                "sec_latest_filed_at": "2026-08-20",
                "finra_settlement_date": "2026-08-14",
                "finra_known_at": "2026-08-17T12:00:00Z",
            },
        ),
        PortfolioResearchPosition(
            position=unresolved,
            latest_sec_metrics={},
            latest_finra_metrics={},
            research_data_freshness={},
        ),
    ]


def test_portfolio_snapshot_handler_refresh_path(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    snapshot = _hand_built_snapshot()
    monkeypatch.setattr(
        tools, "sync_robinhood_portfolio",
        lambda provider, **kwargs: snapshot,
    )
    monkeypatch.setattr(
        tools, "enrich_portfolio_research",
        lambda snapshot, **kwargs: _hand_built_research(snapshot),
    )
    result = tools._get_portfolio_snapshot({"refresh": True}, model="test")

    assert result["result_type"] == "portfolio_snapshot"
    assert "snapshot_id" not in result
    assert result["account_count"] == 1
    assert result["position_count"] == 2
    assert result["priced_position_count"] == 2
    assert result["unresolved_position_count"] == 1
    assert result["unresolved"] == ["ZZZZ"]
    assert isinstance(result["total_value"], str)
    assert isinstance(result["cash"], str)
    assert isinstance(result["invested_value"], str)
    assert isinstance(result["concentration"], str)
    assert len(result["positions"]) == 2
    for row in result["positions"]:
        assert isinstance(row["quantity"], str)
        assert isinstance(row["market_value"], str)
        assert isinstance(row["market_price"], str)
    assert result["positions"][0]["resolved"] is True
    assert result["positions"][0]["sec"]["Revenue"]["value"] == "1000000"
    assert result["positions"][0]["finra"]["short_position"] == "100"
    assert result["positions"][1]["resolved"] is False
    assert result["positions"][1]["sec"] == {}
    assert result["freshness"]["sec_latest_filed_at"] == "2026-08-20"
    assert result["freshness"]["snapshot_created_at"] == snapshot.created_at.isoformat()
    # No raw provider payloads or internal structures leak to the top level.
    assert "accounts" not in result
    assert "quotes" not in result
    assert "structured_content" not in result
    assert all(
        not ({"symbol", "id", "account_id", "instrument_id", "avg_price"} & set(row))
        for row in result["positions"]
    )


def test_portfolio_payload_and_rendering_never_expose_account_identifiers(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    snapshot = _hand_built_snapshot()
    monkeypatch.setattr(tools, "sync_robinhood_portfolio", lambda provider, **kwargs: snapshot)
    monkeypatch.setattr(tools, "enrich_portfolio_research", lambda snapshot, **kwargs: [])

    result = tools._get_portfolio_snapshot({"refresh": True}, model="test")
    payload = json.dumps(result)
    rendered = render_tool_result(result)
    for account_id in snapshot.account_ids:
        assert account_id not in payload
        assert account_id not in rendered
    assert "account_ids" not in result
    assert "snapshot_id" not in result


def test_robinhood_provider_errors_do_not_expose_request_identifiers(monkeypatch):
    def fail(arguments, model):
        raise RuntimeError("provider rejected account_number=100000001")

    monkeypatch.setitem(tools._ROBINHOOD_HANDLERS, "get_portfolio_snapshot", fail)
    result = tools.execute_tool("get_portfolio_snapshot", {}, model="test")
    assert "100000001" not in result["error"]
    assert "100000001" not in render_tool_result(result)


def test_portfolio_snapshot_refresh_flag_controls_sync(monkeypatch):
    fake = FakeRobinhood()
    monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
    snapshot = _hand_built_snapshot()
    sync_calls = {"count": 0}

    def fake_sync(provider, **kwargs):
        sync_calls["count"] += 1
        return snapshot

    monkeypatch.setattr(tools, "sync_robinhood_portfolio", fake_sync)
    monkeypatch.setattr(tools, "read_latest_snapshot", lambda **kwargs: snapshot)
    monkeypatch.setattr(tools, "enrich_portfolio_research", lambda snapshot, **kwargs: [])

    tools._get_portfolio_snapshot({"refresh": False}, model="test")
    assert sync_calls["count"] == 0
    tools._get_portfolio_snapshot({"refresh": True}, model="test")
    assert sync_calls["count"] == 1


def test_option_chain_renderer_is_bounded_markdown():
    fake = FakeRobinhood()
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(tools, "_robinhood_client", lambda: fake)
        result = tools.get_option_chain("WING", "put")
    rendered = render_tool_result(result, max_bytes=4096)
    assert "WING PUT OPTIONS" in rendered
    assert "Delta" in rendered
    assert "robinhood_mcp" in rendered
    assert "structured_content" not in rendered
