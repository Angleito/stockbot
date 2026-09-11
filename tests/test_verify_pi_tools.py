"""Offline unit tests for scripts/verify_pi_tools.py (fakes only, no Pi/network)."""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import scripts.verify_pi_tools as v
from app.config import get_data_root
from app.storage.runs import _SCHEMA

MODEL = "test-model"


def _db(path: Path, tool: str = "get_fundamentals", model: str = MODEL, status: str = "completed", tool_error: str | None = None, tool_message: str | None = None, event: str = "completed", other_tool: str | None = None, rejected_other: str | None = None, extra_tool: str | None = None, extra_error: str | None = None, extra_message: str | None = None, discovery: str | None = "search_tools", disc_at: str = "2026-01-01T00:00:00+00:00", inner_at: str = "2026-01-01T00:00:01+00:00", via_call_tool: bool = True) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?,?,?)",
        (disc_at, "q", status),
    )
    target_name = other_tool or tool
    # Discovery row (browse OR search) strictly before inner when routing-clean.
    if discovery is not None and discovery != target_name:
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc0','r1',?,?,NULL)",
            (discovery, disc_at),
        )
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e0','r1',0,'tool_completed',?,?)",
            (disc_at, discovery),
        )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type, error_message) VALUES ('tc1','r1',?,?,?,?)",
        (target_name, inner_at, tool_error, tool_message),
    )
    # search_tools itself executes directly; gateway forbids dispatching discovery via call_tool.
    effective_via = via_call_tool and target_name != "search_tools" and event not in ("failed-only",)
    if effective_via and event in ("completed", "both", "harness-rejected"):
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name, arguments) VALUES ('e1','r1',1,'tool_started',?,'call_tool',?)",
            (inner_at, json.dumps({"name": target_name, "arguments": {"ticker": "AAPL"}})),
        )
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e2','r1',2,'tool_completed',?,?)",
            (inner_at, "call_tool"),
        )
    elif event in ("completed", "both", "harness-rejected"):
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e1','r1',1,'tool_completed',?,?)",
            (inner_at, tool if other_tool is None else other_tool if False else tool if event == 'both' else target_name),
        )
    if event == "both":
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e3','r1',3,'tool_failed',?,?)",
            (inner_at, target_name),
        )
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc2','r1',?,?,'tool_error')",
            (target_name, inner_at),
        )
    if event == "harness-rejected":
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e4','r1',4,'tool_failed',?,?)",
            (inner_at, target_name),
        )
    if event == "failed-only":
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e1','r1',1,'tool_failed',?,?)",
            (inner_at, target_name),
        )
    if rejected_other is not None:
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e9','r1',9,'tool_failed',?,?)",
            (inner_at, rejected_other),
        )
    if extra_tool is not None:
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type, error_message) VALUES ('tc9','r1',?,?,?,?)",
            (extra_tool, inner_at, extra_error, extra_message),
        )
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e9','r1',9,?,?,?)",
            ("tool_completed" if extra_error is None else "tool_failed", inner_at, extra_tool),
        )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
        (model, disc_at),
    )
    conn.commit()
    conn.close()
    return path


def _ok(
    tmp_path: Path,
    tool: str = "get_fundamentals",
    model: str = MODEL,
    status: str = "completed",
    tool_error: str | None = None,
    tool_message: str | None = None,
    event: str = "completed",
    other_tool: str | None = None,
    rejected_other: str | None = None,
    extra_tool: str | None = None,
    extra_error: str | None = None,
    extra_message: str | None = None,
    discovery: str | None = "search_tools",
    disc_at: str = "2026-01-01T00:00:00+00:00",
    inner_at: str = "2026-01-01T00:00:01+00:00",
    via_call_tool: bool = True,
) -> Path:
    p = tmp_path / "runs.sqlite"
    _db(p, tool=tool, model=model, status=status, tool_error=tool_error, tool_message=tool_message, event=event, other_tool=other_tool, rejected_other=rejected_other, extra_tool=extra_tool, extra_error=extra_error, extra_message=extra_message, discovery=discovery, disc_at=disc_at, inner_at=inner_at, via_call_tool=via_call_tool)
    return p


def test_job_expansion_n_times_r():
    jobs = v.expand_jobs(["a", "b"], 3)
    assert len(jobs) == 6
    assert jobs.count(("a", 1)) == 1 and jobs.count(("b", 3)) == 1


def test_new_discovered_tool_auto_creates_jobs():
    assert v.expand_jobs(["brand_new_tool"], 3) == [("brand_new_tool", 1), ("brand_new_tool", 2), ("brand_new_tool", 3)]


def test_missing_dispatcher_fails_pre_pi(monkeypatch: pytest.MonkeyPatch):
    import scripts.verify_tool_registry as reg

    real = reg.get_registry_sets()
    monkeypatch.setattr(reg, "get_registry_sets", lambda: {**real, "handlers": set()})
    monkeypatch.setattr(v, "get_registry_sets", lambda: {**real, "handlers": set()})
    assert v.check_pre_pi(["get_fundamentals"]) is not None

def test_missing_fixture_fails_closed():
    try:
        v.resolve_arguments("some_new_tool", {"some_new_tool": {"required": ["ticker"]}})
    except LookupError as exc:
        assert "missing verification fixture" in str(exc)
    else:
        raise AssertionError("expected LookupError")


def test_nonzero_exit_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 1, False, attempt=1)
    assert not ok


def test_timeout_fails(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 0, True, attempt=1)
    assert not ok
    assert reason.startswith("transient: ")


def test_timeout_with_wrong_tool_still_routing_fails(tmp_path: Path):
    ok, reason = v.evaluate_attempt(
        _ok(tmp_path, extra_tool="get_xbrl_facts", extra_error="tool_error"),
        "get_fundamentals", 0, True, attempt=1,
    )
    assert not ok
    assert "routing failed" in reason


def test_target_rate_limited_returns_transient(tmp_path: Path):
    ok, reason = v.evaluate_attempt(
        _ok(tmp_path, tool_error="rate_limited"), "get_fundamentals", 0, False, attempt=1,
    )
    assert not ok
    assert reason.startswith("transient: ")


def test_target_quota_exhausted_returns_transient(tmp_path: Path):
    ok, reason = v.evaluate_attempt(
        _ok(tmp_path, tool_error="quota_exhausted"), "get_fundamentals", 0, False, attempt=1,
    )
    assert not ok
    assert reason.startswith("transient: ")


def test_target_timeout_message_returns_transient(tmp_path: Path):
    ok, reason = v.evaluate_attempt(
        _ok(tmp_path, tool_error="tool_error", tool_message="Exa search timed out"),
        "get_fundamentals", 0, False, attempt=1,
    )
    assert not ok
    assert reason.startswith("transient: ")


def test_target_mixed_errors_plain_fail(tmp_path: Path):
    p = tmp_path / "runs.sqlite"
    _db(p, tool="get_fundamentals", tool_error="rate_limited")
    conn = sqlite3.connect(str(p))
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc2','r1','get_fundamentals','2026-01-01T00:00:01+00:00','tool_error')"
    )
    conn.commit()
    conn.close()
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert not reason.startswith("transient: ")




def test_zero_call_absence_plain_fail(tmp_path: Path):
    p = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1','2026-01-01T00:00:00+00:00','q','completed')")
    conn.execute("INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?, '2026-01-01T00:00:00+00:00')", (MODEL,))
    conn.commit()
    conn.close()
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False)
    assert not ok
    assert not reason.startswith("transient: ")


def test_required_tool_absent_fails(tmp_path: Path):
    p = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1','2026-01-01T00:00:00+00:00','q','completed')")
    conn.execute("INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?, '2026-01-01T00:00:00+00:00')", (MODEL,))
    conn.commit()
    conn.close()
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False)
    assert not ok


def test_wrong_tool_only_fails(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, other_tool="search_web", event="completed"), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "routing failed" in reason


def test_error_envelope_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, tool_error="tool_error"), "get_fundamentals", 0, False, attempt=1)
    assert not ok


def test_valid_empty_passes(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_empty_model_telemetry_fails(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, model=""), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "no model telemetry" in reason


def test_tool_failed_event_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, event="both"), "get_fundamentals", 0, False, attempt=1)
    assert not ok


def test_harness_rejection_fails_attempt_1(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="harness-rejected"), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "harness-rejected" in reason


def test_harness_rejection_without_execution_fails_attempt_3(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="harness-rejected"), "get_fundamentals", 0, False, attempt=3)
    assert not ok
    assert "routing failed" in reason


def test_rejected_wrong_tool_fails_attempt_1(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="completed", rejected_other="get_xbrl_facts"), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "harness-rejected" in reason


def test_rejected_wrong_tool_fails_attempt_3(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="completed", rejected_other="get_xbrl_facts"), "get_fundamentals", 0, False, attempt=3)
    assert not ok
    assert "routing failed" in reason


def test_dispatched_wrong_tool_error_fails_attempt_1(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="completed", extra_tool="get_xbrl_facts", extra_error="tool_error"), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "unexpected" in reason


def test_dispatched_wrong_tool_clean_stray_fails_attempt_1(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="completed", extra_tool="get_xbrl_facts"), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "routing failed" in reason




def test_rejected_prereq_fails_attempt_1(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, tool="list_sec_filings", rejected_other="search_sec_filings"), "list_sec_filings", 0, False, attempt=1)
    assert not ok
    assert "harness-rejected" in reason




def test_errored_prereq_fails_attempt_1(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, tool="list_sec_filings", extra_tool="search_sec_filings", extra_error="tool_error"), "list_sec_filings", 0, False, attempt=1)
    assert not ok


def test_prereq_chains_are_documented_in_descriptions():
    from app.tools import TOOLS
    from scripts.verify_tool_registry import tool_schema_function, tool_schema_name
    descriptions = {tool_schema_name(raw): str(tool_schema_function(raw).get("description", "")) for raw in TOOLS}
    assert set(v.PREREQ_CHAINS) <= set(descriptions)
    for target, prereqs in v.PREREQ_CHAINS.items():
        for edge in prereqs:
            assert edge in descriptions, f"unknown prereq tool: {edge}"
            assert edge.lower() in descriptions[target].lower(), f"{edge} not named in {target} description"


def test_dispatched_wrong_tool_fails_attempt_3(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, event="completed", extra_tool="get_xbrl_facts", extra_error="tool_error"), "get_fundamentals", 0, False, attempt=3)
    assert not ok
    assert "routing failed" in reason


def test_failed_builtin_ignored_attempt_1(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, event="completed", rejected_other="read"), "get_fundamentals", 0, False, attempt=1)
    assert ok



def test_two_of_three_is_not_pass():
    trio = [v.AttemptResult("t", 1, True, "pass", 0, "", 0.0), v.AttemptResult("t", 2, True, "pass", 0, "", 0.0), v.AttemptResult("t", 3, False, "routing failed: x", 1, "", 0.0)]
    assert not v.tool_passes(trio)
    assert v.tool_passes([v.AttemptResult("t", n, True, "pass", 0, "", 0.0) for n in (1, 2, 3)])


def test_doctor_describe_skew_fails():
    desc = {"tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}
    doc = {"bridge_ok": True, "tool_count": 1, "tool_names": ["a"]}
    assert v.check_discovery(desc, doc) is not None

def test_any_pi_model_id_accepted(tmp_path: Path):
    ok, _ = v.evaluate_attempt(
        _ok(tmp_path, model="muse-spark-1.3-contributor"),
        "get_fundamentals", 0, False, attempt=1,
    )
    assert ok


def test_completed_override_passes_despite_kill(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", -9, False, completed_override=True, attempt=1)
    assert ok


def test_completed_override_still_needs_db_evidence(tmp_path: Path):
    p = tmp_path / "missing.sqlite"
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", -9, False, completed_override=True)
    assert not ok


def test_db_terminal(tmp_path: Path):
    assert v.db_terminal(_ok(tmp_path))
    assert not v.db_terminal(tmp_path / "missing.sqlite")


def test_durable_capture_pure_preset_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    monkeypatch.setenv("STOCKBOT_DATA_DIR", str(durable))
    before = os.environ.get("STOCKBOT_DATA_DIR")
    captured = get_data_root()
    assert captured == durable
    assert os.environ.get("STOCKBOT_DATA_DIR") == before
    assert not hasattr(v, "setup_isolated_store")


def test_durable_capture_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STOCKBOT_DATA_DIR", "~/.stockbot-data")
    assert get_data_root() == Path.home() / ".stockbot-data"


def test_per_attempt_env_carries_distinct_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    durable = tmp_path / "durable"
    durable.mkdir()
    monkeypatch.setenv("STOCKBOT_DATA_DIR", str(durable))
    captured_envs: list[dict[str, str]] = []
    captured_cmds: list[list[str]] = []

    class _FakeProc:
        pid = 999999
        def poll(self) -> int | None:
            return 0
        def wait(self, timeout: float | None = None) -> int:
            return 0

    def _fake_popen(*args: object, **kwargs: object) -> _FakeProc:
        first = args[0]
        assert isinstance(first, (list, tuple))
        cmd: list[str] = []
        for c in first:
            assert isinstance(c, str)
            cmd.append(c)
        captured_cmds.append(cmd)
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured_envs.append(dict(env))
        return _FakeProc()

    monkeypatch.setattr(v.subprocess, "Popen", _fake_popen)
    batch = tmp_path / "batch"
    db1, store1 = v.attempt_dirs(batch, "get_fundamentals", 1)
    db2, store2 = v.attempt_dirs(batch, "get_fundamentals", 2)
    v.run_pi("prompt", db1, tmp_path, store1)
    v.run_pi("prompt", db2, tmp_path, store2)
    assert len(captured_envs) == 2
    assert captured_envs[0]["STOCKBOT_DATA_DIR"] == str(store1.resolve())
    assert captured_envs[1]["STOCKBOT_DATA_DIR"] == str(store2.resolve())
    assert captured_envs[0]["STOCKBOT_DATA_DIR"] != captured_envs[1]["STOCKBOT_DATA_DIR"]
    assert os.environ.get("STOCKBOT_DATA_DIR") == str(durable)
    assert len(captured_cmds) == 2
    for cmd in captured_cmds:

        assert "--no-builtin-tools" in cmd
        assert "--no-extensions" in cmd
        assert "--no-skills" in cmd
        assert "--no-prompt-templates" in cmd
        assert "--no-context-files" in cmd
        assert "--exclude-tools" not in cmd
        assert cmd.index("--no-context-files") < cmd.index("--")
        assert cmd[-2] == "--"
        assert cmd[-1] == "prompt"
        assert cmd[:8] == ["pi", "-p", "--no-session", "--no-builtin-tools", "--no-extensions", "--no-skills",
                          "--no-prompt-templates", "--no-context-files"]
        assert cmd[8:10] == ["--extension", v.EXTENSION]



def test_get_concurrency_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PI_VERIFY_CONCURRENCY", raising=False)
    assert v.get_concurrency() == v.DEFAULT_CONCURRENCY == 6
    monkeypatch.setenv("PI_VERIFY_CONCURRENCY", "1")
    assert v.get_concurrency() == 1
    monkeypatch.setenv("PI_VERIFY_CONCURRENCY", "3")
    assert v.get_concurrency() == 3


def test_get_concurrency_rejects_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for bad in ("0", "-2", "abc", ""):
        monkeypatch.setenv("PI_VERIFY_CONCURRENCY", bad)
        try:
            v.get_concurrency()
        except ValueError as exc:
            assert "PI_VERIFY_CONCURRENCY must be an integer >= 1" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def test_run_matrix_bounds_concurrency() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(tool: str, attempt: int) -> v.AttemptResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return v.AttemptResult(tool, attempt, True, "pass", 0, "", 0.0)

    jobs = [(f"tool-{i}", 1) for i in range(12)]
    results = v.run_matrix(jobs, worker, 6)
    assert len(results) == 12
    assert peak <= 6
    assert peak >= 2


def test_run_matrix_completes_all_jobs() -> None:
    tools = [f"t{i}" for i in range(5)]
    jobs = v.expand_jobs(tools, 3)

    def worker(tool: str, attempt: int) -> v.AttemptResult:
        return v.AttemptResult(tool, attempt, True, "pass", 0, "", 0.0)

    results = v.run_matrix(jobs, worker, 6)
    assert len(results) == 15
    assert {(r.tool, r.attempt) for r in results} == set(jobs)


def test_run_matrix_failure_does_not_cancel_siblings() -> None:
    jobs = v.expand_jobs(["a", "b"], 3)
    ran: list[tuple[str, int]] = []
    ran_lock = threading.Lock()

    def worker(tool: str, attempt: int) -> v.AttemptResult:
        with ran_lock:
            ran.append((tool, attempt))
        if (tool, attempt) == ("a", 1):
            return v.AttemptResult(tool, attempt, False, "boom", 1, "", 0.0)
        return v.AttemptResult(tool, attempt, True, "pass", 0, "", 0.0)

    results = v.run_matrix(jobs, worker, 6)
    assert len(results) == 6
    assert sorted(ran) == sorted(jobs)
    assert sum(1 for r in results if not r.ok) == 1


def test_two_pass_one_fail_is_tool_failure() -> None:
    trio = [v.AttemptResult("t", 1, True, "pass", 0, "", 0.0), v.AttemptResult("t", 2, True, "pass", 0, "", 0.0), v.AttemptResult("t", 3, False, "x", 1, "", 0.0)]
    assert not v.tool_passes(trio)
    assert v.tool_passes([v.AttemptResult("t", n, True, "pass", 0, "", 0.0) for n in (1, 2, 3)])


def test_infra_only_still_fails_but_reports_infra() -> None:
    assert v.is_infra_failure("ratelimit 429 quota exceeded")
    assert v.is_infra_failure("pi timeout before terminal state")
    assert not v.is_infra_failure("Overloaded 503 try again")
    assert not v.is_infra_failure("routing failed: unexpected research tool call(s): foo")
    assert not v.is_infra_failure("target 'x' absent (ok)")
    infra_trio = [v.AttemptResult("t", n, False, "transient: pi timeout before terminal state", 124, "", 0.0) for n in (1, 2, 3)]
    assert not v.tool_passes(infra_trio)


def test_routing_reasons_win_over_mixed_infra_keywords() -> None:
    assert v.is_routing_failure("routing failed: unexpected research tool call(s): foo (timed out after 30s)")
    assert not v.is_infra_failure("routing failed: unexpected research tool call(s): foo (timed out after 30s)")
    assert v.is_routing_failure("target 'get_x' absent (429 Too Many Requests)")
    assert not v.is_infra_failure("target 'get_x' absent (429 Too Many Requests)")
    assert not v.is_routing_failure("pi timeout before terminal state")
    assert not v.is_routing_failure("ratelimit 429 quota exceeded")
    assert v.is_infra_failure("ratelimit 429 quota exceeded")


@pytest.mark.parametrize("stderr", ["Error 429 Too Many Requests", "model request failed: 429 Too Many Requests"])
def test_stderr_429_does_not_reclassify_routing_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    def rate_limited(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        return (1, False, "", stderr, False)

    monkeypatch.setattr(v, "run_pi", rate_limited)
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r = v.run_verification_attempt("get_fundamentals", 1, {"ticker": "AAPL"}, batch, tmp_path, durable, 3)
    assert not r.ok
    assert not r.model_config_failed


def test_timeout_still_fails_gate_but_flags_infra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        return (0, True, "", "", False)

    monkeypatch.setattr(v, "run_pi", hung)
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r = v.run_verification_attempt("get_fundamentals", 1, {"ticker": "AAPL"}, batch, tmp_path, durable, 3)
    assert not r.ok
    assert r.model_config_failed
    assert v.is_infra_failure(r.reason) and not v.is_routing_failure(r.reason)


def test_run_matrix_returns_sorted_order() -> None:
    def worker(tool: str, attempt: int) -> v.AttemptResult:
        time.sleep(0.03 * (4 - attempt))
        return v.AttemptResult(tool, attempt, True, "pass", 0, "", 0.0)

    jobs = [("solo", 1), ("solo", 2), ("solo", 3)]
    results = v.run_matrix(jobs, worker, 3)
    assert [(r.tool, r.attempt) for r in results] == [("solo", 1), ("solo", 2), ("solo", 3)]


def test_attempt_dirs_isolate_db_and_store(tmp_path: Path) -> None:
    db1, store1 = v.attempt_dirs(tmp_path, "get_fundamentals", 1)
    db2, store2 = v.attempt_dirs(tmp_path, "get_fundamentals", 2)
    assert str(db1) != str(db2)
    assert str(store1) != str(store2)
    assert db1.parent != db2.parent
    assert db1.name == db2.name == "runs.sqlite"
    assert store1.parent == db1.parent and store2.parent == db2.parent


def test_verification_attempt_isolates_thesis_per_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stores: list[Path] = []
    prompts: list[str] = []
    ids = iter(["thesis-1", "thesis-2"])

    def fake_fixture(store: Path) -> str:
        stores.append(store)
        return next(ids)

    def fake_run_pi(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        assert stockbot_store is not None
        prompts.append(prompt)
        return (0, False, "", "", True)

    def fake_eval(db_path: Path, tool: str, code: int, timed_out: bool, *, completed_override: bool = False, attempt: int = 3) -> tuple[bool, str]:
        return (True, "pass")

    monkeypatch.setattr(v, "ensure_thesis_fixture", fake_fixture)
    monkeypatch.setattr(v, "run_pi", fake_run_pi)
    monkeypatch.setattr(v, "evaluate_attempt", fake_eval)
    base: dict[str, object] = {"id": v.THESIS_ID_PLACEHOLDER}
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r1 = v.run_verification_attempt("thesis_show", 1, base, batch, tmp_path, durable, 3)
    r2 = v.run_verification_attempt("thesis_show", 2, base, batch, tmp_path, durable, 3)
    assert r1.ok and r2.ok
    assert len(stores) == 2 and stores[0] != stores[1]
    assert "thesis-1" in prompts[0] and "thesis-2" in prompts[1]
    assert base == {"id": v.THESIS_ID_PLACEHOLDER}


def test_run_matrix_survives_worker_crash() -> None:
    jobs = [("a", 1), ("a", 2), ("a", 3)]

    def worker(tool: str, attempt: int) -> v.AttemptResult:
        if attempt == 2:
            raise RuntimeError("boom")
        return v.AttemptResult(tool, attempt, True, "pass", 0, "", 0.0)

    results = v.run_matrix(jobs, worker, 3)
    assert len(results) == 3
    by_attempt = {r.attempt: r for r in results}
    assert not by_attempt[2].ok and "boom" in by_attempt[2].reason
    assert by_attempt[1].ok and by_attempt[3].ok


def test_verification_attempt_crash_is_failed_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        raise RuntimeError("pi exploded")

    monkeypatch.setattr(v, "run_pi", boom)
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r = v.run_verification_attempt("get_fundamentals", 1, {"ticker": "AAPL"}, batch, tmp_path, durable, 3)
    assert not r.ok and "pi exploded" in r.reason and r.exit == 124


def test_verification_attempt_retries_transient_then_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    del v.TRANSIENT_RETRIES[:]

    def fake_run_pi(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        calls.append(db_path)
        if len(calls) == 1:
            _db(db_path, tool="get_fundamentals", tool_error="rate_limited")
        else:
            _db(db_path, tool="get_fundamentals")
        return (0, False, "", "", False)

    monkeypatch.setattr(v, "run_pi", fake_run_pi)
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r = v.run_verification_attempt("get_fundamentals", 1, {"ticker": "AAPL"}, batch, tmp_path, durable, 3)
    assert r.ok
    assert len(calls) == 2
    assert calls[0].parent.name == "attempt-1"
    assert calls[1].parent.name == "attempt-1-retry-1"
    assert len(v.TRANSIENT_RETRIES) == 1
    del v.TRANSIENT_RETRIES[:]


def test_verification_attempt_cap_exhaustion_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    del v.TRANSIENT_RETRIES[:]

    def fake_run_pi(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
        calls.append(db_path)
        _db(db_path, tool="get_fundamentals", tool_error="rate_limited")
        return (0, False, "", "", False)

    monkeypatch.setattr(v, "run_pi", fake_run_pi)
    batch = tmp_path / "batch"
    durable = tmp_path / "durable"
    durable.mkdir()
    r = v.run_verification_attempt("get_fundamentals", 1, {"ticker": "AAPL"}, batch, tmp_path, durable, 3)
    assert not r.ok
    assert "transient budget exhausted" in r.reason
    assert len(calls) == v.TRANSIENT_RETRY_CAP + 1
    assert len(v.TRANSIENT_RETRIES) == v.TRANSIENT_RETRY_CAP + 1
    del v.TRANSIENT_RETRIES[:]


def test_expand_jobs_interleaves_repetitions() -> None:
    assert v.expand_jobs(["a", "b", "c"], 3) == [
        ("a", 1),
        ("b", 1),
        ("c", 1),
        ("a", 2),
        ("b", 2),
        ("c", 2),
        ("a", 3),
        ("b", 3),
        ("c", 3),
    ]


def test_remove_successful_attempt_dirs_prunes_empty_tool_dir(tmp_path: Path) -> None:
    batch = tmp_path / "batch"
    recs: list[dict[str, object]] = []
    for tool in ("tool-ok", "tool-bad"):
        for attempt in (1, 2, 3):
            attempt_dir = batch / tool / f"attempt-{attempt}"
            (attempt_dir / "store").mkdir(parents=True)
            db = attempt_dir / "runs.sqlite"
            db.write_text("x")
            (attempt_dir / "store" / "seed.txt").write_text("y")
            (attempt_dir / f"attempt-{attempt}.pi.log").write_text("log")
            (attempt_dir / f"attempt-{attempt}.stderr.log").write_text("err")
            if tool == "tool-ok":
                recs.append({"ok": True, "db": str(db)})
    recs.append({"ok": True, "db": ""})
    v.remove_successful_attempt_dirs(batch, "tool-ok", recs)
    assert not (batch / "tool-ok").exists()
    for attempt in (1, 2, 3):
        attempt_dir = batch / "tool-bad" / f"attempt-{attempt}"
        assert (attempt_dir / "runs.sqlite").is_file()
        assert (attempt_dir / "store" / "seed.txt").is_file()
        assert (attempt_dir / f"attempt-{attempt}.pi.log").is_file()
        assert (attempt_dir / f"attempt-{attempt}.stderr.log").is_file()


def test_tool_success_via_call_tool_dispatch(tmp_path: Path) -> None:
    """Inner success + outer call_tool lifecycle counts (no inner event row)."""
    p = tmp_path / "runs.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    disc = "2026-01-01T00:00:00+00:00"
    inner = "2026-01-01T00:00:01+00:00"
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?,?,?)",
        (disc, "q", "completed"),
    )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc1','r1','get_short_interest',?,NULL)",
        (inner,),
    )
    conn.execute(
        "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name, arguments) VALUES ('e1','r1',1,'tool_started',?,'call_tool',?)",
        (inner, json.dumps({"name": "get_short_interest", "arguments": {"ticker": "AAPL"}})),
    )
    conn.execute(
        "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e2','r1',2,'tool_completed',?,?)",
        (inner, "call_tool"),
    )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc0','r1','search_tools',?,NULL)",
        (disc,),
    )
    conn.execute(
        "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e0','r1',0,'tool_completed',?,'search_tools')",
        (disc,),
    )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
        (MODEL, disc),
    )
    conn.commit()
    assert v._tool_success(conn, "get_short_interest", attempt=1) is None
    assert v._tool_success(conn, "get_xbrl_facts", attempt=1) == "absent"
    conn.close()
    ok, _ = v.evaluate_attempt(p, "get_short_interest", 0, False, attempt=1)
    assert ok


def _holdout_db(path: Path, *, discovery: str = "browse_tools", disc_at: str = "2026-01-01T00:00:00+00:00", inner_at: str = "2026-01-01T00:00:01+00:00", via_call_tool: bool = True) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?,?,?)",
        (disc_at, "q", "completed"),
    )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc0','r1',?,?,NULL)",
        (discovery, disc_at),
    )
    conn.execute(
        "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e0','r1',0,'tool_completed',?,?)",
        (disc_at, discovery),
    )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc1','r1','get_short_interest',?,NULL)",
        (inner_at,),
    )
    if via_call_tool:
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name, arguments) VALUES ('e1','r1',1,'tool_started',?,'call_tool',?)",
            (inner_at, json.dumps({"name": "get_short_interest", "arguments": {"ticker": "AAPL"}})),
        )
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e2','r1',2,'tool_completed',?,?)",
            (inner_at, "call_tool"),
        )
    else:
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e1','r1',1,'tool_completed',?,?)",
            (inner_at, "get_short_interest"),
        )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
        (MODEL, disc_at),
    )
    conn.commit()
    conn.close()
    return path


def test_holdout_passes_on_discover_then_dispatch(tmp_path: Path) -> None:
    ok, _ = v.evaluate_holdout_attempt(_holdout_db(tmp_path / "runs.sqlite"), "get_short_interest")
    assert ok


def test_holdout_fails_when_discovery_after_dispatch(tmp_path: Path) -> None:
    p = _holdout_db(tmp_path / "runs.sqlite", disc_at="2026-01-01T00:00:02+00:00", inner_at="2026-01-01T00:00:01+00:00")
    ok, reason = v.evaluate_holdout_attempt(p, "get_short_interest")
    assert not ok
    assert "precede" in reason


def test_holdout_fails_without_call_tool_dispatch(tmp_path: Path) -> None:
    p = _holdout_db(tmp_path / "runs.sqlite", via_call_tool=False)
    ok, reason = v.evaluate_holdout_attempt(p, "get_short_interest")
    assert not ok
    assert "call_tool" in reason

def test_attempt1_browse_only_passes_before_call_tool(tmp_path: Path) -> None:
    ok, _ = v.evaluate_attempt(_ok(tmp_path, discovery="browse_tools"), "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_attempt1_search_only_passes_before_call_tool(tmp_path: Path) -> None:
    ok, _ = v.evaluate_attempt(_ok(tmp_path, discovery="search_tools"), "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_attempt1_missing_discovery_fails(tmp_path: Path) -> None:
    ok, reason = v.evaluate_attempt(_ok(tmp_path, discovery=None), "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "routing failed" in reason


def test_attempt1_late_discovery_fails(tmp_path: Path) -> None:
    p = _ok(tmp_path, disc_at="2026-01-01T00:00:02+00:00", inner_at="2026-01-01T00:00:01+00:00")
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "precede" in reason


def test_attempt1_direct_hidden_tool_completion_fails(tmp_path: Path) -> None:
    p = _ok(tmp_path, via_call_tool=False)
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "call_tool" in reason


def test_attempt3_exact_call_tool_passes_with_zero_discovery(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery=None, via_call_tool=True)
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=3)
    assert ok
def test_attempt3_clean_stray_fails_and_harness_rejected_fails(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery=None, extra_tool="get_xbrl_facts")
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=3)
    assert not ok
    assert "routing failed" in reason
    epath = tmp_path / "err.sqlite"
    _db(epath, discovery=None, extra_tool="get_xbrl_facts", extra_error="tool_error")
    ok_err, reason_err = v.evaluate_attempt(epath, "get_fundamentals", 0, False, attempt=3)
    assert not ok_err
    assert "routing failed" in reason_err
    qpath = tmp_path / "rej.sqlite"
    _db(qpath, discovery=None, rejected_other="get_xbrl_facts")
    ok2, reason2 = v.evaluate_attempt(qpath, "get_fundamentals", 0, False, attempt=3)
    assert not ok2
    assert "harness-rejected" in reason2
def test_search_tools_directly_verifiable(tmp_path: Path) -> None:
    ok1, _ = v.evaluate_attempt(_ok(tmp_path, tool="search_tools", discovery=None, via_call_tool=False), "search_tools", 0, False, attempt=1)
    assert ok1
    p2 = tmp_path / "s3.sqlite"
    _db(p2, tool="search_tools", discovery=None, via_call_tool=False)
def test_explicit_prompt_exact_dispatch_shape() -> None:
    prompt = v.build_explicit_prompt("get_short_interest", {"ticker": "AAPL"})
    assert "You may use browse_tools, search_tools, or describe_tool" in prompt
    assert "Do not call browse_tools" not in prompt
    assert 'Call call_tool exactly once with name="get_short_interest"' in prompt
    assert '{"ticker": "AAPL"}' in prompt
    assert "TOOL_CHECK: PASS" in prompt and "TOOL_CHECK: FAIL" in prompt
    direct = v.build_explicit_prompt("search_tools", {"query": "short interest"})
    assert "Call the `search_tools` tool" in direct
    assert "call_tool exactly once" not in direct

def test_attempt1_describe_only_passes_before_call_tool(tmp_path: Path) -> None:
    ok, _ = v.evaluate_attempt(_ok(tmp_path, discovery="describe_tool"), "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_attempt1_list_domains_only_passes_before_call_tool(tmp_path: Path) -> None:
    ok, _ = v.evaluate_attempt(_ok(tmp_path, discovery="list_tool_domains"), "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_errored_discovery_passes_with_clean_discovery(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery="search_tools", extra_tool="browse_tools", extra_error="tool_error")
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert ok


def test_rejected_discovery_passes_but_rejected_call_tool_fails(tmp_path: Path) -> None:
    p = tmp_path / "runs.sqlite"
    _db(p, discovery="browse_tools", rejected_other="search_tools")
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert ok
    q = tmp_path / "rej_call.sqlite"
    _db(q, discovery="browse_tools", rejected_other="call_tool")
    ok2, reason2 = v.evaluate_attempt(q, "get_fundamentals", 0, False, attempt=1)
    assert not ok2
    assert "harness-rejected" in reason2


def _add_discovery_rows(path: Path, n: int) -> None:
    conn = sqlite3.connect(str(path))
    for i in range(n):
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES (?,?,?,?,NULL)",
            (f"tcx{i}", "r1", "browse_tools", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def test_discovery_cap_trips_at_four(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery="browse_tools")
    _add_discovery_rows(p, 2)
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert ok
    q = tmp_path / "cap.sqlite"
    _db(q, discovery="browse_tools")
    _add_discovery_rows(q, 3)
    ok2, reason2 = v.evaluate_attempt(q, "get_fundamentals", 0, False, attempt=1)
    assert not ok2
    assert "too-many-discovery:4" in reason2


def test_attempt3_with_discovery_passes_when_dispatched(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery="browse_tools", via_call_tool=True)
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=3)
    assert ok


def test_attempt3_missing_dispatch_still_fails(tmp_path: Path) -> None:
    p = _ok(tmp_path, discovery="browse_tools", via_call_tool=False)
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=3)
    assert not ok
    assert "call_tool" in reason


def test_transient_stray_returns_transient(tmp_path: Path) -> None:
    p = _ok(tmp_path, extra_tool="get_xbrl_facts", extra_error="rate_limited")
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert reason.startswith("transient: ")


def test_errored_nontransient_stray_fails(tmp_path: Path) -> None:
    p = _ok(tmp_path, extra_tool="get_xbrl_facts", extra_error="tool_error", extra_message="boom")
    ok, reason = v.evaluate_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert not ok
    assert "unexpected research" in reason

def _norm_holdout_text(value: str) -> str:
    return " ".join(value.lower().split())


def test_holdout_prompts_are_novel_and_unquoted() -> None:
    from app.tools import TOOL_DISCOVERY_REGISTRY
    holdout = __import__("json").loads(__import__("pathlib").Path("evals/holdout_discovery.json").read_text())
    assert isinstance(holdout, list) and len(holdout) == 20
    verify_texts = {_norm_holdout_text(c.get("natural_v1", "")) for c in v.VERIFY_CASES.values()}
    verify_texts |= {_norm_holdout_text(c.get("natural_v2", "")) for c in v.VERIFY_CASES.values()}
    verify_texts.discard("")
    registry_phrases: list[str] = []
    for meta in TOOL_DISCOVERY_REGISTRY.values():
        for phrase in (meta.summary, *meta.use_when):
            norm = _norm_holdout_text(phrase)
            if len(norm.split()) >= 5:
                registry_phrases.append(norm)
    for case in holdout:
        prompt = str(case.get("prompt", ""))
        norm_prompt = _norm_holdout_text(prompt)
        assert norm_prompt not in verify_texts, f"holdout copies live-matrix prompt: {prompt!r}"
        for phrase in registry_phrases:
            assert phrase not in norm_prompt, f"holdout copies catalog prose: {phrase!r} in {prompt!r}"

def _probe_db(path: Path, *, clean_args: str | None, failed_args: str | None) -> Path:
    """Discovery + dispatched target with one clean and one failed same-tool call."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    disc = "2026-01-01T00:00:00+00:00"
    inner = "2026-01-01T00:00:01+00:00"
    conn.execute("INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?, 'q', 'completed')", (disc,))
    conn.execute("INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc0','r1','browse_tools',?,NULL)", (disc,))
    conn.execute("INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e0','r1',0,'tool_completed',?,'browse_tools')", (disc,))
    conn.execute("INSERT INTO tool_calls (tool_call_id, run_id, tool_name, arguments_json, started_at, error_type) VALUES ('tc1','r1','get_threshold_securities',?,?,NULL)", (clean_args, inner))
    conn.execute("INSERT INTO tool_calls (tool_call_id, run_id, tool_name, arguments_json, started_at, error_type, error_message) VALUES ('tc2','r1','get_threshold_securities',?,?,'tool_error','No data')", (failed_args, inner))
    conn.execute("INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name, arguments) VALUES ('e1','r1',1,'tool_started',?,'call_tool',?)", (inner, json.dumps({"name": "get_threshold_securities", "arguments": {"ticker": "AAPL"}})))
    conn.execute("INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e2','r1',2,'tool_completed',?,'call_tool')", (inner,))
    conn.execute("INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)", (MODEL, disc))
    conn.commit()
    conn.close()
    return path


def test_same_tool_different_args_failure_still_fails(tmp_path: Path) -> None:
    p = _probe_db(tmp_path / "runs.sqlite", clean_args='{"ticker": "AAPL"}', failed_args='{"ticker": "AAPL", "tradeDate": "2026-09-11"}')
    ok, reason = v.evaluate_attempt(p, "get_threshold_securities", 0, False, attempt=1)
    assert not ok
    assert "failed execution present" in reason


def test_same_tool_same_args_failure_still_fails(tmp_path: Path) -> None:
    p = _probe_db(tmp_path / "runs.sqlite", clean_args='{"ticker": "AAPL"}', failed_args='{"ticker": "AAPL"}')
    ok, reason = v.evaluate_attempt(p, "get_threshold_securities", 0, False, attempt=1)
    assert not ok
    assert "failed execution present" in reason
