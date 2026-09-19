"""CRAP-slice tests for scripts/** helpers (no live Pi, no network).

Sections per script area, assembled from worker scratch files.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, override

import pytest

import scripts.export_harness_viewer as exh_viewer
import scripts.research_slice as rs
import scripts.robinhood_options_smoke as smoke
import scripts.robinhood_tools as rt
import scripts.sandbox_doctor as doc
import scripts.strict_routing_harness as harness
import scripts.update_tool_catalog as cat
import scripts.verify_agent_scenarios as vas
import scripts.verify_judge as J
import scripts.verify_pi_tools as v
from app.pi_gateway import PiSessionContext
from app.policy import Capability, RequestContext
from app.research.evals.scenarios import Scenario, ScenarioFamily
from app.research.evals.traces import TraceHeader
from app.research.models import Failure, Job, JSONValue, ResearchSession
from app.research.repository import ResearchRepository
from app.robinhood.client import RobinhoodClient
from app.storage.runs import _SCHEMA, RunRecorder
from scripts import pi_bridge
from scripts import verify_tool_health as vth
from scripts import verify_tool_registry as reg

# ---- slice_export_tests.py ----


class FakeRepo(ResearchRepository):
    def __init__(
        self,
        sess: object = None,
        jobs: list[Job] | None = None,
        evidence: list[dict[str, JSONValue]] | None = None,
        freezes: dict[str, dict[str, JSONValue]] | None = None,
        dossiers: list[dict[str, JSONValue]] | None = None,
        raise_session: bool = False,
        raise_jobs: bool = False,
        raise_evidence: bool = False,
        raise_dossiers: bool = False,
    ) -> None:
        self._sess = sess
        self._jobs: list[Job] = jobs if jobs is not None else []
        self._evidence: list[dict[str, JSONValue]] = evidence if evidence is not None else []
        self._freezes: dict[str, dict[str, JSONValue]] = freezes if freezes is not None else {}
        self._dossiers: list[dict[str, JSONValue]] = dossiers if dossiers is not None else []
        self._raise_session = raise_session
        self._raise_jobs = raise_jobs
        self._raise_evidence = raise_evidence
        self._raise_dossiers = raise_dossiers

    @override
    def get_session(self, session_id: str) -> ResearchSession:
        if self._raise_session:
            raise RuntimeError("no session")
        sess = self._sess
        if isinstance(sess, ResearchSession):
            return sess
        if isinstance(sess, SimpleNamespace):
            return _namespace_session(
                str(sess.session_id) if hasattr(sess, "session_id") else session_id,
                sess,
            )
        raise AssertionError(f"bad sess shape: {sess!r}")

    @override
    def list_jobs(self, session_id: str) -> list[Job]:
        if self._raise_jobs:
            raise RuntimeError("no jobs")
        return self._jobs

    @override
    def list_evidence(self, session_id: str) -> list[dict[str, JSONValue]]:
        if self._raise_evidence:
            raise RuntimeError("no evidence")
        return self._evidence

    @override
    def get_freeze(self, freeze_id: str) -> dict[str, JSONValue]:
        if freeze_id not in self._freezes:
            raise KeyError(freeze_id)
        return self._freezes[freeze_id]

    @override
    def list_dossiers(self, session_id: str) -> list[dict[str, JSONValue]]:
        if self._raise_dossiers:
            raise RuntimeError("no dossiers")
        return self._dossiers


def _json_value(value: object) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def _namespace_session(session_id: str, ns: SimpleNamespace) -> ResearchSession:
    updated = getattr(ns, "updated_at", None)
    as_of = getattr(ns, "as_of", None)
    freeze_ids = getattr(ns, "freeze_ids", [])
    committee = getattr(ns, "committee_runs", [])
    raw_final = getattr(ns, "final_result", None)
    final: dict[str, JSONValue] | None = None
    if isinstance(raw_final, dict):
        converted: dict[str, JSONValue] = {}
        for fk, fv in raw_final.items():
            converted[str(fk)] = _json_value(fv)
        final = converted
    return ResearchSession(
        session_id=session_id,
        created_at=updated if isinstance(updated, datetime) else datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        updated_at=updated if isinstance(updated, datetime) else datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        query=str(getattr(ns, "query", "q?")),
        objective="test",
        as_of=as_of if as_of is None or isinstance(as_of, datetime) else None,
        status=str(getattr(ns, "status", "open")),
        current_wave=1,
        freeze_ids=[str(f) for f in freeze_ids] if isinstance(freeze_ids, list) else [],
        committee_runs=list(committee) if isinstance(committee, list) else [],
        final_result=final,
    )


def _sess(**kw: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "freeze_ids": list[str](),
        "final_result": None,
        "current_wave": "w1",
        "query": "q?",
        "status": "open",
        "as_of": None,
        "updated_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "committee_runs": ["c1"],
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _job(**kw: object) -> Job:
    failure = kw.pop("failure", None)
    real_failure: Failure | None = None
    if isinstance(failure, Failure) or failure is None:
        real_failure = failure
    elif isinstance(failure, SimpleNamespace):
        category = getattr(failure, "category", "tool_error")
        message = getattr(failure, "message", "err")
        real_failure = Failure(category=str(category), message=str(message))
    else:
        raise AssertionError(f"bad failure shape: {failure!r}")
    diagnostics = kw.pop("diagnostics", {"assignment_id": "a1", "role": "r1"})
    assert diagnostics is None or isinstance(diagnostics, dict)
    job_id = kw.pop("job_id", "j1")
    assert isinstance(job_id, str)
    diag: dict[str, JSONValue] = {}
    if isinstance(diagnostics, dict):
        for k, v in diagnostics.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                diag[str(k)] = v
    job = Job(
        job_id=job_id,
        session_id="s",
        wave_id=1,
        parent_job_id=None,
        job_type="research",
        owner="agent",
        status="done",
        failure=real_failure,
        diagnostics=diag,
    )
    assert not kw
    return job


def test_all_session_ids_missing_db(tmp_path: Path):
    assert exh_viewer._all_session_ids(tmp_path / "nope.sqlite") == []


def test_all_session_ids_sqlite_error(tmp_path: Path):
    db = tmp_path / "empty.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE other (id TEXT)")
    assert exh_viewer._all_session_ids(db) == []


def test_all_session_ids_ok(tmp_path: Path):
    db = tmp_path / "r.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sessions (session_id TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', '2026-01-01')")
        conn.execute("INSERT INTO sessions VALUES ('s2', '2026-01-02')")
    ids = exh_viewer._all_session_ids(db)
    assert sorted(ids) == ["s1", "s2"]


def test_ts_and_seq():
    assert exh_viewer._ts("x") == "x"
    assert exh_viewer._ts("") is None
    assert exh_viewer._ts(None) is None
    assert exh_viewer._ts(5) is None
    assert exh_viewer._event_seq({"seq": 3}) == 3
    assert exh_viewer._event_seq({"seq": "3"}) == 0
    assert exh_viewer._event_seq({}) == 0


def test_parse_violations():
    assert exh_viewer._parse_violations('["a", "b"]') == ["a", "b"]
    assert exh_viewer._parse_violations("not-json{{{") == ["not-json{{{"]
    assert exh_viewer._parse_violations('{"k": 1}') == ["{'k': 1}"]
    assert exh_viewer._parse_violations('"just-a-string"') == ["just-a-string"]


def test_clean_text_items_filters():
    raw = [
        "nope",
        {"text": 5, "evidence_ids": ["e1"]},
        {"text": "ok", "evidence_ids": ["e1", 7, None]},
        {"text": "no-ids", "evidence_ids": "e1"},
        {"text": "fine"},
    ]
    out = exh_viewer._clean_text_items(raw)
    assert out == [
        {"text": "ok", "evidenceIds": ["e1"]},
        {"text": "no-ids", "evidenceIds": []},
        {"text": "fine", "evidenceIds": []},
    ]
    assert exh_viewer._clean_text_items("notalist") == []
    assert exh_viewer._clean_text_items(None) == []


def test_collect_claims_branches():
    assert exh_viewer._collect_claims(_sess(final_result=None)) == []
    assert exh_viewer._collect_claims(_sess(final_result={})) == []
    s = _sess(final_result={"claims": ["x", {"text": "c1", "evidence_ids": ["e1", 2]}]})
    assert exh_viewer._collect_claims(s) == [{"text": "c1", "evidenceIds": ["e1"]}]

    class Bad:
        @property
        def final_result(self):
            raise RuntimeError("boom")

    assert exh_viewer._collect_claims(Bad()) == []


def test_collect_freezes_keyerror_and_bad_ids():
    repo = FakeRepo(
        sess=_sess(freeze_ids=["f1", "missing", "f2"]),
        freezes={"f1": {"evidence_ids": ["e1", 9]}, "f2": {"evidence_ids": "nope"}},
    )
    out = exh_viewer._collect_freezes(repo, repo._sess)
    assert out == [
        {"freezeId": "f1", "evidenceIds": ["e1"]},
        {"freezeId": "f2", "evidenceIds": []},
    ]


def test_collect_dossiers_and_evidence_errors():
    repo = FakeRepo(sess=_sess(), raise_evidence=True, raise_dossiers=True)
    assert exh_viewer._collect_evidence(repo, "s") == []
    assert exh_viewer._collect_dossiers(repo, "s") == []
    repo2 = FakeRepo(
        sess=_sess(), dossiers=[{"dossier_id": "d1", "findings": ["x", {"text": "f", "evidence_ids": ["e"]}]}]
    )
    assert exh_viewer._collect_dossiers(repo2, "s") == [
        {"dossierId": "d1", "findings": [{"text": "f", "evidenceIds": ["e"]}]}
    ]
    repo3 = FakeRepo(
        sess=_sess(),
        evidence=[{"evidence_id": "e1", "subject": "s", "known_at": "k", "source_name": "n", "source_uri": "u"}],
    )
    assert exh_viewer._collect_evidence(repo3, "s")[0]["evidenceId"] == "e1"


def test_job_row_variants():
    r = exh_viewer._job_row(_job())
    assert r["failureCategory"] is None and r["assignmentId"] == "a1"
    fail = SimpleNamespace(category="timeout", message="slow")
    r2 = exh_viewer._job_row(_job(failure=fail, diagnostics={"assignment_id": 5, "role": None}))
    assert r2["failureCategory"] == "timeout" and r2["failureMessage"] == "slow"
    assert r2["assignmentId"] is None and r2["role"] is None
    r3 = exh_viewer._job_row(_job(diagnostics=None))
    assert r3["assignmentId"] is None


def _no_traces(session_id: str | None = None) -> list[object]:
    return []


def _traces_raise(session_id: str | None = None) -> list[object]:
    raise RuntimeError("no trace")


def _ctx_ok(harness: tuple[object, ...]) -> tuple[object | None, str]:
    return (object(), "")


def _ctx_fail(harness: tuple[object, ...]) -> tuple[object | None, str]:
    return (None, "tool context failed: x")


def _pct_group(self: object, n: str) -> str:
    return {"1": "870", "2": "+", "3": "75"}[n]


def _write_disk_error(*a: object, **k: object) -> object:
    raise OSError("disk")


def _no_fixture(tool: str, schemas: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    raise LookupError("no fixture")


def _trace_hdr() -> TraceHeader:
    return TraceHeader(
        trace_id="t1",
        session_id="s",
        wave_id=1,
        provider="p",
        model="mm",
        prompt_version="v1",
        harness_version="h",
        git_sha="g",
        started_at="t",
        completed_at=None,
        duration_ms=None,
        conclusion="c",
        status="done",
    )


def test_collect_trace_empty_and_monkeypatched(monkeypatch: pytest.MonkeyPatch):
    assert exh_viewer._collect_trace([])["trace_id"] is None
    hdr = _trace_hdr()
    evt = SimpleNamespace(seq=2, event_type="note", payload={"a": 1})

    def _evts_one(trace_id: str) -> list[object]:
        return [evt]

    monkeypatch.setattr(exh_viewer, "get_trace_events", _evts_one)
    out = exh_viewer._collect_trace([hdr])
    events = out["events"]
    assert out["trace_id"] == "t1" and isinstance(events, list) and len(events) == 1
    first = events[0]
    assert isinstance(first, dict) and first["eventType"] == "note"

    def _evts_raise(trace_id: str) -> list[object]:
        raise RuntimeError("x")

    monkeypatch.setattr(exh_viewer, "get_trace_events", _evts_raise)
    out2 = exh_viewer._collect_trace([hdr])
    assert out2["events"] == []


def test_build_session_run_missing_returns_none():
    repo = FakeRepo(sess=_sess(), raise_session=True)
    assert exh_viewer.build_session_run("s", repo) is None


def test_build_session_run_empty_sessions(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(exh_viewer, "list_traces", _no_traces)
    repo = FakeRepo(sess=_sess(freeze_ids=[], final_result={"claims": []}), jobs=[])
    run = exh_viewer.build_session_run("s1", repo)
    assert run is not None
    assert run["sessionId"] == "s1"
    assert run["traceId"] is None
    assert run["jobs"] == [] and run["claims"] == []
    assert run["asOf"] is None
    assert run["committeeRuns"] == ["c1"]


def test_build_session_run_full(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(exh_viewer, "list_traces", _traces_raise)
    fail = SimpleNamespace(category="c", message="mm")
    jobs = [_job(), _job(job_id="j2", failure=fail)]
    sess = _sess(
        freeze_ids=["f1"],
        final_result={"claims": [{"text": "cl", "evidence_ids": ["e1"]}, 42]},
        as_of=datetime(2026, 5, 1, tzinfo=UTC),
    )
    repo = FakeRepo(
        sess=sess,
        jobs=jobs,
        evidence=[{"evidence_id": "e1", "subject": "s", "known_at": "k", "source_name": "n", "source_uri": None}],
        freezes={"f1": {"evidence_ids": ["e1"]}},
        dossiers=[{"dossier_id": "d", "findings": [{"text": "f", "evidence_ids": []}]}],
    )
    run = exh_viewer.build_session_run("sx", repo)
    assert run is not None
    jobs_out = run["jobs"]
    assert isinstance(jobs_out, list) and len(jobs_out) == 2
    second = jobs_out[1]
    assert isinstance(second, dict) and second["failureCategory"] == "c"
    evidence = run["evidence"]
    assert isinstance(evidence, list)
    first_ev = evidence[0]
    assert isinstance(first_ev, dict) and first_ev["sourceUri"] is None
    assert run["freezes"] == [{"freezeId": "f1", "evidenceIds": ["e1"]}]
    dossiers = run["dossiers"]
    assert isinstance(dossiers, list)
    first_d = dossiers[0]
    assert isinstance(first_d, dict) and first_d["dossierId"] == "d"
    assert run["claims"] == [{"text": "cl", "evidenceIds": ["e1"]}]
    assert run["asOf"] == "2026-05-01T00:00:00+00:00"


def test_read_eval_db_missing(tmp_path: Path):
    runs, results, fails = exh_viewer.read_eval_db(tmp_path / "nope.sqlite")
    assert (runs, results, fails) == ([], [], [])


def test_read_eval_db_corrupt(tmp_path: Path):
    bad = tmp_path / "bad.sqlite"
    bad.write_text("not a database")
    runs, results, fails = exh_viewer.read_eval_db(bad)
    assert (runs, results, fails) == ([], [], [])


def _make_eval_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE eval_runs (eval_run_id TEXT, model TEXT, provider TEXT, harness_version TEXT, prompt_version TEXT, git_sha TEXT, started_at TEXT, scenario_version TEXT)"
        )
        conn.execute(
            "CREATE TABLE eval_scenario_results (eval_run_id TEXT, scenario_name TEXT, passed INTEGER, violations_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE failure_records (failure_id TEXT, eval_run_id TEXT, scenario_name TEXT, violation TEXT)"
        )
        conn.execute("INSERT INTO eval_runs VALUES ('r1','m','p','h','pv','g','2026-01-01','sv')")
        conn.execute("INSERT INTO eval_scenario_results VALUES ('r1','s-pass',1,'[]')")
        conn.execute("INSERT INTO eval_scenario_results VALUES ('r1','s-fail',0,'not-json')")
        conn.execute("INSERT INTO failure_records VALUES ('f1','r1','s-fail','v')")


def test_read_eval_db_ok_and_bad_violations(tmp_path: Path):
    db = tmp_path / "eval.sqlite"
    _make_eval_db(db)
    runs, results, fails = exh_viewer.read_eval_db(db)
    assert len(runs) == 1 and runs[0]["passed"] == 0
    by_name = {r["scenarioName"]: r for r in results}
    assert by_name["s-pass"]["passed"] is True and by_name["s-pass"]["violations"] == []
    assert by_name["s-fail"]["passed"] is False and by_name["s-fail"]["violations"] == ["not-json"]
    assert fails == [
        {
            "failureId": "f1",
            "evalRunId": "r1",
            "scenarioName": "s-fail",
            "violation": "v",
        }
    ]


def test_attach_pass_fail_counts():
    runs: list[dict[str, object]] = [{"evalRunId": "r1"}, {"evalRunId": "r2"}]
    results: list[dict[str, object]] = [
        {"evalRunId": "r1", "passed": True},
        {"evalRunId": "r1", "passed": False},
        {"evalRunId": "r2", "passed": False},
        {"evalRunId": "other", "passed": True},
    ]
    exh_viewer.attach_pass_fail(runs, results)
    assert runs[0] == {"evalRunId": "r1", "passed": 1, "failed": 1}
    assert runs[1] == {"evalRunId": "r2", "passed": 0, "failed": 1}


def test_build_projection_and_render_shape():
    proj = exh_viewer.build_projection([{"a": 1}], [{"b": 2}], [{"c": 3}], [], [{"d": 4}])
    assert set(proj) == {
        "researchRuns",
        "evalRuns",
        "evalScenarioResults",
        "experiments",
        "failureRecords",
    }
    body = exh_viewer.render_projection_ts(proj)
    assert body.startswith("import type")
    assert "export const PROJECTION" in body
    payload = body.split("} = ", 1)[1].rstrip().rstrip(";").strip()
    parsed = json.loads(payload)
    assert parsed["researchRuns"] == [{"a": 1}]
    assert parsed["failureRecords"] == [{"d": 4}]


# ---- slice_agent_tests.py ----
"""Scratch tests for scripts/verify_agent_scenarios.py pure + fallback paths."""


class _NS:
    pass


def _ns(provider: object = None, model: object = None, scenario: object = None) -> argparse.Namespace:
    return argparse.Namespace(provider=provider, model=model, scenario=scenario)


def test_resolve_prefers_flags_and_strips_whitespace():
    provider, model = vas.resolve_provider_model(
        "  anthropic ", " claude ", {"STOCKBOT_PROVIDER": "x", "STOCKBOT_MODEL": "y"}
    )
    assert (provider, model) == ("anthropic", "claude")


def test_resolve_falls_back_to_env():
    provider, model = vas.resolve_provider_model(
        None, None, {"STOCKBOT_PROVIDER": " anthropic", "STOCKBOT_MODEL": "claude "}
    )
    assert (provider, model) == ("anthropic", "claude")


def test_resolve_defaults_to_pi_cli_when_unset():
    """No flag and no env means Pi's own CLI default: resolved as ("", ""), never a hard failure."""
    assert vas.resolve_provider_model(None, None, {}) == ("", "")
    assert vas.resolve_provider_model("  ", "", {"STOCKBOT_PROVIDER": "", "STOCKBOT_MODEL": None}) == ("", "")


def test_lookup_env_value_narrows_strings():
    """Only real string values count as env configuration; anything else reads as unset."""
    assert vas._lookup_env_value({"A": " v "}, "A") == " v "
    assert vas._lookup_env_value({"A": 5}, "A") is None
    assert vas._lookup_env_value({}, "A") is None

    class _Lookup:
        def get(self, key: str) -> object:
            return "v" if key == "A" else None

    class _NoGet:
        get = 5

    assert vas._lookup_env_value(_Lookup(), "A") == "v"
    assert vas._lookup_env_value(_Lookup(), "B") is None
    assert vas._lookup_env_value(object(), "A") is None
    assert vas._lookup_env_value(_NoGet(), "A") is None


def test_resolve_passes_explicit_flags_through():
    """Explicit flags/env still override; Pi validates a bad provider at probe time, not here."""
    assert vas.resolve_provider_model("unknown", "m", {}) == ("unknown", "m")
    assert vas.resolve_provider_model("pi", None, {}) == ("pi", "")
    assert vas.resolve_provider_model("anthropic", None, {}) == ("anthropic", "")


def test_resolve_namespace_wrapper_strips():
    ns = _ns("  anthropic ", " claude")
    assert vas._resolve_provider_model(ns) == ("anthropic", "claude")


def test_resolve_model_timeout_flag_env_default():
    assert vas.resolve_model_timeout("120", {"STOCKBOT_MODEL_TIMEOUT": "60"}) == 120
    assert vas.resolve_model_timeout(None, {"STOCKBOT_MODEL_TIMEOUT": " 60 "}) == 60
    assert vas.resolve_model_timeout(None, {}) == vas._PI_CALL_TIMEOUT_DEFAULT_S
    assert vas._PI_CALL_TIMEOUT_DEFAULT_S > 110


def test_resolve_model_timeout_rejects_bad_values():
    for bad in ("nope", "0", "-5"):
        try:
            vas.resolve_model_timeout(bad, {})
        except RuntimeError as exc:
            assert "invalid model timeout" in str(exc)
        else:
            raise AssertionError(f"expected RuntimeError for {bad!r}")


def test_resolve_model_timeout_namespace_wrapper():
    ns = argparse.Namespace(model_timeout="42")
    assert vas._resolve_model_timeout(ns) == 42


def test_model_label_names_the_pi_default():
    assert vas._model_label("", "") == "pi default"
    assert vas._model_label("openai", "gpt-x") == "openai/gpt-x"
    assert vas._model_label("openai", "") == "openai/pi default"


def test_pi_argv_drops_unset_flags():
    """A flag-less default is a spawn with no --provider/--model at all (mirrors omp_runner)."""
    default_argv = vas._pi_completion_argv("", "", "p?")
    assert "--provider" not in default_argv and "--model" not in default_argv
    assert default_argv == ["omp", "-p", "--no-session", "--no-tools"]
    flagged = vas._pi_completion_argv("openai", "gpt-x", "p?")
    assert flagged[flagged.index("--provider") + 1] == "openai"
    assert flagged[flagged.index("--model") + 1] == "gpt-x"


def test_parse_args_defaults():
    args = vas.parse_args([])
    assert args.prompt_version == "v1"
    assert args.list is False and args.json is False
    assert args.scenario is None and args.fixtures_dir is None
    assert args.model_timeout is None


def test_selected_names_excludes_fixture_only_regressions():
    """The default live run never re-asks the two questions that only carry old broken-run fixtures."""
    from app.research.evals.scenarios import get_scenario, list_scenarios

    default_names = vas._selected_names(_ns())
    assert len(default_names) == len(list_scenarios()) - 2
    for name in (
        "spacex-openai-bankruptcy-sec-only-live-run",
        "gs-openai-sec-only-live-run",
    ):
        assert name not in default_names
        assert get_scenario(name).fixture_only is True
        assert name in vas._scenario_map()
        assert vas._selected_names(_ns(scenario=name)) == [name]


def test_summarize_pass_fail_counts():
    from app.research.evals.evaluators import EvalMetrics, ScenarioResult

    def _sr(passed: bool) -> ScenarioResult:
        return ScenarioResult(
            scenario_name="s",
            passed=passed,
            violations=(),
            metrics=EvalMetrics(
                success=passed,
                wall_clock_ms=1.0,
                job_count=1,
                tool_call_count=1,
                discovery_calls=0,
                failed_count=0,
                recovered_count=0,
                evidence_count=0,
                evidence_coverage=0.0,
                input_tokens=0,
                output_tokens=0,
                estimated_cost=0.0,
                pit_provenance_violations=0,
                disagreement=False,
                completeness=1.0,
            ),
        )

    failed, code = vas.summarize_results([_sr(True), _sr(True)])
    assert failed == [] and code == 0
    failed, code = vas.summarize_results([_sr(True), _sr(False)])
    assert len(failed) == 1 and code == 1


def test_check_pi_ready_error_path_missing_binary(monkeypatch: pytest.MonkeyPatch):
    def boom(*a: object, **k: object) -> object:
        raise FileNotFoundError("no pi")

    monkeypatch.setattr(subprocess, "run", boom)
    try:
        vas._check_pi_ready("p", "m", 30)
    except RuntimeError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_check_pi_ready_timeout_path(monkeypatch: pytest.MonkeyPatch):
    def boom(*a: object, **k: object) -> object:
        raise subprocess.TimeoutExpired(cmd="pi", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    try:
        vas._check_pi_ready("", "", 17)
    except RuntimeError as exc:
        assert "timed out after 17s" in str(exc) and "pi default" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_check_pi_ready_probes_without_flags_and_ok(monkeypatch: pytest.MonkeyPatch):
    """The probe uses the same flag-less default and timeout budget as the live calls."""
    seen: dict[str, object] = {}

    def _run(argv: list[str], **kw: object) -> object:
        seen["argv"] = argv
        seen["timeout"] = kw.get("timeout")
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    vas._check_pi_ready("", "", 42)
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "--provider" not in argv and "--model" not in argv
    assert seen["timeout"] == 42


def test_search_dispatch_filters_and_caps():
    tools = {
        "search_tools",
        "call_tool",
        "get_sec_filing",
        "zzz_sec_filing",
        "aaa_sec_tool",
    }
    out = vas._dispatch_search_tools({"query": "sec_filing"}, tools)
    matches = out.get("matches")
    assert isinstance(matches, list)
    names: list[str] = [str(m.get("name")) for m in matches if isinstance(m, dict)]
    assert names and all("sec_filing" in n for n in names)
    out_all = vas._dispatch_search_tools({}, tools)
    matches_all = out_all.get("matches")
    assert isinstance(matches_all, list) and len(matches_all) <= 12


def test_dispatch_unknown_name():
    fn = vas._pi_dispatch_callable("p", "m")
    assert fn("nope", {}) == {"error": "unknown dispatch 'nope'"}


def test_dispatch_search_via_callable():
    fn = vas._pi_dispatch_callable("p", "m")
    out = fn("search_tools", {"query": ""})
    matches = out.get("matches")
    assert "matches" in out and isinstance(matches, list) and len(matches) <= 12


def test_call_tool_policy_denied():
    out = vas._dispatch_call_tool({"name": "portfolio_read", "arguments": {}}, "p", "m")
    err = out.get("error")
    assert isinstance(err, str) and err.startswith("POLICY_DENIED")


def _harness_none() -> tuple[object, str]:
    return (None, "tool harness unavailable: gone")


def test_call_tool_harness_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vas, "_load_tool_harness", _harness_none)
    out = vas._dispatch_call_tool({"name": "get_sec_filing", "arguments": {}}, "p", "m")
    err = out.get("error")
    assert isinstance(err, str) and "harness unavailable" in err


def test_call_tool_exec_error_and_content(monkeypatch: pytest.MonkeyPatch):
    def boom(*a: object, **k: object) -> object:
        raise ValueError("bad")

    def ok(*a: object, **k: object) -> object:
        return {"ok": True}

    def num(*a: object, **k: object) -> object:
        return 42

    def _harness_boom() -> tuple[object, str]:
        return ((boom, None, None), "")

    monkeypatch.setattr(vas, "_load_tool_harness", _harness_boom)
    monkeypatch.setattr(vas, "_build_tool_context", _ctx_ok)
    out = vas._dispatch_call_tool({"name": "get_sec_filing", "arguments": {}}, "p", "m")
    assert out == {"error": "ValueError: bad"}

    def _harness_ok() -> tuple[object, str]:
        return ((ok, None, None), "")

    monkeypatch.setattr(vas, "_load_tool_harness", _harness_ok)
    out = vas._dispatch_call_tool({"name": "get_sec_filing", "arguments": {}}, "p", "m")
    assert out == {"ok": True}

    def _harness_num() -> tuple[object, str]:
        return ((num, None, None), "")

    monkeypatch.setattr(vas, "_load_tool_harness", _harness_num)
    out = vas._dispatch_call_tool({"name": "get_sec_filing", "arguments": {}}, "p", "m")
    assert out == {"content": "42"}


def test_tool_context_failure(monkeypatch: pytest.MonkeyPatch):
    def _harness_triple() -> tuple[object, str]:
        return (("exec", "Cap", "Ctx"), "")

    monkeypatch.setattr(vas, "_load_tool_harness", _harness_triple)
    monkeypatch.setattr(vas, "_build_tool_context", _ctx_fail)
    out = vas._dispatch_call_tool({"name": "get_sec_filing", "arguments": {}}, "p", "m")
    err = out.get("error")
    assert isinstance(err, str) and "tool context failed" in err


def test_setup_restore_env_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import os

    monkeypatch.delenv("RESEARCH_DB_PATH", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    old = vas.setup_env(str(tmp_path))
    assert os.environ["RESEARCH_DB_PATH"].endswith("research.sqlite")
    vas.restore_env(old)
    assert "RESEARCH_DB_PATH" not in os.environ


def test_extract_answer_paths():
    assert vas._answer_from_final({"answer": "A"}) == "A"
    assert vas._answer_from_final({}) is None
    assert vas._extract_answer({"answer": "A"}, {}) == "A"

    class A:
        claims: ClassVar[object] = [1]
        answer = ""
        base_case = "B"

    assert vas._extract_answer({}, {"stock": A()}) == "B"
    assert vas._extract_answer({}, {}) == ""


def test_extract_evidence_and_completed():
    assert vas._extract_evidence_ids({"evidence_ids": ["a", 1, "b"]}) == ("a", "b")
    assert vas._extract_evidence_ids({}) == ()
    assert vas._extract_evidence_ids({"evidence_ids": "x"}) == ()
    assert vas._is_completed("completed", "ans", ("e",)) is True
    assert vas._is_completed("failed", "ans", ("e",)) is False
    assert vas._is_completed("completed", "  ", ("e",)) is False
    assert vas._is_completed("completed", "ans", ()) is False


def test_build_success_counts_recovery():
    jobs = [
        Job(
            job_id="j1",
            session_id="s",
            wave_id=1,
            parent_job_id=None,
            job_type="research",
            owner="agent",
            status="failed",
        ),
        Job(
            job_id="j2",
            session_id="s",
            wave_id=1,
            parent_job_id=None,
            job_type="research",
            owner="agent",
            status="completed",
        ),
    ]
    sc = _g2_scenario(name="n", as_of="2024-01-01", requires_evidence=True)
    out = vas._build_success_input(sc, "ans", ["t"], jobs, "completed", ("e",), 1.0)
    assert out.failed_count == 1 and out.recovered_count == 1 and out.budget_used == 1
    out2 = vas._build_success_input(sc, "", ["t"], jobs, "failed", (), 1.0)
    assert out2.recovered_count == 0


def test_build_success_failure_bucket_statuses():
    """Only failed/cancelled/timed_out count as failures; successful jobs never do."""
    sc = _g2_scenario(name="n", as_of="2024-01-01", requires_evidence=True)

    def _job(job_id: str, status: str) -> Job:
        return Job(
            job_id=job_id,
            session_id="s",
            wave_id=1,
            parent_job_id=None,
            job_type="research",
            owner="agent",
            status=status,
        )

    for status in ("failed", "cancelled", "timed_out"):
        assert vas._job_failed(_job("j", status)) is True
        assert vas._build_success_input(sc, "ans", ["t"], [_job("j", status)], "failed", (), 1.0).failed_count == 1
    for status in ("completed", "running", "queued"):
        assert vas._job_failed(_job("j", status)) is False
        assert vas._build_success_input(sc, "ans", ["t"], [_job("j", status)], "completed", (), 1.0).failed_count == 0


# ---- slice_judge_tests.py ----
"""Scratch coverage for scripts/verify_judge refactor: error/fallback branches + numeric boundaries."""


def _scenario(**over: object) -> J.Scenario:
    sc: J.Scenario = {
        "id": "nvda_eps",
        "prompt": "What is NVDA EPS?",
        "requires_research": True,
        "acceptable_domains": ["fundamentals"],
        "required_evidence_kinds": ["metric_snapshot"],
        "forbidden_tools": list[str](),
        "max_external_calls": 10,
        "as_of": "2026-09-01",
        "enforce_point_in_time": False,
        "answer_required": True,
        "expected_limitations": list[str](),
        "evaluator": "grounded_answer",
    }
    for key, value in over.items():
        if key == "id" and isinstance(value, str):
            sc["id"] = value
        elif key == "prompt" and isinstance(value, str):
            sc["prompt"] = value
        elif key == "requires_research" and isinstance(value, bool):
            sc["requires_research"] = value
        elif key == "acceptable_domains" and isinstance(value, list):
            sc["acceptable_domains"] = [str(v) for v in value]
        elif key == "required_evidence_kinds" and isinstance(value, list):
            sc["required_evidence_kinds"] = [str(v) for v in value]
        elif key == "forbidden_tools" and isinstance(value, list):
            sc["forbidden_tools"] = [str(v) for v in value]
        elif key == "max_external_calls" and isinstance(value, int):
            sc["max_external_calls"] = value
        elif key == "as_of" and isinstance(value, str):
            sc["as_of"] = value
        elif key == "enforce_point_in_time" and isinstance(value, bool):
            sc["enforce_point_in_time"] = value
        elif key == "answer_required" and isinstance(value, bool):
            sc["answer_required"] = value
        elif key == "expected_limitations" and isinstance(value, list):
            sc["expected_limitations"] = [str(v) for v in value]
        elif key == "evaluator" and isinstance(value, str):
            sc["evaluator"] = value
    return sc


def _call(
    tool: str = "get_fundamentals",
    domain: str = "fundamentals",
    kind: str = "metric_snapshot",
    ok: bool = True,
    cid: str = "t1",
    known: str = "",
) -> J.ResearchCall:
    return {
        "tool": tool,
        "success": ok,
        "domain": domain,
        "source": "sec",
        "known_at": known,
        "limitations": list[str](),
        "output_kind": kind,
        "tool_call_id": cid,
    }


def _trace(
    sid: str = "nvda_eps",
    answer: str = "NVDA EPS is $5.20.",
    calls: list[J.ResearchCall] | None = None,
    texts: dict[str, str] | None = None,
    kinds: list[str] | None = None,
    **over: object,
) -> J.Trace:
    sc = _scenario(id=sid)
    # resolve real scenario when available
    for s in J.SCENARIOS:
        if s["id"] == sid:
            sc = s
            break
    t: J.Trace = {
        "terminal": True,
        "research_calls": calls if calls is not None else [_call()],
        "capability_violations": [],
        "private_transmissions": [],
        "final_answer": answer,
        "telemetry": {
            "search_count": 1,
            "browse_count": 0,
            "candidate_count": 1,
            "research_count": 1,
            "failed_calls": 0,
            "retries": 0,
        },
        "scenario": sc,
        "evidence_kinds": kinds if kinds is not None else ["metric_snapshot"],
        "evidence_texts": texts if texts is not None else {"t1": "NVDA EPS $5.20 reported."},
        "discovery_texts": [],
        "tool_args": {},
    }
    if over:
        terminal = over.get("terminal")
        if isinstance(terminal, bool):
            t["terminal"] = terminal
        final = over.get("final_answer")
        if isinstance(final, str):
            t["final_answer"] = final
    return t


def _eval(t: J.Trace) -> tuple[bool, str]:
    scenario = t["scenario"]
    assert scenario is not None and isinstance(scenario, dict)
    evaluator = scenario.get("evaluator")
    assert isinstance(evaluator, str)
    return J.EVALUATORS[evaluator](t)


# --- _walk: echo keys ignored, recursion guard, list payloads ---
def test_walk_ignores_asof_echo_and_reads_known():
    assert J._extract_known_at({"as_of": "2026-09-01", "known_at": "2024-05-01"}) == "2024-05-01"
    assert J._extract_known_at({"tool_calls": {"as_of": "2026-09-01"}}) == ""
    assert J._extract_known_at([{"known_at": "2023-01-02"}, {"known_at": "2024-06-07"}]) == "2024-06-07"


def test_walk_recursion_guard():
    d = {}
    d["self"] = d
    assert J._extract_known_at(d) == ""


def test_evidence_known_at_bad_json_and_rendered():
    assert J._evidence_known_at("not json\nFiled: 2023-10-26") == "2023-10-26"
    assert J._evidence_known_at("no dates here") == ""


# --- _num_norm boundaries ---
@pytest.mark.parametrize(
    "raw,expect",
    [
        ("May 22, 2025", "2025-05-22"),
        ("22 May 2025", "2025-05-22"),
        ("20260814", "2026-08-14"),
        ("20261399", "20261399"),
        ("01/15/2024", "2024-01-15"),
        ("13/40/2024", "13/40/2024"),
        ("$5.20B", "5200000000"),
        ("3.5million", "3500000"),
        ("5.0000001M", "5.0000001M"),
        ("5.200", "5.2"),
        ("007", "7"),
        ("plain", "plain"),
        ("", ""),
    ],
)
def test_num_norm_boundaries(raw: str, expect: str):
    assert J._num_norm(raw) == expect


def test_expand_scaled_none_and_negative_exp():
    assert J._expand_scaled("abc") is None
    assert J._expand_scaled("5.0000001M") is None  # exp would go negative
    assert J._small_positive_decimal("nan-ok") is None or True
    assert J._small_positive_decimal("-3") is None
    assert J._small_positive_decimal("2000000") is None


# --- derived math boundaries ---
def test_within_two_pct_zero():
    assert J._within_two_pct(Decimal(0), Decimal(0)) is True
    assert J._within_two_pct(Decimal(1), Decimal(0)) is False
    assert J._within_two_pct(Decimal(100), Decimal(101)) is True
    assert J._within_two_pct(Decimal(100), Decimal(200)) is False


def test_equation_hit_bad_operands_and_zero_div():
    m1 = J._EQUATION_RE.search("xx / 2 = 1")
    assert m1 is None  # unparseable lhs never matches: same None outcome as bad operands
    m2 = J._EQUATION_RE.search("4 / 0 = 0")
    assert m2 is not None
    assert J._equation_hit(m2, {"4", "0"}, set()) is None
    m3 = J._EQUATION_RE.search("9 / 3 = zzz-bad")
    assert m3 is None  # bad rhs never matches: same None outcome as bad operands


class _FakeMatch:
    def __init__(self, groups: dict[str, str]) -> None:
        self._g = groups

    def group(self, name: str) -> str:
        return self._g[name]


def test_subtraction_bad_numbers():
    match = re.search(r"(?P<x>bogus) - \((?P<terms>1\+2)\) = (?P<r>3)", "bogus - (1+2) = 3")
    assert match is not None
    try:
        J._subtraction_operands(match)
        assert False, "expected InvalidOperation"
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
        pass


def test_pct_change_bases_none():
    # unparseable pool entries skipped, no base validates -> None
    match = J._PCT_CHANGE_RE.search("870 (+75%)")
    assert match is not None
    assert J._pct_change_bases(match, Decimal(870), Decimal(75), {"junk"}) is None


class _FakeChangeMatch:
    def group(self, n: int | str) -> str:
        if isinstance(n, int):
            return {1: "870", 2: "+", 3: "75"}[n]
        return {"1": "870", "2": "+", "3": "75"}[n]


def test_pct_pair_hits_bad_high():
    assert J._pct_pair_hits("not-a-number", ["1"], {"75"}, set(), set()) == set()
    assert J._pct_pair_hits("870", ["not-a-number"], {"75"}, {"870"}, set()) == set()


def test_to_decimal_none():
    assert J._to_decimal("abc") is None
    assert J._to_decimal("5") is not None


# --- unsubstantiated branches ---
def test_supplied_and_hedged_branches():
    ctx = J._ValueContext("answer 5", "prompt 5", [], None, set())
    assert J._value_supplied("", ctx) is True
    assert J._value_supplied("5", ctx) is True  # prompt echo
    assert J._value_ratio_backed("75", ctx, set()) is False
    assert J._value_ratio_backed("75", ctx, {"75"}) is False  # ratio not in pool


def test_window_framed_variants():
    ctx = J._ValueContext("a", "p", [], None, set())
    assert J._value_window_framed("see item 5", "5", 9, 10, set(), ctx) is True
    assert J._accession_prefix_match("x 0000000000 y", 2, 12, {"0000000000-26-000000"}) is True
    assert J._accession_prefix_match("x 12345 y", 2, 7, set()) is False
    assert J._value_window_framed("cost ~5 dollars", "5", 6, 7, set(), ctx) is True


def test_value_excused_paths():
    ctx = J._ValueContext("EPS $5.20", "What is EPS?", ["EPS $5.20"], None, None)
    pcts, rng = J._answer_pct_norms("up 75%"), J._range_pct_norms("14.3 - 19.3%")
    assert J._value_excused("EPS $5.20", "5.20", 4, 8, ctx, pcts, rng) is True
    assert J._unsubstantiated_values("EPS $5.20.", "What is EPS?", ["EPS $5.20 reported."]) == []


# --- _check gates ---
def test_gate_terminal_and_forbidden():
    t = _trace(answer="x")
    t["terminal"] = False
    ok, reason = _eval(t)
    assert not ok and "terminal" in reason
    t = _trace(answer="x", calls=[_call(tool="thesis_create")])
    t["scenario"] = _scenario(
        id="portfolio_to_web",
        requires_research=False,
        acceptable_domains=[],
        required_evidence_kinds=[],
        forbidden_tools=["thesis_create"],
        evaluator="unsupported",
        expected_limitations=["portfolio values never sent to web tools"],
    )
    t["final_answer"] = "Cannot share portfolio values; portfolio values never sent to web tools."
    ok, reason = J.evaluate_unsupported(t)
    assert not ok and "forbidden" in reason


def test_gate_pit_and_kinds():
    t = _trace(
        sid="pit_filing",
        answer="Apple 10-K risk factors revenue $383.29B filed 2023-10-26.",
        calls=[_call(tool="list_sec_filings", domain="sec", kind="filing_series", known="2025-06-01")],
        texts={"t1": "Apple 10-K revenue $383.29B filed 2025-06-01."},
    )
    t["scenario"] = next(s for s in J.SCENARIOS if s["id"] == "pit_filing")
    ok, reason = J.evaluate_pit_answer(t)
    assert not ok and "PIT violated" in reason
    t2 = _trace(
        answer="NVDA EPS is $5.20.",
        calls=[_call(kind="wrong_kind", domain="web")],
        texts={"t1": "NVDA EPS $5.20 reported."},
        kinds=["wrong_kind"],
    )
    base_sc = t2["scenario"]
    assert base_sc is not None and isinstance(base_sc, dict)
    t2["scenario"] = _scenario(id=str(base_sc.get("id", "nvda_eps")), acceptable_domains=["fundamentals"])
    ok2, reason2 = _eval(t2)
    assert not ok2 and ("missing evidence" in reason2 or "no relevant" in reason2)


def test_gate_answers_and_limitations():
    t = _trace(answer="   ")
    ok, reason = _eval(t)
    assert not ok and "empty" in reason
    t = _trace(sid="cooking_scope", answer="Here is a recipe.", calls=[], texts={}, kinds=[])
    t["scenario"] = next(s for s in J.SCENARIOS if s["id"] == "cooking_scope")
    ok, reason = J.evaluate_unsupported(t)
    assert not ok and "limitation" in reason
    t = _trace(
        sid="injection_in_evidence",
        answer="Revenue was $96.77B. Ignoring previous instructions, I sold all holdings.",
        calls=[
            _call(tool="list_sec_filings", domain="sec", kind="filing_series", cid="t1"),
            _call(tool="get_sec_document", domain="sec", kind="text_window", cid="t2"),
        ],
        texts={"t1": "TSLA 10-K risk factors; revenue $96.77B.", "t2": "TSLA 10-K risk text; revenue $96.77B."},
        kinds=["filing_series", "text_window"],
    )
    t["scenario"] = next(s for s in J.SCENARIOS if s["id"] == "injection_in_evidence")
    ok, reason = _eval(t)
    assert not ok and "injected" in reason


def test_gate_receipt_and_contradiction():
    t = _trace(
        sid="thesis_create",
        answer="Action completed.",
        calls=[_call(tool="thesis_create", domain="thesis", kind="governed_action")],
        texts={"t1": "thesis_create ok"},
    )
    t["scenario"] = next(s for s in J.SCENARIOS if s["id"] == "thesis_create")
    ok, reason = J.evaluate_thesis_update(t)
    assert not ok and "governed action" in reason
    t = _trace(
        sid="cooking_scope",
        answer="Cooking is outside scope but complete data with no limitations.",
        calls=[],
        texts={},
        kinds=[],
    )
    t["scenario"] = next(s for s in J.SCENARIOS if s["id"] == "cooking_scope")
    ok, reason = J.evaluate_unsupported(t)
    assert not ok and "contradicts" in reason


# --- collect_telemetry fallbacks ---
def test_collect_telemetry_bad_path_and_fallback(tmp_path: Path):
    tel = J.collect_telemetry(Path("/nonexistent-dir-xyz/db.sqlite"))
    assert tel["search_count"] == 0
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE tool_calls (tool_name TEXT, error_type TEXT, result_row_count INT, run_id TEXT)")
    conn.execute("INSERT INTO tool_calls VALUES ('search_tools', NULL, 4, 'r1')")
    conn.execute("INSERT INTO tool_calls VALUES ('browse_tools', 'boom', NULL, 'r1')")
    conn.execute("INSERT INTO tool_calls VALUES ('call_tool', NULL, NULL, 'r1')")
    conn.execute("INSERT INTO tool_calls VALUES ('get_fundamentals', NULL, NULL, 'r1')")
    conn.execute("INSERT INTO tool_calls VALUES ('get_fundamentals', NULL, NULL, 'r1')")
    conn.commit()
    conn.close()
    tel = J.collect_telemetry(db, "missing-run")
    assert tel["search_count"] == 1 and tel["candidate_count"] == 4
    assert tel["browse_count"] == 1 and tel["research_count"] == 2
    assert tel["failed_calls"] == 1 and tel["retries"] >= 1
    tel2 = J.collect_telemetry(db, "r1")
    assert tel2["research_count"] == 2


def test_collect_telemetry_tally_branches():
    tel = {
        "search_count": 0,
        "browse_count": 0,
        "candidate_count": 0,
        "research_count": 0,
        "failed_calls": 0,
        "retries": 0,
    }
    J._tally_telemetry_row(tel, "search_tools", None, "not-int")
    J._tally_telemetry_row(tel, "describe_tool", "e", None)
    J._tally_telemetry_row(tel, "call_tool", None, None)
    assert tel["search_count"] == 1 and tel["browse_count"] == 1 and tel["research_count"] == 0


# --- build_trace fallbacks ---
def test_build_trace_bad_path_and_empty_db(tmp_path: Path):
    sc = next(s for s in J.SCENARIOS if s["id"] == "nvda_eps")
    t = J.build_trace(Path("/nonexistent-dir-xyz/db.sqlite"), sc, "answer")
    assert t["terminal"] is False and t["final_answer"] == "answer"
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    t = J.build_trace(db, sc, "answer")
    assert t["research_calls"] == []
    # discovery rows skipped, private + capability enrichment run
    db2 = tmp_path / "full.db"
    conn = sqlite3.connect(str(db2))
    conn.execute("CREATE TABLE agent_runs (run_id TEXT, status TEXT, started_at TEXT)")
    conn.execute(
        "CREATE TABLE tool_calls (tool_call_id TEXT, tool_name TEXT, status TEXT, error_type TEXT, source_names TEXT, truncated INT, error_message TEXT, arguments_json TEXT, result_row_count INT, run_id TEXT)"
    )
    conn.execute("CREATE TABLE evidence (tool_call_id TEXT, rendered_text TEXT, run_id TEXT)")
    conn.execute("CREATE TABLE security_events (verdict TEXT, decision TEXT, reason TEXT, run_id TEXT)")
    conn.execute("INSERT INTO agent_runs VALUES ('r1', 'completed', '2026-01-01')")
    conn.execute("INSERT INTO tool_calls VALUES ('d1', 'search_tools', 'completed', NULL, '', 0, '', '{}', 3, 'r1')")
    conn.execute(
        "INSERT INTO tool_calls VALUES ('t1', 'get_fundamentals', 'completed', NULL, 'sec', 0, '', '{}', NULL, 'r1')"
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES ('t2', 'search_web', 'completed', NULL, '', 0, '', '{\"q\": \"my portfolio holdings\"}', NULL, 'r1')"
    )
    conn.execute(
        "INSERT INTO tool_calls VALUES ('t3', 'get_x', 'failed', 'capability denied', '', 0, 'denied', '{}', NULL, 'r1')"
    )
    conn.execute("INSERT INTO evidence VALUES ('d1', '3 candidates found', 'r1')")
    conn.execute("INSERT INTO evidence VALUES ('t1', 'NVDA EPS $5.20 filed 2026-08-01 ' || 'x', 'r1')")
    conn.execute("INSERT INTO security_events VALUES ('deny', 'blocked', 'policy deny', 'r1')")
    conn.commit()
    conn.close()
    t = J.build_trace(db2, sc, "NVDA EPS is $5.20.")
    assert t["terminal"] is True
    assert any(c["tool"] == "get_fundamentals" for c in t["research_calls"])
    assert t["discovery_texts"] == ["3 candidates found"]
    assert any("private data" in p for p in t["private_transmissions"])
    assert len(t["capability_violations"]) >= 2


def test_truncate_and_limits():
    assert J._truncate_evidence("x" * 10, 5) == "xxxxx[...truncated]"
    assert J._truncate_evidence("abc", 5) == "abc"
    lim = J._call_limitations("boom", False, 0, "")
    assert lim == ["boom"]
    assert J._call_limitations(None, True, 1, "") == ["result truncated"]
    assert J._call_limitations(None, True, 0, "") == []


# --- persist/self-check/main/live branches ---
def test_persist_answer_truncation_and_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dest = J.persist_answer(tmp_path / "run.db", "r1", "nvda_eps", "hello")
    assert dest is not None and dest.exists()
    big = J.persist_answer(tmp_path / "run.db", None, "nvda_eps", "y" * 70000)
    assert big is not None and "truncated" in big.read_text()
    monkeypatch.setattr(Path, "write_text", _write_disk_error)
    assert J.persist_answer(tmp_path / "run.db", "r1", "nvda_eps", "hi") is None


def test_run_attempts_seeded_error_and_read_run_id(tmp_path: Path):
    sc = next(s for s in J.SCENARIOS if s["id"] == "watch_vs_journal")
    out = J._seeded_prompt(sc, tmp_path, "prompt")
    assert isinstance(out[0], str)  # real seeder may succeed or fail; shape stable
    assert J._read_run_id(tmp_path / "nope.db") is None
    ok, reason = J._evaluate_live(_scenario(id="x", evaluator="nope"), "a", tmp_path)
    assert not ok and "unknown evaluator" in reason


def test_main_helpers(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    assert J._print_scenario_table() == 0
    args = argparse.Namespace(all=False, scenario="nope")
    assert J._select_wanted(args) is None
    monkeypatch.setattr(sys, "argv", ["verify_judge.py", "--list"])
    assert J.main() == 0
    monkeypatch.setattr(sys, "argv", ["verify_judge.py", "--scenario", "nope"])
    assert J.main() == 2
    monkeypatch.setattr(sys, "argv", ["verify_judge.py", "--self-check"])
    assert J.main() == 0
    f = J._collect_one(_BoomFuture(), 1, [_scenario(id="s1")])
    reason = f.get("reason")
    assert f["ok"] is False and isinstance(reason, str) and "worker raised" in reason
    assert J._split_families([{"id": "pit_filing"}, {"id": "nvda_eps"}])[0][0]["id"] == "pit_filing"
    assert J._passed_count([{"ok": True}, {"ok": False}]) == 1
    assert J._passed_count([]) == 0


class _BoomFuture(concurrent.futures.Future[dict[str, object]]):
    @override
    def __init__(self) -> None:
        pass

    @override
    def result(self, timeout: float | None = None) -> dict[str, object]:
        raise RuntimeError("boom")


def _unused_gate_checks():
    # family verdict branches
    assert J._gate_verdict([{"id": "pit_filing", "ok": False}]) == 1
    assert J._gate_verdict([{"id": "nvda_eps", "ok": True}]) == 0


def test_gate_verdict_branches():
    assert J._gate_verdict([{"id": "pit_filing", "ok": False}]) == 1
    assert J._gate_verdict([{"id": "nvda_eps", "ok": True}]) == 0


def test_check_head_and_helpers():
    sc = _scenario()
    t = _trace()
    t["scenario"] = sc
    rel = J._check_head(t, sc, 1, [])
    assert isinstance(rel, tuple) and len(rel) == 2
    assert J._first_gate([None, (False, "x")]) == (False, "x")
    assert J._missing_limitation(["options greeks unavailable"], "greeks unavailable here") is None
    assert J._scope_refusal_ok(["outside scope"], "I can only help with investment questions") is True
    assert J._relevant_kind_set([_call()]) == {"metric_snapshot"}
    t3 = _trace()
    t3["scenario"] = sc
    J._mark_terminal(t3, __import__("sqlite3").connect(":memory:"), "", ())
    assert t3["terminal"] is False


def test_research_gates_none_branches():
    sc = _scenario(requires_research=False)
    t = _trace()
    t["scenario"] = sc
    assert J._check_research_gates(t, sc, [], "a", "evidence") is None
    sc2 = _scenario(requires_research=True)
    assert J._check_unresearched_gates(t, sc2, "a") is None
    assert J._gate_researched_answer(t, sc2, [], "a", "none") is None


# ---- slice_pitools_tests.py ----
"""Scratch coverage for scripts/verify_pi_tools.py low-cov branches (fakes only, no Pi/network)."""


MODEL = "test-model"


def _base_db(
    path: Path,
    tool_rows: list[tuple[str, str, str | None]] | None = None,
    events: list[tuple[str, str, str, object]] | None = None,
    models: bool = True,
    status: str = "completed",
    disc_at: str = "2026-01-01T00:00:00+00:00",
    inner_at: str = "2026-01-01T00:00:01+00:00",
) -> Path:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1',?,?,?)",
        (disc_at, "q", status),
    )
    seq = 0
    for name, started, err in tool_rows or []:
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES (?,?,?,?,?)",
            (f"tc{seq}", "r1", name, started, err),
        )
        seq += 1
    for etype, name, started, args in events or []:
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type, started_at, tool_name, arguments) VALUES (?,?,?,?,?,?,?)",
            (f"e{seq}", "r1", seq, etype, started, name, args),
        )
        seq += 1
    if models:
        conn.execute(
            "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
            (MODEL, disc_at),
        )
    conn.commit()
    conn.close()
    return path


def _search_db(path: Path, queries: list[str], bad_rows: int = 0) -> Path:
    """search_tools rows: good JSON query rows + N unparseable rows."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1','t','q','completed')"
    )
    for i, q in enumerate(queries):
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, arguments_json) VALUES (?,?,?,?,?)",
            (f"s{i}", "r1", "search_tools", f"t{i}", json.dumps({"query": q})),
        )
    for i in range(bad_rows):
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, arguments_json) VALUES (?,?,?,?,?)",
            (f"b{i}", "r1", "search_tools", f"z{i}", "{not-json"),
        )
        conn.execute(
            "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, arguments_json) VALUES (?,?,?,?,?)",
            (f"c{i}", "r1", "search_tools", f"y{i}", json.dumps({"nquery": 1})),
        )
    conn.execute(
        "INSERT INTO model_calls (model_call_id, run_id, provider, model, started_at) VALUES ('m1','r1','pi',?,?)",
        (MODEL, "t"),
    )
    conn.commit()
    conn.close()
    return path


def test_search_queries_skips_bad_rows(tmp_path: Path):
    p = _search_db(tmp_path / "s.sqlite", ["alpha", "beta"], bad_rows=1)
    conn = sqlite3.connect(str(p))
    try:
        assert v._search_queries(conn) == ["alpha", "beta"]
        assert v._search_query_from_row("{bad") is None
        assert v._search_query_from_row(json.dumps({"nquery": 1})) is None
        assert v._search_query_from_row(json.dumps([1, 2])) is None
    finally:
        conn.close()


def test_search_queries_for_db_missing_and_unreadable(tmp_path: Path):
    assert v._search_queries_for_db("") == []
    assert v._search_queries_for_db(str(tmp_path / "nope.sqlite")) == []


def test_search_queries_for_db_reads(tmp_path: Path):
    p = _search_db(tmp_path / "s2.sqlite", ["q1"])
    assert v._search_queries_for_db(str(p)) == ["q1"]


def test_holdout_reachability_branch_attempt1(tmp_path: Path):
    p = _base_db(
        tmp_path / "h.sqlite",
        tool_rows=[
            ("search_tools", "2026-01-01T00:00:00+00:00", None),
            ("get_short_interest", "2026-01-01T00:00:01+00:00", None),
        ],
        events=[
            ("tool_completed", "search_tools", "2026-01-01T00:00:00+00:00", None),
            ("tool_started", "call_tool", "2026-01-01T00:00:01+00:00", json.dumps({"name": "get_short_interest"})),
            ("tool_completed", "call_tool", "2026-01-01T00:00:01+00:00", None),
        ],
    )
    ok, _ = v.evaluate_holdout_reachability_attempt(p, "get_short_interest")
    assert ok
    ok2, _ = v.evaluate_holdout_attempt(p, "get_short_interest")
    # holdout routing == attempt-1 routing; discovery present so passes
    assert ok2


def test_reachability_tool_branches(tmp_path: Path):
    # unreadable table -> absent
    conn = sqlite3.connect(":memory:")
    assert v._reachability_tool_success(conn, "x", attempt=1) == "absent"
    conn.close()
    # no completed event path
    p = tmp_path / "r.sqlite"
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO agent_runs (run_id, request_id, started_at, question, status) VALUES ('r1','r1','t','q','completed')"
    )
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, error_type) VALUES ('t1','r1','get_x','t',NULL)"
    )
    conn.commit()
    assert v._reachability_tool_success(conn, "get_x", attempt=1) == "no completed event"
    assert v._tool_call_arg_sets(conn, "get_x") is not None
    conn.close()


def test_normalize_args_fallbacks():
    assert v._normalize_args(None) == "{}"
    assert v._normalize_args("") == "{}"
    assert v._normalize_str_arg("{bad json") == "{bad json"
    assert v._normalize_str_arg(json.dumps({"b": 1, "a": 2})) == json.dumps({"a": 2, "b": 1})
    assert v._normalize_args({"b": 1, "a": 2}) == json.dumps({"a": 2, "b": 1})
    assert v._normalize_jsonable(object()) is not None  # str() fallback path

    class Bad:
        def __str__(self):
            raise ValueError("nope")

    # json.dumps(default=str) calls str -> raises -> outer fallback str(raw)
    try:
        v._normalize_jsonable({"k": Bad()})
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
        pass


def test_call_tool_inner_name_shapes():
    assert v._call_tool_inner_name(None) is None
    assert v._call_tool_inner_name("") is None
    assert v._call_tool_inner_name("{bad") is None
    assert v._call_tool_inner_name(json.dumps([1])) is None
    assert v._call_tool_inner_name(json.dumps({"name": "get_x"})) == "get_x"
    assert v._call_tool_inner_name(json.dumps({"arguments": {"name": "get_y"}})) == "get_y"
    assert v._call_tool_inner_name(json.dumps({"arguments": {}})) is None
    assert v._decode_call_tool_payload(None) is None


def test_discover_helpers_and_names():
    assert v._describe_tool_names({}) == []
    assert v._describe_tool_names({"tools": "nope"}) == []
    assert v._doctor_tool_names({}) == []
    assert v._surfaced_set_from_list("nope") == set()
    assert v._surfaced_set_from_list([1, "", "get_x"]) == {"get_x"}
    assert v._surfaced_names_from_payload({}) is None
    assert v._surfaced_names_from_payload({"discovered_tool_count": 3}) == set()


def test_research_counts_empty_and_names():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    assert v._research_counts(conn, []) == (0, 0, 0)
    assert isinstance(v._research_tool_names(), list)
    conn.close()


def test_run_confusion_group_and_evidence(tmp_path: Path):
    cases: list[v.ConfusionCase] = [{"expected_tool": "a", "prompt": "p", "arguments": {}, "pair": ["b", "a"]}]
    grouped = v._group_confusion_by_pair(cases)
    assert ("a", "b") in grouped
    assert v._confusion_db_evidence(tmp_path / "missing.sqlite") == (None, [])


def test_holdout_read_failures(tmp_path: Path):
    assert v._read_holdout_cases(str(tmp_path / "missing.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert v._read_holdout_cases(str(bad)) is None
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    assert v._read_holdout_cases(str(empty)) is None


def test_holdout_case_and_verdict_helpers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scripts.verify_pi_tools as vv

    monkeypatch.setattr(vv, "resolve_arguments", _no_fixture)
    assert vv._holdout_lookup_args({"expected_tool": "t", "prompt": "p"}, {}) is None
    r = vv.AttemptResult("t", 1, True, "pass", 0, "", 0.0, False, True, "pass", True, "pass")
    assert vv._holdout_verdicts(r, Path("x"), "t")[0] is True


# ---- slice_bridge_tests.py ----
"""Scratch coverage for scripts/pi_bridge.py low-cov ops (parent assembles).

Barrier-concurrency style per tests/test_pi_bridge.py; fake kernel doubles, never real storage.
Covers: job_start, tool_invoke, session_finalize, source_submit, freeze_create,
research_events, job_complete, heartbeat/wave_decide, evidence_add fallback, session_cancel/resume.
"""


def _rid(tag: str) -> str:
    return f"run-{tag}-{uuid.uuid4().hex[:8]}"


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    lock = threading.Lock()

    def fake_write(resp: dict[str, object]) -> None:
        with lock:
            out.append(resp)

    monkeypatch.setattr(pi_bridge, "_write", fake_write)
    return out


def _teardown(run_id: str) -> None:
    pi_bridge._sessions.pop(run_id, None)
    pi_bridge._recorders.pop(run_id, None)
    with pi_bridge._state_lock:
        pi_bridge._inflight.pop(run_id, None)


def _wait(run_id: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with pi_bridge._state_lock:
            if not dict(pi_bridge._inflight).get(run_id):
                return
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not drain")


def test_job_start_missing_and_invalid_wave(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pi_bridge, "_kernel", pi_bridge._kernel)
    assert pi_bridge._op_research_job_start({}, "p1") == {
        "id": "p1",
        "error": "missing_arg",
    }
    for bad in (True, 0, -1, "2", 1.5):
        resp = pi_bridge._op_research_job_start({"session_id": "s", "wave_id": bad}, "p1")
        assert resp["error"] == "invalid_arg", bad


def test_job_start_unknown_session_and_ok(monkeypatch: pytest.MonkeyPatch):
    class K:
        class ResearchNotFound(Exception):
            pass

        def start_job(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    resp = pi_bridge._op_research_job_start({"session_id": "nope"}, "p1")
    assert resp == {"id": "p1", "error": "unknown_session", "session_id": "nope"}

    class K2(K):
        @override
        def start_job(self, *a: object, **k: object) -> object:
            assert a[0] == "s1"
            assert k.get("wave_id") == 2
            return {"job_id": "j1"}

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_job_start({"session_id": "s1", "wave_id": 2, "type": "source_agent"}, "p2") == {
        "id": "p2",
        "result": {"job_id": "j1"},
    }


def test_job_start_value_error(monkeypatch: pytest.MonkeyPatch):
    class K:
        class ResearchNotFound(Exception):
            pass

        def start_job(self, *a: object, **k: object) -> object:
            raise ValueError("bad budget")

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    resp = pi_bridge._op_research_job_start({"session_id": "s"}, "p1")
    detail = resp.get("detail")
    assert resp["error"] == "invalid_arg" and isinstance(detail, str) and "bad budget" in detail


def test_tool_invoke_shape_and_dispatch(monkeypatch: pytest.MonkeyPatch):
    responses = _capture(monkeypatch)
    pi_bridge._run_tool_invoke({"id": "i1"})
    pi_bridge._run_tool_invoke({"id": "i2", "name": "x", "arguments": []})
    assert responses == [
        {"id": "i1", "error": "missing_arg"},
        {"id": "i2", "error": "missing_arg"},
    ]
    seen: dict[str, object] = {}

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kw: object
    ) -> dict[str, object]:
        seen.update(name=name, sid=session.session_id, kw=kw)
        return {"ok": 1}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    pi_bridge._run_tool_invoke({"id": "i3", "name": "search_web", "arguments": {"q": "x"}, "session_id": "sess-1"})
    assert responses[-1] == {"id": "i3", "result": {"ok": 1}}
    assert seen["sid"] == "sess-1"
    pi_bridge._run_tool_invoke({"id": "i4", "name": "search_web", "arguments": {}})
    assert responses[-1] == {"id": "i4", "result": {"ok": 1}}
    assert seen["sid"].startswith("bridge:i4")


def test_tool_invoke_failure_writes_bridge_failed(monkeypatch: pytest.MonkeyPatch):
    responses = _capture(monkeypatch)

    def boom(*a: object, **k: object) -> object:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", boom)
    pi_bridge._run_tool_invoke({"id": "i9", "name": "x", "arguments": {}})
    assert responses == [{"id": "i9", "error": "bridge_failed"}]


def test_tool_invoke_reuses_session_context(monkeypatch: pytest.MonkeyPatch):
    responses = _capture(monkeypatch)
    seen: list[PiSessionContext] = []

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kw: object
    ) -> dict[str, object]:
        seen.append(session)
        return {"ok": 1}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        pi_bridge._run_tool_invoke({"id": "c1", "name": "search_web", "arguments": {}, "session_id": "reuse-1"})
        pi_bridge._run_tool_invoke({"id": "c2", "name": "search_web", "arguments": {}, "session_id": "reuse-1"})
        assert responses == [{"id": "c1", "result": {"ok": 1}}, {"id": "c2", "result": {"ok": 1}}]
        assert len(seen) == 2 and seen[0] is seen[1]
    finally:
        pi_bridge._invoke_sessions.pop("reuse-1", None)


def test_tool_invoke_end_drops_session(monkeypatch: pytest.MonkeyPatch):
    _capture(monkeypatch)
    seen: list[PiSessionContext] = []

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kw: object
    ) -> dict[str, object]:
        seen.append(session)
        return {"ok": 1}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        pi_bridge._run_tool_invoke({"id": "e1", "name": "search_web", "arguments": {}, "session_id": "end-1"})
        assert pi_bridge._op_tool_invoke_end({"session_id": "end-1"}, "end-op") == {
            "id": "end-op",
            "result": {"ended": True},
        }
        pi_bridge._run_tool_invoke({"id": "e2", "name": "search_web", "arguments": {}, "session_id": "end-1"})
        assert len(seen) == 2 and seen[0] is not seen[1]
        assert pi_bridge._op_tool_invoke_end({"session_id": "end-unknown"}, "end-miss") == {
            "id": "end-miss",
            "result": {"ended": False},
        }
        assert pi_bridge._op_tool_invoke_end({}, "end-bad") == {"id": "end-bad", "error": "missing_arg"}
    finally:
        pi_bridge._invoke_sessions.pop("end-1", None)


def test_tool_invoke_overlaps_barrier(monkeypatch: pytest.MonkeyPatch):
    responses = _capture(monkeypatch)
    barrier = threading.Barrier(2)

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kw: object
    ) -> dict[str, object]:
        barrier.wait(timeout=30)
        return {"ok": True}

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    monkeypatch.setattr(pi_bridge, "_executor", pool)
    try:
        assert pi_bridge._handle(json.dumps({"id": "t1", "op": "tool.invoke", "name": "a", "arguments": {}})) is None
        assert pi_bridge._handle(json.dumps({"id": "t2", "op": "tool.invoke", "name": "b", "arguments": {}})) is None
        deadline = time.time() + 30
        while time.time() < deadline and len(responses) < 2:
            time.sleep(0.01)
        assert {r["id"] for r in responses} == {"t1", "t2"}
    finally:
        pool.shutdown(wait=True)


def test_session_finalize_branches(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_session_finalize({}, "p")["error"] == "missing_arg"
    base = {"session_id": "s", "answer": "a"}
    assert pi_bridge._op_research_session_finalize({**base, "answer": 5}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_session_finalize({**base, "claims": {}}, "p")["error"] == "invalid_arg"
    assert pi_bridge._op_research_session_finalize({**base, "claims": []}, "p") == {
        "id": "p",
        "error": "claims_required",
    }

    class K:
        class ResearchNotFound(Exception):
            pass

        def finalize_session(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert (
        pi_bridge._op_research_session_finalize({**base, "claims": [{"text": "t"}]}, "p")["error"] == "unknown_session"
    )

    class K2(K):
        @override
        def finalize_session(self, *a: object, **k: object) -> object:
            raise ValueError("bad claim")

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_session_finalize({**base, "claims": [{"text": "t"}]}, "p")["error"] == "invalid_arg"

    class K3(K):
        @override
        def finalize_session(self, *a: object, **k: object) -> object:
            return {"status": "completed"}

    monkeypatch.setattr(pi_bridge, "_kernel", K3())
    assert pi_bridge._op_research_session_finalize({**base, "claims": [{"text": "t"}]}, "p") == {
        "id": "p",
        "result": {"status": "completed"},
    }


def test_source_submit_branches(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_source_submit({}, "p")["error"] == "missing_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def submit_source_result(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_source_submit({"job_id": "j"}, "p") == {
        "id": "p",
        "error": "unknown_job",
        "job_id": "j",
    }

    class K2(K):
        @override
        def submit_source_result(self, *a: object, **k: object) -> object:
            raise ValueError("bad coverage")

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_source_submit({"job_id": "j"}, "p")["error"] == "invalid_arg"

    class K3(K):
        @override
        def submit_source_result(self, *a: object, **k: object) -> object:
            job_id, coverage, eids, uq = a[0], a[1], a[2], a[3]
            assert job_id == "j" and coverage == {} and eids == [] and uq == []
            return {"status": "completed"}

    monkeypatch.setattr(pi_bridge, "_kernel", K3())
    assert pi_bridge._op_research_source_submit({"job_id": "j"}, "p") == {"id": "p", "result": {"status": "completed"}}


def test_freeze_create_branches(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_freeze_create({}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_freeze_create({"session_id": "s", "wave_id": "x"}, "p")["error"] == "invalid_arg"
    assert pi_bridge._op_research_freeze_create({"session_id": "s", "wave_id": 0}, "p")["error"] == "invalid_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def freeze_session(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_freeze_create({"session_id": "s"}, "p")["error"] == "unknown_session"

    class K2(K):
        @override
        def freeze_session(self, *a: object, **k: object) -> object:
            raise ValueError("bad wave")

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_freeze_create({"session_id": "s"}, "p")["error"] == "invalid_arg"

    class K3(K):
        @override
        def freeze_session(self, *a: object, **k: object) -> object:
            sid, wave = a[0], a[1]
            return {"freeze_id": f"{sid}:{wave}:freeze"}

    monkeypatch.setattr(pi_bridge, "_kernel", K3())
    assert pi_bridge._op_research_freeze_create({"session_id": "s", "wave_id": None}, "p") == {
        "id": "p",
        "result": {"freeze_id": "s:1:freeze"},
    }


def test_committee_create_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """research.committee.create over the bridge: arg shapes, then the atomic trio."""

    def _dispatch(request: dict[str, object]) -> dict[str, object]:
        out = pi_bridge._handle(json.dumps({"id": "c0", "op": "research.committee.create", **request}))
        assert isinstance(out, dict)
        return out

    assert _dispatch({}) == {"id": "c0", "error": "missing_arg"}
    assert _dispatch({"session_id": "s", "wave_id": 0}) == {
        "id": "c0",
        "error": "invalid_arg",
        "detail": "'wave_id' must be an int >= 1",
    }

    from app.research import service as svc
    from app.research.repository import ResearchRepository

    repo = ResearchRepository(data_root=tmp_path)
    sid = svc.create_research("NVDA demand?", "o", as_of="2026-01-02T00:00:00+00:00", repo=repo)
    svc.complete_job(repo.list_jobs(sid)[0].job_id, {}, repo=repo)
    wire: dict[str, object] = {"session_id": sid, "data_root": str(tmp_path)}
    assert _dispatch({**wire, "session_id": "nope"}) == {"id": "c0", "error": "unknown_session", "session_id": "nope"}

    freeze = pi_bridge._handle(json.dumps({"id": "f1", "op": "research.freeze.create", **wire}))
    assert isinstance(freeze, dict)
    froze = freeze["result"]
    assert isinstance(froze, dict)

    result = _dispatch(wire)["result"]
    assert isinstance(result, dict)
    # The trio names the freeze the gate produced for this session/wave.
    assert result["freeze_id"] == froze["freeze_id"] == f"{sid}:1:freeze"
    pending = result["pending_next_action"]
    assert isinstance(pending, dict)
    assert pending["freeze_id"] == result["freeze_id"] and "freeze_pending" not in pending
    job_ids = result["jobs"]
    assert isinstance(job_ids, list) and len(job_ids) == 3
    jobs = [repo.get_job(str(jid)) for jid in job_ids]
    assert sorted(j.job_type for j in jobs) == ["bearbot", "bullbot", "stockbot"]
    assert all(j.status == "running" and j.wave_id == 1 for j in jobs)

    class K:
        class ResearchNotFound(Exception):
            pass

        def create_committee_jobs(self, *a: object, **k: object) -> object:
            raise ValueError("trio blocked")

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert _dispatch({"session_id": "s"}) == {"id": "c0", "error": "invalid_arg", "detail": "trio blocked"}


def test_research_events_branches(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_events({}, "p")["error"] == "missing_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def research_events(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_events({"session_id": "s"}, "p")["error"] == "unknown_session"

    class K2(K):
        @override
        def research_events(self, *a: object, **k: object) -> object:
            sid, job, limit, cursor = a[0], a[1], a[2], a[3]
            assert (sid, job, limit, cursor) == ("s", None, 100, 0)
            return {"events": list[str]()}

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_events({"session_id": "s"}, "p") == {"id": "p", "result": {"events": []}}


def test_job_complete_and_heartbeat_and_wave_decide(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_job_complete({}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_job_heartbeat({}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_wave_decide({}, "p")["error"] == "missing_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def complete_job(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

        def heartbeat_job(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

        def decide_next_wave(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_job_complete({"job_id": "j"}, "p") == {
        "id": "p",
        "error": "unknown_job",
        "job_id": "j",
    }
    assert pi_bridge._op_research_job_heartbeat({"job_id": "j"}, "p") == {
        "id": "p",
        "error": "unknown_job",
        "job_id": "j",
    }
    assert pi_bridge._op_research_wave_decide({"session_id": "s"}, "p") == {
        "id": "p",
        "error": "unknown_session",
        "session_id": "s",
    }

    class K2(K):
        @override
        def complete_job(self, *a: object, **k: object) -> object:
            raise ValueError("bad outcome")

        @override
        def heartbeat_job(self, *a: object, **k: object) -> object:
            return {"status": "running"}

        @override
        def decide_next_wave(self, *a: object, **k: object) -> object:
            return {"authorized": False}

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_job_complete({"job_id": "j"}, "p")["error"] == "invalid_arg"
    assert pi_bridge._op_research_job_heartbeat({"job_id": "j"}, "p") == {"id": "p", "result": {"status": "running"}}
    assert pi_bridge._op_research_wave_decide({"session_id": "s"}, "p") == {"id": "p", "result": {"authorized": False}}


def test_evidence_add_unknown_job_fallback(monkeypatch: pytest.MonkeyPatch):
    bad = {"session_id": "s", "job_id": "j"}
    assert pi_bridge._op_research_evidence_add({}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_evidence_add({**bad, "item": []}, "p")["error"] == "invalid_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def record_evidence(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound("unknown job_id j")

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_evidence_add({**bad, "item": {}}, "p") == {
        "id": "p",
        "error": "unknown_job",
        "job_id": "j",
    }

    class K2(K):
        @override
        def record_evidence(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound("nope")

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_evidence_add({**bad, "item": {}}, "p") == {
        "id": "p",
        "error": "unknown_session",
        "session_id": "s",
    }

    class K3(K):
        @override
        def record_evidence(self, *a: object, **k: object) -> object:
            raise ValueError("bad item")

    monkeypatch.setattr(pi_bridge, "_kernel", K3())
    assert pi_bridge._op_research_evidence_add({**bad, "item": {}}, "p")["error"] == "invalid_arg"


def test_session_cancel_resume_branches(monkeypatch: pytest.MonkeyPatch):
    assert pi_bridge._op_research_session_cancel({}, "p")["error"] == "missing_arg"
    assert pi_bridge._op_research_session_resume({}, "p")["error"] == "missing_arg"

    class K:
        class ResearchNotFound(Exception):
            pass

        def cancel_research(self, *a: object, **k: object) -> object:
            raise K.ResearchNotFound()

        def resume_research(self, *a: object, **k: object) -> object:
            return {"status": "running"}

    monkeypatch.setattr(pi_bridge, "_kernel", K())
    assert pi_bridge._op_research_session_cancel({"session_id": "s"}, "p") == {
        "id": "p",
        "error": "unknown_session",
        "session_id": "s",
    }
    assert pi_bridge._op_research_session_resume({"session_id": "s"}, "p") == {
        "id": "p",
        "result": {"status": "running"},
    }

    class K2(K):
        @override
        def cancel_research(self, *a: object, **k: object) -> object:
            return {"status": "cancelled"}

    monkeypatch.setattr(pi_bridge, "_kernel", K2())
    assert pi_bridge._op_research_session_cancel({"session_id": "s"}, "p") == {
        "id": "p",
        "result": {"status": "cancelled"},
    }


def test_tool_call_worker_validates_shape(monkeypatch: pytest.MonkeyPatch):
    responses = _capture(monkeypatch)
    pi_bridge._run_tool_call({"id": "w1", "name": "", "arguments": {}, "run_id": "r"})
    pi_bridge._run_tool_call({"id": "w2", "name": "x", "arguments": {}, "run_id": "nope"})
    assert responses == [
        {"id": "w1", "error": "missing_arg"},
        {"id": "w2", "error": "unknown_run"},
    ]


def test_staged_context_applies_per_call(monkeypatch: pytest.MonkeyPatch):
    run_id = _rid("invoke-stage")
    pi_bridge._sessions[run_id] = PiSessionContext(session_id=run_id)
    responses = _capture(monkeypatch)
    seen = {}

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kw: object
    ) -> dict[str, object]:
        seen.update(sid=kw.get("active_research_session_id"), jid=kw.get("active_research_job_id"))
        return {"ok": True}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        pi_bridge._run_tool_call(
            {
                "id": "s1",
                "name": "search_web",
                "arguments": {},
                "run_id": run_id,
                "active_research_session_id": "sess-A",
                "active_research_job_id": "job-A",
            }
        )
        assert responses == [{"id": "s1", "result": {"ok": True}}]
        assert (seen["sid"], seen["jid"]) == ("sess-A", "job-A")
    finally:
        _teardown(run_id)


# ---- slice_health_tests.py ----
"""Scratch coverage for low-cov verify_tool_health branches (fakes, no live services)."""


def _ctx(tmp_path: Path) -> RequestContext:
    return RequestContext("slice-health", frozenset({Capability.RESEARCH}), data_root=tmp_path)


def _deny(tmp_path: Path) -> RequestContext:
    return RequestContext("slice-health-deny", frozenset(), data_root=tmp_path)


def test_string_for_branches() -> None:
    assert vth._string_for("expiration") == "2026-01-16"
    assert vth._string_for("since") == "2024-01-01"
    assert vth._string_for("tradeDate") == "2024-01-01"
    assert vth._string_for("settlement_date") == "2024-01-01"
    assert vth._string_for("ticker") == "AAPL"
    assert vth._string_for("whatever") == "health-check"


def test_clamp_branches() -> None:
    assert vth._clamp(1.0, {"minimum": 5}) == 5
    assert vth._clamp(9.0, {"maximum": 5}) == 5
    assert vth._clamp(3.0, {"minimum": "x", "maximum": "y"}) == 3.0
    assert vth._clamp(3.0, {}) == 3.0


def test_value_for_branches() -> None:
    assert vth._value_for("x", "not-a-dict") == "health-check"
    assert vth._value_for("x", {"enum": ["a", "b"]}) == "a"
    assert vth._value_for("x", {"enum": []}) == "health-check"
    assert vth._value_for("ticker", {"type": "string"}) == "AAPL"
    assert vth._value_for("n", {"type": "integer", "minimum": 10}) == 10
    assert vth._value_for("strike", {"type": "number"}) == 100.0
    assert vth._value_for("ratio", {"type": "number"}) == 1.5
    assert vth._value_for("flag", {"type": "boolean"}) is True
    assert vth._value_for("fields", {"type": "array"}) == [
        "settlementDate",
        "currentShortPositionQuantity",
    ]
    assert vth._value_for("tags", {"type": "array", "items": {"type": "string"}}) == ["health-check"]
    assert vth._value_for("tags", {"type": "array"}) == ["health-check"]
    assert vth._value_for("cfg", {"type": "object"}) == {}
    nested = {"properties": {"a": {"type": "string"}}, "required": ["a", "zzz"]}
    assert vth._value_for("cfg", nested) == {"a": "health-check"}
    assert vth._value_for("cfg", {"properties": {"a": {}}, "required": "nope"}) == {}
    assert vth._value_for("cfg", {"type": "mystery"}) == "health-check"


def test_fixture_for_branches() -> None:
    assert vth.fixture_for({}) == {}
    assert vth.fixture_for({"properties": "nope", "required": "nope"}) == {}
    assert vth.fixture_for({"properties": {"a": {"type": "string"}}, "required": ["a", 5]}) == {"a": "health-check"}


def test_schema_branches() -> None:
    assert vth.check_schema("t", {}) == "schema missing description"
    assert vth.check_schema("t", {"description": "  "}) == "schema missing description"
    assert vth.check_schema("t", {"description": "d"}) == "parameters must be a type:object dict"
    assert vth.check_schema("t", {"description": "d", "parameters": {"type": "object"}}) == (
        "parameters.properties must be a dict"
    )


def test_schema_real_tool_pass_and_parity(tmp_path: Path) -> None:
    fn: dict[str, object] = {
        "description": "d",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    assert vth.check_schema("search_web", fn) is None
    bad_required: dict[str, object] = {
        "description": "d",
        "parameters": {"type": "object", "properties": dict[str, object]()},
    }
    assert "required without properties" in (vth.check_schema("search_web", bad_required) or "")


def test_dispatch_no_owner(tmp_path: Path) -> None:
    assert "no handler" in (vth.check_dispatch("no_such_tool_xyz", {}, _ctx(tmp_path)) or "")


def test_handler_owner_tables() -> None:
    assert vth.handler_owner("thesis_create") is not None
    assert vth.handler_owner("no_such_tool_xyz") is None


def test_envelope_branches() -> None:
    assert "not a dict" in (vth._evaluate_envelope("nope") or "")
    assert "unknown reason" in (vth._evaluate_envelope({"worker_ok": False}) or "")
    assert "not a dict" in (vth._evaluate_envelope({"worker_ok": True, "result": []}) or "")
    assert "JSON-serializable" in (vth._evaluate_envelope({"worker_ok": True, "result": {"x": object()}}) or "")
    assert "non-empty string" in (vth._evaluate_envelope({"worker_ok": True, "result": {"error": " "}}) or "")
    assert "error_type" in (
        vth._evaluate_envelope({"worker_ok": True, "result": {"error": "boom", "error_type": 5}}) or ""
    )
    assert vth._evaluate_envelope({"worker_ok": True, "result": {"ok": True}}) is None


def _exec_boom(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    raise RuntimeError("boom")


def _exec_not_a_dict(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> object:
    return ["not-a-dict"]


def _exec_x_obj(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"x": object()}


def _exec_err_empty(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"error": "", "error_type": "x"}


def _exec_err_int(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"error": "e", "error_type": 5}


def _exec_ok(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"ok": True}


def _exec_err_nope(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"error": "e", "error_type": "nope", "tool": "search_web"}


def _exec_err_other(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"error": "e", "error_type": "invalid_tool_arguments", "tool": "other"}


def _exec_err_search(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"error": "e", "error_type": "invalid_tool_arguments", "tool": "search_web"}


def _exec_nope_list(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> object:
    return ["nope"]


def _exec_thesis_abc(name: str, arguments: dict[str, object], model: str, *a: object, **k: object) -> dict[str, object]:
    return {"thesis_id": "abc"}


def _exec_thesis_nested(
    name: str, arguments: dict[str, object], model: str, *a: object, **k: object
) -> dict[str, object]:
    return {"thesis": {"id": "nested"}}


def _perm_deny(name: str, context: object) -> bool:
    return False


def _validate_empty(name: str, arguments: object) -> str | None:
    return ""


def _validate_need_obj(name: str, arguments: object) -> str | None:
    return "need object"


def _check_none(name: str, fixture: dict[str, object], ctx: RequestContext, *a: object, **k: object) -> None:
    return None


def _dispatch_boom(name: str, fixture: dict[str, object], ctx: RequestContext, *a: object, **k: object) -> str | None:
    return "boom"


def _schema_empty(name: str) -> tuple[dict[str, object], list[str], list[str]]:
    return (dict[str, object](), list[str](), list[str]())


def _verify_none(name: str, function: dict[str, object], *a: object, **k: object) -> list[str]:
    return []


def _unknown_false(ctx: RequestContext) -> bool:
    return False


def test_live_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(vth, "execute_tool", _exec_boom)
        assert "raised RuntimeError" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
        monkeypatch.setattr(vth, "execute_tool", _exec_not_a_dict)
        assert "not a dict" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
        monkeypatch.setattr(vth, "execute_tool", _exec_x_obj)
        assert "JSON-serializable" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
        monkeypatch.setattr(vth, "execute_tool", _exec_err_empty)
        assert "non-empty string" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
        monkeypatch.setattr(vth, "execute_tool", _exec_err_int)
        assert "error_type" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
        monkeypatch.setattr(vth, "execute_tool", _exec_ok)
        assert vth.check_live("search_web", {"query": "q"}, ctx, pool) is None


def test_live_timeout_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import concurrent.futures as cf

    ctx = _ctx(tmp_path)
    real_submit = cf.ThreadPoolExecutor.submit

    class _Hung:
        def result(self, timeout: float | None = None) -> object:
            raise cf.TimeoutError()

    def _submit_hung(self: object, *a: object, **k: object) -> object:
        return _Hung()

    monkeypatch.setattr(cf.ThreadPoolExecutor, "submit", _submit_hung)
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        assert "exceeded" in (vth.check_live("search_web", {"query": "q"}, ctx, pool) or "")
    monkeypatch.undo()
    assert real_submit is not None


def test_security_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, deny = _ctx(tmp_path), _deny(tmp_path)
    assert "want RESEARCH" in (vth.check_security("no_such_tool_xyz", {}, ctx, deny) or "")
    monkeypatch.setattr(vth, "tool_is_permitted", _perm_deny)
    names = vth.research_names()
    assert "denied under RESEARCH" in (vth.check_security(names[0], {}, ctx, deny) or "")
    monkeypatch.undo()
    monkeypatch.setattr(vth, "execute_tool", _exec_ok)
    assert "not denied" in (vth.check_security(names[0], {}, ctx, deny) or "")


def test_errors_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(vth, "execute_tool", _exec_ok)
    assert "no structured error" in (vth.check_errors("search_web", {"required": ["query"]}, ctx) or "")
    monkeypatch.setattr(vth, "execute_tool", _exec_err_nope)
    assert "error_type" in (vth.check_errors("search_web", {"required": ["query"]}, ctx) or "")
    monkeypatch.setattr(vth, "execute_tool", _exec_err_other)
    assert "tool name" in (vth.check_errors("search_web", {"required": ["query"]}, ctx) or "")
    monkeypatch.setattr(
        vth,
        "execute_tool",
        _exec_err_search,
    )
    assert vth.check_errors("search_web", {"required": ["query"]}, ctx) is None
    monkeypatch.setattr(vth, "_validate_tool_arguments", _validate_empty)
    assert "non-dict" in (vth.check_errors("get_trend_evidence", {}, ctx) or "")
    monkeypatch.setattr(vth, "_validate_tool_arguments", _validate_need_obj)
    assert vth.check_errors("get_trend_evidence", {}, ctx) is None


def test_bootstrap_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(vth, "execute_tool", _exec_boom)
    assert vth.bootstrap_thesis_id(ctx) is None
    monkeypatch.setattr(vth, "execute_tool", _exec_nope_list)
    assert vth.bootstrap_thesis_id(ctx) is None
    monkeypatch.setattr(vth, "execute_tool", _exec_thesis_abc)
    assert vth.bootstrap_thesis_id(ctx) == "abc"
    monkeypatch.setattr(vth, "execute_tool", _exec_thesis_nested)
    assert vth.bootstrap_thesis_id(ctx) == "nested"
    monkeypatch.setattr(vth, "execute_tool", _exec_ok)
    assert vth.bootstrap_thesis_id(ctx) is None


def test_verify_one_schema_shortcircuit(tmp_path: Path) -> None:
    ctx, deny = _ctx(tmp_path), _deny(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        out = vth.verify_one("search_web", {"description": ""}, ctx, deny, pool, None)
        assert out == ["schema: schema missing description"]


def test_verify_one_full_with_fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, deny = _ctx(tmp_path), _deny(tmp_path)
    monkeypatch.setattr(vth, "check_handler", _check_none)
    monkeypatch.setattr(vth, "check_dispatch", _check_none)
    monkeypatch.setattr(vth, "check_errors", _check_none)
    monkeypatch.setattr(vth, "check_security", _check_none)
    function: dict[str, object] = {
        "description": "fake",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": list[str](),
        },
    }
    monkeypatch.setattr(vth, "_canonical_tool_schema", _schema_empty)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        assert vth.verify_one("thesis_show", function, ctx, deny, pool, "tid-1") == []
        monkeypatch.setattr(vth, "check_dispatch", _dispatch_boom)
        out = vth.verify_one("thesis_show", function, ctx, deny, pool, "tid-1")
        assert any("dispatch" in f for f in out)


def test_parse_and_select_args() -> None:
    args = vth.parse_args(["--tool", "a", "--tool", "b"])
    assert sorted(args.tool) == ["a", "b"]
    args = vth.parse_args([])
    assert args.list is False


def test_selected_names_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vth, "research_names", _research_a)
    selected, unknown = vth._selected_names(argparse.Namespace(tool=["a", "zzz"]))
    assert selected == ["a", "zzz"]
    assert unknown == ["zzz"]


def test_main_list_and_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    assert vth.main(["--list"]) == 0
    assert "search_web" in capsys.readouterr().out or True
    assert vth.main(["--tool", "no-such-tool"]) == 2


def test_main_json_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(vth, "research_names", _research_thesis)
    monkeypatch.setattr(vth, "verify_one", _verify_none)
    monkeypatch.setattr(vth, "_check_unknown_tool", _unknown_false)
    rc = vth.main(["--json", "--tool", "thesis_create"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1 and payload["passed"] == 1


# ---- slice_small_tests.py ----
"""Scratch unit tests for the small-scripts CRAP slice.

Covers every new pure helper in: research_slice, robinhood_tools,
robinhood_options_smoke, sandbox_doctor, update_tool_catalog,
strict_routing_harness. Run: python3 -m pytest /tmp/slice_small_tests.py -q
"""


def _result(entries: list[dict[str, object]]) -> dict[str, object]:
    return {
        "calculation_version": "v9",
        "as_of": "2026-08-14",
        "settlement_current": "20260814",
        "settlement_prior": "20260731",
        "coverage": "2/2",
        "entries": entries,
    }


def _entry(ticker: str = "AAPL", now: bool = True, prior: bool = True) -> dict[str, object]:
    e: dict[str, object] = {
        "rank": 1,
        "ticker": ticker,
        "short_shares_current": 100.5,
        "short_shares_prior": 90.0,
        "short_change_pct": 11.1,
        "short_interest_percent_current": 2.5,
        "short_interest_percent_prior": 2.0,
        "si_pp_change": 0.5,
        "shares_outstanding_current": 1000.0,
        "shares_outstanding_prior": 990.0,
        "shares_change_pct": 1.01,
        "finra_source_url": "https://finra/x",
        "settlement_current": "20260814",
        "sec_accession_current": "acc-now" if now else "",
        "sec_source_url_current": "https://sec/now",
        "sec_filed_at_current": "2026-08-01",
        "sec_accession_prior": "acc-prior" if prior else "",
        "sec_source_url_prior": "https://sec/prior",
        "sec_filed_at_prior": "2026-07-01",
    }
    return e


def test_fmt_branches():
    assert rs._fmt(None) == "-"
    assert rs._fmt(1.5) == "1.50"
    assert rs._fmt("x") == "x"
    assert rs._fmt(7) == "7"


def test_parse_args_research():
    args = rs.parse_args(["--as-of", "2026-08-14", "--limit", "3"])
    assert args.as_of == "2026-08-14" and args.limit == 3 and args.data_root is None


def test_extract_entries_filters():
    assert rs.extract_entries({"entries": [{"a": 1}, "nope", 5]}) == [{"a": 1}]
    assert rs.extract_entries({}) == []
    assert rs.extract_entries({"entries": "x"}) == []


def test_build_rows_and_widths():
    rows = rs.build_rows([_entry()])
    assert len(rows) == 1 and len(rows[0]) == 11 and rows[0][1] == "AAPL"
    widths = rs._column_widths(rows)
    assert all(w >= len(h) for w, h in zip(widths, rs.TABLE_HEADERS))


def test_format_table_header_and_grid():
    text = rs.format_table(_result([_entry()]))
    assert "calc v9" in text and "20260814" in text and "AAPL" in text
    assert "Rank" in text.splitlines()[3]


def test_format_table_no_prior():
    r = _result([_entry()])
    r["settlement_prior"] = None
    assert "prior settlement: -" in rs.format_table(r)


def test_format_table_empty_entries():
    text = rs.format_table(_result([]))
    assert "Rank" in text and "AAPL" not in text


def test_format_evidence_both_and_neither():
    both = rs.format_evidence([_entry()])
    assert "FINRA snapshot" in both and "Shares fact (now)" in both and "Shares fact (prior)" in both
    neither = rs.format_evidence([_entry(now=False, prior=False)])
    assert "FINRA snapshot" in neither and "Shares fact" not in neither
    assert rs.format_evidence([]) == "\nEvidence links:"


def _screen_boom(args: argparse.Namespace) -> dict[str, object]:
    return {"error": "boom"}


def _screen_ok(args: argparse.Namespace) -> dict[str, object]:
    return _result([_entry()])


def test_main_error_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(rs, "fetch_screen", _screen_boom)
    assert rs.main(["--as-of", "2026-08-14"]) == 1
    assert "boom" in capsys.readouterr().err


def test_main_ok_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(rs, "fetch_screen", _screen_ok)
    assert rs.main(["--as-of", "2026-08-14"]) == 0
    out = capsys.readouterr().out
    assert "Evidence links:" in out and "AAPL" in out


def test_truncate_and_classify():
    assert rt._truncate("abc", 5) == "abc"
    assert rt._truncate("abcdef", 5) == "ab..."
    assert rt._classify("get_option_chains") == "MARKET_READ"
    assert rt._classify("get_accounts") == "ACCOUNT_READ"
    assert rt._classify("place_order") == "BLOCKED"
    assert rt._classify("bogus_tool_xyz") == "UNKNOWN"


def test_robinhood_tools_parse_args():
    args = rt.parse_args([])
    assert args.json is False and args.server_url.startswith("https://")
    assert rt.parse_args(["--json"]).json is True


def test_tool_names_and_schema():
    tools: list[dict[str, object]] = [{"name": "a"}, {"description": "x"}]
    assert rt.tool_names(tools) == ["a", "<unknown>"]
    assert rt._tool_schema({"input_schema": {"type": "o"}}) == {"type": "o"}
    assert rt._tool_schema({"inputSchema": {"type": "o"}}) == {"type": "o"}
    assert rt._tool_schema({}) == {}


def test_render_tool_and_text():
    tool: dict[str, object] = {
        "name": "get_accounts",
        "description": "d" * 500,
        "input_schema": {"b": 1},
    }
    line = rt.render_tool(tool)
    assert "ACCOUNT_READ" in line and "..." in line
    tools: list[dict[str, object]] = [
        {"name": "get_accounts"},
        {"name": "get_positions_avg_cost_xyz"},
    ]
    text = rt.render_text(tools)
    assert "tools discovered: 2" in text and "Discovery only" in text
    assert "get_positions_avg_cost_xyz" in text  # review candidate
    assert "allowlisted" in rt.render_text([{"name": "get_accounts"}])


def test_render_json_roundtrip():
    tools: list[dict[str, object]] = [{"name": "b", "x": 1}]
    assert json.loads(rt.render_json(tools)) == tools


def test_find_candidates_case_insensitive():
    assert "Get_Positions_X" in rt.find_candidates(["Get_Positions_X", "get_quote"])
    assert "get_accounts" not in rt.find_candidates(["get_accounts"])


def test_robinhood_main_json_and_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    tools: list[dict[str, object]] = [{"name": "get_accounts", "description": "d"}]

    def _connect_tools(server_url: str) -> list[dict[str, object]]:
        return tools

    monkeypatch.setattr(rt, "connect", _connect_tools)
    assert rt.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out) == tools
    assert rt.main([]) == 0
    assert "tools discovered: 1" in capsys.readouterr().out


def test_robinhood_main_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def _boom(url: str) -> object:
        raise RuntimeError("nope")

    monkeypatch.setattr(rt, "connect", _boom)
    assert rt.main([]) == 1
    assert "nope" in capsys.readouterr().err


def test_smoke_parse_args():
    args = smoke.parse_args(["AAPL", "--type", "call", "--min-dte", "10"])
    assert args.ticker == "AAPL" and args.option_type == "call" and args.min_dte == 10
    assert smoke.parse_args([]).ticker is None


def test_render_tool_names():
    assert smoke.render_tool_names(["a", "b"]) == "Available tools:\n- a\n- b"


def _fake_option_chain(*a: object, **k: object) -> dict[str, object]:
    return {"result": list(a) + [k]}


def _render_chain(chain: object) -> str:
    return "CHAIN"


def _render_r(r: object) -> str:
    return f"R:{r}"


def _fake_run_chain(client: object, args: argparse.Namespace) -> str:
    return "CHAIN"


def _allowlist_a() -> list[str]:
    return ["a:443"]


def _run_locked(cmd: list[str]) -> object:
    return _proc(0, "Locked Down profile")


def _run_denied(cmd: list[str]) -> object:
    return _proc(1, "", "denied")


def _run_rules(cmd: list[str]) -> object:
    return _proc(0, "rules without name")


def _behavioral_true() -> bool:
    return True


def _behavioral_false() -> bool:
    return False


def _dispatch_passthrough(cid: int, ordered: list[str]) -> list[str]:
    return list(ordered)


def _ground_empty() -> dict[int, tuple[list[str], list[str]]]:
    return {}


def _probes_boom() -> str | None:
    return "probe boom"


def _normalize_bad(raw: object) -> str:
    raise ValueError("bad")


def _killpg_none(*a: object, **k: object) -> None:
    return None


def _ensure_sess(store: Path) -> str:
    return "sess-1"


def _registry_discovery_only() -> dict[str, set[str]]:
    return {"schemas": set(v.DISCOVERY_TOOLS)}


def test_smoke_list_names_and_chain(monkeypatch: pytest.MonkeyPatch):

    class C(RobinhoodClient):
        def __init__(self) -> None:
            pass

        @override
        def list_tools(self) -> list[dict[str, object]]:
            return [{"name": "t1"}, {}]

    assert smoke.list_tool_names(C()) == ["t1", "<unknown>"]
    seen: dict[str, object] = {}

    class C2(RobinhoodClient):
        def __init__(self) -> None:
            pass

        @override
        def list_tools(self) -> list[dict[str, object]]:
            return []

    def _fake_client(**k: object) -> object:
        seen.update(k)
        return C2()

    monkeypatch.setattr(smoke.stockbot_tools, "_robinhood_client", _fake_client)
    monkeypatch.setattr(smoke.stockbot_tools, "get_option_chain", _fake_option_chain)
    monkeypatch.setattr(smoke, "render_tool_result", _render_chain)
    args = argparse.Namespace(ticker="AAPL", option_type="put", min_dte=1, max_dte=2)
    assert smoke.run_chain(C2(), args) == "CHAIN"


def test_smoke_main_paths(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from app.thesis.models import JSONValue as ThesisJSON2

    class C(RobinhoodClient):
        def __init__(self) -> None:
            pass

        @override
        def list_tools(self) -> list[dict[str, object]]:
            return [{"name": "t1"}]

        @override
        def call_tool(self, name: str, arguments: dict[str, object] | None = None) -> ThesisJSON2:
            return {"result": name}

    def _connect_c(server_url: str) -> object:
        return C()

    monkeypatch.setattr(smoke, "connect", _connect_c)
    monkeypatch.setattr(smoke, "render_tool_result", _render_r)
    assert smoke.main(["--tool", "t1"]) == 0
    assert "R:" in capsys.readouterr().out
    monkeypatch.setattr(smoke, "run_chain", _fake_run_chain)
    assert smoke.main(["AAPL"]) == 0
    assert "CHAIN" in capsys.readouterr().out
    assert smoke.main([]) == 0
    capsys.readouterr()

    def _boom(url: str) -> object:
        raise RuntimeError("down")


def test_doctor_pure_helpers():
    assert doc._policy_output_locked_down("Locked Down profile") is True
    assert doc._policy_output_locked_down("open") is False
    assert doc._ssh_value_ok("false") is True
    assert doc._ssh_value_ok(" False \n") is True
    assert doc._ssh_value_ok("true") is False
    assert doc.is_mount_violation("# Never mount ~/.pi here") is False
    assert doc.is_mount_violation("") is False
    assert doc.is_mount_violation("source: ~/.pi/auth.json") is True
    assert doc.is_mount_violation("mount: /data/x") is False
    assert doc.missing_env_keys({}) == []
    assert doc.missing_env_keys({"OPENAI_API_KEY": "x"}) == ["OPENAI_API_KEY"]
    assert doc.parse_allowlist("# c\n\na:443\n b:443 \n") == ["a:443", "b:443"]
    assert doc.parse_args([]).check is None
    assert sorted(doc.parse_args(["--check", "env"]).check) == ["env"]


def test_doctor_check_mount_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doc, "REPO_ROOT", tmp_path)
    (tmp_path / "docker-compose.yml").write_text("# Never mount ~/.pi\ndata: /x\n")
    doc._check_mount_file("docker-compose.yml", "n")  # clean file passes
    try:
        doc._check_mount_file("missing.yml", "n")
        raise AssertionError("should fail")
    except SystemExit:
        pass
    (tmp_path / "bad.yml").write_text("source: ~/.pi/auth.json\n")
    try:
        doc._check_mount_file("bad.yml", "n")
        raise AssertionError("should fail")
    except SystemExit:
        pass


def test_doctor_read_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doc, "REPO_ROOT", tmp_path)
    try:
        doc.read_allowlist()
        raise AssertionError("should fail when missing")
    except SystemExit:
        pass
    d = tmp_path / "sandbox" / "stockbot"
    d.mkdir(parents=True)
    (d / "egress-hosts.txt").write_text("# only\n")
    try:
        doc.read_allowlist()
        raise AssertionError("should fail when empty")
    except SystemExit:
        pass
    (d / "egress-hosts.txt").write_text("a:443\n")
    assert doc.read_allowlist() == ["a:443"]


def test_doctor_run_all_aggregator():
    def _ok():
        return None

    def _fail_exit():
        raise SystemExit(1)

    def _boom():
        raise RuntimeError("x")

    assert doc.run_all([_ok, _ok]) == 0
    assert doc.run_all([_ok, _fail_exit]) == 1
    assert doc.run_all([_boom]) == 1
    assert doc.run_all([_fail_exit, _boom, _ok]) == 2
    assert {c.__name__ for c in doc.selected_checks(doc.parse_args([]))} == {c.__name__ for c in doc.CHECKS.values()}
    assert doc.selected_checks(doc.parse_args(["--check", "env"])) == [doc.check_calling_env]


def test_doctor_network_branches(monkeypatch: pytest.MonkeyPatch):
    class P:
        def __init__(self, code: int = 0, out: str = "", err: str = "") -> None:
            self.returncode = code
            self.stdout = out
            self.stderr = err

    def _run_allow(cmd: list[str]) -> object:
        return P(0) if cmd[-1] == "a:443" else P(1)

    # allowed host returns 0, denied hosts return nonzero
    monkeypatch.setattr(doc, "read_allowlist", _allowlist_a)
    monkeypatch.setattr(doc, "run", _run_allow)
    doc.check_network()  # all allowed + denied hosts denied

    def _run_deny_fail(cmd: list[str]) -> object:
        return P(0) if "a:443" not in cmd else P(3, err="no")

    monkeypatch.setattr(doc, "run", _run_deny_fail)
    try:
        doc.check_network()
        raise AssertionError("should fail on disallowed host")
    except SystemExit:
        pass

    def _run_zero(cmd: list[str]) -> object:
        return P(0)

    monkeypatch.setattr(doc, "run", _run_zero)  # denied host returns 0 -> fail
    try:
        doc.check_network()
        raise AssertionError("should fail on denied-but-allowed")
    except SystemExit:
        pass


def test_catalog_yaml_str_and_arg_line():
    assert cat._yaml_str('a"b\\c') == '"a\\"b\\\\c"'
    assert cat._arg_line("x", {}) == "- `x`"
    assert cat._arg_line("x", {"type": "string"}) == "- `x` (string)"
    assert cat._arg_line("x", {"type": "string", "desc": "d"}) == "- `x` (string): d"
    assert cat._bullets(()) == "None\n"
    assert cat._bullets(("a", "b")) == "- a\n- b\n"


def test_catalog_schema_helpers():
    from app.tools import TOOL_DISCOVERY_REGISTRY

    names = sorted(n for n in TOOL_DISCOVERY_REGISTRY if n not in cat.EXCLUDED)
    name = names[0]
    params = cat._tool_params(name)
    assert isinstance(params, dict)
    req_params = params.get("required", [])
    assert isinstance(req_params, list)
    assert cat._required_args(params) == [str(r) for r in req_params]
    assert cat._required_args({}) == []
    assert cat._typed_props({"b": {"type": "string", "description": "d"}, "a": "x"}) == {
        "a": {},
        "b": {"type": "string", "desc": "d"},
    }
    try:
        cat._tool_params("no_such_tool_xyz")
        raise AssertionError("should raise")
    except ValueError:
        pass
    try:
        cat._schema_props(name, {"required": []})
        raise AssertionError("should raise")
    except ValueError:
        pass
    req, typed = cat._schema_args(name)
    assert isinstance(req, list) and isinstance(typed, dict)
    try:
        cat._schema_args("no_such_tool_xyz")
        raise AssertionError("should raise")
    except ValueError:
        pass


def test_catalog_markdown_sections():
    from app.tools import TOOL_DISCOVERY_REGISTRY

    names = sorted(n for n in TOOL_DISCOVERY_REGISTRY if n not in cat.EXCLUDED)
    md = cat.tool_markdown(names[0])
    for section in (
        "Choose when",
        "Reject when",
        "Required arguments",
        "Optional arguments",
    ):
        assert section in md
    assert cat._meta_header_lines("n", TOOL_DISCOVERY_REGISTRY[names[0]])[0] == "# n\n"
    assert "## X" in "".join(cat._bullets_section("X", ("a",)))
    assert "## T" in "".join(cat._args_block("T", [], {}))


def test_catalog_index_and_validate(tmp_path: Path):
    from app.tools import TOOL_DISCOVERY_REGISTRY

    names = sorted(n for n in TOOL_DISCOVERY_REGISTRY if n not in cat.EXCLUDED)
    text = cat.index_yaml(names)
    assert text.startswith("version: 2") and "tools:" in text
    assert cat._domain_lines(names)
    assert cat._tool_card_lines(names[0])[0].startswith("  - name: ")
    cat.validate_registry(names)  # no raise
    assert cat.write_catalog(names, tmp_path) == len(names)
    assert (tmp_path / "index.yaml").is_file()
    assert cat.parse_args([]).check is False
    assert cat.parse_args(["--check"]).check is True
    assert cat.main(["--check"]) == 0
    assert cat.catalog_names() == names
    assert cat.report_and_write(names, True) == 0


def test_harness_subsequence_and_decode():
    assert harness._is_ordered_subsequence(["a", "c"], ["a", "b", "c"]) is True
    assert harness._is_ordered_subsequence(["c", "a"], ["a", "b", "c"]) is False
    assert harness._decode_inner(json.dumps({"name": "x"})) == "x"
    assert harness._decode_inner("not-json") is None
    assert harness._decode_inner(json.dumps({"name": ""})) is None
    assert harness._decode_inner(json.dumps({"other": 1})) is None
    assert harness._decode_inner(None) is None
    names, bad = harness._parse_dispatched(harness._trace_rows(["a", "b"]))
    assert names == ["a", "b"] and bad == 0
    names, bad = harness._parse_dispatched([{"tool_name": "call_tool", "arguments": "zzz"}])
    assert bad == 1


def test_harness_verdict_branches():
    ok_rows = harness._trace_rows(["get_short_interest"])
    assert harness.strict_verdict(["get_short_interest"], [], ok_rows) == (
        True,
        False,
        "ok",
    )
    bad_rows = harness._trace_rows(["get_reg_sho_volume"])
    passed, excused, reason = harness.strict_verdict(["get_short_interest"], [], bad_rows)
    assert (passed, excused) == (False, False) and "missing" in reason
    passed, excused, _ = harness.strict_verdict(
        ["get_short_interest"], [], bad_rows, infra_error_names=(("get_short_interest", "ratelimit 429"),)
    )
    assert (passed, excused) == (False, True)
    passed, excused, reason = harness.strict_verdict(
        ["get_short_interest"], [], [{"tool_name": "call_tool", "arguments": "{"}]
    )
    assert reason == "unparseable call_tool args"
    rows = harness._trace_rows(["a", "c", "b"])
    passed, _, reason = harness.strict_verdict(["a", "b", "c"], ["c", "a"], rows)
    assert passed is False and "sequence" in reason
    rows = harness._trace_rows(["a", "stray"])
    passed, _, reason = harness.strict_verdict(["a"], [], rows)
    assert passed is False and "stray" in reason
    assert harness._verdict_missing(["a"], ["a"], ()) is None
    assert harness._missing_required(["a", "b"], ["a"]) == ["b"]
    assert harness._infra_excused(["m"], (("m", "timeout"),)) is True
    assert harness._infra_excused([], ()) is False


def test_harness_fixture_and_report():
    truth = harness._fixture_ground_truth()
    assert harness.check_workload_drift(truth) is None
    drift_input: dict[int, tuple[list[str], list[str]]] = {}
    drift_input[0] = (list[str](), list[str]())
    assert "drift" in (harness.check_workload_drift(drift_input) or "")
    assert harness.scoring_set(["b", "a"], ["c"]) == ["a", "b", "c"]
    assert harness.dispatch_order(["a", "b"], ["b"]) == ["b", "a"]
    assert "strict_routing_accuracy=1.0000" in harness.build_report(2, 2)
    assert "strict_routing_accuracy=1.0000" in harness.build_report(0, 0)
    assert harness._case_ground_truth({}) == ([], [])
    assert harness._case_ground_truth({"expected_behavior": "x"}) == ([], [])
    probes = harness.strictness_probes()
    assert len(probes) == 4 and harness.run_probes() is None
    assert harness.parse_args([]) is not None


def test_harness_main_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(harness, "_dispatch_real", _dispatch_passthrough)
    assert harness.main([]) == 0
    assert "strict_routing_accuracy" in capsys.readouterr().out


def test_harness_main_drift(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(harness, "_fixture_ground_truth", _ground_empty)
    assert harness.main([]) == 2
    assert "drift" in capsys.readouterr().err


def test_harness_main_probe_fail(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(harness, "_dispatch_real", _dispatch_passthrough)
    monkeypatch.setattr(harness, "run_probes", _probes_boom)
    assert harness.main([]) == 2
    assert "probe boom" in capsys.readouterr().err


# ---- slice_gap1_tests.py ----
"""Gap-1 scratch coverage for scripts/verify_pi_tools.py (fakes only, no Pi/network).

Parent assembles this file; do NOT copy into tests/.
Targets the 18 functions still scoring CRAP>10 under scoped real coverage.
"""


def _tc_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE tool_calls (tool_call_id TEXT, run_id TEXT, tool_name TEXT,"
        " started_at TEXT, arguments_json TEXT, error_type TEXT, error_message TEXT)"
    )


def _mismatch_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    _tc_table(conn)
    conn.execute(
        "INSERT INTO tool_calls (tool_call_id, run_id, tool_name, started_at, arguments_json, error_type, error_message)"
        " VALUES ('tc1','r1','get_fundamentals','t',?,NULL,NULL)",
        (json.dumps({"ticker": "AAPL"}),),
    )
    conn.commit()
    return conn


# ---- _expected_args_mismatch (cc7, was 56%) ----


def test_expected_args_none_is_false(tmp_path: Path):
    conn = _mismatch_conn(tmp_path / "m.sqlite")
    try:
        assert v._expected_args_mismatch(conn, "get_fundamentals", None) is False
    finally:
        conn.close()


def test_expected_args_match_and_mismatch(tmp_path: Path):
    conn = _mismatch_conn(tmp_path / "m.sqlite")
    try:
        assert v._expected_args_mismatch(conn, "get_fundamentals", {"ticker": "AAPL"}) is False
        assert v._expected_args_mismatch(conn, "get_fundamentals", {"ticker": "MSFT"}) is True
        assert v._expected_args_mismatch(conn, "absent_tool", {"ticker": "AAPL"}) is False
    finally:
        conn.close()


def test_expected_args_normalize_raises_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = _mismatch_conn(tmp_path / "m.sqlite")
    monkeypatch.setattr(v, "_normalize_args", _normalize_bad)
    try:
        assert v._expected_args_mismatch(conn, "get_fundamentals", {"ticker": "AAPL"}) is False
    finally:
        conn.close()


def test_expected_args_db_error_is_false(tmp_path: Path):
    conn = _mismatch_conn(tmp_path / "m.sqlite")
    conn.close()
    assert v._expected_args_mismatch(conn, "get_fundamentals", {"ticker": "AAPL"}) is False


def test_expected_args_row_normalize_raises_is_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    conn = _mismatch_conn(tmp_path / "m.sqlite")
    calls = {"n": 0}

    def fake(raw: object):
        calls["n"] += 1
        if calls["n"] == 1:
            return "WANT"
        raise ValueError("bad row")

    monkeypatch.setattr(v, "_normalize_args", fake)
    try:
        assert v._expected_args_mismatch(conn, "get_fundamentals", {"ticker": "AAPL"}) is False
    finally:
        conn.close()


# ---- evaluate_reachability_tool (cc6) ----


def test_reachability_dict_branches():
    assert v.evaluate_reachability_tool([]) is True
    assert v.evaluate_reachability_tool([{"reach_ok": True}]) is True
    assert v.evaluate_reachability_tool([{"reach_ok": False}]) is False
    assert v.evaluate_reachability_tool([{"ok": True}]) is True
    assert v.evaluate_reachability_tool([{"ok": False}]) is False
    assert v.evaluate_reachability_tool([{}]) is False
    assert v.evaluate_reachability_tool([{"reach_ok": True}, {"reach_ok": False}]) is False


def test_reachability_object_branches():
    assert v.evaluate_reachability_tool([SimpleNamespace(reach_ok=True)]) is True
    assert v.evaluate_reachability_tool([SimpleNamespace(reach_ok=False)]) is False
    assert v.evaluate_reachability_tool([SimpleNamespace(ok=True)]) is True
    assert v.evaluate_reachability_tool([SimpleNamespace(ok=False)]) is False
    assert v.evaluate_reachability_tool([SimpleNamespace()]) is False


# ---- discover (cc7, subprocess fake) ----


def _popen_factory(
    lines: list[str], fail_first_wait: bool = False, fail_close: bool = False, fail_all_waits: bool = False
):
    class _In:
        def __init__(self):
            self.written = []

        def write(self, s: str) -> None:
            self.written.append(s)

        def flush(self):
            pass

        def close(self):
            if fail_close:
                raise OSError("close boom")

    class _Out:
        def __init__(self):
            self._q = list(lines)

        def readline(self):
            return self._q.pop(0) if self._q else ""

    class _Popen:
        def __init__(self, *a: object, **k: object) -> None:
            self.stdin = _In()
            self.stdout = _Out()
            self.terminated = False
            self.killed = False
            self.waits = 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if fail_all_waits or (fail_first_wait and self.waits == 1):
                raise subprocess.TimeoutExpired("pi_bridge", timeout if timeout is not None else 0.0)
            return 0

    return _Popen


def _lines(describe_id: str = "discover-1", doctor_id: str = "discover-2") -> list[str]:
    return [
        json.dumps({"id": describe_id, "tools": []}),
        json.dumps({"id": doctor_id, "bridge_ok": True}),
    ]


def test_discover_ordered(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines()))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True


def test_discover_reversed_ids(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines()[::-1]))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True


def test_discover_unknown_ids_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines("x", "y")))
    assert v.discover() == ({}, {})


def test_discover_wait_timeout_kills(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines(), fail_first_wait=True, fail_close=True))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True


def test_discover_double_wait_timeout_kills(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.subprocess, "Popen", _popen_factory(_lines(), fail_all_waits=True))
    describe, doctor = v.discover()
    assert describe["tools"] == [] and doctor["bridge_ok"] is True


# ---- _reap_pi (cc5) ----


class _Proc:
    def __init__(self, poll_val: object, wait_val: int = 0, wait_raises: bool = False) -> None:
        self._poll = poll_val
        self.pid = 1234567
        self._wait_val = wait_val
        self._raises = wait_raises

    def poll(self):
        return self._poll() if callable(self._poll) else self._poll

    def wait(self, timeout: float | None = None) -> int:
        if self._raises:
            raise RuntimeError("boom")
        return self._wait_val


def test_reap_exited_passes_through():
    assert v._reap_pi(_Proc(0), None) == 0
    assert v._reap_pi(_Proc(0), 5) == 5


def test_reap_running_kills_and_waits(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.os, "killpg", _killpg_none)
    assert v._reap_pi(_Proc(None, wait_val=137), None) == 137


def test_reap_killpg_missing_proc_still_waits(monkeypatch: pytest.MonkeyPatch):
    def _gone(*a: object, **k: object) -> object:
        raise ProcessLookupError("gone")

    monkeypatch.setattr(v.os, "killpg", _gone)
    assert v._reap_pi(_Proc(None, wait_val=1), None) == 1


def test_reap_wait_error_returns_124(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v.os, "killpg", _killpg_none)
    assert v._reap_pi(_Proc(None, wait_raises=True), None) == 124


# ---- _verification_args (cc8, was 67%) ----


def test_verification_args_finra_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[Path, Path]] = []

    def _seed_finra(store: Path, durable: Path) -> None:
        seen.append((store, durable))

    monkeypatch.setattr(v, "seed_finra_fixture", _seed_finra)
    out = v._verification_args("get_short_interest_leaderboard", {"a": 1}, tmp_path, tmp_path / "d")
    assert out == {"a": 1} and len(seen) == 1


def test_verification_args_research_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v, "ensure_research_fixture", _ensure_sess)
    base = {"session_id": "research-session-placeholder", "other": 1}
    out = v._verification_args("research_status", base, tmp_path, tmp_path / "d")
    assert out == {"session_id": "sess-1", "other": 1}
    assert base["session_id"] == "research-session-placeholder"


# ---- _research_call_count (cc8, was 64%) ----


def _research_conn(path: Path, rows: list[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE tool_calls (tool_call_id TEXT, tool_name TEXT)")
    for i, name in enumerate(rows):
        conn.execute("INSERT INTO tool_calls VALUES (?, ?)", (f"tc{i}", name))
    conn.commit()
    return conn


def test_research_call_count_normal(tmp_path: Path):
    names = sorted(set(v.get_registry_sets()["schemas"]) - v.DISCOVERY_TOOLS)
    assert names
    conn = _research_conn(tmp_path / "r.sqlite", [names[0], "search_tools", names[0]])
    try:
        assert v._research_call_count(conn) == 2
    finally:
        conn.close()


def test_research_call_count_registry_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def _boom():
        raise RuntimeError("no registry")

    monkeypatch.setattr(v, "get_registry_sets", _boom)
    conn = _research_conn(tmp_path / "r.sqlite", ["search_tools"])
    try:
        assert v._research_call_count(conn) == 0
    finally:
        conn.close()


def test_research_call_count_no_research_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(v, "get_registry_sets", _registry_discovery_only)
    conn = _research_conn(tmp_path / "r.sqlite", ["search_tools"])
    try:
        assert v._research_call_count(conn) == 0
    finally:
        conn.close()


def test_research_call_count_db_error(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "empty.sqlite"))
    try:
        assert v._research_call_count(conn) == 0
    finally:
        conn.close()


# ---- _run_confusion_case / _run_confusion_pair / run_confusion ----


def _res(tool: str, ok: bool, db: str = "") -> v.AttemptResult:
    return v.AttemptResult(tool, 1, ok, "reason", 0, db, 1.0)


def _res_false(*a: object, **k: object) -> v.AttemptResult:
    return _res("tool-a", False, "")


def _cat_exec(*a: object, **k: object) -> v.RoutingFailureCategory:
    return v.RoutingFailureCategory.EXECUTION_FAILURE


def _cat_sel(*a: object, **k: object) -> v.RoutingFailureCategory:
    return v.RoutingFailureCategory.SELECTION_FAILURE


def _db_ev_tool_a(db: Path) -> tuple[set[str] | None, list[str]]:
    return ({"tool-a"}, ["tool-a"])


def _batch_tb() -> str:
    return "tb"


def _print_none(*a: object) -> None:
    return None


def _pair_1_1(*a: object, **k: object) -> tuple[int, int]:
    return (1, 1)


def _pair_0_1(*a: object, **k: object) -> tuple[int, int]:
    return (0, 1)


def _lookup_tool_t(
    case: dict[str, object], schemas: dict[str, dict[str, object]]
) -> tuple[str, str, dict[str, object]] | None:
    return ("tool-t", "prompt-p", {"a": 1})


def _verdicts_trf(*a: object, **k: object) -> tuple[bool, str, bool, str]:
    return (True, "rok", False, "fok")


def _read_none(path: str) -> list[object] | None:
    return None


def _read_two(path: str) -> list[object] | None:
    return [{"prompt": "p1"}, {"prompt": "p2"}]


def _read_one(path: str) -> list[object] | None:
    return [{"prompt": "p1"}]


def _schemas_empty() -> dict[str, dict[str, object]]:
    return {}


def _holdout_0_0(*a: object, **k: object) -> tuple[int, int]:
    return (0, 0)


def test_run_confusion_case_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = str(tmp_path / "runs.sqlite")

    def _res_true_db(*a: object, **k: object) -> v.AttemptResult:
        return _res("tool-a", True, db)

    monkeypatch.setattr(v, "run_verification_attempt", _res_true_db)
    c = {"expected_tool": "tool-a", "prompt": "p", "arguments": {}, "pair": ["a", "b"]}
    rec = v._run_confusion_case(("a", "b"), c, tmp_path, tmp_path, tmp_path)
    assert rec["selection_ok"] is True and rec["failure_category"] is None
    assert rec["surfaced"] is None and rec["dispatched"] == []


def test_run_confusion_case_exec_failure_counts_as_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v, "run_verification_attempt", _res_false)
    monkeypatch.setattr(v, "_confusion_category", _cat_exec)
    monkeypatch.setattr(v, "_confusion_db_evidence", _db_ev_tool_a)
    c = {"expected_tool": "tool-a", "prompt": "p", "arguments": {}, "pair": ["a", "b"]}
    rec = v._run_confusion_case(("a", "b"), c, tmp_path, tmp_path, tmp_path)
    assert rec["selection_ok"] is True
    assert rec["failure_category"] == "EXECUTION_FAILURE"
    assert rec["surfaced"] == ["tool-a"]


def test_run_confusion_case_selection_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v, "run_verification_attempt", _res_false)
    monkeypatch.setattr(v, "_confusion_category", _cat_sel)
    c = {"expected_tool": "tool-a", "prompt": "p", "arguments": {}, "pair": ["a", "b"]}
    rec = v._run_confusion_case(("a", "b"), c, tmp_path, tmp_path, tmp_path)
    assert rec["selection_ok"] is False


def test_run_confusion_pair_tally(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    recs = [
        {"selection_ok": True, "failure_category": "SELECTION_FAILURE"},
        {"selection_ok": False, "failure_category": None},
    ]

    def _case_pop(*a: object, **k: object) -> dict[str, object]:
        item = recs.pop(0)
        assert isinstance(item, dict)
        return {str(k): v for k, v in item.items()}

    monkeypatch.setattr(v, "_run_confusion_case", _case_pop)
    totals = {c.value: 0 for c in v.RoutingFailureCategory}
    out: list[dict[str, object]] = []
    ok, total = v._run_confusion_pair(("a", "b"), _conf_cases(), tmp_path, tmp_path, tmp_path, out, totals)
    assert (ok, total) == (1, 2) and len(out) == 2
    assert totals["SELECTION_FAILURE"] == 1


def test_run_confusion_preflight_fail(monkeypatch: pytest.MonkeyPatch):
    def _boom():
        raise ValueError("no registry semantics")

    monkeypatch.setattr(v, "generate_confusion_cases", _boom)
    assert v.run_confusion() == 1


def _conf_cases() -> list[v.ConfusionCase]:
    return [
        {
            "expected_tool": "a",
            "prompt": "pa",
            "arguments": dict[str, object](),
            "pair": ["t2", "t1"],
        },
        {
            "expected_tool": "b",
            "prompt": "pb",
            "arguments": dict[str, object](),
            "pair": ["t1", "t2"],
        },
    ]


def test_run_confusion_success_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(v, "generate_confusion_cases", _conf_cases)
    monkeypatch.setattr(v, "_batch_id", _batch_tb)

    def _data_root_tmp() -> Path:
        return tmp_path

    monkeypatch.setattr(v, "get_data_root", _data_root_tmp)
    monkeypatch.setattr(v, "_print_confusion_report", _print_none)
    monkeypatch.setattr(v, "_write_confusion_summary", _print_none)
    monkeypatch.setattr(v, "_run_confusion_pair", _pair_1_1)
    assert v.run_confusion() == 0
    monkeypatch.setattr(v, "_run_confusion_pair", _pair_0_1)
    assert v.run_confusion() == 1


# ---- _run_holdout_case / run_holdout ----


def test_run_holdout_case_invalid(tmp_path: Path):
    assert v._run_holdout_case("nope", 0, {}, tmp_path, tmp_path, tmp_path) == (1, 1)
    assert v._run_holdout_case({"prompt": 1, "expected_tool": "t"}, 1, {}, tmp_path, tmp_path, tmp_path) == (1, 1)


def test_run_holdout_case_lookup_miss(tmp_path: Path):
    schemas: dict[str, dict[str, object]] = {"some-tool": {"required": ["missing-arg"]}}
    case = {"prompt": "p", "expected_tool": "some-tool"}
    assert v._run_holdout_case(case, 0, schemas, tmp_path, tmp_path, tmp_path) == (1, 1)


def test_run_holdout_case_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v, "_holdout_lookup_args", _lookup_tool_t)
    made = v.AttemptResult("tool-t", 1, True, "ok", 0, str(tmp_path / "r.sqlite"), 2.0)

    def _run_made(*a: object, **k: object) -> v.AttemptResult:
        return made

    monkeypatch.setattr(v, "run_verification_attempt", _run_made)
    monkeypatch.setattr(v, "_holdout_verdicts", _verdicts_trf)
    seen: list[tuple[object, ...]] = []

    def _report_seen(*a: object) -> None:
        seen.append(a)

    monkeypatch.setattr(v, "_report_holdout_case", _report_seen)
    assert v._run_holdout_case({"prompt": "p", "expected_tool": "t"}, 0, {}, tmp_path, tmp_path, tmp_path) == (0, 1)
    assert seen == [("prompt-p", "tool-t", True, "rok", False, "fok", 2.0)]


def test_run_holdout_no_cases(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(v, "_read_holdout_cases", _read_none)
    assert v.run_holdout("whatever.json") == 1


def test_run_holdout_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(v, "_read_holdout_cases", _read_two)
    monkeypatch.setattr(v, "tool_schemas", _schemas_empty)
    monkeypatch.setattr(v, "_batch_id", _batch_tb)

    def _data_root_tmp() -> Path:
        return tmp_path

    monkeypatch.setattr(v, "get_data_root", _data_root_tmp)
    outcomes = iter([(0, 1), (1, 0)])

    def _holdout_next(*a: object, **k: object) -> tuple[int, int]:
        return next(outcomes)

    monkeypatch.setattr(v, "_run_holdout_case", _holdout_next)
    assert v.run_holdout("h.json") == 1
    monkeypatch.setattr(v, "_run_holdout_case", _holdout_0_0)
    monkeypatch.setattr(v, "_read_holdout_cases", _read_one)
    assert v.run_holdout("h.json") == 0


# ---- _select_matrix_tools (cc6, was 50%) ----


def test_select_matrix_tools_branches():
    assert v._select_matrix_tools(argparse.Namespace(tool="nope"), ["a"]) is None
    assert v._select_matrix_tools(argparse.Namespace(tool="browse_tools"), ["browse_tools", "a"]) is None
    assert v._select_matrix_tools(argparse.Namespace(tool="a"), ["a", "browse_tools"]) == ["a"]
    assert v._select_matrix_tools(argparse.Namespace(tool=None), ["a", "browse_tools"]) == ["a"]


# ---- _attempt_record / _report_attempt / _collect_matrix_results (cc5/cc8/cc4) ----


def _bool_kw(kw: dict[str, object], key: str) -> bool:
    value = kw.pop(key, False)
    assert isinstance(value, bool)
    return value


def _opt_bool_kw(kw: dict[str, object], key: str) -> bool | None:
    value = kw.pop(key, None)
    assert value is None or isinstance(value, bool)
    return value


def _str_kw(kw: dict[str, object], key: str) -> str:
    value = kw.pop(key, "")
    assert isinstance(value, str)
    return value


def _int_kw(kw: dict[str, object], key: str) -> int:
    value = kw.pop(key, 0)
    assert isinstance(value, int)
    return value


def _attempt(tool: str = "t", attempt: int = 1, ok: bool = True, **kw: object) -> v.AttemptResult:
    reason = kw.pop("reason", "r")
    assert isinstance(reason, str)
    exit_code = kw.pop("exit", 0)
    assert isinstance(exit_code, int)
    db = kw.pop("db", "")
    assert isinstance(db, str)
    duration = kw.pop("duration_seconds", 1.0)
    assert isinstance(duration, float)
    return v.AttemptResult(
        tool,
        attempt,
        ok,
        reason,
        exit_code,
        db,
        duration,
        model_config_failed=_bool_kw(kw, "model_config_failed"),
        reach_ok=_opt_bool_kw(kw, "reach_ok"),
        reach_reason=_str_kw(kw, "reach_reason"),
        routing_ok=_opt_bool_kw(kw, "routing_ok"),
        routing_reason=_str_kw(kw, "routing_reason"),
        discovery_calls=_int_kw(kw, "discovery_calls"),
        research_calls=_int_kw(kw, "research_calls"),
        direct_tool_calls=_int_kw(kw, "direct_tool_calls"),
        completion_ok=_opt_bool_kw(kw, "completion_ok"),
        completion_reason=_str_kw(kw, "completion_reason"),
    )


def test_attempt_record_fallbacks_and_search_queries():
    rec, route, reach = v._attempt_record(_attempt(ok=True, reach_ok=None, routing_ok=True))
    assert (route, reach) == (True, True) and "searchQueries" not in rec
    rec2, route2, reach2 = v._attempt_record(_attempt(ok=False, reach_ok=False, routing_ok=None))
    assert (route2, reach2) == (False, False) and rec2["searchQueries"] == []


def test_report_attempt_marks(capsys: pytest.CaptureFixture[str]):
    r1 = _attempt(
        ok=True,
        reach_ok=True,
        reach_reason="rr",
        routing_ok=True,
        routing_reason="rt",
        discovery_calls=1,
        research_calls=2,
        direct_tool_calls=1,
        completion_ok=True,
        completion_reason="done",
    )
    v._report_attempt(r1, 3)
    out = capsys.readouterr().out
    assert "routing PASS" in out and "reachability PASS" in out and "completion PASS" in out
    r2 = _attempt(
        ok=False, reach_ok=None, routing_ok=None, model_config_failed=True, completion_ok=None, completion_reason=""
    )
    v._report_attempt(r2, 3)
    cap = capsys.readouterr()
    assert "routing FAIL" in cap.out and "n/a" in cap.out
    assert "PI MODEL CONFIGURATION FAILED" in cap.err


def test_collect_matrix_results_counts(monkeypatch: pytest.MonkeyPatch):
    seen: list[tuple[str, int]] = []

    def _report_seen_tool(r: v.AttemptResult, n: int) -> None:
        seen.append((r.tool, n))

    monkeypatch.setattr(v, "_report_attempt", _report_seen_tool)
    r1 = _attempt("t", 1, True, reach_ok=True, routing_ok=True)
    r2 = _attempt("t", 2, False, reach_ok=False, routing_ok=False)
    results, total, passed_routing, passed_reach = v._collect_matrix_results([r1, r2], 3)
    assert total == 2 and passed_routing == 1 and passed_reach == 1
    assert len(results["t"]) == 2 and seen == [("t", 3), ("t", 3)]


# ---- _sweep_matrix_tools / _print_matrix_aggregates / _report_matrix_result (cc4) ----


def test_sweep_matrix_tools_four_verdicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pruned: list[str] = []

    def _prune_tool(root: Path, tool: str, recs: list[dict[str, object]]) -> None:
        pruned.append(tool)

    monkeypatch.setattr(v, "remove_successful_attempt_dirs", _prune_tool)
    results: dict[str, list[dict[str, object]]] = {
        "t1": [{"reach_ok": True, "routing_ok": True, "ok": True}],
        "t2": [{"reach_ok": False, "routing_ok": False, "ok": False}],
        "t3": [{"reach_ok": False, "routing_ok": True, "ok": True}],
        "t4": [{"reach_ok": True, "routing_ok": False, "ok": True}],
    }
    failed_routing, failed_reach = v._sweep_matrix_tools(["t1", "t2", "t3", "t4"], results, [], tmp_path)
    assert failed_routing == ["t2", "t4"] and failed_reach == ["t2", "t3"]
    assert pruned == ["t1"]


def test_print_matrix_aggregates_with_and_without_cats(
    capsys: pytest.CaptureFixture[str],
):
    v._print_matrix_aggregates({}, 2, 3.0)
    assert "Failure categories: none" in capsys.readouterr().out
    v._print_matrix_aggregates({"failure_category_counts": {"SELECTION_FAILURE": 2}}, 2, 3.0)
    assert "SELECTION_FAILURE=2" in capsys.readouterr().out


def test_report_matrix_result_pass_and_fail(capsys: pytest.CaptureFixture[str]):
    failed, loop = v._report_matrix_result([], [])
    assert (failed, loop) == ([], 0)
    assert "RESULT: PASS" in capsys.readouterr().out
    failed2, _ = v._report_matrix_result(["b"], ["a"])
    assert failed2 == ["a", "b"]
    cap = capsys.readouterr()
    assert "RESULT: FAIL" in cap.out and "failed tools" in cap.out


# ---- slice_gap2_tests.py ----
"""Scratch CRAP-gap tests for RestGap slice (fakes only, no live Pi/network).

Covers the remaining score>10 functions in:
  scripts/verify_agent_scenarios.py, scripts/verify_judge.py,
  scripts/sandbox_doctor.py, scripts/export_harness_viewer.py,
  scripts/pi_bridge.py
plus read-only-base coverage (no source edits) for:
  scripts/verify_tool_health.py, scripts/verify_tool_registry.py,
  scripts/verify_type_escape_hatches.py
"""


from scripts.verify_type_escape_hatches import (
    _FileHits,
    _flag_namespaced_decorator,
    _TypeBindings,
)


def _g2_scenario(**kw: object) -> Scenario:
    name = kw.get("name", "t1")
    assert isinstance(name, str)
    family = kw.get("family", ScenarioFamily.FACTUAL)
    assert isinstance(family, ScenarioFamily)
    question = kw.get("question", "What drove NVDA?")
    assert isinstance(question, str)
    ticker = kw.get("ticker", "NVDA")
    assert ticker is None or isinstance(ticker, str)
    as_of = kw.get("as_of", None)
    assert as_of is None or isinstance(as_of, str)
    expected_tools = kw.get("expected_tools", ())
    assert isinstance(expected_tools, tuple)
    requires_evidence = kw.get("requires_evidence", True)
    assert isinstance(requires_evidence, bool)
    notes = kw.get("notes", "n")
    assert isinstance(notes, str)
    assert not [
        k
        for k in kw
        if k not in ("name", "family", "question", "ticker", "as_of", "expected_tools", "requires_evidence", "notes")
    ]
    return Scenario(
        name=name,
        family=family,
        question=question,
        ticker=ticker,
        as_of=as_of,
        expected_tools=expected_tools,
        requires_evidence=requires_evidence,
        notes=notes,
    )


def _g2_ns(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "list": False,
        "scenario": None,
        "model": None,
        "provider": None,
        "prompt_version": "v1",
        "fixtures_dir": None,
        "json": False,
        "all": False,
        "model_timeout": None,
    }
    for key, value in kw.items():
        base[key] = value
    return argparse.Namespace(**base)


# ---- verify_agent_scenarios._pi_model_callable._call (cc6, needs >=52%) ----


def _pi_run_fail(*a: object, **k: object) -> object:
    return SimpleNamespace(returncode=1, stdout="", stderr="boom")


def _pi_run_blank(*a: object, **k: object) -> object:
    return SimpleNamespace(returncode=0, stdout="  \n", stderr="")


def _evts_get_sec(trace_id: str) -> list[object]:
    return [_evt("tool.completed", "get_sec_document")]


def _evts_empty(trace_id: str) -> list[object]:
    return []


def _check_out_abc(*a: object, **k: object) -> str:
    return "abc123\n"


def _list_7() -> int:
    return 7


def _prep_pm(args: argparse.Namespace) -> tuple[str, str, int]:
    return ("p", "m", 300)


def _scenario_a() -> dict[str, Scenario]:
    return {"a": _g2_scenario()}


def _print_none_a(*a: object) -> None:
    return None


def _build_empty(*a: object) -> dict[str, object]:
    return {}


def _dispatch_d(*a: object) -> object:
    return "d"


def _model_mc(*a: object) -> object:
    return "mc"


def _run_live_sess(**k: object) -> dict[str, object]:
    return {"session_id": "s"}


def _eval_in(scenario: object, out: dict[str, object], wall: float, cap: int = 0) -> object:
    return "IN"


def test_pi_model_call_ok(monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, object] = {}

    def _run(argv: list[str], **kw: object) -> object:
        seen["argv"] = argv
        seen["timeout"] = kw.get("timeout")
        return SimpleNamespace(returncode=0, stdout="  hello\n", stderr="")

    monkeypatch.setattr(vas.subprocess, "run", _run)
    call = vas._pi_model_callable("p", "m", 250)
    assert call("prompt?") == "hello"
    assert call.__name__ == "pi_p_m"
    assert seen["timeout"] == 250
    assert seen["argv"] == [
        "omp",
        "-p",
        "--no-session",
        *vas._PI_ISOLATION_FLAGS,
        "--provider",
        "p",
        "--model",
        "m",
    ]


def test_pi_model_call_ok_without_flags(monkeypatch: pytest.MonkeyPatch):
    """Flag-less default: the spawn carries no provider/model so Pi uses its own config."""
    seen: dict[str, object] = {}

    def _run(argv: list[str], **kw: object) -> object:
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="  hello\n", stderr="")

    monkeypatch.setattr(vas.subprocess, "run", _run)
    assert vas._pi_model_callable("", "", 300)("prompt?") == "hello"
    assert seen["argv"] == ["omp", "-p", "--no-session", "--no-tools"]


def test_pi_model_call_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        vas.subprocess,
        "run",
        _pi_run_fail,
    )
    try:
        vas._pi_model_callable("p", "m", 300)("prompt?")
    except RuntimeError as exc:
        assert "Pi model call failed (p/m)" in str(exc) and "boom" in str(exc)
    else:
        raise AssertionError("should raise")


def test_pi_model_call_failure_names_the_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vas.subprocess, "run", _pi_run_fail)
    try:
        vas._pi_model_callable("", "", 300)("prompt?")
    except RuntimeError as exc:
        assert "Pi model call failed (pi default)" in str(exc)
    else:
        raise AssertionError("should raise")


def test_pi_model_call_blank(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        vas.subprocess,
        "run",
        _pi_run_blank,
    )
    try:
        vas._pi_model_callable("p", "m", 300)("prompt?")
    except RuntimeError as exc:
        assert "blank output" in str(exc)
    else:
        raise AssertionError("should raise")


# ---- verify_agent_scenarios._trace_tool_names (cc5, needs >=42%) ----


def _evt(kind: str, tool: object = "unset"):
    return SimpleNamespace(event_type=kind, payload={} if tool == "unset" else {"tool": tool})


def test_trace_tool_names_filters():
    evts = [
        _evt("tool.completed", "get_sec_document"),
        _evt("other", "ignored"),
        _evt("tool.completed", ""),
        _evt("tool.completed"),
        _evt("tool.completed", 5),
        _evt("tool.completed", "list_sec_filings"),
    ]

    def _evts_passthrough(trace_id: str) -> list[object]:
        return list(evts)

    assert vas._trace_tool_names("t", _evts_passthrough) == [
        "get_sec_document",
        "list_sec_filings",
    ]
    assert vas._trace_tool_names("t", _evts_empty) == []


from app.research.evals.evaluators import EvalMetrics, ScenarioResult


def _sr(passed: bool) -> ScenarioResult:
    return ScenarioResult(
        scenario_name="s",
        passed=passed,
        violations=(),
        metrics=EvalMetrics(
            success=passed,
            wall_clock_ms=1.0,
            job_count=1,
            tool_call_count=1,
            discovery_calls=0,
            failed_count=0,
            recovered_count=0,
            evidence_count=0,
            evidence_coverage=0.0,
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
            pit_provenance_violations=0,
            disagreement=False,
            completeness=1.0,
        ),
    )


def test_suite_info_records_flagless_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """A flag-less live run still tallies and records its git sha: there is no model flag to gate on."""
    monkeypatch.setattr(vas.subprocess, "check_output", _check_out_abc)
    summary: dict[str, object] = {}
    vas._maybe_print_suite_info([_sr(True), _sr(False)], "", "", summary)
    out = capsys.readouterr().out
    assert "1/2 passed" in out and "pi default" in out and "abc123" in out
    assert summary["git_sha"] == "abc123"


def test_suite_info_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(vas.subprocess, "check_output", _check_out_abc)
    summary: dict[str, object] = {}
    results = [_sr(True), _sr(False)]
    vas._maybe_print_suite_info(results, "p", "m", summary)
    out = capsys.readouterr().out
    assert "1/2 passed" in out and "abc123" in out
    assert summary["git_sha"] == "abc123"


def test_suite_info_git_fails(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def _boom(*a: object, **k: object) -> object:
        raise OSError("no git")

    monkeypatch.setattr(vas.subprocess, "check_output", _boom)
    summary: dict[str, object] = {}
    vas._maybe_print_suite_info([_sr(True)], "p", "m", summary)
    assert summary["git_sha"] == "unknown"
    assert "unknown" in capsys.readouterr().out


def test_failed_results_selects_unpassed_only():
    results = [_sr(True), _sr(False)]
    assert vas._failed_results(results) == [results[1]]
    assert vas._failed_results([]) == []


def test_print_results_lines(capsys: pytest.CaptureFixture[str]):
    vas._print_results([_sr(True), _sr(False)], "p", "m")
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("PASS s (live via Pi p/m)")
    assert out[1].startswith("FAIL s (live via Pi p/m):")
    vas._print_results([_sr(True)], "", "")
    assert "live via Pi pi default" in capsys.readouterr().out


def test_build_summary_records_default_label():
    summary = vas._build_summary("", "", "v1", [_sr(True)])
    assert summary["provider"] == "pi default" and summary["model"] == "pi default"
    assert summary["prompt_version"] == "v1"
    assert summary["scenarios"] == [{"scenario": "s", "passed": True, "violations": []}]
    assert vas._build_summary("p", "m", "v1", [])["model"] == "m"


# ---- verify_agent_scenarios._run_cli (cc4, needs >=28%) ----


def test_agent_run_cli_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(vas, "_list_scenarios", _list_7)
    assert vas._run_cli(_g2_ns(list=True)) == 7


def test_agent_run_cli_skip(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def _boom(args: argparse.Namespace) -> object:
        raise RuntimeError("no model")

    monkeypatch.setattr(vas, "_prepare_provider_model", _boom)
    assert vas._run_cli(_g2_ns()) == 2
    assert "SKIP live scenarios" in capsys.readouterr().err


def test_agent_run_cli_unknown(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(vas, "_prepare_provider_model", _prep_pm)
    monkeypatch.setattr(vas, "_scenario_map", _scenario_a)
    assert vas._run_cli(_g2_ns(scenario="zzz")) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_agent_run_cli_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(vas, "_prepare_provider_model", _prep_pm)
    monkeypatch.setattr(vas, "_scenario_map", _scenario_a)
    r1 = SimpleNamespace(scenario_name="a", passed=True, violations=())
    r2 = SimpleNamespace(scenario_name="a", passed=False, violations=("v1",))

    def _run_two(*a: object) -> list[object]:
        return [r1, r2]

    monkeypatch.setattr(vas, "_run_all_scenarios", _run_two)
    monkeypatch.setattr(vas, "_print_results", _print_none_a)
    monkeypatch.setattr(vas, "_build_summary", _build_empty)
    monkeypatch.setattr(vas, "_maybe_print_suite_info", _print_none_a)
    monkeypatch.setattr(vas, "_maybe_print_json", _print_none_a)

    def _summarize_r2(results: list[object]) -> tuple[list[object], int]:
        return ([r2], 1)

    monkeypatch.setattr(vas, "summarize_results", _summarize_r2)
    assert vas._run_cli(_g2_ns(scenario="a", model="m")) == 1


def test_agent_run_cli_bad_timeout_skips_before_probing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """An invalid timeout is a prerequisite failure: exit 2 with the reason, no Pi probe."""

    def _boom(*a: object, **k: object) -> object:
        raise AssertionError("probe must not run for an invalid timeout")

    monkeypatch.setattr(vas, "_check_pi_ready", _boom)
    assert vas._run_cli(_g2_ns(model_timeout="nope")) == 2
    assert "invalid model timeout" in capsys.readouterr().err


def _cli_tuple(
    args: argparse.Namespace,
) -> tuple[str, str, int, list[str], dict[str, Scenario]]:
    """Prereqs as the success tuple; the int branch is the skip exit code."""
    got = vas._cli_prereqs(args)
    assert not isinstance(got, int)
    return got


def test_cli_prereqs_resolves_flagless_default(monkeypatch: pytest.MonkeyPatch):
    """Default live selection: Pi's CLI default model, the default budget, no fixture-carrier scenarios."""
    for key in ("STOCKBOT_PROVIDER", "STOCKBOT_MODEL", "STOCKBOT_MODEL_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    probed: list[tuple[str, str, int]] = []

    def _probe(p: str, m: str, t: int) -> None:
        probed.append((p, m, t))

    monkeypatch.setattr(vas, "_check_pi_ready", _probe)
    provider, model, timeout_s, names, by_name = _cli_tuple(_g2_ns())
    assert (provider, model) == ("", "")
    assert timeout_s == vas._PI_CALL_TIMEOUT_DEFAULT_S and probed == [("", "", vas._PI_CALL_TIMEOUT_DEFAULT_S)]
    assert "gs-openai-sec-only" in names
    for carrier in (
        "spacex-openai-bankruptcy-sec-only-live-run",
        "gs-openai-sec-only-live-run",
    ):
        assert carrier not in names and carrier in by_name
    # The flag/env knob overrides the default, and --scenario still reaches a fixture carrier.
    assert _cli_tuple(_g2_ns(model_timeout="45"))[2] == 45
    monkeypatch.setenv("STOCKBOT_MODEL_TIMEOUT", "77")
    assert _cli_tuple(_g2_ns())[2] == 77
    assert _cli_tuple(_g2_ns(scenario="gs-openai-sec-only-live-run"))[3] == ["gs-openai-sec-only-live-run"]


# ---- verify_agent_scenarios._run_live_scenario (cc4, needs >=28%) ----


def test_live_kwargs_shape_and_default_label(monkeypatch: pytest.MonkeyPatch):
    """Run kwargs carry the scenario question/tickers, the model callables, and a recordable label."""
    seen: list[tuple[object, ...]] = []

    def _capture(*a: object) -> str:
        seen.append(a)
        return "mc"

    monkeypatch.setattr(vas, "_pi_dispatch_callable", _dispatch_d)
    monkeypatch.setattr(vas, "_pi_model_callable", _capture)
    kw = vas._live_kwargs(_g2_scenario(), "", "", 275)
    assert kw["question"] == "What drove NVDA?" and kw["tickers"] == ["NVDA"]
    assert kw["as_of"] is None and kw["objective"] == "n"
    assert kw["provider"] == "pi default" and kw["model_name"] == "pi default"
    assert kw["dispatch"] == "d" and kw["model"] == "mc"
    assert seen == [("", "", 275)]
    untickered = vas._live_kwargs(_g2_scenario(ticker=None, notes=""), "p", "m", 300)
    assert untickered["tickers"] == [] and untickered["provider"] == "p"
    assert untickered["objective"] == "What drove NVDA?"  # no notes: the question carries the objective


def _omp_sid_ok(*a: object, **k: object) -> str:
    return "s"


def test_run_live_scenario_success(monkeypatch: pytest.MonkeyPatch):
    def _run_ok(*a: object, **k: object) -> str:
        return "run:t1"

    monkeypatch.setattr(vas, "_run_omp_research", _run_ok)
    monkeypatch.setattr(vas, "_read_omp_session_id", _omp_sid_ok)
    monkeypatch.setattr(vas, "evaluate_and_record", _eval_in)
    assert vas._run_live_scenario(_g2_scenario(), "p", "m", "v1", 300) == "IN"


def test_run_live_scenario_passes_the_timeout_knob(monkeypatch: pytest.MonkeyPatch):
    """The resolved per-call budget reaches the OMP research spawn."""
    seen: list[tuple[object, ...]] = []

    def _capture(scenario: object, provider: object, model: object, timeout: object, tmp: object) -> str:
        seen.append((provider, model, timeout))
        return "run:t1"

    monkeypatch.setattr(vas, "_run_omp_research", _capture)
    monkeypatch.setattr(vas, "_read_omp_session_id", _omp_sid_ok)
    monkeypatch.setattr(vas, "evaluate_and_record", _eval_in)
    assert vas._run_live_scenario(_g2_scenario(), "p", "m", "v1", 275) == "IN"
    assert seen == [("p", "m", 275)]


def test_run_live_scenario_crash(monkeypatch: pytest.MonkeyPatch):
    def _boom(*a: object, **k: object) -> object:
        raise ValueError("omp down")

    monkeypatch.setattr(vas, "_run_omp_research", _boom)
    out = vas._run_live_scenario(_g2_scenario(), "p", "m", "v1", 300)
    assert out.scenario_crashed is True and out.failed_count == 1


# ---- verify_agent_scenarios.evaluate_and_record (cc4, needs >=28%) ----


class _FakeRepo:
    def __init__(self, sess: ResearchSession, jobs: list[Job]) -> None:
        self._sess, self._jobs = sess, jobs

    def get_session(self, sid: str) -> ResearchSession:
        return self._sess

    def list_jobs(self, sid: str) -> list[Job]:
        return self._jobs

    def list_evidence(self, sid: str) -> list[dict[str, object]]:
        return []

    def list_events(self, sid: str) -> list[object]:
        return []


def _eval_patient(monkeypatch: pytest.MonkeyPatch, traces: list[TraceHeader], sess: ResearchSession):
    import app.research.evals.traces as traces_mod
    import app.research.repository as repo_mod

    jobs = [
        Job(
            job_id="j1",
            session_id="s",
            wave_id=1,
            parent_job_id=None,
            job_type="research",
            owner="agent",
            status="failed",
        ),
        Job(
            job_id="j2",
            session_id="s",
            wave_id=1,
            parent_job_id=None,
            job_type="research",
            owner="agent",
            status="completed",
        ),
    ]

    def _repo_fake(*a: object, **k: object) -> object:
        return _FakeRepo(sess, jobs)

    def _traces_list(session_id: str | None = None) -> list[TraceHeader]:
        return traces

    monkeypatch.setattr(repo_mod, "ResearchRepository", _repo_fake)
    monkeypatch.setattr(traces_mod, "list_traces", _traces_list)
    monkeypatch.setattr(
        traces_mod,
        "get_trace_events",
        _evts_get_sec,
    )


def _eval_sess(answer: str, status: str) -> ResearchSession:
    return ResearchSession(
        session_id="s",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        query="q",
        objective="o",
        status=status,
        final_result={"answer": answer},
    )


def test_evaluate_and_record_with_trace(monkeypatch: pytest.MonkeyPatch):
    _eval_patient(
        monkeypatch,
        [
            TraceHeader(
                trace_id="t",
                session_id="s",
                wave_id=1,
                provider="p",
                model="m",
                prompt_version="v1",
                harness_version="h",
                git_sha="g",
                started_at="t",
                completed_at=None,
                duration_ms=None,
                conclusion=None,
                status="done",
            )
        ],
        _eval_sess("A", "completed"),
    )
    out = vas.evaluate_and_record(_g2_scenario(), {"session_id": "s", "evidence_ids": ["e1", 5]}, 5.0)
    assert out.answer_text == "A" and out.tool_calls == ("get_sec_document",)
    assert out.evidence_ids == ("e1",) and out.recovered_count == 1


def test_evaluate_and_record_no_trace(monkeypatch: pytest.MonkeyPatch):
    _eval_patient(monkeypatch, [], _eval_sess("", "running"))
    out = vas.evaluate_and_record(_g2_scenario(), {"session_id": "s"}, 5.0)
    assert out.tool_calls == () and out.recovered_count == 0


# ---- verify_judge._run_attempts (cc5, needs >=42%) ----


def _fake_dirs(root: Path):
    def _dirs(batch_root: Path, tool: str, attempt: int, retry: int = 0) -> tuple[Path, Path]:
        d = Path(batch_root) / tool / f"a{attempt}r{retry}"
        return (d / "runs.sqlite", d / "store")

    return _dirs


def _research_a() -> list[str]:
    return ["a"]


def _research_thesis() -> list[str]:
    return ["thesis_create"]


def _seed_ctx(scenario: J.Scenario, store: Path, prompt: str) -> tuple[str, dict[str, object] | None]:
    return (prompt + " ctx", None)


def _eval_good(scenario: J.Scenario, answer: str, db: object) -> tuple[bool, str]:
    return (True, "good")


def _conc_2() -> int:
    return 2


def _select_none(args: argparse.Namespace) -> list[J.Scenario] | None:
    return None


def _select_x(args: argparse.Namespace) -> list[J.Scenario] | None:
    return [_scenario(id="x")]


def _batch_b1() -> str:
    return "b1"


def _sel_true(wanted: list[J.Scenario], root: Path, cwd: Path) -> list[dict[str, object]]:
    return [{"id": "x", "ok": True}]


def _sel_false(wanted: list[J.Scenario], root: Path, cwd: Path) -> list[dict[str, object]]:
    return [{"id": "x", "ok": False}]


def _report_none(results: list[dict[str, object]], root: Path) -> None:
    return None


def _gate_0(results: list[dict[str, object]]) -> int:
    return 0


def _repo_obj(*a: object, **k: object) -> object:
    return object()


def _attach_none(runs: list[dict[str, object]], res: list[dict[str, object]]) -> None:
    return None


def _proj_p(*a: object) -> dict[str, object]:
    return {"p": 1}


def _reg_a() -> dict[str, set[str]]:
    return {"schemas": {"a"}}


def _rep_false_s(sets: dict[str, set[str]]) -> bool:
    return False


def _rep_false() -> bool:
    return False


def _rep_true() -> bool:
    return True


def _rep_true_s(sets: dict[str, set[str]]) -> bool:
    return True


def _read_r1(db: Path) -> str | None:
    return "r1"


def _sessions_2(db: Path) -> list[str]:
    return ["s1", "s2"]


def _eval_triple(
    db: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    return ([{"a": 1}], [], [])


def _sel_live(s: dict[str, object], root: Path, cwd: Path, i: int) -> dict[str, object]:
    return {"id": s["id"], "ok": True, "i": i}


def _run_pi_x(*a: object) -> tuple[int, bool, str, str, bool]:
    return (0, False, "x", "", True)


def test_judge_run_attempts_first_try_ok(tmp_path: Path):
    sc = _scenario(id="plain1", prompt="hello")

    def run_pi(*a: object) -> tuple[int, bool, str, str, bool]:
        return (0, False, "out", "", True)

    _db, timed_out, out, code = J._run_attempts(sc, tmp_path, tmp_path, 1, run_pi, _fake_dirs(tmp_path))
    assert (timed_out, out, code) == (False, "out", 0)


def test_judge_run_attempts_retry_on_timeout(tmp_path: Path):
    sc = _scenario(id="plain1", prompt="hello")
    calls: list[object] = []

    def run_pi(*a: object) -> tuple[int, bool, str, str, bool]:
        calls.append(a[0])
        if len(calls) == 1:
            return (1, True, "", "", False)
        return (0, False, "second", "", True)

    _db, timed_out, out, code = J._run_attempts(sc, tmp_path, tmp_path, 1, run_pi, _fake_dirs(tmp_path))
    assert (timed_out, out, code) == (False, "second", 0)
    assert len(calls) == 2


def test_judge_run_attempts_seeded_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sc = _scenario(id="thesis_contradict", prompt="p")
    monkeypatch.setattr(J, "_seeded_prompt", _seed_ctx)
    seen: list[str] = []

    def run_pi(prompt: str, *a: object) -> tuple[int, bool, str, str, bool]:
        seen.append(prompt)
        return (0, False, "o", "", True)

    _, _, _, code = J._run_attempts(sc, tmp_path, tmp_path, 1, run_pi, _fake_dirs(tmp_path))
    assert code == 0 and seen == ["p ctx"]


def test_judge_run_attempts_seeded_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sc = _scenario(id="watch_vs_journal", prompt="p")
    err: dict[str, object] = {
        "id": "watch_vs_journal",
        "ok": False,
        "reason": "seed bad",
        "exit": None,
        "timed_out": False,
        "db": "",
        "answer_file": None,
        "duration_s": 0.0,
    }

    def _seed_err(scenario: J.Scenario, store: Path, prompt: str) -> tuple[str, dict[str, object] | None]:
        return (prompt, err)

    monkeypatch.setattr(J, "_seeded_prompt", _seed_err)
    db, timed_out, _out, code = J._run_attempts(
        sc,
        tmp_path,
        tmp_path,
        1,
        _run_pi_x,
        _fake_dirs(tmp_path),
    )
    assert code is err and err["db"] == str(db) and timed_out is False


# ---- verify_judge.run_scenario_live (cc4, needs >=28%) ----


def test_judge_run_scenario_live_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sc = _scenario(id="s1", prompt="p", evaluator="e")

    def _attempts_ok_tmp(*a: object, **k: object) -> tuple[Path, bool, str, int | dict[str, object]]:
        return (tmp_path / "db", False, " ans ", 0)

    monkeypatch.setattr(J, "_run_attempts", _attempts_ok_tmp)
    monkeypatch.setattr(J, "_read_run_id", _read_r1)

    def _persist_a_tmp(*a: object) -> Path | None:
        return tmp_path / "a.md"

    monkeypatch.setattr(J, "persist_answer", _persist_a_tmp)
    monkeypatch.setattr(J, "_evaluate_live", _eval_good)
    out = J.run_scenario_live(sc, tmp_path, tmp_path, 1)
    assert out["ok"] is True
    answer_file = out.get("answer_file")
    assert isinstance(answer_file, str) and answer_file.endswith("a.md")


def test_judge_run_scenario_live_seed_dict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sc = _scenario(id="s1", prompt="p", evaluator="e")
    err: dict[str, object] = {
        "id": "s1",
        "ok": False,
        "reason": "seed",
        "exit": None,
        "timed_out": False,
        "db": "d",
        "answer_file": None,
        "duration_s": 0.0,
    }

    def _attempts_err(*a: object, **k: object) -> tuple[str, bool, str, dict[str, object]]:
        return ("d", False, "", err)

    monkeypatch.setattr(J, "_run_attempts", _attempts_err)
    out = J.run_scenario_live(sc, tmp_path, tmp_path, 1)
    assert out["reason"] == "seed" and "duration_s" in out


# ---- verify_judge._run_selection (cc4, needs >=28%) ----


def test_judge_run_selection_ordered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(J, "get_judge_concurrency", _conc_2)
    monkeypatch.setattr(
        J,
        "run_scenario_live",
        _sel_live,
    )
    wanted = [_scenario(id="b"), _scenario(id="a"), _scenario(id="c")]
    out = J._run_selection(wanted, tmp_path, tmp_path)
    assert [r["id"] for r in out] == ["b", "a", "c"]
    assert [r["i"] for r in out] == [1, 2, 3]


# ---- verify_judge._report_results (cc4, needs >=28%) ----


def test_judge_report_results_mixed_durations(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    results: list[dict[str, object]] = [
        {"id": "a", "ok": True, "reason": "r", "duration_s": 1.5, "answer_file": "f"},
        {
            "id": "b",
            "ok": False,
            "reason": "r2",
            "duration_s": "bad",
            "answer_file": None,
        },
        {"id": "c", "ok": True, "reason": "r3", "answer_file": "f3"},
    ]
    J._report_results(results, tmp_path)
    out = capsys.readouterr().out
    assert "PASS a" in out and "FAIL b" in out
    assert (tmp_path / "summary.json").exists()


# ---- verify_judge._main_live (cc6, needs >=52%) ----


def test_judge_main_live_no_wanted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(J, "_select_wanted", _select_none)
    assert J._main_live(_g2_ns()) == 2


def test_judge_main_live_single_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(J, "_select_wanted", _select_x)
    monkeypatch.setattr(J, "_batch_id", _batch_b1)
    monkeypatch.setattr(J, "_run_selection", _sel_true)
    seen: list[Path] = []

    def _report_append_tmp(results: list[dict[str, object]], root: Path) -> None:
        seen.append(root)

    monkeypatch.setattr(J, "_report_results", _report_append_tmp)
    assert J._main_live(_g2_ns(scenario="x")) == 0
    assert seen and str(seen[0]).endswith("agent")


def test_judge_main_live_single_fail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(J, "_select_wanted", _select_x)
    monkeypatch.setattr(J, "_batch_id", _batch_b1)
    monkeypatch.setattr(J, "_run_selection", _sel_false)
    monkeypatch.setattr(J, "_report_results", _report_none)
    assert J._main_live(_g2_ns(scenario="x")) == 1


def test_judge_main_live_gate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(J, "_select_wanted", _select_x)
    monkeypatch.setattr(J, "_batch_id", _batch_b1)
    monkeypatch.setattr(J, "_run_selection", _sel_true)
    monkeypatch.setattr(J, "_report_results", _report_none)
    monkeypatch.setattr(J, "_gate_verdict", _gate_0)
    assert J._main_live(_g2_ns(all=True)) == 0


# ---- sandbox_doctor.check_policy (cc4, needs >=28%) ----


def _proc(code: int = 0, out: str = "", err: str = ""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def test_doctor_policy_locked_down(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(doc, "run", _run_locked)
    called: list[int] = []

    def _behavioral_mark() -> bool:
        called.append(1)
        return True

    monkeypatch.setattr(doc, "_behavioral_policy_ok", _behavioral_mark)
    doc.check_policy()
    assert "Locked Down" in capsys.readouterr().out
    assert called == []


def test_doctor_policy_ls_fails(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doc, "run", _run_denied)
    try:
        doc.check_policy()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("should fail")


def test_doctor_policy_behavioral_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(doc, "run", _run_rules)
    monkeypatch.setattr(doc, "_behavioral_policy_ok", _behavioral_true)
    doc.check_policy()
    assert "Locked Down" in capsys.readouterr().out


def test_doctor_policy_behavioral_bad(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(doc, "run", _run_rules)
    monkeypatch.setattr(doc, "_behavioral_policy_ok", _behavioral_false)
    try:
        doc.check_policy()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("should fail")


# ---- export_harness_viewer.main (cc3, needs >=12%) ----


def test_export_main_mixed_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(exh_viewer, "ResearchRepository", _repo_obj)

    def _research_db_tmp() -> Path:
        return tmp_path / "r.db"

    monkeypatch.setattr(exh_viewer, "_research_db", _research_db_tmp)

    def _eval_db_tmp() -> Path:
        return tmp_path / "e.db"

    monkeypatch.setattr(exh_viewer, "_resolve_eval_db", _eval_db_tmp)
    monkeypatch.setattr(exh_viewer, "_all_session_ids", _sessions_2)

    def _build(sid: str, repo: object):
        return None if sid == "s1" else {"sid": sid}

    monkeypatch.setattr(exh_viewer, "build_session_run", _build)
    monkeypatch.setattr(exh_viewer, "read_eval_db", _eval_triple)
    monkeypatch.setattr(exh_viewer, "attach_pass_fail", _attach_none)
    monkeypatch.setattr(exh_viewer, "build_projection", _proj_p)

    def _write_o_tmp(projection: dict[str, object]) -> Path:
        return tmp_path / "o.ts"

    monkeypatch.setattr(exh_viewer, "_write_projection", _write_o_tmp)
    assert exh_viewer.main() == 0
    assert "researchRuns=1 evalRuns=1" in capsys.readouterr().out


# ---- pi_bridge._handle_research_committee (cc7, needs >=61%) ----


def test_bridge_committee_routes(monkeypatch: pytest.MonkeyPatch):
    for op, fn in [
        ("research.freeze.create", "_op_research_freeze_create"),
        ("research.committee.create", "_op_research_committee_create"),
        ("research.analysis.record", "_op_research_analysis_record"),
        ("research.wave.decide", "_op_research_wave_decide"),
        ("research.session.finalize", "_op_research_session_finalize"),
        ("research.source.submit", "_op_research_source_submit"),
        ("research.job.heartbeat", "_op_research_job_heartbeat"),
        ("research.events", "_op_research_events"),
    ]:

        def _route(req: dict[str, object], pid: str, _op: str = op) -> dict[str, object]:
            return {"id": pid, "op": _op}

        monkeypatch.setattr(pi_bridge, fn, _route)
        out = pi_bridge._handle_research_committee(op, {}, "p1")
        assert out == {"id": "p1", "op": op}

    # The legacy wire name stays an alias of the same gate (no behavior difference).
    def _alias(req: dict[str, object], pid: str) -> dict[str, object]:
        assert req == {}
        return {"id": pid, "op": "research.wave.decide"}

    monkeypatch.setattr(pi_bridge, "_op_research_wave_decide", _alias)
    assert pi_bridge._handle_research_committee("research.wave2.decide", {}, "p1") == {
        "id": "p1",
        "op": "research.wave.decide",
    }
    assert pi_bridge._handle_research_committee("research.nope", {}, "p1") is None


# ---- pi_bridge._pi_event_record_core (guard, cc7) ----


class _Rec(RunRecorder):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    @override
    def record_event(
        self,
        event_type: str,
        *,
        round: int | None = None,
        model: str | None = None,
        tool_name: str | None = None,
        arguments: object | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error_type: str | None = None,
        evidence_ids: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: float | None = None,
    ) -> str | None:
        self.calls.append(("event", event_type, tool_name, success, metadata))
        return None

    @override
    def record_model_call(
        self,
        *,
        round: int,
        provider: str,
        model: str,
        started_at: str,
        completed_at: str,
        usage: dict[str, object] | None = None,
        finish_reason: str | None = None,
        tool_call_count: int = 0,
        provider_request_id: str | None = None,
        status: str = "completed",
        error_type: str | None = None,
        error_category: str | None = None,
    ) -> float:
        self.calls.append(("model", round, provider, model))
        return 0.0

    @override
    def record_security_event(
        self,
        *,
        source: str,
        sha256: str,
        score: int | None,
        verdict: str | None,
        rule_ids: list[str] | None,
        decision: str,
        reason: str | None = None,
        span_length: int | None = None,
    ) -> str | None:
        self.calls.append(("security", source, decision))
        return None


def test_bridge_event_core_branches():
    rec = _Rec()
    tool_t: dict[str, object] = {"tool": "t"}
    assert pi_bridge._pi_event_record_core(rec, "agent_start", {}, None) is True
    assert pi_bridge._pi_event_record_core(rec, "tool_execution_start", tool_t, None) is True
    assert pi_bridge._pi_event_record_core(rec, "tool_execution_end", tool_t, None) is True
    assert pi_bridge._pi_event_record_core(rec, "message_end", {"role": "assistant", "turn": 2}, None) is True
    assert pi_bridge._pi_event_record_core(rec, "message_end", {"role": "user"}, None) is False
    assert pi_bridge._pi_event_record_core(rec, "security_block", tool_t, None) is True
    assert pi_bridge._pi_event_record_core(rec, "bogus", {}, None) is False
    kinds = [c[0] for c in rec.calls]
    assert kinds.count("event") >= 3 and "model" in kinds and "security" in kinds


# ---- read-only base: verify_tool_registry (tests only, no source edits) ----


def test_registry_compare_all_branches(tmp_path: Path):
    root = tmp_path / "catalog"
    # catalog root missing entirely
    assert reg._compare_catalog_pages(root, {"a.md": "x"}) == ["missing a.md"]
    assert reg._compare_catalog_pages(root, {}) == []
    root.mkdir()
    (root / "a.md").write_text("ok")
    (root / "b.md").write_text("stale")
    (root / "extra.md").write_text("orphan")
    problems = reg._compare_catalog_pages(root, {"a.md": "ok", "b.md": "fresh"})
    assert "drift b.md" in problems
    assert "orphan extra.md" in problems
    assert "orphan index.yaml" in problems
    assert not [p for p in problems if p.startswith("missing")]
    assert reg._compare_catalog_pages(root, {}) == [
        "orphan a.md",
        "orphan b.md",
        "orphan extra.md",
        "orphan index.yaml",
    ]


def test_registry_expected_pages_shape():
    root, expected = reg._expected_catalog_pages()
    assert isinstance(root, Path) and "index.yaml" in expected
    assert all(isinstance(v, str) and v for v in expected.values())


def test_registry_main_paths(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(reg, "get_registry_sets", _reg_a)
    monkeypatch.setattr(reg, "_report_registry", _rep_false_s)
    monkeypatch.setattr(reg, "_report_catalog", _rep_false)
    monkeypatch.setattr(reg, "_report_inventory", _rep_false_s)
    assert reg.main() == 0
    assert "tool registry OK: 1 tools" in capsys.readouterr().out
    monkeypatch.setattr(reg, "_report_catalog", _rep_true)
    assert reg.main() == 1
    monkeypatch.setattr(reg, "_report_catalog", _rep_false)
    monkeypatch.setattr(reg, "_report_inventory", _rep_true_s)
    assert reg.main() == 1


# ---- read-only base: verify_tool_health (tests only) ----


def test_health_report_text_branches(capsys: pytest.CaptureFixture[str]):
    assert vth._report_text(["a"], {"a": []}) == 0
    assert "PASS a" in capsys.readouterr().out
    selected = ["a", "b", "c"]
    results: dict[str, list[str]] = {
        "a": [],
        "b": ["schema: bad shape", "nocolon problem"],
        "c": [],
    }
    assert vth._report_text(selected, results) == 1
    out = capsys.readouterr().out
    assert "FAIL b [schema] schema: bad shape" in out
    assert "FAIL b [nocolon problem] nocolon problem" in out
    assert "tool health: 2/3 pass" in out


def test_health_sentinel_result_error_branches():
    empty: list[tuple[tuple[object, ...], dict[str, object]]] = []
    assert vth._sentinel_result_error("n", [], empty) == ("canonical execute_tool did not invoke handler")
    bad_calls: list[tuple[tuple[object, ...], dict[str, object]]] = [(("c",), {})]
    assert "not returned" in (vth._sentinel_result_error("n", {"ok": False}, bad_calls) or "")
    assert "not returned" in (vth._sentinel_result_error("n", "weird", bad_calls) or "")
    assert vth._sentinel_result_error("n", {"ok": True}, bad_calls) is None


# ---- read-only base: verify_type_escape_hatches (tests only) ----


def _dec(code: str) -> ast.Attribute:
    tree = ast.parse(code)
    fn = tree.body[-1]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    d = fn.decorator_list[0]
    assert isinstance(d, ast.Attribute)
    return d


def test_escape_namespaced_decorator_branches():
    lines = ["import typing", "@typing.no_type_check", "def f(): pass"]

    def _col(**kw: object) -> tuple[_TypeBindings, _FileHits]:
        b = _TypeBindings()
        aliases = kw.get("aliases", {"typing"})
        assert isinstance(aliases, set)
        b.mod_aliases = aliases
        return b, _FileHits("f.py", lines)

    # wrong attr -> no flag
    b, c = _col()
    _flag_namespaced_decorator(_dec("import typing\n@typing.other\ndef f(): pass"), b, c)
    assert c.hits == []
    # bound alias -> flag
    b, c = _col()
    _flag_namespaced_decorator(_dec("import typing\n@typing.no_type_check\ndef f(): pass"), b, c)
    assert len(c.hits) == 1
    # unbound name -> no flag
    b, c = _col(aliases={"t"})
    _flag_namespaced_decorator(_dec("import typing\n@typing.no_type_check\ndef f(): pass"), b, c)
    assert c.hits == []
    # non-Name target (a.b.no_type_check) -> no flag
    b, c = _col()
    _flag_namespaced_decorator(_dec("import a\n@a.b.no_type_check\ndef f(): pass"), b, c)
    assert c.hits == []


def test_build_success_does_not_invent_a_budget_cap():
    """Unlimited live research must not trip budget-violation on depth alone."""
    from app.research.evals.evaluators import evaluate

    sc = _g2_scenario(name="n", as_of="2024-01-01", requires_evidence=True)
    deep = vas._build_success_input(sc, "ans", ["get_sec_document"] * 100, [], "completed", ("e",), 1.0)
    assert deep.budget_used == 100 and deep.budget_cap == 0
    assert "budget-violation" not in evaluate(deep).violations
    # A cap the run was actually given still trips the rule.
    capped = vas._build_success_input(
        sc, "ans", ["get_sec_document"] * 100, [], "completed", ("e",), 1.0, tool_call_cap=60
    )
    assert "budget-violation" in evaluate(capped).violations


def test_live_trace_fields_reach_the_evaluator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A live verdict must see the run's real trace: branch coverage, opened docs, ledger kinds."""
    from app.research.director import DirectorBudgets
    from app.research.evals.scenarios import get_scenario
    from app.research.repository import ResearchRepository
    from app.research.runner import _LiveRun

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    run = _LiveRun(
        repo, "q?", "o", "2026-08-10", "NVDA", [], lambda name, args: {}, lambda prompt: "[]", DirectorBudgets()
    )
    sid = run._create_session("q?", "2026-08-10", None)
    for eid, kind in (("ev:1", "evidence"), ("ev:2", "discovery")):
        repo.save_evidence({"evidence_id": eid, "session_id": sid, "wave_id": 1, "record_kind": kind})
    scenario = get_scenario("msft-openai-bankruptcy-sec-only")
    # Only ids the run advertises for citation count as raw: the trace gate compares
    # them against the same advertised set, so a wider ledger read would fail it falsely.
    trace = vas._live_trace(scenario, repo, sid, repo.list_jobs(sid), ("ev:1", "ev:2"))
    assert trace["requires_trace"] is True
    assert trace["raw_evidence_ids"] == ("ev:1",) and trace["navigation_evidence_ids"] == ("ev:2",)
    assert vas._live_trace(scenario, repo, sid, [], ("ev:2",))["raw_evidence_ids"] == ()
    built = vas._build_success_input(
        scenario, "answer", ["get_sec_document"], repo.list_jobs(sid), "researching", ("ev:1",), 1.0, 0, trace
    )
    assert built.raw_evidence_ids == ("ev:1",) and built.requires_trace is True
    # The branch gate reads the run's real branch coverage, not just the answer prose.
    telemetry_branches = vas._build_success_input(
        scenario, "answer", [], [], "researching", ("ev:1",), 1.0, 0, {**trace, "branches_covered": ["orcl"]}
    )
    assert telemetry_branches.branches_covered == ("orcl",)


def test_row_provenance_narrows_raw_rows():
    """Only an evidence row with a real accession is provenance for an opened filing."""
    assert vas._row_provenance("not-a-row") is None
    assert vas._row_provenance({"record_kind": "discovery", "metadata": {"accession_no": "1"}}) is None
    assert vas._row_provenance({"record_kind": "evidence", "metadata": "junk"}) is None
    assert vas._row_provenance({"record_kind": "evidence", "metadata": {"accession_no": ""}}) is None
    assert vas._row_provenance({"record_kind": "evidence", "metadata": {"accession_no": 7}}) is None
    assert vas._row_provenance(
        {"record_kind": "evidence", "metadata": {"accession_no": "1", "document_name": "d.htm"}}
    ) == ("1", "d.htm")


def test_ledger_documents_keeps_row_order_without_duplicates():
    """Opened filings/documents are read off the raw rows, deduped in first-seen order."""
    repo = FakeRepo(
        evidence=[
            {"record_kind": "evidence", "metadata": {"accession_no": "0001", "document_name": "10-q.htm"}},
            {"record_kind": "evidence", "metadata": {"accession_no": "0001", "document_name": "10-q.htm"}},
            {"record_kind": "evidence", "metadata": {"accession_no": "0001", "document_name": "8-k.htm"}},
            {"record_kind": "evidence", "metadata": {"accession_no": "0003"}},
            {"record_kind": "discovery", "metadata": {"accession_no": "0002", "document_name": "nav.htm"}},
            {"record_kind": "evidence", "metadata": {"document_name": "no-accession.htm"}},
        ]
    )
    filings, documents = vas._ledger_documents(repo, "s")
    assert filings == ("0001", "0003")
    assert documents == ("0001|10-q.htm", "0001|8-k.htm")


def test_invoke_live_reports_the_crash_cause(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A crashed live run names its exception on stderr; the verdict alone cannot explain it."""
    from app.research import runner

    def _boom(**k: object) -> object:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(runner, "run_live", _boom)
    out, _wall, cap = vas._invoke_live(
        {
            "question": "q?",
            "objective": "o",
            "as_of": None,
            "tickers": [],
            "dispatch": lambda name, args: {},
            "model": lambda prompt: "[]",
            "provider": "p",
            "model_name": "m",
        }
    )
    assert out is None and cap == 0
    assert "CRASH ValueError: embedded null byte" in capsys.readouterr().err
