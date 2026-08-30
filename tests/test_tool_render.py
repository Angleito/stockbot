"""Tests for the tool-result rendering layer (app/tool_render.py).

Verifies that the tool messages sent to the main chat model are compact
rendered text/Markdown within the byte budget — never raw JSON or internal
result structures — using mocked run_chat() responses and direct renderer
unit tests. Offline and deterministic.
"""

import json
from unittest.mock import MagicMock

import pytest

from app import agent as agent_module
from app import finra_client
from app.agent import run_chat
from app.tool_render import (
    MAX_TOOL_MESSAGE_BYTES,
    TRUNCATED_MARKER,
    render_tool_result,
)
from app.tools import execute_tool

from tests.test_finra import (
    FakeCache,
    _mock_get,
    _response,
    _token_response,
)


def _briefing_result(total_records: int | None = 12) -> dict:
    return {
        "dataset": "consolidatedShortInterest",
        "group": "otcMarket",
        "dataset_id": "otcMarket/consolidatedShortInterest",
        "source": "FINRA Query API otcMarket/consolidatedShortInterest",
        "query": {
            "ticker": "AAPL",
            "start_date": "2026-03-01",
            "end_date": "2026-08-14",
            "limit": 50,
            "offset": 0,
        },
        "coverage": {
            "rows_matched": 12,
            "rows_analyzed": 12,
            "complete": True,
            "page_complete": True,
            "query_complete": True if total_records else None,
            "analysis_complete": True if total_records else None,
            "cap": None,
            "first_date": "2026-03-01",
            "last_date": "2026-08-14",
        },
        "metrics": {
            "fields": {
                "currentShortPositionQuantity": {
                    "min": 100, "max": 200, "mean": 150, "median": 150, "sum": 1800
                }
            },
            "latest_vs_prior": [
                {
                    "field": "currentShortPositionQuantity",
                    "latest": 12400000,
                    "prior": 10800000,
                    "change": 1600000,
                    "change_percent": 14.81,
                    "latest_date": "2026-08-14",
                    "prior_date": "2026-08-01",
                }
            ],
            "categorical": {"symbolCode": {"AAPL": 12}},
        },
        "trends": [
            "currentShortPositionQuantity: 12400000 vs prior 10800000 "
            "(+1600000, +14.81%) — up"
        ],
        "warnings": [],
        "briefing": {
            "summary": "Short interest rose 14.8%.",
            "key_findings": ["Position up"],
            "caveats": [],
            "follow_up_suggestion": "",
        },
        "briefing_source": "analysis_model",
        "analysis_model": "mock/analysis-model",
        "returned_count": 12,
        "limit": 50,
        "offset": 0,
        "next_offset": 12,
        "may_have_more": False,
        "total_records": total_records,
        "pagination_source": "finra_header" if total_records else "estimate",
    }


def _datapoints_result(n_fields: int = 5, n_rows: int = 5, cell: str = "v") -> dict:
    fields = [f"field{i}" for i in range(n_fields)]
    return {
        "dataset": "consolidatedShortInterest",
        "group": "otcMarket",
        "dataset_id": "otcMarket/consolidatedShortInterest",
        "source": "FINRA Query API otcMarket/consolidatedShortInterest",
        "fields": fields,
        "records": [
            {f: f"{cell}{i}-{j}" for j, f in enumerate(fields)}
            for i in range(n_rows)
        ],
        "returned_count": n_rows,
        "limit": n_rows,
        "offset": 0,
        "next_offset": n_rows,
        "may_have_more": False,
        "total_records": n_rows,
        "pagination_source": "finra_header",
    }


class FakeOpenRouter:
    """Returns one tool-call round, then a plain-text final answer."""

    def __init__(self, tool_name: str, tool_args: dict):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.calls = []

    def __call__(self, model, messages):
        self.calls.append(messages)
        if len(self.calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": self.tool_name,
                                        "arguments": json.dumps(self.tool_args),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        return {
            "choices": [
                {"message": {"role": "assistant", "content": "Final answer."}}
            ]
        }


@pytest.fixture
def fake_agent(monkeypatch):
    """Patches the agent's model call + tool execution; returns helpers."""

    def _run(tool_name, tool_args, result):
        fake = FakeOpenRouter(tool_name, tool_args)
        monkeypatch.setattr(agent_module, "_call_openrouter", fake)
        monkeypatch.setattr(
            agent_module, "execute_tool", lambda name, args, model: result
        )
        text, _trace = run_chat(
            [{"role": "user", "content": "Test question"}],
            model="test",
            return_trace=True,
        )
        tool_msgs = [
            m for m in fake.calls[-1] if m.get("role") == "tool"
        ]
        return text, fake, tool_msgs

    return _run


# ---------------------------------------------------------------------------
# The tool message is rendered text, never raw JSON or internal structures
# ---------------------------------------------------------------------------


def test_query_finra_tool_message_is_rendered_text_not_json(fake_agent):
    result = _briefing_result()
    _text, _fake, tool_msgs = fake_agent("query_finra", {"dataset": "x"}, result)
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert isinstance(content, str)
    assert content.startswith("FINRA: consolidatedShortInterest")
    assert "Key metrics" in content
    assert "Source: FINRA Query API" in content
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)


def test_query_finra_tool_message_never_contains_raw_records(fake_agent):
    result = _briefing_result()
    _text, _fake, tool_msgs = fake_agent("query_finra", {"dataset": "x"}, result)
    content = tool_msgs[0]["content"]
    assert '"records"' not in content
    assert '"symbolCode"' not in content
    assert '"currentShortPositionQuantity"' not in content
    assert "{" not in content  # no JSON objects reach the main model
    # Raw record values never appear in a briefing message.
    assert "Apple Inc." not in content


def test_query_finra_briefing_unchanged_within_budget(fake_agent):
    result = _briefing_result()
    _text, _fake, tool_msgs = fake_agent("query_finra", {"dataset": "x"}, result)
    content = tool_msgs[0]["content"]
    assert len(content.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert TRUNCATED_MARKER not in content
    assert "Omitted rows" not in content


def test_small_result_rendered_without_markers():
    result = {"a": 1, "b": "hello"}
    text = render_tool_result(result)
    assert text == "a: 1\nb: hello"
    assert TRUNCATED_MARKER not in text


# ---------------------------------------------------------------------------
# Structural reduction under the byte budget
# ---------------------------------------------------------------------------


def test_oversized_datapoints_reduced_preserves_pagination(fake_agent):
    result = _datapoints_result(n_fields=20, n_rows=25, cell="x" * 200)
    _text, _fake, tool_msgs = fake_agent(
        "get_finra_datapoints",
        {"dataset": "x", "fields": ["f0"]},
        result,
    )
    content = tool_msgs[0]["content"]
    assert len(content.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert TRUNCATED_MARKER in content
    assert "Omitted rows:" in content
    # Provenance and pagination survive the reduction.
    assert "Source: FINRA Query API" in content
    assert "Pagination:" in content
    assert "finra_header" in content
    assert "25 returned of 25 total" in content
    # The table still renders valid Markdown rows (complete entries only).
    assert "| field0 |" in content


def test_oversized_datapoint_text_fields_truncated_in_cells(fake_agent):
    result = _datapoints_result(n_fields=3, n_rows=5, cell="N" * 50_000)
    _text, _fake, tool_msgs = fake_agent(
        "get_finra_datapoints",
        {"dataset": "x", "fields": ["f0"]},
        result,
    )
    content = tool_msgs[0]["content"]
    assert len(content.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert "... [" in content and "chars]" in content  # per-cell marker
    assert "| field0 |" in content
    assert "Pagination:" in content


def test_oversized_filing_text_truncated_preserves_header(fake_agent):
    result = {
        "ticker": "AAPL",
        "form_type": "10-K",
        "item": "risk_factors",
        "filed": "2026-01-15",
        "accession_no": "0001",
        "text": "Risk factors. " * 100_000,
        "source": "10-K risk_factors filed 2026-01-15",
    }
    _text, _fake, tool_msgs = fake_agent(
        "get_filing_section",
        {"ticker": "AAPL", "form_type": "10-K", "item": "risk_factors"},
        result,
    )
    content = tool_msgs[0]["content"]
    assert len(content.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert TRUNCATED_MARKER in content
    assert "Ticker: AAPL" in content
    assert "item: risk_factors" in content
    assert "Source: 10-K risk_factors filed 2026-01-15" in content
    assert "Risk factors." in content


def test_error_rendered_as_plaintext_with_next_step(fake_agent):
    result = {
        "error": (
            "Unknown FINRA dataset 'nope'. Call list_finra_datasets to "
            "browse available datasets."
        )
    }
    _text, _fake, tool_msgs = fake_agent("query_finra", {"dataset": "nope"}, result)
    content = tool_msgs[0]["content"]
    assert content.startswith("Error: Unknown FINRA dataset 'nope'")
    assert "list_finra_datasets" in content
    assert "{" not in content


# ---------------------------------------------------------------------------
# Strict failed-tool rule: after a failing tool the loop stops and the
# deterministic unavailable-data response is returned — no second model
# completion can invent, derive, or substitute values
# ---------------------------------------------------------------------------


def test_failed_tool_stops_loop_with_deterministic_response(fake_agent):
    result = {
        "error": "FINRA request failed (400): sortFields restricted",
        "dataset": "consolidatedShortInterest",
        "dataset_id": "otcMarket/consolidatedShortInterest",
        "request_purpose": "exact datapoints request (get_finra_datapoints)",
        "http_status": 400,
        "finra_response": '{"error": "sortFields restricted"}',
        "environment": "production",
    }
    text, fake, tool_msgs = fake_agent(
        "get_finra_datapoints", {"dataset": "x", "fields": ["f"]}, result
    )
    assert len(fake.calls) == 1  # no second model completion
    assert len(tool_msgs) == 1
    # The tool message itself preserves dataset, purpose, status, and body.
    content = tool_msgs[0]["content"]
    assert "dataset: consolidatedShortInterest" in content
    assert "http_status: 400" in content
    assert "request_purpose: exact datapoints" in content
    assert "finra_response:" in content
    # The final answer is the deterministic unavailable-data response built
    # from the rendered error context, with a next step.
    assert text.startswith("The requested data is unavailable")
    assert "Tool: get_finra_datapoints" in text
    assert "FINRA request failed (400)" in text
    assert "dataset: consolidatedShortInterest" in text
    assert "http_status: 400" in text
    assert "finra_response:" in text
    assert "Next step" in text
    assert "estimated, derived, or substituted" in text


def test_failed_tool_deterministic_rule_is_general_for_all_tools(fake_agent):
    """The no-second-model-call rule applies to every failing tool, not just
    FINRA exact-data requests."""
    result = {"error": "No data found for ZZZZFAKE99: EDGAR"}
    text, fake, _tool_msgs = fake_agent(
        "get_fundamentals", {"ticker": "ZZZZFAKE99", "metric": "eps"}, result
    )
    assert len(fake.calls) == 1
    assert text.startswith("The requested data is unavailable")
    assert "Tool: get_fundamentals" in text
    assert "No data found for ZZZZFAKE99" in text
    assert "Next step" in text


class FakeTwoToolRound:
    """One round requesting two tools; a second call would fail the test."""

    def __init__(self):
        self.calls = []

    def __call__(self, model, messages):
        self.calls.append(messages)
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_short_interest",
                                    "arguments": json.dumps({"ticker": "AAPL"}),
                                },
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "get_finra_datapoints",
                                    "arguments": json.dumps(
                                        {"dataset": "x", "fields": ["f"]}
                                    ),
                                },
                            },
                        ],
                    }
                }
            ]
        }


def test_failed_tool_does_not_use_successful_sibling_results(monkeypatch):
    """Successful sibling tool results from the same round never replace the
    failed request's missing values; the loop still stops."""
    from app import agent as agent_module

    fake = FakeTwoToolRound()
    monkeypatch.setattr(agent_module, "_call_openrouter", fake)

    def _execute(name, args, model):
        if name == "get_short_interest":
            return {
                "dataset": "consolidatedShortInterest",
                "metrics": {"fields": {"currentShortPositionQuantity": {"min": 100}}},
            }
        return {
            "error": "FINRA request failed (400): nope",
            "http_status": 400,
            "finra_response": "{}",
        }

    monkeypatch.setattr(agent_module, "execute_tool", _execute)
    text, _trace = run_chat(
        [{"role": "user", "content": "Test question"}],
        model="test",
        return_trace=True,
    )
    assert len(fake.calls) == 1  # loop stopped after the failed round
    assert text.startswith("The requested data is unavailable")
    assert "Tool: get_finra_datapoints" in text
    assert "Tool: get_short_interest" not in text  # success is not a failure
    assert "100" not in text  # the sibling's value never fills the gap
    assert "Next step" in text


# ---------------------------------------------------------------------------
# Direct renderer unit tests
# ---------------------------------------------------------------------------


def test_datapoints_table_escapes_pipe_characters():
    result = _datapoints_result(n_fields=1, n_rows=1)
    result["records"][0]["field0"] = "a|b\nc"
    text = render_tool_result(result)
    assert "a\\|b c" in text
    assert "\n\n" not in text


def test_briefing_completeness_statuses_rendered():
    result = _briefing_result(total_records=3)
    text = render_tool_result(result)
    assert "query complete: yes" in text
    assert "analysis complete: yes" in text

    incomplete = _briefing_result(total_records=99)
    incomplete["coverage"]["query_complete"] = False
    incomplete["coverage"]["analysis_complete"] = False
    text = render_tool_result(incomplete)
    assert "query complete: no" in text
    assert "analysis complete: no" in text

    unknown = _briefing_result(total_records=None)
    text = render_tool_result(unknown)
    assert "query complete: unknown" in text
    assert "analysis complete: unknown" in text
    assert "estimate" in text


def test_render_always_fits_budget():
    result = {
        "source": "S",
        "records": [
            {"a": "x" * 1_000_000} for _ in range(100)
        ],
        "fields": ["a"],
    }
    text = render_tool_result(result, max_bytes=2048)
    assert len(text.encode("utf-8")) <= 2048
    assert text  # non-empty


# ---------------------------------------------------------------------------
# End-to-end composition: real FINRA client result -> renderer -> run_chat()
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _finra_isolation(monkeypatch):
    """Offline FINRA env + cache resets (same contract as the FINRA tests)."""
    monkeypatch.setenv("FINRA_USE_MOCK", "")
    monkeypatch.setenv("FINRA_ANALYSIS_MODEL", "")
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    yield
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()


@pytest.fixture
def finra_http(monkeypatch):
    get_mock = MagicMock(side_effect=_mock_get)
    post_mock = MagicMock()
    monkeypatch.setattr("app.finra_client.requests.get", get_mock)
    monkeypatch.setattr("app.finra_client.requests.post", post_mock)
    monkeypatch.setattr("app.finra_client.get_finra_client_id", lambda: "client")
    monkeypatch.setattr(
        "app.finra_client.get_finra_client_secret", lambda: "secret"
    )
    fc = FakeCache()
    monkeypatch.setattr(finra_client, "cache", fc)
    return {"get": get_mock, "post": post_mock}


def test_query_finra_end_to_end_real_result_rendered(finra_http, monkeypatch):
    """The real query_finra result (no raw rows) flows through run_chat and
    reaches the main model as rendered briefing text."""
    rows = [
        {
            "symbolCode": "AAPL",
            "issueName": "Apple Inc.",
            "settlementDate": "2026-08-14",
            "currentShortPositionQuantity": 100,
            "daysToCoverQuantity": 1.2,
        },
        {
            "symbolCode": "AAPL",
            "issueName": "Apple Inc.",
            "settlementDate": "2026-08-01",
            "currentShortPositionQuantity": 150,
            "daysToCoverQuantity": 1.4,
        },
    ]
    finra_http["post"].side_effect = [
        _token_response(),
        _response(rows, headers={"Record-Total": "2"}),
    ]

    args = {
        "dataset": "otcMarket/consolidatedShortInterest",
        "ticker": "AAPL",
        "limit": 2,
    }
    fake = FakeOpenRouter("query_finra", args)
    monkeypatch.setattr(agent_module, "_call_openrouter", fake)
    # execute_tool is intentionally NOT patched: the real FINRA client runs
    # against the mocked HTTP layer.

    _text, _trace = run_chat(
        [{"role": "user", "content": "What is AAPL's short interest?"}],
        model="test",
        return_trace=True,
    )

    tool_msgs = [m for m in fake.calls[-1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]

    # The client-side result itself carries the briefing contract.
    real_result = execute_tool("query_finra", args, model="test")
    assert "error" not in real_result, real_result
    assert "records" not in real_result
    assert real_result["coverage"]["page_complete"] is True
    assert real_result["coverage"]["query_complete"] is True
    assert real_result["coverage"]["analysis_complete"] is True
    assert real_result["briefing_source"] == "deterministic_only"

    # The tool message is rendered briefing text, not raw JSON/records.
    assert content.startswith("FINRA: consolidatedShortInterest — AAPL")
    assert "Key metrics" in content
    assert "Source: FINRA Query API" in content
    assert "query complete: yes" in content
    assert "analysis complete: yes" in content
    assert '"records"' not in content
    assert "Apple Inc." not in content
    assert '"currentShortPositionQuantity": 100' not in content
    assert "{" not in content
    assert len(content.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES


def test_non_dict_results_are_handled():
    text = render_tool_result("just a string")
    assert text == "result: just a string"


# ---------------------------------------------------------------------------
# Portfolio snapshot rendering
# ---------------------------------------------------------------------------


def _portfolio_snapshot_result(n_positions: int = 3, omitted: int = 0) -> dict:
    positions = []
    for i in range(n_positions):
        positions.append({
            "ticker": f"T{i}",
            "quantity": "10",
            "market_price": "100.00",
            "price_type": "last",
            "market_value": "1000.00",
            "portfolio_weight": "0.25",
            "unrealized_gain": "50.00",
            "security_id": f"sec:equity:{i}",
            "entity_id": f"sec:cik:{i}",
            "resolved": True,
            "sec": {
                "Revenue": {"value": "1000000", "period_end": "2026-06-30"},
                "NetIncomeLoss": {"value": "200000", "period_end": "2026-06-30"},
                "CashAndCashEquivalents": {"value": "300000", "period_end": "2026-06-30"},
                "LongTermDebt": {"value": "400000", "period_end": "2026-06-30"},
                "EntityCommonStockSharesOutstanding": {"value": "500000", "period_end": "2026-07-01"},
            },
            "finra": {
                "short_position": "100",
                "prev_position": "90",
                "change": "10",
                "change_pct": "0.1111111111",
                "days_to_cover": "1.5",
                "settlement_date": "2026-08-14",
            },
        })
    positions[0]["resolved"] = False
    return {
        "result_type": "portfolio_snapshot",
        "snapshot_id": "portfolio:robinhood:2026-08-25T12:00:00+00:00",
        "created_at": "2026-08-25T12:00:00+00:00",
        "broker": "robinhood",
        "account_ids": ["acc-1"],
        "total_value": "3234.56",
        "cash": "1234.56",
        "invested_value": "2000.00",
        "position_count": n_positions + omitted,
        "priced_position_count": n_positions - 1,
        "unresolved_position_count": 1,
        "concentration": "0.25",
        "positions": positions,
        "omitted_count": omitted,
        "largest_positions": [{"ticker": "T0", "market_value": "1000.00"}],
        "unresolved": ["T0"],
        "freshness": {
            "snapshot_created_at": "2026-08-25T12:00:00+00:00",
            "as_of": "2026-08-25",
            "sec_latest_filed_at": "2026-08-20",
            "finra_settlement_date": "2026-08-14",
            "finra_known_at": "2026-08-17T12:00:00Z",
        },
    }


def test_portfolio_snapshot_renders_markdown_within_budget():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result)
    assert text
    assert "portfolio" in text.lower()
    assert "T0" in text and "T2" in text
    assert "{" not in text  # no raw JSON reaches the main model
    assert len(text.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES


def test_portfolio_snapshot_reports_unresolved_and_research_freshness():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result)
    assert "[UNRESOLVED]" in text
    assert "Unresolved securities: T0" in text
    assert "SEC latest filing 2026-08-20" in text
    assert "FINRA settlement 2026-08-14" in text
    assert "Source: robinhood_mcp" in text


def test_large_portfolio_renders_omitted_count_within_budget():
    result = _portfolio_snapshot_result(n_positions=25, omitted=10)
    text = render_tool_result(result)
    assert len(text.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert "10 smaller positions omitted" in text
    assert "{" not in text


def test_portfolio_snapshot_truncated_when_budget_tiny():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result, max_bytes=128)
    assert len(text.encode("utf-8")) <= 128
    assert text  # non-empty