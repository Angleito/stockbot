"""Observability: typed runtime objects, redaction, and the runs store.

Offline: monkeypatches agent._call_openrouter and agent.execute_tool.
RUNS_DB_PATH is isolated per session by the root conftest fixture.
"""

import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import requests

from app import agent
from app import analytics
from app import finra_analysis
from app import finra_client
from app import tools as tools_module
from app.agent import _BUDGET_EXHAUSTED_RESPONSE, run_chat
from app.normalization import (
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)
from app.policy import Capability, ChatPolicy, RequestContext, RunLimits
from app.redact import redact_json, redact_text, redact_value
from app.runtime import (
    AgentState,
    BudgetExhaustedError,
    BudgetRemaining,
    EventType,
    ExecutionBudget,
    ModelCall,
    ResearchPlan,
    ResearchResult,
    ToolCall,
)
from app.tool_render import render_tool_result
from app.storage import parquet
from app.storage.runs import (
    get_events,
    get_evidence,
    get_model_calls,
    get_run,
    get_runs_db_path,
    get_tool_calls,
    list_runs,
    reset_current_budget,
    set_current_budget,
)

from tests.test_finra import FakeCache, _mock_get, _response, _token_response

TEST_POLICY = ChatPolicy(
    allowed_models=frozenset({"test"}),
    max_messages=20,
    max_message_chars=12_000,
    upstream_timeout_seconds=1,
)


def _usage(**overrides):
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost": 0.00012,
    }
    usage.update(overrides)
    return usage


class FakeOpenRouter:
    """Scripted OpenRouter responses; each call pops the next response."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, model, messages, *_):
        self.calls.append(messages)
        return self.script.pop(0)


def _tool_round(tool_name, tool_args, usage=None, request_id="req_abc"):
    return {
        "id": request_id,
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
                                "name": tool_name,
                                "arguments": json.dumps(tool_args),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": usage or _usage(),
    }


def _tool_round_raw(tool_name, raw_arguments, usage=None, request_id="req_abc"):
    """One tool request whose arguments field is a raw string, not JSON."""
    return {
        "id": request_id,
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
                                "name": tool_name,
                                "arguments": raw_arguments,
                            },
                        }
                    ],
                }
            }
        ],
        "usage": usage or _usage(),
    }


def _tool_round_multi(tool_specs, usage=None, request_id="req_abc"):
    """One response requesting several tools: list of (name, args) tuples."""
    return {
        "id": request_id,
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": name, "arguments": json.dumps(args)}}
                    for i, (name, args) in enumerate(tool_specs)
                ],
            }
        }],
        "usage": usage or _usage(),
    }

def _tool_round_multi_raw(tool_specs, usage=None, request_id="req_abc"):
    """One response requesting several tools: (name, raw arguments string)."""
    return {
        "id": request_id,
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": f"call_{i}", "type": "function",
                     "function": {"name": name, "arguments": raw}}
                    for i, (name, raw) in enumerate(tool_specs)
                ],
            }
        }],
        "usage": usage or _usage(),
    }


def _final(content, usage=None, request_id="req_def"):
    return {
        "id": request_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage or _usage(),
    }


def _research_context(**overrides):
    return RequestContext("test", frozenset({Capability.RESEARCH}), **overrides)


def _seed_leaderboard_data(data_root):
    """Seed a tmp parquet store with production normalizers only.

    Same fixture payloads as tests/test_analytics_screens.py (AAA/BBB/CCC
    tickers, shares-outstanding facts, FINRA rows for 2026-08-14).
    """
    tickers = {
        str(i): {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}
        for i, (ticker, cik) in enumerate(zip(("AAA", "BBB", "CCC"), (1, 2, 3)))
    }
    datasets = normalize_sec_tickers(tickers, retrieved_at="2026-08-10T12:00:00Z", content_hash="tickers-hash")
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=data_root / "parquet")
    for cik, shares in ((1, 100), (2, 200), (3, 10)):
        payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-08-01", "val": shares, "accn": f"a{cik}", "filed": "2026-08-02"},
            ]}},
        }}}
        datasets = normalize_sec_company_facts(
            payload, retrieved_at="2026-08-10T12:00:00Z", content_hash=f"facts-{cik}",
            source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
            source_record_id=f"cik{cik:010d}",
        )
        for name, rows in datasets.items():
            parquet.write_rows(name, rows, root=data_root / "parquet")
    rows = [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": "2026-08-14", "currentShortPositionQuantity": 20},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": "2026-08-14", "currentShortPositionQuantity": 20},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": "2026-08-14", "currentShortPositionQuantity": 5},
    ]
    datasets = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", known_at="2026-08-10T12:00:00Z",
        retrieved_at="2026-08-10T12:00:00Z", content_hash="snapshot-hash",
        source_url="https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest",
        source_record_id="otcMarket/consolidatedShortInterest:2026-08-14",
    )
    for name, rows_ in datasets.items():
        parquet.write_rows(name, rows_, root=data_root / "parquet")


def test_agent_tool_path_short_interest_leaderboard(monkeypatch, tmp_path):
    """Source-to-tool-to-runtime demo path: production normalizers seed a
    tmp store, and the REAL dispatcher (unpatched execute_tool) serves the
    leaderboard tool to the scripted model."""
    screens_module = analytics.screens
    _seed_leaderboard_data(tmp_path)
    monkeypatch.setattr(screens_module, "DEFAULT_DATA_ROOT", tmp_path)
    fake = FakeOpenRouter([
        _tool_round("get_short_interest_leaderboard", {"settlement_date": "2026-08-14", "as_of": "2026-08-21"}),
        _final("The short-interest leaderboard ranked CCC first."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)

    result = run_chat(
        [{"role": "user", "content": "Rank the short-interest leaderboard for 2026-08-14."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )

    assert result.answer == "The short-interest leaderboard ranked CCC first."
    assert result.groundedness == "grounded"
    assert result.evidence_refs
    run = get_run(result.run_id)
    assert run["status"] == "completed"
    assert run["tool_call_count"] == 1
    (call,) = get_tool_calls(result.run_id)
    assert call["tool_name"] == "get_short_interest_leaderboard"
    assert call["status"] == "completed"
    event_types = [ev["event_type"] for ev in get_events(result.run_id)]
    assert (
        event_types.index("tool_requested")
        < event_types.index("tool_completed")
        < event_types.index("evidence_added")
    )

def test_leaderboard_real_telemetry_envelope(monkeypatch, tmp_path):
    """Real leaderboard payload through the real dispatcher carries the
    envelope: row_count is the eligible universe, returned_count the
    post-LIMIT entries, truncated reflects the cut, and as_of persists on
    the tool_calls and evidence rows."""
    screens_module = analytics.screens
    _seed_leaderboard_data(tmp_path)
    monkeypatch.setattr(screens_module, "DEFAULT_DATA_ROOT", tmp_path)
    fake = FakeOpenRouter([
        _tool_round("get_short_interest_leaderboard", {
            "settlement_date": "2026-08-14", "as_of": "2026-08-21", "limit": 2,
        }),
        _final("ok"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)

    result = run_chat(
        [{"role": "user", "content": "Rank the short-interest leaderboard for 2026-08-14."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )

    assert result.answer == "ok"
    (tc,) = get_tool_calls(result.run_id)
    assert tc["result_row_count"] == 3
    assert tc["returned_count"] == 2
    assert tc["truncated"] == 1
    assert tc["as_of"] == "2026-08-21"
    (evidence,) = get_evidence(result.run_id)
    assert evidence["as_of"] == "2026-08-21"


# ---------------------------------------------------------------------------
# Full stream: typed runtime objects, event ordering, summary row
# ---------------------------------------------------------------------------


def test_runtime_objects_and_full_stream(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL", "metric": "eps"}),
        _final("AAPL EPS is 6.3 per the 10-Q."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent,
        "execute_tool",
        lambda name, args, model, **kwargs: {
            "ticker": "AAPL",
            "eps": 6.3,
            "source": "SEC EDGAR company facts",
            "as_of": "2026-08-29",
        },
    )
    result = run_chat(
        [{"role": "user", "content": "What is AAPL's diluted EPS?"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )

    assert isinstance(result, ResearchResult)
    assert result.run_id.startswith("run:")
    assert result.answer == "AAPL EPS is 6.3 per the 10-Q."
    assert result.groundedness == "grounded"
    assert len(result.evidence_refs) == 1
    assert result.evidence_refs[0].startswith(f"{result.run_id}:evid:")
    assert result.data_freshness == {"SEC EDGAR company facts": "2026-08-29"}

    # Typed runtime objects construct and behave as specified.
    plan = ResearchPlan(question="q", as_of="d")
    assert plan.to_dict() == {"question": "q", "as_of": "d"}
    budget = BudgetRemaining(
        rounds=8, tool_calls=64, model_calls=32, runtime_seconds=600.0, evidence_tokens=48000
    )
    state = AgentState(run_id="run:x", plan=plan, budget_remaining=budget)
    assert state.round == 0 and state.tool_calls == []
    call = ToolCall(
        tool_call_id="t", run_id="run:x", round=0, tool_name="n", tool_version="v",
        arguments_json="{}", started_at="a", completed_at="b", duration_ms=1.0,
        status="completed", result_row_count=0, returned_count=None, truncated=False,
        result_bytes=2, result_hash="h",
        source_names="[]", source_freshness="{}", as_of=None, error_type=None, error_message=None,
    )
    ModelCall(
        model_call_id="m", run_id="run:x", round=0, provider="p", model="m",
        started_at="a", completed_at="b", duration_ms=1.0, input_tokens=1,
        output_tokens=1, reasoning_tokens=0, cached_tokens=0, estimated_cost=0.0,
        finish_reason="stop", tool_call_count=0, provider_request_id=None,
    )
    assert call.status == "completed"
    assert EventType.RUN_STARTED == "run_started"
    assert len(list(EventType)) == 14

    # Summary row.
    run = get_run(result.run_id)
    assert run is not None
    assert run["status"] == "completed"
    assert run["question"] == "What is AAPL's diluted EPS?"
    assert run["as_of"] == date.today().isoformat()
    assert run["model_name"] == "test"
    assert run["model_provider"] == "openrouter"
    assert run["model_call_count"] == 2
    assert run["tool_call_count"] == 1
    assert run["round_count"] == 1
    assert run["input_tokens"] == 20
    assert run["output_tokens"] == 10
    assert run["total_tokens"] == 30
    assert run["estimated_total_cost"] == pytest.approx(0.00024)
    assert run["agent_version"] == "0.1.0"
    assert run["prompt_version"] == "1"
    assert run["tool_registry_version"] == agent.TOOL_REGISTRY_VERSION
    assert run["duration_ms"] is not None and run["duration_ms"] >= 0
    assert run["final_answer_hash"] is not None

    # Event stream: exact order, contiguous 1..N sequences.
    events = get_events(result.run_id)
    assert [ev["event_type"] for ev in events] == [
        "run_started", "research_context_created",
        "model_requested", "model_responded",
        "tool_requested", "tool_started", "tool_completed", "evidence_added",
        "model_requested", "model_responded",
        "finalization_started", "final_answer_created", "run_completed",
    ]
    assert [ev["sequence"] for ev in events] == list(range(1, len(events) + 1))
    assert json.loads(events[7]["evidence_ids"]) == result.evidence_refs

    # Tool call row.
    tool_calls = get_tool_calls(result.run_id)
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["tool_name"] == "get_fundamentals"
    assert tc["round"] == 0
    assert tc["status"] == "completed"
    assert tc["tool_version"] == agent.TOOL_REGISTRY_VERSION
    assert json.loads(tc["source_names"]) == ["SEC EDGAR company facts"]
    assert json.loads(tc["source_freshness"]) == {"SEC EDGAR company facts": "2026-08-29"}
    assert tc["as_of"] == "2026-08-29"
    assert tc["result_row_count"] == 0
    assert tc["error_type"] is None and tc["error_message"] is None

    # Model call rows.
    model_calls = get_model_calls(result.run_id)
    assert len(model_calls) == 2
    assert model_calls[0]["provider"] == "openrouter"
    assert model_calls[0]["provider_request_id"] == "req_abc"
    assert model_calls[0]["estimated_cost"] == pytest.approx(0.00012)
    assert model_calls[0]["tool_call_count"] == 1
    assert model_calls[1]["provider_request_id"] == "req_def"
    assert model_calls[1]["tool_call_count"] == 0

    # Recent-runs listing includes this run, newest first.
    rows = list_runs(limit=5)
    assert rows[0]["run_id"] == result.run_id


# ---------------------------------------------------------------------------
# Redaction: secrets never reach the store
# ---------------------------------------------------------------------------


def test_redaction_enforced(monkeypatch):
    secrets = {"account_number": "12345678", "ticker": "AAPL"}
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", secrets),
        _final("Bearer sk-or-v1-abcdefghijklmnop"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent,
        "execute_tool",
        lambda name, args, model, **kwargs: {
            "access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "account_id": "87654321",
            "nested": {"account_number": "11223344"},
            "source": "test",
        },
    )
    result = run_chat(
        [{"role": "user", "content": "Show my account"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    # The raw model answer passes through to the caller unchanged (redaction
    # applies to observability records, not to the user-facing answer).
    assert "Bearer sk-or-v1-abcdefghijklmnop" in result.answer

    # Stored tool row + TOOL_REQUESTED event arguments are redacted.
    tool_calls = get_tool_calls(result.run_id)
    assert "[REDACTED]" in tool_calls[0]["arguments_json"]
    requested = [
        ev for ev in get_events(result.run_id) if ev["event_type"] == "tool_requested"
    ]
    assert "[REDACTED]" in requested[0]["arguments"]

    # Raw DB dump of every text column across all five tables leaks nothing.
    db_path = get_runs_db_path(Path("."))
    conn = sqlite3.connect(str(db_path))
    try:
        chunks = []
        for table in ("agent_runs", "agent_events", "tool_calls", "model_calls", "evidence"):
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for col in cols:
                for row in conn.execute(f"SELECT {col} FROM {table}"):
                    if row[0] is not None:
                        chunks.append(str(row[0]))
    finally:
        conn.close()
    dump = "\n".join(chunks)

    for secret in (
        "12345678",
        "87654321",
        "11223344",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "sk-or-v1-abcdefghijklmnop",
    ):
        assert secret not in dump, f"secret leaked in runs DB: {secret}"
    # No unredacted credential-shaped text either.
    assert re.search(r"sk-or-v1-[A-Za-z0-9_-]{8,}", dump) is None
    assert re.search(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", dump) is None
    assert re.search(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", dump
    ) is None
    # The redaction marker is present (redaction happened, not absence).
    # Rule 1 masks "Bearer <token>" wholesale, so the stored sk-or form is
    # "Bearer [REDACTED]" — no "sk-or-v1-" prefix may survive anywhere.
    assert "Bearer [REDACTED]" in dump
    assert "sk-or-v1-" not in dump


# ---------------------------------------------------------------------------
# Failed and denied tools: deterministic close-outs, statuses, error rows
# ---------------------------------------------------------------------------


def test_failed_tool_run_is_partial(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL", "metric": "eps"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: {"error": "no data found"}
    )
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer.startswith("The requested data is unavailable")
    assert "no data found" in result.answer
    assert result.groundedness == "partial"
    assert result.evidence_refs == []

    run = get_run(result.run_id)
    assert run["status"] == "partial"
    events = get_events(result.run_id)
    failed_events = [ev for ev in events if ev["event_type"] == "tool_failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["success"] == 0
    assert failed_events[0]["tool_name"] == "get_fundamentals"
    tool_calls = get_tool_calls(result.run_id)
    assert len(tool_calls) == 1
    assert tool_calls[0]["status"] == "failed"
    assert tool_calls[0]["error_type"] == "tool_error"
    assert tool_calls[0]["error_message"] == "no data found"


def test_unpermitted_tool_denied(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("get_portfolio_snapshot", {}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda *a, **k: pytest.fail("execute_tool must not be called")
    )
    context = RequestContext("research", frozenset({Capability.RESEARCH}))
    result = run_chat(
        [{"role": "user", "content": "Show my portfolio"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert "Tool is not permitted: get_portfolio_snapshot" in result.answer
    run = get_run(result.run_id)
    assert run["status"] == "partial"
    tool_calls = get_tool_calls(result.run_id)
    assert len(tool_calls) == 1
    assert tool_calls[0]["status"] == "denied"
    assert tool_calls[0]["error_type"] == "permission_denied"


# ---------------------------------------------------------------------------
# Budget and recorder-degradation safety
# ---------------------------------------------------------------------------


def test_budget_exhausted_stops_run(monkeypatch):
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: {"ticker": "AAPL"}
    )
    context = _research_context(run_limits=RunLimits(max_model_calls=1))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert len(fake.calls) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    assert result.groundedness == "partial"
    run = get_run(result.run_id)
    assert run["status"] == "budget_exhausted"
    assert run["model_call_count"] == 1
    events = get_events(result.run_id)
    assert events[-1]["event_type"] == "run_completed"


def test_run_recorder_disabled_never_breaks(monkeypatch, tmp_path):
    # Point RUNS_DB_PATH at a path whose parent is a regular file, so the
    # recorder cannot create the database at all.
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("RUNS_DB_PATH", str(blocker / "runs.sqlite"))
    fake = FakeOpenRouter([_final("ok")])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": "hi"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer == "ok"
    assert get_run(result.run_id) is None


# ---------------------------------------------------------------------------
# Redaction units
# ---------------------------------------------------------------------------


def test_redact_text_units():
    assert redact_text("Authorization: Bearer abc123") == "Authorization: Bearer [REDACTED]"
    assert redact_text("key=sk-or-v1-abcdefghijklmnop end") == "key=sk-or-v1-[REDACTED] end"
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert redact_text(jwt) == "eyJ[REDACTED JWT]"
    assert redact_text("plain text") == "plain text"
    # Already-redacted output is stable under re-redaction.
    assert redact_text("Bearer [REDACTED]") == "Bearer [REDACTED]"
    assert redact_text("sk-or-v1-[REDACTED]") == "sk-or-v1-[REDACTED]"
    # Account identifiers in free text (review round 2 P1).
    assert redact_text("My account_number is 12345678") == "My account_number is [REDACTED]"
    assert redact_text("My account_id=87654321") == "My account_id=[REDACTED]"
    # Bare digit runs stay untouched (CIKs/accession numbers are structural).
    assert redact_text("cik 0000320193 filing") == "cik 0000320193 filing"
    assert redact_text("account 2026 taxes") == "account 2026 taxes"


def test_redact_value_units():
    value = {
        "accountNumber": "12345678",
        "nested": {"client_secret": "s3cret", "position_id": "pos-42"},
        "tags": ["a", "Bearer tok"],
        "count": 3,
        "flag": None,
        "provider_instrument_id": "inst-7",
    }
    redacted = redact_value(value)
    assert redacted["accountNumber"] == "[REDACTED]"
    assert redacted["nested"]["client_secret"] == "[REDACTED]"
    # Structural research identifiers pass through.
    assert redacted["nested"]["position_id"] == "pos-42"
    assert redacted["provider_instrument_id"] == "inst-7"
    assert redacted["tags"] == ["a", "Bearer [REDACTED]"]
    assert redacted["count"] == 3
    assert redacted["flag"] is None


def test_redact_json_units():
    assert redact_json('{"token": "abc", "ticker": "AAPL"}') == (
        '{"token": "[REDACTED]", "ticker": "AAPL"}'
    )
    assert redact_json("not json") == "not json"

# ---------------------------------------------------------------------------
# Recorder-independent budgets + stored-question redaction (PR #2 fixes)
# ---------------------------------------------------------------------------


def test_question_redacted_in_store(monkeypatch):
    """The stored question column is redacted at write time; the raw user
    message (with credentials) never reaches the DB."""
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, args, model, **kwargs: {"source": "test"},
    )

    # (a) Pure-JSON question: dict-key-norm redaction applies.
    fake = FakeOpenRouter([_final("ok")])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": '{"account_number": "12345678", "access_token": "abc"}'}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    run = get_run(result.run_id)
    assert "[REDACTED]" in run["question"]
    assert "12345678" not in run["question"]
    assert "abc" not in run["question"]

    # (b) Free-text question: Bearer/sk-or text rules apply via the fallback.
    fake = FakeOpenRouter([_final("ok")])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": (
            "My account_number is 12345678, my account_id=87654321. "
            "What is my balance? Bearer abc123, key sk-or-v1-abcdefghijklmnop"
        )}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    run = get_run(result.run_id)
    assert run["question"] == (
        "My account_number is [REDACTED], my account_id=[REDACTED]. "
        "What is my balance? Bearer [REDACTED], key sk-or-v1-[REDACTED]"
    )

    # Raw-DB dump of every text column across all five tables leaks nothing.
    db_path = get_runs_db_path(Path("."))
    conn = sqlite3.connect(str(db_path))
    try:
        chunks = []
        for table in ("agent_runs", "agent_events", "tool_calls", "model_calls", "evidence"):
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for col in cols:
                for row in conn.execute(f"SELECT {col} FROM {table}"):
                    if row[0] is not None:
                        chunks.append(str(row[0]))
    finally:
        conn.close()
    dump = "\n".join(chunks)

    for secret in ("12345678", "87654321", "abc123", "sk-or-v1-abcdefghijklmnop"):
        assert secret not in dump, f"secret leaked in runs DB: {secret}"
    # The "abc" credential value from (a); word boundaries so provider
    # request ids from other tests ("req_abc") cannot false-positive.
    assert re.search(r"\babc\b", dump) is None


def test_forced_answer_respects_model_budget(monkeypatch):
    """The forced-final-answer branch reserves before its extra model call."""
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: {"ticker": "AAPL"}
    )
    context = _research_context(run_limits=RunLimits(max_rounds=0, max_model_calls=1))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert len(fake.calls) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    run = get_run(result.run_id)
    assert run["status"] == "budget_exhausted"


def test_round_cap_forced_answer(monkeypatch):
    """Happy path of the round-cap branch: the forced answer call is budgeted
    and recorded."""
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
        _final("answer"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: {"ticker": "AAPL"}
    )
    context = _research_context(run_limits=RunLimits(max_rounds=0, max_model_calls=2))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert len(fake.calls) == 2
    assert result.answer == "answer"
    run = get_run(result.run_id)
    assert run["status"] == "partial"
    assert len(get_model_calls(result.run_id)) == 2


def test_nested_model_call_reserves_budget(monkeypatch):
    """A nested model call inside a tool reserves against the run budget
    before the network call; exhaustion surfaces as a failed tool."""
    fake = FakeOpenRouter([
        _tool_round("get_earnings_summary", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        tools_module.edgar_client, "get_latest_earnings_release",
        lambda ticker: {"ticker": ticker, "text": "press release text", "source": "8-K test"},
    )
    monkeypatch.setattr("app.cache.get", lambda *a, **k: None)
    monkeypatch.setattr(
        tools_module.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    context = _research_context(run_limits=RunLimits(max_model_calls=1))
    result = run_chat(
        [{"role": "user", "content": "How did AAPL do last quarter?"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    assert len(fake.calls) == 1
    assert result.answer.startswith("The requested data is unavailable")
    assert "model call budget exhausted" in result.answer
    run = get_run(result.run_id)
    assert run["status"] == "partial"


def test_nested_earnings_failure_recorded(monkeypatch):
    """A failed nested model call inside a tool records a failed model_calls
    row (status/error_type/error_category), and model_call_count counts it."""
    fake = FakeOpenRouter([
        _tool_round("get_earnings_summary", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        tools_module.edgar_client, "get_latest_earnings_release",
        lambda ticker: {"ticker": ticker, "text": "press release text", "source": "8-K test"},
    )
    monkeypatch.setattr("app.cache.get", lambda *a, **k: None)

    def _boom(*a, **k):
        raise requests.Timeout("boom")

    monkeypatch.setattr(tools_module.requests, "post", _boom)
    result = run_chat(
        [{"role": "user", "content": "How did AAPL do last quarter?"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer.startswith("The requested data is unavailable")
    run = get_run(result.run_id)
    assert run["status"] == "partial"
    assert run["model_call_count"] == 2
    (tc,) = get_tool_calls(result.run_id)
    assert tc["status"] == "failed"
    model_calls = get_model_calls(result.run_id)
    assert len(model_calls) == 2
    assert model_calls[0]["status"] == "completed"
    failed = model_calls[1]
    assert failed["status"] == "failed"
    assert failed["error_type"] == "Timeout"
    assert failed["error_category"] == "timeout"
    assert failed["input_tokens"] == 0
    assert failed["estimated_cost"] == 0


def test_finra_nested_failure_records_and_falls_back(monkeypatch):
    """A failed nested FINRA analysis call records a failed model_calls row
    while the deterministic briefing still wins."""
    monkeypatch.setenv("FINRA_ANALYSIS_MODEL", "mock/analysis-model")
    monkeypatch.setenv("FINRA_USE_MOCK", "")
    monkeypatch.setattr(finra_client, "get_finra_client_id", lambda: "client")
    monkeypatch.setattr(finra_client, "get_finra_client_secret", lambda: "secret")
    finra_client.reset_token_cache()
    finra_client.reset_discovery_cache()
    finra_client.reset_partitions_cache()
    monkeypatch.setattr(finra_client, "cache", FakeCache())
    monkeypatch.setattr(finra_analysis, "cache", FakeCache())
    monkeypatch.setattr(finra_client.requests, "get", _mock_get)

    rows = [{
        "symbolCode": "AAPL",
        "issueName": "Apple Inc.",
        "settlementDate": "2026-08-14",
        "currentShortPositionQuantity": 100,
    }]

    def fake_post(url, **kwargs):
        if "/chat/completions" in url:
            raise requests.Timeout("nested analysis timeout")
        if "token" in url:
            return _token_response()
        return _response(rows)

    monkeypatch.setattr(requests, "post", fake_post)

    fake = FakeOpenRouter([
        _tool_round("query_finra", {
            "dataset": "otcMarket/consolidatedShortInterest",
            "ticker": "AAPL",
        }),
        _final("ok"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": "Analyze AAPL short interest."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer == "ok"
    assert result.groundedness == "grounded"
    assert result.evidence_refs
    run = get_run(result.run_id)
    assert run["status"] == "completed"
    # 3 rows: two primary rounds (tool round + final answer) + the failed
    # nested analysis attempt, which model_call_count must include.
    assert run["model_call_count"] == 3
    (tc,) = get_tool_calls(result.run_id)
    assert tc["status"] == "completed"
    events = get_events(result.run_id)
    completed = [
        ev for ev in events
        if ev["event_type"] == "tool_completed" and ev["tool_name"] == "query_finra"
    ]
    assert completed and "deterministic_only" in completed[0]["result_summary"]
    model_calls = get_model_calls(result.run_id)
    assert len(model_calls) == 3
    assert model_calls[0]["status"] == "completed"
    failed = next(m for m in model_calls if m["status"] == "failed")
    assert failed["status"] == "failed"
    assert failed["error_type"] == "Timeout"
    assert failed["error_category"] == "timeout"
    assert failed["input_tokens"] == 0
    assert failed["estimated_cost"] == 0


def test_nested_helpers_reserve_budget(monkeypatch):
    """Nested model helpers reserve against the active budget before any
    network call; with capacity they proceed to the call."""
    budget = ExecutionBudget(
        max_rounds=8, max_tool_calls=64, max_model_calls=1,
        max_runtime=600.0, max_evidence_tokens=48000,
    )
    assert budget.reserve_model_call() is True
    token = set_current_budget(budget)
    monkeypatch.setattr(
        tools_module.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    monkeypatch.setattr(
        finra_analysis.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            tools_module._llm_complete("test", "prompt")
        with pytest.raises(BudgetExhaustedError):
            finra_analysis._post_completion("test", [{"role": "user", "content": "x"}], 10)
    finally:
        reset_current_budget(token)

    # With capacity the helpers run through to the (stubbed) network call.
    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "req_nested",
                "usage": _usage(),
                "choices": [{"message": {"content": "summary text", "finish_reason": "stop"}}],
            }

    budget2 = ExecutionBudget(
        max_rounds=8, max_tool_calls=64, max_model_calls=2,
        max_runtime=600.0, max_evidence_tokens=48000,
    )
    token2 = set_current_budget(budget2)
    monkeypatch.setattr(tools_module.requests, "post", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(finra_analysis.requests, "post", lambda *a, **k: _FakeResp())
    try:
        assert tools_module._llm_complete("test", "prompt") == "summary text"
        assert (
            finra_analysis._post_completion("test", [{"role": "user", "content": "x"}], 10)
            == "summary text"
        )
    finally:
        reset_current_budget(token2)


@pytest.mark.parametrize("disable_recorder", [False, True])
def test_budget_limits_independent_of_recorder(monkeypatch, tmp_path, disable_recorder):
    """Every budget limit behaves identically whether the recorder is healthy
    or disabled: enforcement never depends on observability."""

    def _budget_case(limits, script):
        fake = FakeOpenRouter(script)
        monkeypatch.setattr(agent, "_call_openrouter", fake)
        executed = []
        monkeypatch.setattr(
            agent, "execute_tool",
            lambda name, args, model, **kwargs: executed.append(name)
            or {"ticker": "AAPL", "source": "test"},
        )
        if disable_recorder:
            blocker = tmp_path / "file"
            blocker.write_text("x")
            monkeypatch.setenv("RUNS_DB_PATH", str(blocker / "runs.sqlite"))
        result = run_chat(
            [{"role": "user", "content": "q"}], model="test",
            context=_research_context(run_limits=limits),
            policy=TEST_POLICY, return_result=True,
        )
        return result, fake, executed

    # Model-call cap: one call + one tool, then exhausted at the loop top.
    result, fake, executed = _budget_case(
        RunLimits(max_model_calls=1),
        [_tool_round("get_fundamentals", {"ticker": "AAPL"})],
    )
    assert len(fake.calls) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    assert len(executed) == 1
    if not disable_recorder:
        # When disabled the broken RUNS_DB_PATH makes get_run return None.
        assert get_run(result.run_id)["status"] == "budget_exhausted"

    # Tool-call cap: the second tool in one round is refused before it runs.
    result, fake, executed = _budget_case(
        RunLimits(max_tool_calls=1),
        [_tool_round_multi([
            ("get_fundamentals", {"ticker": "AAPL"}),
            ("get_xbrl_facts", {"ticker": "AAPL", "concept": "Revenue"}),
        ])],
    )
    assert len(executed) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    assert len(fake.calls) == 1
    if not disable_recorder:
        assert get_run(result.run_id)["status"] == "budget_exhausted"

    # Runtime cap: no model call happens at all.
    result, fake, executed = _budget_case(
        RunLimits(max_runtime=0),
        [_final("ok")],
    )
    assert len(fake.calls) == 0
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    if not disable_recorder:
        assert get_run(result.run_id)["status"] == "budget_exhausted"

    # Evidence cap: same.
    result, fake, executed = _budget_case(
        RunLimits(max_evidence_tokens=0),
        [_final("ok")],
    )
    assert len(fake.calls) == 0
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    if not disable_recorder:
        assert get_run(result.run_id)["status"] == "budget_exhausted"

def test_evidence_budget_terminates_run(monkeypatch):
    """A tool result that would cross the evidence budget is a hard stop:
    the run ends budget_exhausted and the result never reaches the model."""
    big_result = {
        "ticker": "AAPL",
        "rows": [{"period": f"2026-Q{i}", "value": i * 1.5, "note": "x" * 20}
                 for i in range(60)],
        "source": "test",
    }
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: big_result
    )
    context = _research_context(run_limits=RunLimits(max_evidence_tokens=100))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test", context=context, policy=TEST_POLICY, return_result=True,
    )
    assert len(fake.calls) == 1  # the tool result never fed a second call
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    assert result.evidence_refs == []
    run = get_run(result.run_id)
    assert run["status"] == "budget_exhausted"
    events = get_events(result.run_id)
    assert [ev for ev in events if ev["event_type"] == "evidence_added"] == []


def test_reserve_methods_enforce_runtime(monkeypatch):
    """Reserves refuse once elapsed runtime is gone, even with call slots left."""
    budget = ExecutionBudget(
        max_rounds=8, max_tool_calls=2, max_model_calls=2,
        max_runtime=1.0, max_evidence_tokens=48000,
    )
    assert budget.reserve_model_call() is True
    assert budget.reserve_tool_call() is True
    budget._started -= 60  # pretend the budget started 60s ago
    assert budget.runtime_remaining() <= 0
    assert budget.reserve_model_call() is False
    assert budget.reserve_tool_call() is False

    # The nested-helper path surfaces the same refusal as an exception.
    token = set_current_budget(budget)
    monkeypatch.setattr(
        tools_module.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            tools_module._llm_complete("test", "prompt")
    finally:
        reset_current_budget(token)


class _RuntimeScriptedBudget(ExecutionBudget):
    """runtime_remaining yields scripted values. Read order in run_chat:
    AgentState construction, loop-top _update_budget view, loop-top check,
    main reserve, forced-answer reserve (5 reads)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._remaining = iter([1.0, 1.0, 1.0, 1.0, 0.0])

    def runtime_remaining(self):
        return next(self._remaining)


def test_forced_answer_respects_runtime_budget(monkeypatch):
    """The forced-final-answer branch refuses the extra model call when the
    runtime budget is already gone."""
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(agent, "ExecutionBudget", _RuntimeScriptedBudget)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: {"ticker": "AAPL"}
    )
    context = _research_context(run_limits=RunLimits(
        max_rounds=0, max_model_calls=2, max_runtime=1.0
    ))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test", context=context, policy=TEST_POLICY, return_result=True,
    )
    assert len(fake.calls) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    run = get_run(result.run_id)
    assert run["status"] == "budget_exhausted"

def test_tool_result_bounded_by_max_tool_result_bytes(monkeypatch):
    """max_tool_result_bytes bounds the tool message the model receives,
    not just the observability-summary truncation."""
    big = {
        "ticker": "AAPL",
        "rows": [{"period_end": f"2026-Q{i}", "value": i, "note": "x" * 30}
                 for i in range(300)],
        "source": "test",
    }
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL"}),
        _final("ok"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: big
    )
    context = _research_context(run_limits=RunLimits(max_tool_result_bytes=1024))
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test", context=context, policy=TEST_POLICY, return_result=True,
    )
# ---------------------------------------------------------------------------
# R1/R2/R3/R5/R6 hardening: malformed arguments, telemetry envelope,
# evidence records, groundedness semantics, model failures
# ---------------------------------------------------------------------------


def test_malformed_tool_arguments_rejected(monkeypatch):
    """Unparseable tool arguments are rejected before execution: no
    TOOL_STARTED, no dispatcher invocation."""
    fake = FakeOpenRouter([
        _tool_round_raw("get_short_interest_leaderboard", "{invalid"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda *a, **k: pytest.fail("execute_tool must not be called")
    )
    result = run_chat(
        [{"role": "user", "content": "Rank the leaderboard."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )

    assert result.answer.startswith("The requested data is unavailable")
    assert result.groundedness == "partial"
    assert result.evidence_refs == []
    run = get_run(result.run_id)
    assert run["status"] == "partial"
    (call,) = get_tool_calls(result.run_id)
    assert call["status"] == "failed"
    assert call["error_type"] == "invalid_tool_arguments"
    assert "{invalid" in call["arguments_json"]
    event_types = [ev["event_type"] for ev in get_events(result.run_id)]
    assert "tool_requested" in event_types
    assert "tool_failed" in event_types
    assert "tool_started" not in event_types

def test_malformed_call_consumes_tool_budget(monkeypatch):
    """Every model-requested tool call reserves a budget slot before parsing:
    with max_tool_calls=1 a valid call followed by a malformed one exhausts
    the budget (the malformed call is never parsed into a ToolCall row)."""
    fake = FakeOpenRouter([
        _tool_round_multi_raw([
            ("get_fundamentals", '{"ticker": "AAPL"}'),
            ("get_fundamentals", "{bad"),
        ]),
        _final("irrelevant"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool",
        lambda name, args, model, **kwargs: {"ticker": "AAPL", "source": "test"},
    )
    result = run_chat(
        [{"role": "user", "content": "What is AAPL EPS?"}],
        model="test",
        context=_research_context(run_limits=RunLimits(max_tool_calls=1)),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert len(fake.calls) == 1
    assert result.answer == _BUDGET_EXHAUSTED_RESPONSE
    assert get_run(result.run_id)["status"] == "budget_exhausted"
    (call,) = get_tool_calls(result.run_id)
    assert call["tool_name"] == "get_fundamentals"
    assert call["status"] == "completed"


def test_schema_invalid_tool_arguments_rejected(monkeypatch):
    """Schema validation inside the real dispatcher rejects missing required
    arguments and non-object arguments before any handler/network code."""
    # (a) Missing required ticker on get_short_interest.
    fake = FakeOpenRouter([
        _tool_round("get_short_interest", {}),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": "Short interest for the ticker."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert "Missing required argument" in result.answer
    assert "ticker" in result.answer
    (call,) = get_tool_calls(result.run_id)
    assert call["status"] == "failed"
    assert call["error_type"] == "invalid_tool_arguments"
    assert get_run(result.run_id)["status"] == "partial"

    # (b) Non-object arguments reach the dispatcher's validator.
    fake = FakeOpenRouter([
        _tool_round_raw("get_short_interest_leaderboard", "[1,2,3]"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": "Rank the leaderboard."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert "must be a JSON object" in result.answer
    (call,) = get_tool_calls(result.run_id)
    assert call["status"] == "failed"
    assert call["error_type"] == "invalid_tool_arguments"
    assert get_run(result.run_id)["status"] == "partial"


def test_tool_telemetry_envelope(monkeypatch):
    """The tool telemetry envelope: data rows (not metadata lists), returned
    count, truncation, and freshness all reach the tool_calls row."""
    payload = {
        "source": "FINRA consolidated short interest + SEC EDGAR company facts (parquet)",
        "as_of_date": "2026-08-21",
        "data_freshness": "current",
        "source_records": ["a", "b", "c"],
        "entries": [{"rank": 1, "ticker": "CCC"}, {"rank": 2, "ticker": "BBB"}],
        "returned_count": 2,
        "may_have_more": True,
        "total_records": 5,
    }
    fake = FakeOpenRouter([
        _tool_round("get_short_interest_leaderboard", {}),
        _final("ok"),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: payload
    )
    result = run_chat(
        [{"role": "user", "content": "Rank the leaderboard."}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    (tc,) = get_tool_calls(result.run_id)
    source = payload["source"]
    assert tc["as_of"] == "2026-08-21"
    assert json.loads(tc["source_names"]) == [source]
    assert json.loads(tc["source_freshness"]) == {source: "current"}
    assert result.data_freshness == {source: "current"}


def test_evidence_record_rendered_hash(monkeypatch):
    """Evidence rows persist exactly what the model received: rendered hash,
    byte/token sizes, redacted text, and the tool-call linkage."""
    payload = {
        "ticker": "AAPL",
        "eps": 6.3,
        "source": "SEC EDGAR company facts",
        "as_of": "2026-08-29",
    }
    fake = FakeOpenRouter([
        _tool_round("get_fundamentals", {"ticker": "AAPL", "metric": "eps"}),
        _final("AAPL EPS is 6.3 per the 10-Q."),
    ])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    monkeypatch.setattr(
        agent, "execute_tool", lambda name, args, model, **kwargs: payload
    )
    context = _research_context()
    result = run_chat(
        [{"role": "user", "content": "What is AAPL's diluted EPS?"}],
        model="test",
        context=context,
        policy=TEST_POLICY,
        return_result=True,
    )
    rendered = render_tool_result(
        payload, max_bytes=context.run_limits.max_tool_result_bytes
    )
    rows = get_evidence(result.run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["rendered_hash"] == hashlib.sha256(rendered.encode()).hexdigest()
    assert row["estimated_tokens"] == len(rendered) // 4
    assert row["rendered_bytes"] == len(rendered.encode("utf-8"))
    assert row["rendered_text"] == redact_text(rendered)
    (tc,) = get_tool_calls(result.run_id)
    assert row["tool_call_id"] == tc["tool_call_id"]
    assert row["evidence_id"] == result.evidence_refs[0]


def test_groundedness_unverified(monkeypatch):
    """A completed run with zero tools is unverified, not grounded."""
    fake = FakeOpenRouter([_final("ok")])
    monkeypatch.setattr(agent, "_call_openrouter", fake)
    result = run_chat(
        [{"role": "user", "content": "hi"}],
        model="test",
        context=_research_context(),
        policy=TEST_POLICY,
        return_result=True,
    )
    assert result.answer == "ok"
    assert result.groundedness == "unverified"
    assert result.evidence_refs == []
    assert get_run(result.run_id)["status"] == "completed"


def test_model_failed_event(monkeypatch):
    """A provider failure surfaces as model_requested -> model_failed ->
    run_failed, a failed run row, and a failed model_calls row."""
    def _boom(*a, **k):
        raise requests.Timeout("boom")

    monkeypatch.setattr(agent, "_call_openrouter", _boom)
    with pytest.raises(requests.Timeout):
        run_chat(
            [{"role": "user", "content": "hi"}],
            model="test",
            context=_research_context(),
            policy=TEST_POLICY,
            return_result=True,
        )
    run = list_runs()[0]
    assert run["status"] == "failed"
    events = get_events(run["run_id"])
    types = [ev["event_type"] for ev in events]
    assert (
        types.index("model_requested")
        < types.index("model_failed")
        < types.index("run_failed")
    )
    failed = next(ev for ev in events if ev["event_type"] == "model_failed")
    assert failed["model"] == "test"
    assert failed["duration_ms"] is not None and failed["duration_ms"] >= 0
    assert json.loads(failed["metadata"])["error_category"] == "timeout"
    assert json.loads(failed["metadata"])["error_type"] == "Timeout"
    (call,) = get_model_calls(run["run_id"])
    assert call["status"] == "failed"
    assert call["error_type"] == "Timeout"
    assert call["error_category"] == "timeout"
    assert call["input_tokens"] == 0
    assert call["estimated_cost"] == 0
    assert run["model_call_count"] == 1
