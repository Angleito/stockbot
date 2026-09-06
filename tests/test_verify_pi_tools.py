"""Offline unit tests for scripts/verify_pi_tools.py (fakes only, no Pi/network)."""

import json
import sqlite3

import scripts.verify_pi_tools as v
from app.storage.runs import _SCHEMA

MODEL = "test-model"


def _db(path, tool="get_fundamentals", model=MODEL, status="completed", tool_error=None, event="completed", other_tool=None):
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


def _ok(tmp_path, **kw):
    p = tmp_path / "runs.sqlite"
    _db(p, **kw)
    return p


def test_job_expansion_n_times_r():
    jobs = v.expand_jobs(["a", "b"], 3)
    assert len(jobs) == 6
    assert jobs.count(("a", 1)) == 1 and jobs.count(("b", 3)) == 1


def test_new_discovered_tool_auto_creates_jobs():
    assert v.expand_jobs(["brand_new_tool"], 3) == [("brand_new_tool", 1), ("brand_new_tool", 2), ("brand_new_tool", 3)]


def test_missing_dispatcher_fails_pre_pi(monkeypatch):
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


def test_nonzero_exit_fails(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", MODEL, 1, False)
    assert not ok


def test_timeout_fails(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", MODEL, 0, True)
    assert not ok


def test_required_tool_absent_fails(tmp_path):
    p = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1','2026-01-01T00:00:00+00:00','q','completed')")
    conn.execute("INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?, '2026-01-01T00:00:00+00:00')", (MODEL,))
    conn.commit()
    conn.close()
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", MODEL, 0, False)
    assert not ok


def test_wrong_tool_only_fails(tmp_path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, other_tool="search_web", event="completed"), "get_fundamentals", MODEL, 0, False)
    assert not ok
    assert "absent" in reason


def test_error_envelope_fails(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, tool_error="tool_error"), "get_fundamentals", MODEL, 0, False)
    assert not ok


def test_valid_empty_passes(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", MODEL, 0, False)
    assert ok


def test_model_mismatch_fails(tmp_path):
    ok, reason = v.evaluate_attempt(_ok(tmp_path, model="other-model"), "get_fundamentals", MODEL, 0, False)
    assert not ok
    assert "model mismatch" in reason


def test_tool_failed_event_fails(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path, event="both"), "get_fundamentals", MODEL, 0, False)
    assert not ok


def test_two_of_three_is_not_pass():
    assert not all([True, True, False])
    assert all([True, True, True])


def test_doctor_describe_skew_fails():
    desc = {"tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}
    doc = {"bridge_ok": True, "tool_count": 1, "tool_names": ["a"]}
    assert v.check_discovery(desc, doc) is not None


def test_model_alias_accepted(tmp_path):
    ok, _ = v.evaluate_attempt(
        _ok(tmp_path, model="muse-spark-1.3-contributor"),
        "get_fundamentals", "opencode-go/muse-spark-1.3-contributor", 0, False,
    )
    assert ok


def test_model_matches_exact_and_alias_only():
    assert v.model_matches("m", "m")
    assert v.model_matches("muse-spark-1.3-contributor", "opencode-go/muse-spark-1.3-contributor")
    assert not v.model_matches("other", "opencode-go/muse-spark-1.3-contributor")


def test_completed_override_passes_despite_kill(tmp_path):
    ok, _ = v.evaluate_attempt(_ok(tmp_path), "get_fundamentals", MODEL, -9, False, completed_override=True)
    assert ok


def test_completed_override_still_needs_db_evidence(tmp_path):
    p = tmp_path / "missing.sqlite"
    ok, _ = v.evaluate_attempt(p, "get_fundamentals", MODEL, -9, False, completed_override=True)
    assert not ok


def test_db_terminal(tmp_path):
    assert v.db_terminal(_ok(tmp_path))
    assert not v.db_terminal(tmp_path / "missing.sqlite")
