#!/usr/bin/env python3
"""Live broad-agent scenario verification through Pi (no static PASS, no fakes).

Usage:
    python scripts/verify_agent_scenarios.py [--scenario NAME] [--model LABEL]
        [--provider LABEL] [--model-timeout SECONDS] [--prompt-version VER]
        [--fixtures-dir DIR] [--json]
    python scripts/verify_agent_scenarios.py --list

Each scenario invokes the configured run_live/Pi path, captures the resulting
ResearchSession and trace, extracts observable outcomes, and runs deterministic
validators against those outputs. A scenario without executable prerequisites
(Pi reachability) fails or is explicitly skipped with a non-zero clearly
reported prerequisite status; it never passes from static definitions.
--model/--provider select/record the actual provider/model used; with neither
set, Pi runs on its own CLI default and results record "pi default". Exit 0 when
every scenario passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.evals.evaluators import (
    EvalInput,
    ScenarioResult,
    evaluate,
)
from app.research.evals.scenarios import Scenario, list_scenarios
from app.research.models import Job
from app.research.repository import ResearchRepository


def _clean(value: str | None) -> str:
    return (value or "").strip()


# Pi accepts provider/model from its own configuration; with no flag at all the
# CLI default is used. That path has no concrete names to record, so results
# carry this label instead of an empty string.
_DEFAULT_MODEL_LABEL = "pi default"


def _model_label(provider: str, model: str) -> str:
    """Recorded label for the effective model; Pi's CLI default when neither is set."""
    if not provider and not model:
        return _DEFAULT_MODEL_LABEL
    return f"{provider or _DEFAULT_MODEL_LABEL}/{model or _DEFAULT_MODEL_LABEL}"


# Pure-completion OMP call: the runner owns the agent loop, so the model call must
# not dispatch any tool (that would run a second agent loop inside every model
# turn and blow the call timeout). Mirrors the role-spawn flags.
_PI_ISOLATION_FLAGS: tuple[str, ...] = ("--no-tools",)
# One completion, generous enough for a long source-scout prompt. A slow
# provider must not silently kill runs: the budget is a flag/env knob.
_PI_CALL_TIMEOUT_DEFAULT_S = 300


def _pi_flags(provider: str, model: str) -> list[str]:
    """Provider/model flags, omitted when unset so Pi falls back to its CLI default.

    Mirrors app/thesis/omp_runner.py: an unset flag is absent, never empty.
    """
    return (["--provider", provider] if provider else []) + (["--model", model] if model else [])


def _pi_completion_argv(provider: str, model: str, prompt: str) -> list[str]:
    """Isolated one-shot argv for a plain model completion (no tools, no session).

    The prompt travels on stdin, never in argv: a research prompt embedding
    frozen evidence blows past the kernel's per-argument limit (``OSError:
    [Errno 7] Argument list too long``) and NUL bytes cannot be passed at all.
    ``prompt`` is accepted for call-site clarity and size checks.
    """
    del prompt  # delivered via stdin by the caller
    return [
        "omp",
        "-p",
        "--no-session",
        *_PI_ISOLATION_FLAGS,
        *_pi_flags(provider, model),
    ]


def _lookup_env_value(lookup: object, key: str) -> str | None:
    """One env value narrowed to str; None when absent or non-string."""
    if isinstance(lookup, dict):
        value = lookup.get(key)
        return value if isinstance(value, str) else None
    get = getattr(lookup, "get", None)
    if callable(get):
        value = get(key)
        return value if isinstance(value, str) else None
    return None


def resolve_provider_model(provider_arg: str | None, model_arg: str | None, env: object = None) -> tuple[str, str]:
    """Explicit flag, then env, then "" — meaning "let OMP use its CLI default"."""
    lookup: object = os.environ if env is None else env
    provider = _clean(provider_arg) or _clean(_lookup_env_value(lookup, "STOCKBOT_PROVIDER"))
    model = _clean(model_arg) or _clean(_lookup_env_value(lookup, "STOCKBOT_MODEL"))
    return provider, model


def _resolve_provider_model(args: argparse.Namespace) -> tuple[str, str]:
    return resolve_provider_model(args.provider, args.model, os.environ)


def resolve_model_timeout(timeout_arg: str | None, env: object = None) -> int:
    """Per-call model timeout seconds: flag, then STOCKBOT_MODEL_TIMEOUT, then the default."""
    lookup: object = os.environ if env is None else env
    raw = _clean(timeout_arg) or _clean(_lookup_env_value(lookup, "STOCKBOT_MODEL_TIMEOUT"))
    if not raw:
        return _PI_CALL_TIMEOUT_DEFAULT_S
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid model timeout {raw!r}: expected whole seconds > 0") from exc
    if seconds <= 0:
        raise RuntimeError(f"invalid model timeout {raw!r}: expected whole seconds > 0")
    return seconds


def _resolve_model_timeout(args: argparse.Namespace) -> int:
    return resolve_model_timeout(args.model_timeout, os.environ)


def _check_pi_ready(provider: str, model: str, timeout_s: int) -> None:
    try:
        probe = subprocess.run(
            _pi_completion_argv(provider, model, "Reply with OK."),
            input="Reply with OK.",
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("missing prerequisite: `omp` CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"missing prerequisite: Pi probe timed out after {timeout_s}s for {_model_label(provider, model)}"
        ) from exc
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip()[:500]
        raise RuntimeError(f"missing prerequisite: Pi not ready for {_model_label(provider, model)}: {err}")


def _clean_prompt(prompt: str) -> str:
    """Prompt text for stdin: NUL is not representable in a POSIX pipe payload either."""
    return prompt.replace("\x00", "")


def _pi_model_callable(provider: str, model: str, timeout_s: int) -> Callable[[str], str]:
    label = _model_label(provider, model)

    def _call(prompt: str) -> str:
        proc = subprocess.run(
            _pi_completion_argv(provider, model, prompt),
            input=_clean_prompt(prompt),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:2000]
            raise RuntimeError(f"Pi model call failed ({label}): {err}")
        text = (proc.stdout or "").strip()
        if not text:
            raise RuntimeError(f"Pi model returned blank output ({label})")
        return text

    _call.__name__ = f"pi_{provider}_{model}"
    return _call


_SEARCH_EXCLUDE = frozenset({"browse_tools", "search_tools", "describe_tool", "list_tool_domains", "call_tool"})


def _search_tool_names(sec_tools: object) -> list[str]:
    """Sorted SEC tool names excluding discovery primitives."""
    if not isinstance(sec_tools, Iterable):
        return []
    return sorted(t for t in sec_tools if isinstance(t, str) and t not in _SEARCH_EXCLUDE)


def _search_tool_matches(query: object, sec_tools: object) -> list[str]:
    q = str(query or "").lower()
    base = _search_tool_names(sec_tools)
    if not q:
        return base[:12]
    hits = [t for t in base if q in t.lower()]
    return (hits or base)[:12]


def _dispatch_search_tools(args: dict[str, object], sec_tools: object) -> dict[str, object]:
    query = args.get("query", "") if isinstance(args, dict) else ""
    return {"matches": [{"name": t} for t in _search_tool_matches(query, sec_tools)]}


def _extract_call_tool_request(
    args: dict[str, object],
) -> tuple[str, dict[str, object]]:
    inner = args.get("name") if isinstance(args, dict) else None
    inner_name = inner if isinstance(inner, str) else ""
    raw = args.get("arguments") if isinstance(args, dict) else None
    inner_args = dict(raw) if isinstance(raw, dict) else {}
    return inner_name, inner_args


_ToolHarness = tuple[object, object, object]


def _load_tool_harness() -> tuple[_ToolHarness | None, str]:
    try:
        from app.policy import Capability as _Cap
        from app.policy import RequestContext as _Ctx
        from app.tools import execute_tool as _exec
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None, f"tool harness unavailable: {exc}"
    harness: _ToolHarness = (_exec, _Cap, _Ctx)
    return harness, ""


def _make_tool_context(make_ctx: object, research: object) -> object:
    """Construct the harness context without static call typing."""
    if not callable(make_ctx):
        raise TypeError(f"tool context not callable: {type(make_ctx).__name__}")
    return make_ctx(principal_id="verify-agent-scenarios", capabilities=frozenset({research}))


def _build_tool_context(harness: _ToolHarness) -> tuple[object | None, str]:
    _, _Cap, _Ctx = harness
    research = getattr(_Cap, "RESEARCH", None)
    try:
        return _make_tool_context(_Ctx, research), ""
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None, f"tool context failed: {exc}"


def _call_tool_exec(
    exec_fn: object,
    inner_name: str,
    inner_args: dict[str, object],
    label: str,
    ctx: object,
) -> object:
    """Invoke the tool harness without static call typing."""
    if not callable(exec_fn):
        raise TypeError(f"tool harness not callable: {type(exec_fn).__name__}")
    return exec_fn(inner_name, dict(inner_args), label, context=ctx)


def _run_tool_exec(
    exec_fn: object,
    inner_name: str,
    inner_args: dict[str, object],
    provider: str,
    model: str,
    ctx: object,
) -> dict[str, object]:
    try:
        result = _call_tool_exec(exec_fn, inner_name, inner_args, f"{provider}/{model}", ctx)
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return {"error": f"{type(exc).__name__}: {exc}"}
    if isinstance(result, dict):
        narrowed: dict[str, object] = {str(k): v for k, v in result.items()}
        return narrowed
    return {"content": str(result)}


def _dispatch_call_tool(args: dict[str, object], provider: str, model: str) -> dict[str, object]:
    from app.research.agents.source_agent import is_sec_tool

    inner_name, inner_args = _extract_call_tool_request(args)
    if not is_sec_tool(inner_name):
        return {"error": f"POLICY_DENIED: non-SEC tool {inner_name!r}"}
    harness, err = _load_tool_harness()
    if harness is None:
        return {"error": err}
    ctx, err = _build_tool_context(harness)
    if ctx is None:
        return {"error": err}
    return _run_tool_exec(harness[0], inner_name, inner_args, provider, model, ctx)


def _pi_dispatch_callable(provider: str, model: str) -> Callable[[str, dict[str, object]], dict[str, object]]:
    from app.research.agents.source_agent import SEC_TOOLS

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return _dispatch_search_tools(args, SEC_TOOLS)
        if name == "call_tool":
            return _dispatch_call_tool(args, provider, model)
        return {"error": f"unknown dispatch {name!r}"}

    return _dispatch


_ENV_KEYS = ("RESEARCH_DB_PATH", "XDG_DATA_HOME")


def setup_env(tmp: str) -> dict[str, str | None]:
    old = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ["RESEARCH_DB_PATH"] = str(Path(tmp) / "research.sqlite")
    os.environ["XDG_DATA_HOME"] = str(Path(tmp) / "data")
    return old


def restore_env(old: dict[str, str | None]) -> None:
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _crash_eval_input(scenario: Scenario, wall_ms: float) -> EvalInput:
    return EvalInput(
        scenario_name=scenario.name,
        answer_text="",
        tool_calls=(),
        job_count=0,
        failed_count=1,
        evidence_ids=(),
        as_of=scenario.as_of,
        requires_evidence=scenario.requires_evidence,
        has_fabricated_id=False,
        has_fabricated_source="INJECT" in scenario.question,
        wall_clock_ms=wall_ms,
        budget_used=0,
        budget_cap=0,
        scenario_crashed=True,
    )


def _iter_trace_events(get_events: object, trace_id: str) -> list[object]:
    """Trace events without static call typing."""
    if not callable(get_events):
        return []
    events = get_events(trace_id)
    return list(events) if isinstance(events, list) else []


def _trace_event_tool_name(evt: object) -> str | None:
    """Tool name from one trace event; None when absent."""
    event_type = getattr(evt, "event_type", None)
    if event_type != "tool.completed":
        return None
    payload = getattr(evt, "payload", None)
    tool_name = payload.get("tool") if isinstance(payload, dict) else None
    return tool_name if isinstance(tool_name, str) and tool_name else None


def _trace_tool_names(trace_id: str, get_events: object) -> list[str]:
    names: list[str] = []
    for evt in _iter_trace_events(get_events, trace_id):
        tool_name = _trace_event_tool_name(evt)
        if tool_name is not None:
            names.append(tool_name)
    return names


def _extract_evidence_ids(out: dict[str, object]) -> tuple[str, ...]:
    raw = out.get("evidence_ids", [])
    if not isinstance(raw, list):
        return ()
    return tuple(e for e in raw if isinstance(e, str))


def _answer_from_final(final: object) -> str | None:
    if isinstance(final, dict) and isinstance(final.get("answer"), str):
        return str(final.get("answer"))
    return None


def _analysis_answer(analysis: object) -> str:
    if not getattr(analysis, "claims", None):
        return ""
    for attr in ("answer", "base_case", "bull_case", "bear_case"):
        value = getattr(analysis, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_answer(final: object, out: dict[str, object]) -> str:
    answer = _answer_from_final(final)
    if answer is not None:
        return answer
    for key in ("stock", "bull", "bear"):
        answer = _analysis_answer(out.get(key))
        if answer:
            return answer
    return ""


def _is_completed(sess_status: object, answer: object, evidence_ids: tuple[str, ...]) -> bool:
    if sess_status != "completed":
        return False
    if not isinstance(answer, str) or not answer.strip():
        return False
    return bool(evidence_ids)


_FAILED_JOB_STATUSES: tuple[str, ...] = ("failed", "cancelled", "timed_out")


def _job_failed(job: Job) -> bool:
    """Failed-status probe for one repository job; a successful job never counts."""
    return job.status in _FAILED_JOB_STATUSES


def _trace_strs(trace: Mapping[str, object], key: str) -> tuple[str, ...]:
    """String tuple from one live trace record; non-sequence/non-string entries are dropped."""
    raw = trace.get(key)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(v for v in raw if isinstance(v, str))


def _row_provenance(row: object) -> tuple[str, object] | None:
    """(accession, document) from one raw-source evidence row; None when the row proves no filing.

    Non-evidence rows and rows without an accession are navigation artifacts, not
    opened documents, so they carry no provenance for the opened-filings ledger.
    """
    if not isinstance(row, Mapping) or row.get("record_kind") != "evidence":
        return None
    meta = row.get("metadata")
    if not isinstance(meta, Mapping):
        return None
    accession = meta.get("accession_no")
    if not isinstance(accession, str) or not accession:
        return None
    return accession, meta.get("document_name")


def _ledger_documents(repo: ResearchRepository, session_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(filings, documents) actually opened, from the raw-source rows' own provenance.

    The runner journals no telemetry payload, so the ledger is the only real
    producer: each raw-source row records the accession and document it came from.
    """
    filings: list[str] = []
    documents: list[str] = []
    for row in repo.list_evidence(session_id):
        provenance = _row_provenance(row)
        if provenance is None:
            continue
        accession, document = provenance
        if accession not in filings:
            filings.append(accession)
        if isinstance(document, str):
            entry = f"{accession}|{document}"
            if entry not in documents:
                documents.append(entry)
    return tuple(filings), tuple(documents)


def _ledger_ids(repo: ResearchRepository, session_id: str, record_kind: str) -> tuple[str, ...]:
    """Ledger evidence ids of one record kind: raw-source rows vs navigation artifacts."""
    out: list[str] = []
    for row in repo.list_evidence(session_id):
        if isinstance(row, Mapping) and row.get("record_kind") == record_kind:
            eid = row.get("evidence_id")
            if isinstance(eid, str) and eid:
                out.append(eid)
    return tuple(out)


_COMMITTEE_ROLES = ("stockbot", "bullbot", "bearbot")


def _completed_roles(jobs: Iterable[Job]) -> tuple[str, ...]:
    """Completed committee roles only, in canonical order.

    Child jobs (scouts, source agents) are not committee members; counting them
    would make the role-set gate unsatisfiable.
    """
    done = {j.job_type for j in jobs if j.status == "completed"}
    return tuple(role for role in _COMMITTEE_ROLES if role in done)


def _build_success_input(
    scenario: Scenario,
    answer: str,
    tool_names: list[str],
    jobs: list[Job],
    sess_status: str,
    evidence_ids: tuple[str, ...],
    wall_ms: float,
    tool_call_cap: int = 0,
    trace: Mapping[str, object] | None = None,
) -> EvalInput:
    live: Mapping[str, object] = trace or {}
    failed = sum(1 for j in jobs if _job_failed(j))
    completed = _is_completed(sess_status, answer, evidence_ids)
    recovered = failed if completed else 0
    return EvalInput(
        scenario_name=scenario.name,
        answer_text=answer if isinstance(answer, str) else "",
        tool_calls=tuple(tool_names),
        job_count=len(jobs),
        failed_count=failed,
        recovered_count=recovered,
        evidence_ids=evidence_ids,
        as_of=scenario.as_of,
        requires_evidence=scenario.requires_evidence,
        # Depth is not a completeness boundary: only a cap the run was actually given
        # can be violated (0 = unlimited, the default for live research).
        wall_clock_ms=wall_ms,
        budget_used=len(tool_names),
        budget_cap=tool_call_cap,
        requires_trace=bool(live.get("requires_trace")),
        trace_present=bool(live.get("trace_present")),
        filings_opened=_trace_strs(live, "filings_opened"),
        documents_opened=_trace_strs(live, "documents_opened"),
        raw_evidence_ids=_trace_strs(live, "raw_evidence_ids"),
        navigation_evidence_ids=_trace_strs(live, "navigation_evidence_ids"),
        branches_covered=_trace_strs(live, "branches_covered"),
        waves=_trace_strs(live, "waves"),
        roles_completed=_trace_strs(live, "roles_completed"),
        committee_freeze_ids=_trace_strs(live, "committee_freeze_ids"),
    )


def _live_trace(
    scenario: Scenario, repo: ResearchRepository, sid: str, jobs: list[Job], evidence_ids: tuple[str, ...] = ()
) -> dict[str, object]:
    """Eval fields the live run can actually prove: opened filings/documents + ledger record kinds.

    Every field has a real producer on the runner path. ``raw_evidence_ids`` are the
    ids the run itself advertises for citation (the frozen raw-source set), so the
    subset check compares like with like; fields the runner does not persist
    (model-reported branch names, passages) stay unset rather than invented.
    """
    sess = repo.get_session(sid)
    filings, documents = _ledger_documents(repo, sid)
    freeze_ids = tuple(f for f in getattr(sess, "freeze_ids", ()) if isinstance(f, str))
    raw_rows = set(_ledger_ids(repo, sid, "evidence"))
    return {
        "requires_trace": bool(getattr(scenario, "requires_trace", False)),
        "trace_present": bool(freeze_ids),
        "filings_opened": filings,
        "documents_opened": documents,
        "raw_evidence_ids": tuple(eid for eid in evidence_ids if eid in raw_rows),
        "navigation_evidence_ids": _ledger_ids(repo, sid, "discovery"),
        "waves": freeze_ids,
        "committee_freeze_ids": tuple(str(run) for run in getattr(sess, "committee_runs", ()) if isinstance(run, str)),
        "roles_completed": _completed_roles(jobs),
    }


def evaluate_and_record(
    scenario: Scenario, out: dict[str, object], wall_ms: float, tool_call_cap: int = 0
) -> EvalInput:
    from app.research.evals.traces import get_trace_events, list_traces
    from app.research.repository import ResearchRepository

    repo = ResearchRepository()
    sid = str(out.get("session_id", ""))
    sess = repo.get_session(sid)
    jobs = repo.list_jobs(sid)
    traces = list_traces(sid)
    trace_id = traces[0].trace_id if traces else ""
    tool_names = _trace_tool_names(trace_id, get_trace_events) if trace_id else []
    evidence_ids = _extract_evidence_ids(out)
    answer = _extract_answer(sess.final_result or {}, out)
    return _build_success_input(
        scenario,
        answer,
        tool_names,
        jobs,
        sess.status,
        evidence_ids,
        wall_ms,
        tool_call_cap,
        _live_trace(scenario, repo, sid, jobs, evidence_ids),
    )


class _LiveKwargs(TypedDict):
    question: str
    objective: str
    as_of: str | None
    tickers: list[str]
    dispatch: Callable[[str, dict[str, object]], dict[str, object]]
    model: Callable[[str], str]
    provider: str
    model_name: str


def _live_kwargs(scenario: Scenario, provider: str, model: str, timeout_s: int) -> _LiveKwargs:
    """Retired run_live shim: question/tickers/labels only; dispatch/model unused by OMP path."""
    _ = timeout_s
    tickers: list[str] = [scenario.ticker] if scenario.ticker else []
    return {
        "question": scenario.question,
        "objective": scenario.notes or scenario.question,
        "as_of": scenario.as_of,
        "tickers": tickers,
        "dispatch": _pi_dispatch_callable(provider, model),
        "model": _pi_model_callable(provider, model, timeout_s),
        "provider": provider or _DEFAULT_MODEL_LABEL,
        "model_name": model or _DEFAULT_MODEL_LABEL,
    }


def _invoke_live(kwargs: _LiveKwargs) -> tuple[dict[str, object] | None, float, int]:
    """Retired run_live shim (test helper only): crash-reporting contract preserved."""
    from app.research.director import DirectorBudgets
    from app.research.runner import run_live

    t0 = time.monotonic()
    try:
        out = run_live(**kwargs, repo=ResearchRepository(), budgets=DirectorBudgets())
    except Exception as exc:  # noqa: BLE001 - the verdict reports the crash, never hides it
        print(f"CRASH {type(exc).__name__}: {exc}", file=sys.stderr)
        return None, (time.monotonic() - t0) * 1000.0, 0
    return out, (time.monotonic() - t0) * 1000.0, 0


def _omp_env(provider: str, model: str, timeout_s: int) -> dict[str, str]:
    """Model selectors for the OMP child env; empty means OMP CLI default."""
    env: dict[str, str] = {}
    if provider.strip():
        env["STOCKBOT_PROVIDER"] = provider.strip()
    if model.strip():
        env["STOCKBOT_MODEL"] = model.strip()
    env["STOCKBOT_MODEL_TIMEOUT"] = str(timeout_s)
    return env


def _run_omp_research(scenario: Scenario, provider: str, model: str, timeout_s: int, tmp: str) -> str:
    """Launch production OMP Stockbot on one scenario; return the run id for state reads."""
    from app.thesis.omp_runner import _await_omp, _prepare_launch, _spawn_omp, _verdict

    run_id = f"eval-{scenario.name}"
    data_root = str(Path(tmp) / "data")
    old_model = {k: os.environ.get(k) for k in ("STOCKBOT_PROVIDER", "STOCKBOT_MODEL", "STOCKBOT_MODEL_TIMEOUT")}
    os.environ.update(_omp_env(provider, model, timeout_s))
    try:
        launch = _prepare_launch(scenario.question, data_root, scenario.as_of, run_id)
        proc = _spawn_omp(launch, scenario.name)
        _await_omp(launch, proc, run_id, timeout_s)
        _verdict(launch, proc, "eval", scenario.name, run_id, timeout_s)
    finally:
        for k, v in old_model.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return run_id


def _read_omp_session_id(run_id: str, tmp: str) -> str:
    """Newest persisted research session for one eval data root; "" when none."""
    from app.research.repository import ResearchRepository

    repo = ResearchRepository(data_root=Path(tmp) / "data")
    sessions = repo.list_sessions(limit=1)
    _ = run_id  # the run id isolates the data root; the session is the newest row in it
    if not sessions:
        return ""
    sid = sessions[0].get("session_id")
    return sid if isinstance(sid, str) else ""


def _run_omp_scenario(scenario: Scenario, provider: str, model: str, prompt_version: str, timeout_s: int) -> EvalInput:
    """Production-path live eval: OMP + extension + Director + task subagents + kernel."""
    _ = prompt_version  # stamp recorded in the run summary only
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="agent-scenario-") as tmp:
        old = setup_env(tmp)
        try:
            try:
                run_id = _run_omp_research(scenario, provider, model, timeout_s, tmp)
            except Exception as exc:  # noqa: BLE001 - the verdict reports the crash, never hides it
                print(f"CRASH {type(exc).__name__}: {exc}", file=sys.stderr)
                return _crash_eval_input(scenario, (time.monotonic() - t0) * 1000.0)
            out: dict[str, object] = {
                "session_id": _read_omp_session_id(run_id, tmp),
                "evidence_ids": [],
            }
            wall_ms = (time.monotonic() - t0) * 1000.0
            if not out["session_id"]:
                return _crash_eval_input(scenario, wall_ms)
            # OMP research has no static tool-call cap: 0 = unlimited, judged on real limits only.
            return evaluate_and_record(scenario, out, wall_ms, 0)
        finally:
            restore_env(old)


def _run_live_scenario(scenario: Scenario, provider: str, model: str, prompt_version: str, timeout_s: int) -> EvalInput:
    """Live eval entrypoint: production OMP path only (run_live retired, see runner.py)."""
    return _run_omp_scenario(scenario, provider, model, prompt_version, timeout_s)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    parser.add_argument(
        "--scenario", default=None, help="run one scenario, fixture-only regressions included (default: all live)"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="OMP model ID used for live execution (or STOCKBOT_MODEL; default: OMP's own CLI default)",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="OMP provider used for live execution (or STOCKBOT_PROVIDER; default: OMP's own CLI default)",
    )
    parser.add_argument(
        "--model-timeout",
        default=None,
        help=f"per-call OMP model timeout seconds (or STOCKBOT_MODEL_TIMEOUT; default {_PI_CALL_TIMEOUT_DEFAULT_S})",
    )
    parser.add_argument("--prompt-version", default="v1", help="prompt version stamp (default v1)")
    parser.add_argument(
        "--fixtures-dir", default=None, help="accepted for compat; live runs do not use static fixtures"
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args(argv)


def _list_scenarios() -> int:
    for scenario in list_scenarios():
        print(f"{scenario.name} [{scenario.family.value}]")
    return 0


def _prepare_provider_model(args: argparse.Namespace) -> tuple[str, str, int]:
    """Provider/model/timeout resolution plus the Pi reachability probe, before any live run."""
    provider, model = _resolve_provider_model(args)
    timeout_s = _resolve_model_timeout(args)
    _check_pi_ready(provider, model, timeout_s)
    return provider, model, timeout_s


def _selected_names(args: argparse.Namespace) -> list[str]:
    if args.scenario:
        return [args.scenario]
    return [s.name for s in list_scenarios() if not s.fixture_only]


def _scenario_map() -> dict[str, Scenario]:
    return {s.name: s for s in list_scenarios()}


def _find_unknown(names: list[str], by_name: dict[str, Scenario]) -> list[str]:
    return [n for n in names if n not in by_name]


def _eval_one_scenario(
    name: str, by_name: dict[str, Scenario], provider: str, model: str, prompt_version: str, timeout_s: int
) -> ScenarioResult:
    return evaluate(_run_live_scenario(by_name[name], provider, model, prompt_version, timeout_s))


def _run_all_scenarios(
    names: list[str],
    by_name: dict[str, Scenario],
    provider: str,
    model: str,
    prompt_version: str,
    timeout_s: int,
) -> list[ScenarioResult]:
    return [_eval_one_scenario(name, by_name, provider, model, prompt_version, timeout_s) for name in names]


def _failed_results(results: list[ScenarioResult]) -> list[ScenarioResult]:
    return [r for r in results if not r.passed]


def summarize_results(results: list[ScenarioResult]) -> tuple[list[ScenarioResult], int]:
    failed = _failed_results(results)
    return failed, (1 if failed else 0)


def _print_results(results: list[ScenarioResult], provider: str, model: str) -> None:
    label = _model_label(provider, model)
    for result in results:
        if result.passed:
            print(f"PASS {result.scenario_name} (live via Pi {label})")
        else:
            print(f"FAIL {result.scenario_name} (live via Pi {label}): {', '.join(result.violations)}")


def _build_summary(provider: str, model: str, prompt_version: str, results: list[ScenarioResult]) -> dict[str, object]:
    return {
        "provider": provider or _DEFAULT_MODEL_LABEL,
        "model": model or _DEFAULT_MODEL_LABEL,
        "prompt_version": prompt_version,
        "scenarios": [
            {"scenario": r.scenario_name, "passed": r.passed, "violations": list(r.violations)} for r in results
        ],
    }


def _maybe_print_suite_info(
    results: list[ScenarioResult],
    provider: str,
    model: str,
    summary: dict[str, object],
) -> None:
    """Tally + git sha of a finished live run; the sha is recorded on the summary here only."""
    try:
        git_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=5).strip()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        git_sha = "unknown"
    # Passed/failed counts come from eval results, not from static fixtures.
    passed_count = sum(1 for r in results if r.passed)
    print(
        f"{passed_count}/{len(results)} passed "
        f"(model={model or _DEFAULT_MODEL_LABEL} provider={provider or _DEFAULT_MODEL_LABEL} git={git_sha})"
    )
    summary["git_sha"] = git_sha


def _maybe_print_json(json_flag: bool, summary: dict[str, object]) -> None:
    if json_flag:
        print(json.dumps(summary, indent=2, sort_keys=True))


def _cli_prereqs(
    args: argparse.Namespace,
) -> tuple[str, str, int, list[str], dict[str, Scenario]] | int:
    try:
        provider, model, timeout_s = _prepare_provider_model(args)
    except RuntimeError as exc:
        print(f"SKIP live scenarios: {exc}", file=sys.stderr)
        print(
            "Provide a reachable Pi CLI to evaluate live broad-agent scenarios.",
            file=sys.stderr,
        )
        return 2
    names = _selected_names(args)
    by_name = _scenario_map()
    unknown = _find_unknown(names, by_name)
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    return provider, model, timeout_s, names, by_name


def _report_cli_run(
    args: argparse.Namespace, provider: str, model: str, results: list[ScenarioResult], summary: dict[str, object]
) -> int:
    _print_results(results, provider, model)
    _maybe_print_suite_info(results, provider, model, summary)
    _maybe_print_json(args.json, summary)
    _, exit_code = summarize_results(results)
    return exit_code


def _run_cli(args: argparse.Namespace) -> int:
    if args.list:
        return _list_scenarios()
    prereqs = _cli_prereqs(args)
    if isinstance(prereqs, int):
        return prereqs
    provider, model, timeout_s, names, by_name = prereqs
    results = _run_all_scenarios(names, by_name, provider, model, args.prompt_version, timeout_s)
    summary = _build_summary(provider, model, args.prompt_version, results)
    return _report_cli_run(args, provider, model, results, summary)


def main(argv: list[str] | None = None) -> int:
    return _run_cli(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
