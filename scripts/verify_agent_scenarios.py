#!/usr/bin/env python3
"""Live broad-agent scenario verification through Pi (no static PASS, no fakes).

Usage:
    python scripts/verify_agent_scenarios.py [--scenario NAME] [--model LABEL]
        [--provider LABEL] [--prompt-version VER] [--fixtures-dir DIR] [--json]
    python scripts/verify_agent_scenarios.py --list

Each scenario invokes the configured run_live/Pi path, captures the resulting
ResearchSession and trace, extracts observable outcomes, and runs deterministic
validators against those outputs. A scenario without executable
prerequisites (provider/model credentials, Pi reachability) fails or is
explicitly skipped with a non-zero clearly reported prerequisite status;
it never passes from static definitions. --model selects/records the actual
provider/model used. Exit 0 when every scenario passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypedDict  # noqa: E402

from app.research.evals.evaluators import (  # noqa: E402
    EvalInput,
    ScenarioResult,
    evaluate,
)
from app.research.evals.scenarios import Scenario, list_scenarios  # noqa: E402
from app.research.models import Job  # noqa: E402


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _require_provider_model(provider: str, model: str) -> None:
    if not provider or provider in ("unknown", "pi"):
        raise RuntimeError("missing prerequisite: --provider (or STOCKBOT_PI_PROVIDER) with a configured Pi provider")
    if not model:
        raise RuntimeError("missing prerequisite: --model (or STOCKBOT_PI_MODEL) with a configured Pi model")


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

def resolve_provider_model(
    provider_arg: str | None, model_arg: str | None, env: object = None
) -> tuple[str, str]:
    lookup: object = os.environ if env is None else env
    provider = _clean(provider_arg) or _clean(_lookup_env_value(lookup, "STOCKBOT_PI_PROVIDER"))
    model = _clean(model_arg) or _clean(_lookup_env_value(lookup, "STOCKBOT_PI_MODEL"))
    _require_provider_model(provider, model)
    return provider, model


def _resolve_provider_model(args: argparse.Namespace) -> tuple[str, str]:
    return resolve_provider_model(args.provider, args.model, os.environ)


def _check_pi_ready(provider: str, model: str) -> None:
    try:
        probe = subprocess.run(
            ["pi", "--provider", provider, "--model", model, "--print", "--no-session", "Reply with OK."],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("missing prerequisite: `pi` CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"missing prerequisite: Pi probe timed out for {provider}/{model}") from exc
    if probe.returncode != 0:
        err = (probe.stderr or probe.stdout or "").strip()[:500]
        raise RuntimeError(f"missing prerequisite: Pi not ready for {provider}/{model}: {err}")


def _pi_model_callable(provider: str, model: str) -> Callable[[str], str]:
    def _call(prompt: str) -> str:
        proc = subprocess.run(
            ["pi", "--provider", provider, "--model", model, "--print", "--no-session", prompt],
            capture_output=True, text=True, timeout=110,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:2000]
            raise RuntimeError(f"Pi model call failed ({provider}/{model}): {err}")
        text = (proc.stdout or "").strip()
        if not text:
            raise RuntimeError(f"Pi model returned blank output ({provider}/{model})")
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


def _extract_call_tool_request(args: dict[str, object]) -> tuple[str, dict[str, object]]:
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



def _call_tool_exec(exec_fn: object, inner_name: str, inner_args: dict[str, object], label: str, ctx: object) -> object:
    """Invoke the tool harness without static call typing."""
    call = getattr(exec_fn, "__call__", None)
    if not callable(exec_fn) or call is None:
        raise TypeError(f"tool harness not callable: {type(exec_fn).__name__}")
    return exec_fn(inner_name, dict(inner_args), label, context=ctx)

def _run_tool_exec(
    exec_fn: object, inner_name: str, inner_args: dict[str, object],
    provider: str, model: str, ctx: object,
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
        scenario_name=scenario.name, answer_text="",
        tool_calls=(), job_count=0, failed_count=1,
        evidence_ids=(), as_of=scenario.as_of,
        requires_evidence=scenario.requires_evidence,
        has_fabricated_id=False, has_fabricated_source="INJECT" in scenario.question,
        wall_clock_ms=wall_ms, budget_used=0, budget_cap=60,
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

def _job_failed(job: Job) -> bool:
    """Failed-status probe for one repository job."""
    return job.status == "failed"

def _build_success_input(
    scenario: Scenario, answer: str, tool_names: list[str],
    jobs: list[Job], sess_status: str, evidence_ids: tuple[str, ...], wall_ms: float,
) -> EvalInput:
    failed = sum(1 for j in jobs if _job_failed(j))
    completed = _is_completed(sess_status, answer, evidence_ids)
    recovered = failed if completed else 0
    return EvalInput(
        scenario_name=scenario.name, answer_text=answer if isinstance(answer, str) else "",
        tool_calls=tuple(tool_names), job_count=len(jobs), failed_count=failed,
        recovered_count=recovered,
        evidence_ids=evidence_ids, as_of=scenario.as_of,
        requires_evidence=scenario.requires_evidence,
        wall_clock_ms=wall_ms, budget_used=len(tool_names), budget_cap=60,
    )


def evaluate_and_record(scenario: Scenario, out: dict[str, object], wall_ms: float) -> EvalInput:
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
    return _build_success_input(scenario, answer, tool_names, jobs, sess.status, evidence_ids, wall_ms)


class _LiveKwargs(TypedDict):
    question: str
    objective: str
    as_of: str | None
    tickers: list[str]
    dispatch: Callable[[str, dict[str, object]], dict[str, object]]
    model: Callable[[str], str]
    provider: str
    model_name: str

def _live_kwargs(scenario: Scenario, provider: str, model: str) -> _LiveKwargs:
    tickers: list[str] = [scenario.ticker] if scenario.ticker else []
    return {"question": scenario.question, "objective": scenario.notes or scenario.question,
            "as_of": scenario.as_of, "tickers": tickers,
            "dispatch": _pi_dispatch_callable(provider, model),
            "model": _pi_model_callable(provider, model),
            "provider": provider, "model_name": model}

def _invoke_live(kwargs: _LiveKwargs) -> tuple[dict[str, object] | None, float]:
    from app.research.director import DirectorBudgets
    from app.research.repository import ResearchRepository
    from app.research.runner import run_live

    t0 = time.monotonic()
    try:
        out = run_live(**kwargs, repo=ResearchRepository(), budgets=DirectorBudgets())
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None, (time.monotonic() - t0) * 1000.0
    return out, (time.monotonic() - t0) * 1000.0

def _run_live_scenario(scenario: Scenario, provider: str, model: str, prompt_version: str) -> EvalInput:
    _ = prompt_version  # stamp recorded in the run summary only
    kwargs = _live_kwargs(scenario, provider, model)
    with tempfile.TemporaryDirectory(prefix="agent-scenario-") as tmp:
        old = setup_env(tmp)
        try:
            out, wall_ms = _invoke_live(kwargs)
            if out is None:
                return _crash_eval_input(scenario, wall_ms)
            return evaluate_and_record(scenario, out, wall_ms)
        finally:
            restore_env(old)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    parser.add_argument("--scenario", default=None, help="run one scenario (default: all)")
    parser.add_argument("--model", default=None, help="Pi model ID used for live execution (or STOCKBOT_PI_MODEL)")
    parser.add_argument("--provider", default=None, help="Pi provider used for live execution (or STOCKBOT_PI_PROVIDER)")
    parser.add_argument("--prompt-version", default="v1", help="prompt version stamp (default v1)")
    parser.add_argument("--fixtures-dir", default=None, help="accepted for compat; live runs do not use static fixtures")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    return parser.parse_args(argv)


def _list_scenarios() -> int:
    for scenario in list_scenarios():
        print(f"{scenario.name} [{scenario.family.value}]")
    return 0


def _prepare_provider_model(args: argparse.Namespace) -> tuple[str, str]:
    provider, model = _resolve_provider_model(args)
    _check_pi_ready(provider, model)
    return provider, model


def _selected_names(args: argparse.Namespace) -> list[str]:
    if args.scenario:
        return [args.scenario]
    return [s.name for s in list_scenarios()]


def _scenario_map() -> dict[str, Scenario]:
    return {s.name: s for s in list_scenarios()}


def _find_unknown(names: list[str], by_name: dict[str, Scenario]) -> list[str]:
    return [n for n in names if n not in by_name]


def _eval_one_scenario(name: str, by_name: dict[str, Scenario],
                       provider: str, model: str, prompt_version: str) -> ScenarioResult:
    return evaluate(_run_live_scenario(by_name[name], provider, model, prompt_version))

def _run_all_scenarios(
    names: list[str], by_name: dict[str, Scenario],
    provider: str, model: str, prompt_version: str,
) -> list[ScenarioResult]:
    return [_eval_one_scenario(name, by_name, provider, model, prompt_version) for name in names]


def _failed_results(results: list[ScenarioResult]) -> list[ScenarioResult]:
    return [r for r in results if not r.passed]

def summarize_results(results: list[ScenarioResult]) -> tuple[list[ScenarioResult], int]:
    failed = _failed_results(results)
    return failed, (1 if failed else 0)


def _print_results(results: list[ScenarioResult], provider: str, model: str) -> None:
    for result in results:
        if result.passed:
            print(f"PASS {result.scenario_name} (live via Pi {provider}/{model})")
        else:
            print(f"FAIL {result.scenario_name} (live via Pi {provider}/{model}): {', '.join(result.violations)}")


def _build_summary(provider: str, model: str, prompt_version: str, results: list[ScenarioResult]) -> dict[str, object]:
    return {
        "provider": provider, "model": model, "prompt_version": prompt_version,
        "scenarios": [{"scenario": r.scenario_name, "passed": r.passed, "violations": list(r.violations)} for r in results],
    }


def _maybe_print_suite_info(
    model_flag: str | None, results: list[ScenarioResult], provider: str, model: str, summary: dict[str, object],
) -> None:
    if not model_flag:
        return
    try:
        git_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=5).strip()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        git_sha = "unknown"
    # Determine passed/failed counts without relying on eval-suite internals using static fixtures.
    passed_count = sum(1 for r in results if r.passed)
    print(f"{passed_count}/{len(results)} passed (model={model} provider={provider} git={git_sha})")
    summary["git_sha"] = git_sha


def _maybe_print_json(json_flag: bool, summary: dict[str, object]) -> None:
    if json_flag:
        print(json.dumps(summary, indent=2, sort_keys=True))


def _cli_prereqs(args: argparse.Namespace) -> tuple[str, str, list[str], dict[str, Scenario]] | int:
    try:
        provider, model = _prepare_provider_model(args)
    except RuntimeError as exc:
        print(f"SKIP live scenarios: {exc}", file=sys.stderr)
        print("Provide a configured Pi provider/model to evaluate live broad-agent scenarios.", file=sys.stderr)
        return 2
    names = _selected_names(args)
    by_name = _scenario_map()
    unknown = _find_unknown(names, by_name)
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    return provider, model, names, by_name

def _report_cli_run(args: argparse.Namespace, provider: str, model: str,
                    results: list[ScenarioResult], summary: dict[str, object]) -> int:
    _print_results(results, provider, model)
    _maybe_print_suite_info(args.model, results, provider, model, summary)
    _maybe_print_json(args.json, summary)
    _, exit_code = summarize_results(results)
    return exit_code

def _run_cli(args: argparse.Namespace) -> int:
    if args.list:
        return _list_scenarios()
    prereqs = _cli_prereqs(args)
    if isinstance(prereqs, int):
        return prereqs
    provider, model, names, by_name = prereqs
    results = _run_all_scenarios(names, by_name, provider, model, args.prompt_version)
    summary = _build_summary(provider, model, args.prompt_version, results)
    return _report_cli_run(args, provider, model, results, summary)


def main(argv: list[str] | None = None) -> int:
    return _run_cli(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
