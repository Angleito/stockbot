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