import json
from pathlib import Path

from app import tools
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
    robinhood_names = {"get_market_snapshot", "get_option_chain", "analyze_option_contract", "compare_options"}
    assert robinhood_names <= names
    assert robinhood_names <= set(tools._ROBINHOOD_HANDLERS)
    assert "place_option_order" not in names


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
