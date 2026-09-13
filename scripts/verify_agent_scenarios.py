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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.evals.evaluators import EvalInput, evaluate  # noqa: E402
from app.research.evals.scenarios import Scenario, list_scenarios  # noqa: E402


def _resolve_provider_model(args: argparse.Namespace) -> tuple[str, str]:
    provider = (args.provider or os.environ.get("STOCKBOT_PI_PROVIDER") or "").strip()
    model = (args.model or os.environ.get("STOCKBOT_PI_MODEL") or "").strip()
    if not provider or provider in ("unknown", "pi"):
        raise RuntimeError("missing prerequisite: --provider (or STOCKBOT_PI_PROVIDER) with a configured Pi provider")
    if not model:
        raise RuntimeError("missing prerequisite: --model (or STOCKBOT_PI_MODEL) with a configured Pi model")
    return provider, model


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


def _pi_model_callable(provider: str, model: str):
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


def _pi_dispatch_callable(provider: str, model: str):
    from app.research.agents.source_agent import SEC_TOOLS, is_sec_tool

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            query = ""
            if isinstance(args, dict):
                query = str(args.get("query", "") or "").lower()
            matches = [t for t in sorted(SEC_TOOLS) if t not in ("browse_tools", "search_tools", "describe_tool", "list_tool_domains", "call_tool")]
            if query:
                scored = [t for t in matches if query in t.lower()] or matches
            else:
                scored = matches
            return {"matches": [{"name": t} for t in scored[:12]]}
        if name == "call_tool":
            inner = args.get("name") if isinstance(args, dict) else None
            inner_name = inner if isinstance(inner, str) and inner else ""
            inner_args: dict[str, object] = dict(args.get("arguments")) if isinstance(args.get("arguments"), dict) else {}
            if not isinstance(inner_args, dict):
                return {"error": "invalid call_tool arguments"}
            if not is_sec_tool(inner_name):
                return {"error": f"POLICY_DENIED: non-SEC tool {inner_name!r}"}
            try:
                from app.tools import execute_tool as _exec
                from app.policy import Capability as _Cap
                from app.policy import RequestContext as _Ctx
            except Exception as exc:
                return {"error": f"tool harness unavailable: {exc}"}
            try:
                ctx = _Ctx(principal_id="verify-agent-scenarios", capabilities=frozenset({_Cap.RESEARCH}))
            except Exception as exc:
                return {"error": f"tool context failed: {exc}"}
            try:
                result = _exec(inner_name, dict(inner_args), f"{provider}/{model}", context=ctx)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
            if isinstance(result, dict):
                return result
            return {"content": str(result)}
        return {"error": f"unknown dispatch {name!r}"}
    return _dispatch


def _run_live_scenario(scenario: Scenario, provider: str, model: str, prompt_version: str) -> EvalInput:
    import tempfile

    from app.research.director import DirectorBudgets
    from app.research.evals.traces import get_trace_events, list_traces
    from app.research.repository import ResearchRepository
    from app.research.runner import resume_live, run_live

    _ = prompt_version
    tickers: list[str] = [scenario.ticker] if scenario.ticker else []
    dispatch = _pi_dispatch_callable(provider, model)
    model_call = _pi_model_callable(provider, model)
    with tempfile.TemporaryDirectory(prefix="agent-scenario-") as tmp:
        old_db = os.environ.get("RESEARCH_DB_PATH")
        old_data = os.environ.get("XDG_DATA_HOME")
        os.environ["RESEARCH_DB_PATH"] = str(Path(tmp) / "research.sqlite")
        os.environ["XDG_DATA_HOME"] = str(Path(tmp) / "data")
        try:
            t0 = time.monotonic()
            try:
                out = run_live(
                    question=scenario.question, objective=scenario.notes or scenario.question,
                    as_of=scenario.as_of, tickers=tickers, dispatch=dispatch, model=model_call,
                    repo=ResearchRepository(), budgets=DirectorBudgets(),
                    provider=provider, model_name=model,
                )
            except Exception as exc:
                wall_ms = (time.monotonic() - t0) * 1000.0
                return EvalInput(
                    scenario_name=scenario.name, answer_text="",
                    tool_calls=(), job_count=0, failed_count=1,
                    evidence_ids=(), as_of=scenario.as_of,
                    requires_evidence=scenario.requires_evidence,
                    has_fabricated_id=False, has_fabricated_source="INJECT" in scenario.question,
                    wall_clock_ms=wall_ms, budget_used=0, budget_cap=60,
                    scenario_crashed=True,
                )
            wall_ms = (time.monotonic() - t0) * 1000.0
            sid = str(out.get("session_id", ""))
            repo = ResearchRepository()
            try:
                sess = repo.get_session(sid)
                jobs = repo.list_jobs(sid)
                traces = list_traces(sid)
                tid = traces[0].trace_id if traces else ""
                trace_tool_names: list[str] = []
                if tid:
                    for evt in get_trace_events(tid):
                        if evt.event_type == "tool.completed":
                            tool_name = evt.payload.get("tool")
                            if isinstance(tool_name, str) and tool_name:
                                trace_tool_names.append(tool_name)
                raw_eids: object = out.get("evidence_ids", [])
                evidence_ids: tuple[str, ...] = ()
                if isinstance(raw_eids, list):
                    evidence_ids = tuple(e for e in raw_eids if isinstance(e, str))
                answer = ""
                final = sess.final_result or {}
                if isinstance(final, dict) and isinstance(final.get("answer"), str):
                    answer = str(final.get("answer"))
                else:
                    for key in ("stock", "bull", "bear"):
                        analysis = out.get(key)
                        claims = getattr(analysis, "claims", None)
                        if claims:
                            answer = getattr(analysis, "answer", None) or getattr(analysis, "base_case", None) or getattr(analysis, "bull_case", None) or getattr(analysis, "bear_case", None) or ""
                            if isinstance(answer, str) and answer.strip():
                                break
                failed = sum(1 for j in jobs if j.status == "failed")
                completed = sess.status == "completed" and isinstance(answer, str) and bool(answer.strip()) and bool(evidence_ids)
                recovered = failed if completed else 0
                return EvalInput(
                    scenario_name=scenario.name, answer_text=answer if isinstance(answer, str) else "",
                    tool_calls=tuple(trace_tool_names), job_count=len(jobs), failed_count=failed,
                    recovered_count=recovered,
                    evidence_ids=evidence_ids, as_of=scenario.as_of,
                    requires_evidence=scenario.requires_evidence,
                    wall_clock_ms=wall_ms, budget_used=len(trace_tool_names), budget_cap=60,
                )
            finally:
                pass
        finally:
            if old_db is None:
                os.environ.pop("RESEARCH_DB_PATH", None)
            else:
                os.environ["RESEARCH_DB_PATH"] = old_db
            if old_data is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = old_data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list scenario names and exit")
    parser.add_argument("--scenario", default=None, help="run one scenario (default: all)")
    parser.add_argument("--model", default=None, help="Pi model ID used for live execution (or STOCKBOT_PI_MODEL)")
    parser.add_argument("--provider", default=None, help="Pi provider used for live execution (or STOCKBOT_PI_PROVIDER)")
    parser.add_argument("--prompt-version", default="v1", help="prompt version stamp (default v1)")
    parser.add_argument("--fixtures-dir", default=None, help="accepted for compat; live runs do not use static fixtures")
    parser.add_argument("--json", action="store_true", help="print machine-readable summary")
    args = parser.parse_args()

    if args.list:
        for scenario in list_scenarios():
            print(f"{scenario.name} [{scenario.family.value}]")
        return 0

    try:
        provider, model = _resolve_provider_model(args)
        _check_pi_ready(provider, model)
    except RuntimeError as exc:
        print(f"SKIP live scenarios: {exc}", file=sys.stderr)
        print("Provide a configured Pi provider/model to evaluate live broad-agent scenarios.", file=sys.stderr)
        return 2

    names = [args.scenario] if args.scenario else [s.name for s in list_scenarios()]
    by_name = {s.name: s for s in list_scenarios()}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    results = []
    for name in names:
        scenario = by_name[name]
        inp = _run_live_scenario(scenario, provider, model, args.prompt_version)
        results.append(evaluate(inp))

    failed = [r for r in results if not r.passed]
    for result in results:
        if result.passed:
            print(f"PASS {result.scenario_name} (live via Pi {provider}/{model})")
        else:
            print(f"FAIL {result.scenario_name} (live via Pi {provider}/{model}): {', '.join(result.violations)}")

    summary: dict[str, object] = {
        "provider": provider, "model": model, "prompt_version": args.prompt_version,
        "scenarios": [{"scenario": r.scenario_name, "passed": r.passed, "violations": list(r.violations)} for r in results],
    }
    if args.model:
        import subprocess as _sp

        try:
            git_sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=5).strip()
        except Exception:
            git_sha = "unknown"
        # Determine passed/failed counts without relying on eval-suite internals using static fixtures.
        passed_count = sum(1 for r in results if r.passed)
        suite_info = f"{passed_count}/{len(results)} passed (model={model} provider={provider} git={git_sha})"
        print(suite_info)
        summary["git_sha"] = git_sha
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
