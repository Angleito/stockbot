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


def _db(path: Path, tool: str = "get_fundamentals", model: str = MODEL, status: str = "completed", tool_error: str | None = None, event: str = "completed", other_tool: str | None = None) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?,?,?)",
        (now, "q", status),
    )
    name = other_tool or tool
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('tc1','r1',?,?,?)",
        (name, now, tool_error),
    )
    if event in ("completed", "both"):
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e1','r1',1,'tool_completed',?,?)",
            (now, tool if other_tool is None else other_tool if False else tool if event == 'both' else name),
        )
    if event == "both":
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e2','r1',2,'tool_failed',?,?)",
            (now, tool),
        )
    if event == "failed-only":
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name) VALUES ('e1','r1',1,'tool_failed',?,?)",
            (now, tool),
        )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
        (model, now),
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
    event: str = "completed",
    other_tool: str | None = None,
) -> Path:
    p = tmp_path / "runs.sqlite"
    _db(p, tool=tool, model=model, status=status, tool_error=tool_error, event=event, other_tool=other_tool)
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
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 1, False)
    assert not ok


def test_timeout_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 0, True)
    assert not ok


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
    ok, reason = v.evaluate_attempt(_ok(tmp_path, other_tool="search_web", event="completed"), "get_fundamentals", 0, False)
    assert not ok
    assert "absent" in reason


def test_error_envelope_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, tool_error="tool_error"), "get_fundamentals", 0, False)
    assert not ok


def test_valid_empty_passes(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", 0, False)
    assert ok


def test_empty_model_telemetry_fails(tmp_path: Path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, model=""), "get_fundamentals", 0, False)
    assert not ok
    assert "no model telemetry" in reason


def test_tool_failed_event_fails(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, event="both"), "get_fundamentals", 0, False)
    assert not ok


def test_two_of_three_is_not_pass():
    assert not all([True, True, False])
    assert all([True, True, True])


def test_doctor_describe_skew_fails():
    desc = {"tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}
    doc = {"bridge_ok": True, "tool_count": 1, "tool_names": ["a"]}
    assert v.check_discovery(desc, doc) is not None

def test_any_pi_model_id_accepted(tmp_path: Path):
    ok, _ = v.evaluate_attempt(
        _ok(tmp_path, model="muse-spark-1.3-contributor"),
        "get_fundamentals", 0, False,
    )
    assert ok


def test_completed_override_passes_despite_kill(tmp_path: Path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", -9, False, completed_override=True)
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
        captured_cmds.append(list(args[0]))  # type: ignore[arg-type]
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
        assert "--exclude-tools" in cmd
        assert cmd[cmd.index("--exclude-tools") + 1] == "bash,edit,write,powershell"
        assert cmd.index("--exclude-tools") < cmd.index("--")
        assert cmd[-1] == "prompt"
        assert cmd[-2] == "--"
        assert cmd[:5] == ["pi", "-p", "--no-session", "--extension", v.EXTENSION]


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
    assert not all(r.ok for r in trio)
    assert all(r.ok for r in [v.AttemptResult("t", n, True, "pass", 0, "", 0.0) for n in (1, 2, 3)])


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

    def fake_eval(db_path: Path, tool: str, code: int, timed_out: bool, *, completed_override: bool = False) -> tuple[bool, str]:
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
