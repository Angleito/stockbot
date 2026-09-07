#!/usr/bin/env python3
"""Live Pi tool verification: every describe-visible tool invoked 3/3 by Pi's configured default model.

Fail-closed at every step. Verdict comes only from per-attempt recorder DBs.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOLS, execute_tool  # noqa: E402
from app.policy import Capability, RequestContext  # noqa: E402
from scripts.verify_tool_registry import get_registry_sets, registry_errors  # noqa: E402
EXTENSION = ".pi/extensions/stockbot.ts"
TIMEOUT_S = 180
DEFAULT_REPETITIONS = 3
POLL_S = 2
THESIS_ID_PLACEHOLDER = "thesis-placeholder"
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})


def ensure_thesis_fixture(store: Path) -> str:
    """Create one verification thesis in the batch store; returns its ID."""
    ctx = RequestContext(principal_id="verify", capabilities=frozenset({Capability.RESEARCH}), data_root=store)
    out = execute_tool("thesis_create", {"user_thesis": "Verify wiring: NVDA AI demand stays strong."}, "verify", context=ctx)
    if not isinstance(out, dict) or not out.get("thesis_id"):
        raise RuntimeError(f"thesis fixture setup failed: {str(out)[:300]}")
    return str(out["thesis_id"])


VERIFY_CASES: dict[str, dict] = {
    "get_fundamentals": {"arguments": {"ticker": "AAPL", "metric": "eps"}, "natural_question": "What is Apple's EPS?"},
    "find_sec_entities": {"arguments": {"query": "Apple"}, "natural_question": "Find SEC entities matching Apple."},
    "search_sec_filings": {"arguments": {"query": "Apple", "limit": 5}, "natural_question": "Full-text search SEC filing documents for Apple risk factor language."},
    "search_sec_relationships": {"arguments": {"entity": "Apple"}, "natural_question": "What relationships does Apple disclose?"},
    "get_sec_search_coverage": {"arguments": {}, "natural_question": "Show SEC search coverage for 10-K filings."},
    "list_sec_filings": {"arguments": {"identifier": "AAPL", "limit": 5}, "natural_question": "List Apple's recent SEC filings."},
    "get_sec_filing": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_question": "Get Apple filing 0000320193-25-000079."},
    "list_sec_documents": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_question": "List documents in Apple filing 0000320193-25-000079."},
    "get_sec_document": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_question": "Get the primary document of Apple filing 0000320193-25-000079."},
    "diff_sec_filings": {"arguments": {"current_accession": "0000320193-25-000079", "previous_accession": "0000320193-24-000123"}, "natural_question": "What changed between Apple filing versions?"},
    "get_financial_statements": {"arguments": {"ticker": "MSFT", "statement_type": "income_statement"}, "natural_question": "Show Microsoft's income statement."},
    "get_xbrl_facts": {"arguments": {"ticker": "AAPL", "concept": "Revenue"}, "natural_question": "What is Apple's revenue?"},
    "get_material_events": {"arguments": {"ticker": "AAPL", "since": "2024-01-01"}, "natural_question": "What material events has Apple disclosed since 2024?"},
    "get_beneficial_ownership": {"arguments": {"ticker": "AAPL"}, "natural_question": "Who are Apple's large beneficial owners?"},
    "get_ownership_changes": {"arguments": {"ticker": "AAPL"}, "natural_question": "Have Apple's ownership stakes changed?"},
    "get_insider_activity": {"arguments": {"ticker": "AAPL"}, "natural_question": "What insider activity has Apple had?"},
    "get_planned_insider_sales": {"arguments": {"ticker": "AAPL"}, "natural_question": "Are Apple insiders planning sales?"},
    "get_offering_history": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is Apple's offering history?"},
    "get_dilution_profile": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is Apple's dilution profile?"},
    "get_governance_events": {"arguments": {"ticker": "AAPL"}, "natural_question": "What governance events has Apple had?"},
    "get_transaction_status": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is the status of Apple's transactions?"},
    "get_short_pressure_profile": {"arguments": {"ticker": "AAPL"}, "natural_question": "Is Apple under short pressure?"},
    "search_tools": {"arguments": {"query": "short interest"}, "natural_question": "Which tools handle short interest questions?"},
    "diff_risk_factors": {"arguments": {"ticker": "GOOGL"}, "natural_question": "What changed in Google's risk factors?"},
    "get_recent_ownership_filings": {"arguments": {}, "natural_question": "Show the most recent SC 13D/G filings."},
    "get_threshold_securities": {"arguments": {}, "natural_question": "Which securities are on the FINRA threshold list?"},
    "get_short_interest": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is Apple's short interest?"},
    "get_short_interest_leaderboard": {"arguments": {"limit": 5}, "natural_question": "Which stocks lead short interest?"},
    "get_reg_sho_volume": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is Apple's Reg SHO volume?"},
    "get_analyst_estimates": {"arguments": {"ticker": "AAPL"}, "natural_question": "What are analysts estimating for Apple?"},
    "get_sp500_weight": {"arguments": {"ticker": "AAPL"}, "natural_question": "What is Apple's S&P 500 weight?"},
    "get_obligations": {"arguments": {"ticker": "AAPL"}, "natural_question": "What are Apple's obligations?"},
    "get_valuation_metrics": {"arguments": {"ticker": "AAPL"}, "natural_question": "What are Apple's valuation metrics?"},
    "search_web": {"arguments": {"query": "Apple 10-K risk factors"}, "natural_question": "Search the web for Apple 10-K risk factors."},
    "list_finra_datasets": {"arguments": {}, "natural_question": "List FINRA datasets."},
    "describe_finra_dataset": {"arguments": {"dataset_id": "otcMarket/consolidatedShortInterest"}, "natural_question": "Describe the FINRA consolidated short interest dataset."},
    "get_finra_datapoints": {"arguments": {"dataset": "otcMarket/consolidatedShortInterest", "fields": ["settlementDate", "currentShortPositionQuantity"], "ticker": "AAPL", "limit": 5}, "natural_question": "Show recent FINRA short interest datapoints for Apple."},
    "query_finra": {"arguments": {"dataset": "otcMarket/consolidatedShortInterest", "ticker": "AAPL", "limit": 5}, "natural_question": "Query the FINRA weekly summary dataset for Apple."},
    "thesis_create": {"arguments": {"user_thesis": "I think NVDA AI demand will stay strong."}, "natural_question": "Record my thesis that NVDA AI demand will stay strong."},
    "thesis_show": {"arguments": {"id": "thesis-placeholder"}, "natural_question": "Show thesis thesis-placeholder with its assessment and watch rules."},
    "thesis_refine": {"arguments": {"id": "thesis-placeholder", "clarification": "AI datacenter capex keeps growing."}, "natural_question": "Refine thesis thesis-placeholder: AI datacenter capex keeps growing."},
    "thesis_watch": {"arguments": {"id": "thesis-placeholder"}, "natural_question": "List the watch rules for thesis thesis-placeholder."},
    "thesis_journal": {"arguments": {"id": "thesis-placeholder", "body": "Operator note: still watching NVDA datacenter demand."}, "natural_question": "Journal on thesis thesis-placeholder: still watching NVDA datacenter demand."},
}


def tool_schemas() -> dict[str, dict]:
    return {t["function"]["name"]: t["function"].get("parameters", {}) for t in TOOLS}


def resolve_arguments(tool: str, schemas: dict[str, dict] | None = None) -> dict:
    schemas = schemas if schemas is not None else tool_schemas()
    params = schemas.get(tool, {})
    required = params.get("required") or []
    case = VERIFY_CASES.get(tool)
    if case is None:
        if required:
            raise LookupError(f"missing verification fixture for tool '{tool}' (required={required})")
        return {}
    args = dict(case.get("arguments", {}))
    missing = [k for k in required if k not in args]
    if missing:
        raise LookupError(f"missing verification fixture for tool '{tool}' (missing={missing})")
    return args


def expand_jobs(tool_names: list[str], repetitions: int) -> list[tuple[str, int]]:
    return [(t, n) for t in tool_names for n in range(1, repetitions + 1)]


def build_explicit_prompt(tool: str, args: dict) -> str:
    return (
        f"You are verifying Stockbot tool wiring. Call the `{tool}` tool "
        f"with exactly these arguments: {json.dumps(args, sort_keys=True)}. "
        f"Then summarize the result in one sentence. End your reply with "
        f"`TOOL_CHECK: PASS` if you called `{tool}` or `TOOL_CHECK: FAIL` otherwise."
    )


def build_attempt_prompt(tool: str, args: dict, attempt: int) -> str:
    if attempt == 2:
        natural = VERIFY_CASES.get(tool, {}).get("natural_question")
        if natural:
            return natural
        return f"Please answer this (you may need the `{tool}` tool with {json.dumps(args, sort_keys=True)}): rephrase and fulfill the request using `{tool}`."
    return build_explicit_prompt(tool, args)
def expand_jobs(tool_names: list[str], repetitions: int) -> list[tuple[str, int]]:
    return [(t, n) for t in tool_names for n in range(1, repetitions + 1)]



def check_discovery(describe: dict, doctor: dict) -> str | None:
    if doctor.get("bridge_ok") is not True:
        return "bridge doctor not ok"
    d_tools = describe.get("tools") or []
    d_names = sorted(t["function"]["name"] for t in d_tools)
    if doctor.get("tool_count") != len(d_tools):
        return f"doctor/describe count skew: doctor={doctor.get('tool_count')} describe={len(d_tools)}"
    if sorted(doctor.get("tool_names") or []) != d_names:
        return "doctor/describe tool_names mismatch"
    return None


def check_pre_pi(describe_names: list[str]) -> str | None:
    sets = get_registry_sets()
    errs = registry_errors(sets)
    problems = [f"{k} {v}" for k, v in errs.items() if v]
    if problems:
        return "; ".join(problems)
    undispatchable = sorted(set(describe_names) - sets["handlers"])
    if undispatchable:
        return f"missing dispatcher for describe tools: {undispatchable}"
    outside = sorted(set(describe_names) - sets["schemas"])
    if outside:
        return f"describe tool without schema: {outside}"
    return None


def evaluate_attempt(db_path: Path, required_tool: str, exit_code: int, timed_out: bool, *, completed_override: bool = False) -> tuple[bool, str]:
    # Pi 0.85.0 -p does not exit after answering in this environment; when the
    # recorder DB already shows terminal state, the kill is cleanup, not failure.
    if timed_out and not completed_override:
        return False, "pi timeout"
    if exit_code != 0 and not completed_override:
        return False, f"pi exit {exit_code}"
    if not db_path.is_file():
        return False, f"missing recorder DB: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            ok_tools = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (required_tool,)
            ).fetchone()[0]
            if ok_tools < 1:
                # distinguish wrong-tool vs error-envelope for clearer logs
                any_ok = conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE error_type IS NULL"
                ).fetchone()[0]
                if any_ok >= 1:
                    return False, f"required tool '{required_tool}' absent (other tools called)"
                return False, f"required tool '{required_tool}' has no successful call"
            completed = conn.execute(
                "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (required_tool,)
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_failed' AND tool_name = ?", (required_tool,)
            ).fetchone()[0]
            if completed < 1:
                return False, "no TOOL_COMPLETED for required tool"
            if failed > 0:
                return False, "TOOL_FAILED present for required tool"
            models = conn.execute("SELECT model FROM model_calls").fetchall()
            if not any((r[0] or "").strip() for r in models):
                return False, "no model telemetry: model_calls has no non-empty model ID"
            rows = conn.execute("SELECT status FROM agent_runs").fetchall()
            if not rows or any((r[0] or "") != "completed" for r in rows):
                return False, f"agent_runs not completed: {[r[0] for r in rows]}"
            return True, "pass"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"DB read failed: {exc}"


def discover() -> tuple[dict, dict]:
    proc = subprocess.Popen(
        [sys.executable, "scripts/pi_bridge.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"id": "discover-1", "op": "describe"}) + "\n")
        proc.stdin.write(json.dumps({"id": "discover-2", "op": "doctor"}) + "\n")
        proc.stdin.flush()
        first = json.loads(proc.stdout.readline() or "{}")
        second = json.loads(proc.stdout.readline() or "{}")
        by_id = {first.get("id"): first, second.get("id"): second}
        return by_id.get("discover-1", {}), by_id.get("discover-2", {})
    finally:
        try:
            assert proc.stdin is not None
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass


def git_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10)
        sha = (out.stdout or "").strip()
        return sha or "unknown"
    except Exception:
        return "unknown"


def db_terminal(db_path: Path) -> bool:
    """True once the recorder shows a completed run (agent_end processed)."""
    try:
        if not db_path.is_file():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT status FROM agent_runs").fetchall()
            return bool(rows) and all((r[0] or "") == "completed" for r in rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def run_pi(prompt: str, db_path: Path, cwd: Path) -> tuple[int, bool, str, str, bool]:
    """Run one Pi attempt. Pi 0.85.0 -p lingers after answering, so outputs go
    to files (never pipes) and completion is detected via the recorder DB;
    the process group is then killed. Returns (exit, timed_out, out, err, saw_complete)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_dir = db_path.parent
    out_log = attempt_dir / f"{attempt_dir.name}.pi.log"
    err_log = attempt_dir / f"{attempt_dir.name}.stderr.log"
    env = dict(os.environ, RUNS_DB_PATH=str(db_path))
    cmd = ["pi", "-p", "--no-session", "--extension", EXTENSION, "--", prompt]
    with open(out_log, "w") as out_f, open(err_log, "w") as err_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f, stdin=subprocess.DEVNULL, cwd=str(cwd), env=env, start_new_session=True)
        deadline = time.monotonic() + TIMEOUT_S
        saw_complete = False
        code: int | None = None
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                break
            if db_terminal(db_path):
                saw_complete = True
                break
            time.sleep(POLL_S)
        else:
            code = proc.poll()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                code = proc.wait(timeout=15)
            except Exception:
                code = 124
        timed_out = not saw_complete and code != 0 and time.monotonic() >= deadline
        if saw_complete and code not in (0, None):
            err_f.write("\nKILLED_AFTER_COMPLETE")
    out_text = out_log.read_text() if out_log.is_file() else ""
    err_text = err_log.read_text() if err_log.is_file() else ""
    return (code if code is not None else 124), timed_out, out_text, err_text, saw_complete



def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default=None, help="verify one tool only (debug mode)")
    args = parser.parse_args()
    debug = args.tool is not None
    if debug:
        print("DEBUG MODE — partial verification")
    repetitions = int(os.getenv("PI_VERIFY_REPETITIONS", str(DEFAULT_REPETITIONS))) if debug else DEFAULT_REPETITIONS
    if repetitions < 1:
        print("PI_VERIFY_REPETITIONS must be >= 1", file=sys.stderr)
        return 1

    sha = git_sha()
    print(f"Extension: {EXTENSION} (Pi configured default model)")

    try:
        describe, doctor = discover()
    except Exception as exc:
        print(f"discovery failed: {exc}", file=sys.stderr)
        return 1
    err = check_discovery(describe, doctor)
    if err:
        print(f"discovery failed: {err}", file=sys.stderr)
        return 1
    describe_names = sorted(t["function"]["name"] for t in (describe.get("tools") or []))
    if debug:
        assert args.tool is not None
        if args.tool not in describe_names:
            print(f"unknown tool for --tool: {args.tool}", file=sys.stderr)
            return 1
        tool_names = [args.tool]
    else:
        tool_names = describe_names

    pre = check_pre_pi(describe_names)
    if pre:
        print(f"pre-Pi parity failed: {pre}", file=sys.stderr)
        return 1

    schemas = tool_schemas()
    try:
        case_args = {t: resolve_arguments(t, schemas) for t in tool_names}
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1

    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path("data/verify") / batch
    cwd = Path.cwd()
    # Contain live side effects (e.g. thesis_create) in the batch dir; never
    # the operator's durable store. run_pi children inherit this env.
    store = root / "store"
    os.environ.setdefault("STOCKBOT_DATA_DIR", str(store.resolve()))
    if any(t in THESIS_ID_TOOLS for t in tool_names):
        try:
            fixture_id = ensure_thesis_fixture(store.resolve())
        except Exception as exc:
            print(f"thesis fixture setup failed: {exc}", file=sys.stderr)
            return 1
        for t in tool_names:
            args = case_args.get(t, {})
            if args.get("id") == THESIS_ID_PLACEHOLDER:
                args["id"] = fixture_id
    results: dict[str, list[dict]] = {}
    total = passed = procs = 0
    failed_tools: list[str] = []
    for tool in tool_names:
        results[tool] = []
        tool_ok = True
        for n in range(1, repetitions + 1):
            total += 1
            procs += 1
            prompt = build_attempt_prompt(tool, case_args[tool], n)
            db_path = root / tool / f"attempt-{n}" / "runs.sqlite"
            code, timed_out, _out, err_text, saw_complete = run_pi(prompt, db_path, cwd)
            if code != 0 and not saw_complete and "model" in err_text.lower():
                print("PI MODEL CONFIGURATION FAILED", file=sys.stderr)
            ok, reason = evaluate_attempt(db_path, tool, code, timed_out, completed_override=saw_complete)
            if ok:
                passed += 1
            else:
                tool_ok = False
            results[tool].append({"attempt": n, "ok": ok, "reason": reason, "exit": code, "db": str(db_path)})
            print(f"{tool} attempt {n}/{repetitions}: {'PASS' if ok else 'FAIL'} ({reason})")
        if tool_ok:
            for rec in results[tool]:
                try:
                    Path(rec["db"]).unlink(missing_ok=True)
                except Exception:
                    pass
        else:
            failed_tools.append(tool)
            print(f"preserved DBs for {tool}: {root / tool}")
    coverage = f"{len([t for t in tool_names if all(r['ok'] for r in results[t])])}/{len(tool_names)} tools"
    print(f"git: {sha} | tools {len(tool_names)} x {repetitions} = {total}")
    print(f"Coverage: {coverage} | processes: {procs} | passed: {passed}/{total}")
    print(f"RESULT: {'PASS' if not failed_tools else 'FAIL'}")
    if failed_tools:
        print(f"failed tools: {failed_tools}")
    summary = {"git_sha": sha, "tool_count": len(tool_names), "repetitions": repetitions, "results": results}
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if not failed_tools else 1


if __name__ == "__main__":
    sys.exit(main())
