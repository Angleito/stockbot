"""Observability: redaction units and recorder-independent budget reserves.

RUNS_DB_PATH is isolated per session by the root conftest fixture.
"""

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from app import finra_analysis
from app.redact import redact_json, redact_text, redact_value
from app.runtime import BudgetExhaustedError, ExecutionBudget
from app.security import quarantine_reader
from app.storage.runs import (
    RunRecorder,
    finalize_failed_run,
    reset_current_budget,
    reset_current_recorder,
    set_current_budget,
    set_current_recorder,
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
        quarantine_reader.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    monkeypatch.setattr(
        finra_analysis.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            quarantine_reader._llm_complete("test", "prompt")
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
    monkeypatch.setattr(quarantine_reader.requests, "post", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(finra_analysis.requests, "post", lambda *a, **k: _FakeResp())
    try:
        assert quarantine_reader._llm_complete("test", "prompt") == "summary text"
        assert (
            finra_analysis._post_completion("test", [{"role": "user", "content": "x"}], 10)
            == "summary text"
        )
    finally:
        reset_current_budget(token2)


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
        quarantine_reader.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            quarantine_reader._llm_complete("test", "prompt")
    finally:
        reset_current_budget(token)

def _recorder(run_id, **overrides):
    kwargs = {
        "request_id": "req", "question": "q", "as_of": None, "model": "t",
        "provider": "p", "model_parameters": {}, "agent_version": "0",
        "prompt_version": "0", "tool_registry_version": "t", "git_sha": "g",
    }
    kwargs.update(overrides)
    return RunRecorder(run_id=run_id, **kwargs)


def test_concurrent_tool_reservations_capped():
    """8 threads racing for 20 tool slots hand out exactly 20."""
    budget = ExecutionBudget(
        max_rounds=8, max_tool_calls=20, max_model_calls=8,
        max_runtime=600.0, max_evidence_tokens=48000,
    )
    granted = []
    lock = threading.Lock()

    def worker():
        local = [budget.reserve_tool_call() for _ in range(10)]
        with lock:
            granted.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 20
    assert budget.tool_calls == 20


def test_concurrent_recorder_writes_unique_ids(tmp_path, monkeypatch):
    """8 threads x 10 tool+evidence rows keep every row with a unique ID."""
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    recorder = _recorder("run-conc-1")
    with recorder:
        def worker():
            for _ in range(10):
                seq = recorder.next_tool_seq()
                tc_id = f"{recorder.run_id}:tc:{seq}"
                now = datetime.now(timezone.utc).isoformat()
                recorder.record_tool_call(
                    tool_call_id=tc_id, round=0, tool_name="search_tools",
                    arguments_json="{}", started_at=now, completed_at=now,
                    status="completed", result_row_count=0, returned_count=0,
                    truncated=False, result_bytes=2, result_hash="h",
                    source_names="[]", source_freshness="{}",
                    as_of=None, error_type=None, error_message=None,
                )
                recorder.record_evidence(
                    evidence_id=f"{recorder.run_id}:evid:{recorder.next_evidence_seq():04d}",
                    run_id=recorder.run_id, tool_call_id=tc_id, round=0,
                    tool_name="search_tools", rendered_hash="h", rendered_bytes=1,
                    estimated_tokens=1, source_names="[]", source_freshness="{}",
                    as_of=None, rendered_text="t",
                )

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    conn = sqlite3.connect(str(tmp_path / "runs.sqlite"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(DISTINCT tool_call_id) FROM tool_calls").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 80
        assert conn.execute("SELECT COUNT(DISTINCT evidence_id) FROM evidence").fetchone()[0] == 80
    finally:
        conn.close()


def test_tool_call_telemetry_columns_migrated_and_recorded(tmp_path, monkeypatch):
    """Pre-telemetry DBs gain the columns on open; queue/handler/cache persist."""
    path = tmp_path / "runs.sqlite"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,"
        " started_at TEXT NOT NULL, question TEXT NOT NULL, model_provider TEXT,"
        " model_name TEXT, model_parameters TEXT, agent_version TEXT,"
        " prompt_version TEXT, tool_registry_version TEXT, git_sha TEXT, as_of TEXT)"
    )
    conn.execute(
        "CREATE TABLE tool_calls (tool_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,"
        " round INTEGER, tool_name TEXT NOT NULL, tool_version TEXT, arguments_json TEXT,"
        " started_at TEXT NOT NULL, completed_at TEXT, duration_ms REAL, status TEXT,"
        " result_row_count INTEGER, returned_count INTEGER, truncated INTEGER,"
        " result_bytes INTEGER, result_hash TEXT, source_names TEXT, source_freshness TEXT,"
        " as_of TEXT, error_type TEXT, error_message TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("RUNS_DB_PATH", str(path))
    with _recorder("run-tel-1") as recorder:
        now = datetime.now(timezone.utc).isoformat()
        recorder.record_tool_call(
            tool_call_id="run-tel-1:tc:1", round=0, tool_name="search_tools",
            arguments_json="{}", started_at=now, completed_at=now,
            status="completed", result_row_count=0, returned_count=0,
            truncated=False, result_bytes=2, result_hash="h",
            source_names="[]", source_freshness="{}", as_of=None,
            error_type=None, error_message=None, protocol_id="proto-1",
            bridge_queue_ms=3.5, handler_ms=12.25, cache_hit=True,
            cache_type="stockbot_parsed",
        )
    # Reopen proves the migration is idempotent.
    with _recorder("run-tel-2"):
        pass
    conn = sqlite3.connect(str(path))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
        assert {"protocol_id", "bridge_queue_ms", "handler_ms", "cache_hit", "cache_type"} <= cols
        row = conn.execute(
            "SELECT protocol_id, bridge_queue_ms, handler_ms, cache_hit, cache_type"
            " FROM tool_calls WHERE tool_call_id = 'run-tel-1:tc:1'"
        ).fetchone()
        assert row == ("proto-1", 3.5, 12.25, 1, "stockbot_parsed")
    finally:
        conn.close()


def test_execute_pi_tool_ids_and_telemetry(tmp_path, monkeypatch):
    """Pi-supplied call IDs become run-scoped rows; handler/queue/cache persist."""
    import app.pi_gateway as gateway

    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    monkeypatch.setattr(
        gateway, "execute_tool",
        lambda *a, **k: {"ok": True, "cache_hit": True, "cache_type": "unit_test"},
    )
    session = gateway.PiSessionContext(session_id="s1")
    with _recorder("run-pi-1") as recorder:
        token = set_current_recorder(recorder)
        try:
            fallback = gateway.execute_pi_tool("search_tools", {}, session)
            assert fallback.get("content")
            correlated = gateway.execute_pi_tool(
                "search_tools", {}, session, tool_call_id="call-9",
                protocol_id="proto-9", bridge_queue_ms=7.5,
            )
            assert correlated.get("content")
            assert "proto-9" not in correlated["content"]
        finally:
            reset_current_recorder(token)
    conn = sqlite3.connect(str(tmp_path / "runs.sqlite"))
    try:
        rows = {
            row[0]: row[1:]
            for row in conn.execute(
                "SELECT tool_call_id, protocol_id, bridge_queue_ms, handler_ms,"
                " cache_hit, cache_type FROM tool_calls"
            )
        }
        seq_id, pi_id = "run-pi-1:tc:1", "run-pi-1:tc:call-9"
        assert rows[seq_id][0] is None
        assert rows[seq_id][1] == 0.0
        assert rows[seq_id][2] is not None and rows[seq_id][2] >= 0.0
        assert tuple(rows[seq_id][3:]) == (1, "unit_test")
        assert rows[pi_id][:3] == ("proto-9", 7.5, rows[pi_id][2])
        assert rows[pi_id][2] is not None and rows[pi_id][2] >= 0.0
        assert tuple(rows[pi_id][3:]) == (1, "unit_test")
    finally:
        conn.close()


def test_finalize_failed_run_reconstructs_orphan_summary(tmp_path, monkeypatch):
    """Orphaned runs terminalize as failed with aggregates rebuilt from child rows."""
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    now = datetime.now(timezone.utc).isoformat()
    with _recorder("run-orphan-pop") as recorder:
        recorder.record_model_call(round=2, provider="p", model="t", started_at=now,
            completed_at=now, usage={"prompt_tokens": 100, "completion_tokens": 50,
            "total_tokens": 999, "cost": 0.25})
        recorder.record_tool_call(tool_call_id="tc-1", round=3, tool_name="t",
            arguments_json="{}", started_at=now, completed_at=now, status="completed",
            result_row_count=1, returned_count=1, truncated=False, result_bytes=10,
            result_hash="h", source_names="", source_freshness="", as_of=None,
            error_type=None, error_message=None)
        recorder.record_event("turn_end", round=1)
    with _recorder("run-orphan-empty"):
        pass
    assert finalize_failed_run("run-orphan-pop", error_type="tool_timeout",
        error_message="boom") is True
    assert finalize_failed_run("run-orphan-empty", error_type="tool_timeout",
        error_message="boom") is True
    conn = sqlite3.connect(str(tmp_path / "runs.sqlite"))
    try:
        cols = ("status, completed_at, duration_ms, round_count, model_call_count,"
            " tool_call_count, input_tokens, output_tokens, total_tokens,"
            " estimated_model_cost, estimated_total_cost, error_type")
        pop = conn.execute(
            f"SELECT {cols} FROM agent_runs WHERE run_id = ?", ("run-orphan-pop",)).fetchone()
        assert pop[0] == "failed"
        assert pop[1] is not None and pop[2] is not None
        assert tuple(pop[3:]) == (3, 1, 1, 100, 50, 150, 0.25, 0.25, "tool_timeout")
        empty = conn.execute(
            f"SELECT {cols} FROM agent_runs WHERE run_id = ?", ("run-orphan-empty",)).fetchone()
        assert empty[0] == "failed"
        assert tuple(empty[3:]) == (0, 0, 0, 0, 0, 0, 0.0, 0.0, "tool_timeout")
    finally:
        conn.close()
