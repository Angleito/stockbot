"""Decision-path coverage for scripts/verify_pi_tools.py (fakes only, no Pi/network).

Each test names the uncovered branch it exercises. Helpers mirror the
existing _base_db/_attempt doubles in tests/test_verify_pi_tools.py and
tests/test_scripts_slice.py without importing them.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

import scripts.verify_pi_tools as v
from app.storage.runs import _SCHEMA

MODEL = "test-model"


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def _seed_run(conn: sqlite3.Connection, status: str = "completed") -> None:
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status)"
        " VALUES ('r1','r1','2026-01-01T00:00:00+00:00','q',?)",
        (status,),
    )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at)"
        " VALUES ('m1','r1','pi',?, '2026-01-01T00:00:00+00:00')",
        (MODEL,),
    )


def _tool(conn: sqlite3.Connection, name: str, at: str, err: str | None = None,
          msg: str | None = None, args: str = '{"ticker": "AAPL"}') -> None:
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at,"
        " arguments_json, error_type, error_message)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"tc-{name}-{at}-{err}", "r1", name, at, args, err, msg),
    )


def _event(conn: sqlite3.Connection, seq: int, etype: str, name: str, at: str,
           args: str | None = None) -> None:
    conn.execute(
        "INSERT INTO agent_events (event_id, run_id, sequence, event_type,"
        " started_at, tool_name, arguments) VALUES (?,?,?,?,?,?,?)",
        (f"e{seq}", "r1", seq, etype, at, name, args),
    )


def _pass_db(path: Path, tool: str = "get_fundamentals") -> Path:
    conn = _conn(path)
    _seed_run(conn)
    _tool(conn, "search_tools", "2026-01-01T00:00:00+00:00")
    _event(conn, 0, "tool_completed", "search_tools", "2026-01-01T00:00:00+00:00")
    _tool(conn, tool, "2026-01-01T00:00:01+00:00")
    _event(conn, 1, "tool_started", "call_tool", "2026-01-01T00:00:01+00:00",
           json.dumps({"name": tool}))
    _event(conn, 2, "tool_completed", "call_tool", "2026-01-01T00:00:01+00:00")
    conn.commit()
    conn.close()
    return path


def _res(tool: str = "t", ok: bool = True, db: str = "", reason: str = "r",
         exit_code: int = 0, duration_seconds: float = 1.0,
         reach_ok: bool | None = None, reach_reason: str = "",
         routing_ok: bool | None = None, routing_reason: str = "",
         model_config_failed: bool = False,
         discovery_calls: int = 0, research_calls: int = 0,
         direct_tool_calls: int = 0,
         completion_ok: bool | None = None,
         completion_reason: str = "") -> v.AttemptResult:
    return v.AttemptResult(tool, 1, ok, reason, exit_code, db,
                           duration_seconds,
                           model_config_failed=model_config_failed,
                           reach_ok=reach_ok, reach_reason=reach_reason,
                           routing_ok=routing_ok, routing_reason=routing_reason,
                           discovery_calls=discovery_calls,
                           research_calls=research_calls,
                           direct_tool_calls=direct_tool_calls,
                           completion_ok=completion_ok,
                           completion_reason=completion_reason)


# ---- evaluate_reachability_tool / evaluate_routing_tool: dict + object fallbacks ----

def test_tool_list_evaluators_cover_dict_and_object_fallbacks() -> None:
    ok_dict = {"reach_ok": True, "routing_ok": True, "ok": True}
    bad_dict: dict[str, object] = {"ok": False}
    missing_dict: dict[str, object] = {}
    assert v.evaluate_reachability_tool([ok_dict]) is True
    assert v.evaluate_reachability_tool([bad_dict]) is False
    assert v.evaluate_reachability_tool([missing_dict]) is False
    assert v.evaluate_routing_tool([ok_dict]) is True
    assert v.evaluate_routing_tool([bad_dict]) is False
    assert v.evaluate_routing_tool([missing_dict]) is False
    ok_obj = _res(reach_ok=True, routing_ok=True)
    bad_obj = _res(ok=False, reach_ok=None, routing_ok=None)
    assert v.evaluate_reachability_tool([ok_obj]) is True
    assert v.evaluate_reachability_tool([bad_obj]) is False
    assert v.evaluate_routing_tool([ok_obj]) is True
    assert v.evaluate_routing_tool([bad_obj]) is False


# ---- _search_query_from_row: non-dict JSON + missing query ----

def test_search_query_from_row_rejects_non_dict_and_missing_query() -> None:
    assert v._search_query_from_row(json.dumps([1, 2])) is None
    assert v._search_query_from_row(json.dumps({"nquery": 1})) is None
    assert v._search_query_from_row(json.dumps({"query": 5})) is None
    assert v._search_query_from_row(json.dumps({"query": "q"})) == "q"


# ---- _pick_inner_name wrapped envelope without name ----

def test_wrapped_inner_name_absent_without_name() -> None:
    assert v._wrapped_inner_name({"arguments": {}}) is None
    assert v._wrapped_inner_name({"arguments": {"name": ""}}) is None
    assert v._wrapped_inner_name({"other": 1}) is None
    assert v._pick_inner_name({"arguments": {"name": "get_y"}}) == "get_y"


# ---- _reap_pi: wait failure returns 124 ----

def test_reap_wait_failure_returns_124(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Proc:
        pid: int = 999999

        def poll(self) -> None:
            return None

        def wait(self, timeout: object = None) -> None:
            raise RuntimeError("boom")

    def _nokill(*a: object, **k: object) -> None:
        return None
    monkeypatch.setattr(v.os, "killpg", _nokill)
    assert v._reap_pi(_Proc(), None) == 124
    assert v._wait_pi_exit(_Proc()) == 124


# ---- check_discovery: count skew + names mismatch ----

def test_check_discovery_count_skew_and_names_mismatch() -> None:
    desc = {"tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}
    doc = {"bridge_ok": True, "tool_count": 1, "tool_names": ["a"]}
    assert "count skew" in (v.check_discovery(desc, doc) or "")
    doc2 = {"bridge_ok": True, "tool_count": 2, "tool_names": ["a", "zzz"]}
    assert "mismatch" in (v.check_discovery(desc, doc2) or "")
    assert v._describe_tool_names({}) == []
    assert v._describe_tool_names({"tools": "nope"}) == []
    assert v._doctor_tool_names({}) == []


# ---- _expected_args_mismatch: db error + row normalize error ----

def test_expected_args_mismatch_db_and_row_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "m.sqlite"
    conn = _conn(p)
    _seed_run(conn)
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at,"
        " arguments_json) VALUES ('t1','r1','get_fundamentals','t',?)",
        (json.dumps({"ticker": "AAPL"}),),
    )
    conn.commit()
    try:
        assert v._expected_args_mismatch(conn, "get_fundamentals", None) is False
        assert v._expected_args_mismatch(conn, "get_fundamentals",
                                         {"ticker": "AAPL"}) is False
        assert v._expected_args_mismatch(conn, "get_fundamentals",
                                         {"ticker": "MSFT"}) is True
    finally:
        conn.close()
    closed = sqlite3.connect(str(p))
    closed.close()
    assert v._expected_args_mismatch(closed, "get_fundamentals", {"ticker": "A"}) is False
    conn2 = _conn(tmp_path / "m2.sqlite")
    _seed_run(conn2)
    conn2.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at,"
        " arguments_json) VALUES ('t1','r1','get_fundamentals','t',?)",
        (json.dumps({"ticker": "AAPL"}),),
    )
    conn2.commit()
    calls = {"n": 0}

    def _fake(raw: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            return "WANT"
        raise ValueError("bad row")

    monkeypatch.setattr(v, "_normalize_args", _fake)
    try:
        assert v._expected_args_mismatch(conn2, "get_fundamentals",
                                         {"ticker": "AAPL"}) is False
    finally:
        conn2.close()


# ---- _research_call_count: empty registry + db error ----

def test_research_call_count_registry_and_db_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _empty_registry() -> dict[str, set[str]]:
        return {"schemas": set(v.DISCOVERY_TOOLS)}
    monkeypatch.setattr(v, "get_registry_sets", _empty_registry)
    conn = sqlite3.connect(str(tmp_path / "r.sqlite"))
    conn.execute("CREATE TABLE tool_calls (tool_call_id TEXT, tool_name TEXT)")
    try:
        assert v._research_call_count(conn) == 0
    finally:
        conn.close()
    assert v._registry_research_names() is not None
    conn2 = sqlite3.connect(str(tmp_path / "empty.sqlite"))
    try:
        assert v._research_call_count(conn2) == 0
    finally:
        conn2.close()


# ---- _select_matrix_tools: unknown / excluded / single / all ----

def test_select_matrix_tools_all_branches() -> None:
    assert v._select_matrix_tools(argparse.Namespace(tool="nope"), ["a"]) is None
    assert v._select_matrix_tools(argparse.Namespace(tool="browse_tools"),
                                  ["browse_tools", "a"]) is None
    assert v._select_matrix_tools(argparse.Namespace(tool="a"),
                                  ["a", "browse_tools"]) == ["a"]
    assert v._select_single_tool("nope", ["a"]) is None
    assert v._select_matrix_tools(argparse.Namespace(tool=None),
                                  ["a", "browse_tools"]) == ["a"]


# ---- _verification_args: thesis + research substitution ----

def test_verification_args_thesis_and_research_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _thesis_fixture(store: Path) -> str:
        return "thesis-1"
    def _research_fixture(store: Path) -> str:
        return "sess-1"
    monkeypatch.setattr(v, "ensure_thesis_fixture", _thesis_fixture)
    monkeypatch.setattr(v, "ensure_research_fixture", _research_fixture)
    out = v._verification_args("thesis_show", {"id": "thesis-placeholder"},
                               tmp_path, tmp_path / "d")
    assert out == {"id": "thesis-1"}
    out2 = v._verification_args("research_status",
                                {"session_id": "research-session-placeholder"},
                                tmp_path, tmp_path / "d")
    assert out2 == {"session_id": "sess-1"}


# ---- _attempt_record: ok without searchQueries + fail with ----

def test_search_queries_for_db_reads_real_db(tmp_path: Path) -> None:
    p = tmp_path / "s.sqlite"
    conn = _conn(p)
    _seed_run(conn)
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at,"
        " arguments_json) VALUES ('s0','r1','search_tools','t0',?)",
        (json.dumps({"query": "alpha"}),),
    )
    conn.commit()
    conn.close()
    assert v._search_queries_for_db(str(p)) == ["alpha"]
    assert v._search_queries_for_db("") == []

    rec, route, reach = v._attempt_record(_res(ok=True, reach_ok=None, routing_ok=True))
    assert (route, reach) == (True, True) and "searchQueries" not in rec
    rec2, route2, reach2 = v._attempt_record(_res(ok=False, reach_ok=False, routing_ok=None))
    assert (route2, reach2) == (False, False) and rec2["searchQueries"] == []


# ---- _report_attempt: pass and fail marks ----

def test_report_attempt_pass_and_fail_marks(capsys: pytest.CaptureFixture[str]) -> None:
    r1 = _res(ok=True, reach_ok=True, reach_reason="rr", routing_ok=True,
              routing_reason="rt", discovery_calls=1, research_calls=2,
              direct_tool_calls=1, completion_ok=True, completion_reason="done")
    v._report_attempt(r1, 3)
    out = capsys.readouterr().out
    assert "routing PASS" in out and "completion PASS" in out
    r2 = _res(ok=False, reach_ok=None, routing_ok=None, model_config_failed=True,
              completion_ok=None, completion_reason="")
    v._report_attempt(r2, 3)
    cap = capsys.readouterr()
    assert "routing FAIL" in cap.out and "n/a" in cap.out
    assert "PI MODEL CONFIGURATION FAILED" in cap.err


# ---- _collect_matrix_results: counts + per-attempt report ----

def test_collect_matrix_results_counts_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []
    def _record_report(r: v.AttemptResult, n: int) -> None:
        seen.append((r.tool, n))
    monkeypatch.setattr(v, "_report_attempt", _record_report)
    r1 = _res("t", True, reach_ok=True, routing_ok=True)
    r2 = _res("t", False, reach_ok=False, routing_ok=False)
    results, total, passed_routing, passed_reach = v._collect_matrix_results([r1, r2], 3)
    assert total == 2 and passed_routing == 1 and passed_reach == 1
    assert len(results["t"]) == 2 and seen == [("t", 3), ("t", 3)]


# ---- _sweep_matrix_tools: four verdicts + prune ----

def test_sweep_matrix_tools_four_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pruned: list[str] = []
    def _record_prune(root: Path, tool: str, recs: list[dict[str, object]]) -> None:
        pruned.append(tool)
    monkeypatch.setattr(v, "remove_successful_attempt_dirs", _record_prune)
    results: dict[str, list[dict[str, object]]] = {
        "t1": [{"reach_ok": True, "routing_ok": True, "ok": True}],
        "t2": [{"reach_ok": False, "routing_ok": False, "ok": False}],
        "t3": [{"reach_ok": False, "routing_ok": True, "ok": True}],
        "t4": [{"reach_ok": True, "routing_ok": False, "ok": True}],
    }
    failed_routing, failed_reach = v._sweep_matrix_tools(["t1", "t2", "t3", "t4"],
                                                         results, [], tmp_path)
    assert failed_routing == ["t2", "t4"] and failed_reach == ["t2", "t3"]
    assert pruned == ["t1"]


# ---- _print_matrix_aggregates: with and without categories ----

def test_print_matrix_aggregates_with_and_without_cats(capsys: pytest.CaptureFixture[str]) -> None:
    v._print_matrix_aggregates({}, 2, 3.0)
    assert "Failure categories: none" in capsys.readouterr().out
    v._print_matrix_aggregates({"failure_category_counts": {"SELECTION_FAILURE": 2}},
                               2, 3.0)
    assert "SELECTION_FAILURE=2" in capsys.readouterr().out


# ---- _report_matrix_result: pass and fail ----

def test_report_matrix_result_pass_and_fail(capsys: pytest.CaptureFixture[str]) -> None:
    failed, loop = v._report_matrix_result([], [])
    assert (failed, loop) == ([], 0)
    assert "RESULT: PASS" in capsys.readouterr().out
    failed2, _ = v._report_matrix_result(["b"], ["a"])
    assert failed2 == ["a", "b"]
    cap = capsys.readouterr()
    assert "RESULT: FAIL" in cap.out and "failed tools" in cap.out


# ---- discover: ordered / reversed / unknown ids ----

def _popen_factory(lines: list[str], fail_first_wait: bool = False, fail_close: bool = False, fail_all_waits: bool = False) -> object:
    class _In:
        def write(self, s: object) -> None:
            pass

        def flush(self) -> None:
            pass

        def close(self) -> None:
            if fail_close:
                raise OSError("close boom")

    class _Out:
        def __init__(self) -> None:
            self._q = list(lines)

        def readline(self) -> str:
            return self._q.pop(0) if self._q else ""

    class _Popen:
        def __init__(self, *a: object, **k: object) -> None:
            self.stdin = _In()
            self.stdout = _Out()

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            if fail_all_waits or (fail_first_wait):
                raise subprocess.TimeoutExpired("pi_bridge", float(timeout) if timeout is not None else 0.0)
            return 0

    return _Popen


def _lines(a: str = "discover-1", b: str = "discover-2") -> list[str]:
    return [json.dumps({"id": a, "tools": [], "bridge_ok": True}),
            json.dumps({"id": b, "bridge_ok": True, "tool_count": 0, "tool_names": []})]


def test_discover_ordered_reversed_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines()))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines()[::-1]))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines("x", "y")))
    assert v.discover() == ({}, {})


def test_discover_wait_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v.subprocess, "Popen",
                        _popen_factory(_lines(), fail_first_wait=True, fail_close=True))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True


# ---- run_confusion: preflight fail + success/fail paths ----

def test_run_confusion_preflight_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> object:
        raise ValueError("no registry semantics")

    monkeypatch.setattr(v, "generate_confusion_cases", _boom)
    assert v.run_confusion() == 1


def test_run_confusion_success_and_failure_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases: list[v.ConfusionCase] = [
        {"expected_tool": "a", "prompt": "pa", "arguments": {}, "pair": ["t2", "t1"]},
        {"expected_tool": "b", "prompt": "pb", "arguments": {}, "pair": ["t1", "t2"]},
    ]
    def _gen_cases() -> list[v.ConfusionCase]:
        return cases
    def _batch_tb() -> str:
        return "tb"
    def _report_none(*a: object, **k: object) -> None:
        return None
    def _pair_ok(*a: object, **k: object) -> tuple[int, int]:
        return (1, 1)
    def _pair_fail(*a: object, **k: object) -> tuple[int, int]:
        return (0, 1)
    monkeypatch.setattr(v, "generate_confusion_cases", _gen_cases)
    monkeypatch.setattr(v, "_batch_id", _batch_tb)
    monkeypatch.setattr(v, "get_data_root", lambda: tmp_path)
    monkeypatch.setattr(v, "_print_confusion_report", _report_none)
    monkeypatch.setattr(v, "_write_confusion_summary", _report_none)
    monkeypatch.setattr(v, "_run_confusion_pair", _pair_ok)
    assert v.run_confusion() == 0
    monkeypatch.setattr(v, "_run_confusion_pair", _pair_fail)
    assert v.run_confusion() == 1


def test_run_confusion_case_fail_and_pair_tally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _attempt_fail(*a: object, **k: object) -> v.AttemptResult:
        return _res("tool-a", False, "")
    def _cat_select(result: v.AttemptResult, db_path: Path, expected: str, base_args: dict[str, object]) -> v.RoutingFailureCategory | None:
        return v.RoutingFailureCategory.SELECTION_FAILURE
    monkeypatch.setattr(v, "run_verification_attempt", _attempt_fail)
    monkeypatch.setattr(v, "_confusion_category", _cat_select)
    c: v.ConfusionCase = {"expected_tool": "tool-a", "prompt": "p", "arguments": {}, "pair": ["a", "b"]}
    rec = v._run_confusion_case(("a", "b"), c, tmp_path, tmp_path, tmp_path)
    assert rec["selection_ok"] is False
    recs: list[dict[str, object]] = [
        {"selection_ok": True, "failure_category": "SELECTION_FAILURE"},
        {"selection_ok": False, "failure_category": None},
    ]
    def _pop_rec(*a: object, **k: object) -> dict[str, object]:
        return recs.pop(0)
    monkeypatch.setattr(v, "_run_confusion_case", _pop_rec)
    totals = {c2.value: 0 for c2 in v.RoutingFailureCategory}
    out: list[dict[str, object]] = []
    ok, total = v._run_confusion_pair(
        ("a", "b"), [{"expected_tool": "a", "prompt": "p", "arguments": {}, "pair": ["a", "b"]},
                      {"expected_tool": "b", "prompt": "p", "arguments": {}, "pair": ["a", "b"]}],
        tmp_path, tmp_path, tmp_path, out, totals)
    assert (ok, total) == (1, 2) and len(out) == 2
    assert totals["SELECTION_FAILURE"] == 1


# ---- holdout: invalid / lookup-miss / success / loop ----

def test_run_holdout_case_invalid_and_lookup_miss(tmp_path: Path) -> None:
    assert v._run_holdout_case("nope", 0, {}, tmp_path, tmp_path, tmp_path) == (1, 1)
    schemas: dict[str, dict[str, object]] = {"some-tool": {"required": ["missing-arg"]}}
    case: dict[str, object] = {"prompt": "p", "expected_tool": "some-tool"}
    assert v._run_holdout_case(case, 0, schemas, tmp_path, tmp_path, tmp_path) == (1, 1)


def test_run_holdout_case_success_and_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _holdout_hit(case: dict[str, object], schemas: dict[str, dict[str, object]]) -> tuple[str, str, dict[str, object]] | None:
        return ("tool-t", "prompt-p", {"a": 1})
    monkeypatch.setattr(v, "_holdout_lookup_args", _holdout_hit)
    made = v.AttemptResult("tool-t", 1, True, "ok", 0, str(tmp_path / "r.sqlite"), 2.0)
    def _attempt_made(*a: object, **k: object) -> v.AttemptResult:
        return made
    monkeypatch.setattr(v, "run_verification_attempt", _attempt_made)
    def _verdicts_fixed(result: v.AttemptResult, db_path: Path, tool: str) -> tuple[bool, str, bool, str]:
        return (True, "rok", False, "fok")
    monkeypatch.setattr(v, "_holdout_verdicts", _verdicts_fixed)
    seen: list[tuple[object, ...]] = []
    def _record_holdout(*a: object) -> None:
        seen.append(a)
    monkeypatch.setattr(v, "_report_holdout_case", _record_holdout)
    assert v._run_holdout_case({"prompt": "p", "expected_tool": "t"}, 0, {},
                               tmp_path, tmp_path, tmp_path) == (0, 1)
    assert seen == [("prompt-p", "tool-t", True, "rok", False, "fok", 2.0)]


def test_run_holdout_no_cases_and_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_holdout(path: str) -> None:
        return None
    def _two_holdouts(path: str) -> list[dict[str, str]]:
        return [{"prompt": "p1"}, {"prompt": "p2"}]
    monkeypatch.setattr(v, "_read_holdout_cases", _no_holdout)
    assert v.run_holdout("whatever.json") == 1
    monkeypatch.setattr(v, "_read_holdout_cases", _two_holdouts)
    monkeypatch.setattr(v, "tool_schemas", lambda: {})
    monkeypatch.setattr(v, "_batch_id", lambda: "tb")
    monkeypatch.setattr(v, "get_data_root", lambda: tmp_path)
    outcomes = iter([(0, 1), (1, 0)])
    def _next_outcome(*a: object, **k: object) -> tuple[int, int]:
        return next(outcomes)
    def _zero_outcome(*a: object, **k: object) -> tuple[int, int]:
        return (0, 0)
    def _one_holdout(path: str) -> list[dict[str, str]]:
        return [{"prompt": "p1"}]
    monkeypatch.setattr(v, "_run_holdout_case", _next_outcome)
    assert v.run_holdout("h.json") == 1
    monkeypatch.setattr(v, "_run_holdout_case", _zero_outcome)
    monkeypatch.setattr(v, "_read_holdout_cases", _one_holdout)
    assert v.run_holdout("h.json") == 0


def test_read_holdout_cases_failures(tmp_path: Path) -> None:
    assert v._read_holdout_cases(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert v._read_holdout_cases(str(bad)) is None
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert v._read_holdout_cases(str(empty)) is None


# ---- reachability / routing pass-through on a clean DB ----

def test_clean_db_passes_both_evaluators(tmp_path: Path) -> None:
    p = _pass_db(tmp_path / "ok.sqlite")
    ok, _ = v.evaluate_reachability_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert ok
    ok2, _ = v.evaluate_routing_attempt(p, "get_fundamentals", 0, False, attempt=1)
    assert ok2


def test_holdout_verdicts_passthrough_and_lookup_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_fixture(tool: str, schemas: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
        raise LookupError("no fixture")
    monkeypatch.setattr(v, "resolve_arguments", _no_fixture)
    assert v._holdout_lookup_args({"expected_tool": "t", "prompt": "p"}, {}) is None
    r = v.AttemptResult("t", 1, True, "pass", 0, "", 0.0, False, True, "pass", True, "pass")
    assert v._holdout_verdicts(r, Path("x"), "t")[0] is True
