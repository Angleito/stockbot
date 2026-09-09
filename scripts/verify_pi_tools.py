#!/usr/bin/env python3
"""Live Pi tool verification: every describe-visible tool invoked 3/3 by Pi's configured default model.

Fail-closed at every step. Verdict comes only from per-attempt recorder DBs.
Pi configuration is authoritative for which model runs; Stockbot asserts only that non-empty model telemetry exists.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOLS, execute_tool  # noqa: E402
from app.config import get_data_root  # noqa: E402
from app.policy import Capability, RequestContext  # noqa: E402
from scripts.verify_tool_registry import get_registry_sets, registry_errors, tool_schema_function, tool_schema_name  # noqa: E402
EXTENSION = ".pi/extensions/stockbot.ts"
TIMEOUT_S = 180
DEFAULT_REPETITIONS = 3
DEFAULT_CONCURRENCY = 6
POLL_S = 2
THESIS_ID_PLACEHOLDER = "thesis-placeholder"
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})
FINRA_SEED_TOOLS = frozenset({"get_short_interest_leaderboard"})


def get_concurrency() -> int:
    """Live Pi parallelism; PI_VERIFY_CONCURRENCY override, fail-closed on bad values."""
    raw = os.getenv("PI_VERIFY_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        value = int(raw or "")
    except (ValueError, TypeError):
        raise ValueError("PI_VERIFY_CONCURRENCY must be an integer >= 1")
    if value < 1:
        raise ValueError("PI_VERIFY_CONCURRENCY must be an integer >= 1")
    return value


def attempt_dirs(batch_root: Path, tool: str, attempt: int) -> tuple[Path, Path]:
    """Per-attempt recorder DB and Stockbot store; keeps attempts mutually isolated."""
    attempt_dir = batch_root / tool / f"attempt-{attempt}"
    return attempt_dir / "runs.sqlite", attempt_dir / "store"

def remove_successful_attempt_dirs(batch_root: Path, tool: str, tool_recs: list[dict[str, object]]) -> None:
    """Delete whole attempt dirs for a fully-successful tool; prune tool dir when empty."""
    for rec in tool_recs:
        db_value = rec.get("db")
        if not isinstance(db_value, str) or not db_value:
            continue
        attempt_dir = Path(db_value).parent
        if not attempt_dir.name.startswith("attempt-"):
            continue
        shutil.rmtree(attempt_dir, ignore_errors=True)
    try:
        (batch_root / tool).rmdir()
    except OSError:
        pass


def ensure_thesis_fixture(store: Path) -> str:
    """Create one verification thesis in the batch store; returns its ID."""
    ctx = RequestContext(principal_id="verify", capabilities=frozenset({Capability.RESEARCH}), data_root=store)
    out = execute_tool("thesis_create", {"user_thesis": "Verify wiring: NVDA AI demand stays strong."}, "verify", context=ctx)
    if not isinstance(out, dict) or not out.get("thesis_id"):
        raise RuntimeError(f"thesis fixture setup failed: {str(out)[:300]}")
    return str(out["thesis_id"])


FINRA_SEED_DATASETS = ("short_interest", "entity_aliases", "securities", "financial_facts")



def seed_finra_fixture(store: Path, durable: Path) -> None:
    """Copy leaderboard inputs from the durable store into the batch store.

    Verify batches run in an isolated store that starts empty, so the
    FINRA short-interest snapshot plus its SEC join inputs would otherwise
    be missing and get_short_interest_leaderboard fails deterministically.
    Plain file copy (never symlink): batch runs must not append to the
    operator's durable datasets. Warns and continues when the durable
    store has nothing to copy; the tool then fails in-attempt with the
    refresh-data hint instead of breaking unrelated tools here.
    """
    if store.resolve() == durable.resolve():
        return
    for name in FINRA_SEED_DATASETS:
        src = durable / "parquet" / name
        if not src.is_dir():
            print(f"verify seed: durable dataset missing, skipping: {src}", file=sys.stderr)
            continue
        shutil.copytree(src, store / "parquet" / name, dirs_exist_ok=True)


class _VerifyCase(TypedDict):
    arguments: dict[str, object]
    natural_question: str


VERIFY_CASES: dict[str, _VerifyCase] = {
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
    "find_alternative_signals": {"arguments": {}, "natural_question": "What alternative signals have been collected?"},
    "get_trend_evidence": {"arguments": {"start_date": "2026-09-01", "end_date": "2026-09-02", "geos": ["US"], "limit": 25}, "natural_question": "Show collected trend evidence for early September 2026."},
    "investigate_social_arbitrage_candidate": {"arguments": {"term": "Stanley"}, "natural_question": "Investigate the Stanley discovery candidate."},
    "get_macro_context": {"arguments": {"geos": ["geoId/06"], "variables": ["Count_Person"]}, "natural_question": "Show macro context for California."},
    "search_company_patents": {"arguments": {"company_id": "Apple Inc.", "assignees": ["Apple Inc."], "limit": 5}, "natural_question": "Show recent Apple patent publications."},
    "thesis_create": {"arguments": {"user_thesis": "I think NVDA AI demand will stay strong."}, "natural_question": "Record my thesis that NVDA AI demand will stay strong."},
    "thesis_show": {"arguments": {"id": "thesis-placeholder"}, "natural_question": "Show thesis thesis-placeholder with its assessment and watch rules."},
    "thesis_refine": {"arguments": {"id": "thesis-placeholder", "clarification": "AI datacenter capex keeps growing."}, "natural_question": "Refine thesis thesis-placeholder: AI datacenter capex keeps growing."},
    "thesis_watch": {"arguments": {"id": "thesis-placeholder"}, "natural_question": "List the watch rules for thesis thesis-placeholder."},
    "thesis_journal": {"arguments": {"id": "thesis-placeholder", "body": "Operator note: still watching NVDA datacenter demand."}, "natural_question": "Journal on thesis thesis-placeholder: still watching NVDA datacenter demand."},
}

def tool_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for raw in TOOLS:
        function = tool_schema_function(raw)
        parameters = function.get("parameters", {})
        schemas[tool_schema_name(raw)] = dict(parameters) if isinstance(parameters, Mapping) else {}
    return schemas


def resolve_arguments(tool: str, schemas: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    schemas = schemas if schemas is not None else tool_schemas()
    params = schemas.get(tool, {})
    required_raw = params.get("required")
    required: list[object] = list(required_raw) if isinstance(required_raw, list) else []
    case = VERIFY_CASES.get(tool)
    if case is None:
        if required:
            raise LookupError(f"missing verification fixture for tool '{tool}' (required={required})")
        empty: dict[str, object] = {}
        return empty
    args = dict(case.get("arguments", {}))
    missing = [k for k in required if k not in args]
    if missing:
        raise LookupError(f"missing verification fixture for tool '{tool}' (missing={missing})")
    return args


def expand_jobs(tool_names: list[str], repetitions: int) -> list[tuple[str, int]]:
    return [(tool, attempt) for attempt in range(1, repetitions + 1) for tool in tool_names]


def build_explicit_prompt(tool: str, args: Mapping[str, object]) -> str:
    return (
        f"You are verifying Stockbot tool wiring. Call the `{tool}` tool "
        f"with exactly these arguments: {json.dumps(args, sort_keys=True)}. "
        f"Then summarize the result in one sentence. End your reply with "
        f"`TOOL_CHECK: PASS` if you called `{tool}` or `TOOL_CHECK: FAIL` otherwise."
    )


def build_attempt_prompt(tool: str, args: Mapping[str, object], attempt: int) -> str:
    if attempt == 2:
        case = VERIFY_CASES.get(tool)
        natural = case["natural_question"] if case is not None else ""
        if natural:
            if THESIS_ID_PLACEHOLDER in natural and "id" in args:
                thesis_id = str(args["id"])
                natural = natural.replace(THESIS_ID_PLACEHOLDER, thesis_id)
                natural += f" Use thesis ID `{thesis_id}` exactly for the `id` argument."
            return natural
        return f"Please answer this (you may need the `{tool}` tool with {json.dumps(args, sort_keys=True)}): rephrase and fulfill the request using `{tool}`."
    return build_explicit_prompt(tool, args)


def check_discovery(describe: Mapping[str, object], doctor: Mapping[str, object]) -> str | None:
    if doctor.get("bridge_ok") is not True:
        return "bridge doctor not ok"
    raw_tools = describe.get("tools")
    d_tools: list[Mapping[str, object]] = [t for t in raw_tools if isinstance(t, Mapping)] if isinstance(raw_tools, list) else []
    d_names = sorted(tool_schema_name(t) for t in d_tools)
    if doctor.get("tool_count") != len(d_tools):
        return f"doctor/describe count skew: doctor={doctor.get('tool_count')} describe={len(d_tools)}"
    raw_names = doctor.get("tool_names")
    doc_names: list[str] = sorted(n for n in raw_names if isinstance(n, str)) if isinstance(raw_names, list) else []
    if doc_names != d_names:
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
            # Pi configuration is authoritative for which model runs; assert presence only, never an exact ID.
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


def discover() -> tuple[dict[str, object], dict[str, object]]:
    proc = subprocess.Popen(
        [sys.executable, "scripts/pi_bridge.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"id": "discover-1", "op": "describe"}) + "\n")
        proc.stdin.write(json.dumps({"id": "discover-2", "op": "doctor"}) + "\n")
        proc.stdin.flush()
        first: dict[str, object] = json.loads(proc.stdout.readline() or "{}")
        second: dict[str, object] = json.loads(proc.stdout.readline() or "{}")
        by_id = {first.get("id"): first, second.get("id"): second}
        fallback: dict[str, object] = {}
        return by_id.get("discover-1", fallback), by_id.get("discover-2", fallback)
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


def run_pi(prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None) -> tuple[int, bool, str, str, bool]:
    """Run one Pi attempt. Pi 0.85.0 -p lingers after answering, so outputs go
    to files (never pipes) and completion is detected via the recorder DB;
    the process group is then killed. Returns (exit, timed_out, out, err, saw_complete)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_dir = db_path.parent
    out_log = attempt_dir / f"{attempt_dir.name}.pi.log"
    err_log = attempt_dir / f"{attempt_dir.name}.stderr.log"
    env = dict(os.environ, RUNS_DB_PATH=str(db_path))
    if stockbot_store is not None:
        env["STOCKBOT_DATA_DIR"] = str(stockbot_store.resolve())
    cmd = ["pi", "-p", "--no-session", "--no-builtin-tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-context-files", "--extension", EXTENSION, "--", prompt]
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


@dataclass
class AttemptResult:
    tool: str
    attempt: int
    ok: bool
    reason: str
    exit: int
    db: str
    duration_seconds: float
    model_config_failed: bool = False


def run_matrix(jobs: list[tuple[str, int]], worker: Callable[[str, int], AttemptResult], concurrency: int) -> list[AttemptResult]:
    """Run every (tool, attempt) through the worker with bounded parallelism."""
    futures: dict[Future[AttemptResult], tuple[str, int]] = {}
    results: list[AttemptResult] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for tool, attempt in jobs:
            futures[pool.submit(worker, tool, attempt)] = (tool, attempt)
        for fut in as_completed(futures):
            tool, attempt = futures[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append(AttemptResult(tool, attempt, False, f"attempt error: {exc}", 124, "", 0.0))
    def _key(r: AttemptResult) -> tuple[str, int]:
        return (r.tool, r.attempt)
    results.sort(key=_key)
    return results


def run_verification_attempt(tool: str, attempt: int, base_args: Mapping[str, object], batch_root: Path, cwd: Path, durable: Path, repetitions: int) -> AttemptResult:
    """Own one Pi attempt end to end: isolated store/DB/fixture, then evaluate."""
    start = time.monotonic()
    try:
        db_path, store_dir = attempt_dirs(batch_root, tool, attempt)
        store_dir.mkdir(parents=True, exist_ok=True)
        args = dict(base_args)
        if tool in FINRA_SEED_TOOLS:
            seed_finra_fixture(store_dir, durable)
        if tool in THESIS_ID_TOOLS:
            fixture_id = ensure_thesis_fixture(store_dir.resolve())
            if args.get("id") == THESIS_ID_PLACEHOLDER:
                args["id"] = fixture_id
        prompt = build_attempt_prompt(tool, args, attempt)
        code, timed_out, _out, err_text, saw_complete = run_pi(prompt, db_path, cwd, store_dir)
        model_config_failed = code != 0 and not saw_complete and "model" in err_text.lower()
        ok, reason = evaluate_attempt(db_path, tool, code, timed_out, completed_override=saw_complete)
        duration_seconds = time.monotonic() - start
        return AttemptResult(tool, attempt, ok, reason, code, str(db_path), duration_seconds, model_config_failed)
    except Exception as exc:
        elapsed = time.monotonic() - start
        try:
            fallback = str(attempt_dirs(batch_root, tool, attempt)[0])
        except Exception:
            fallback = ""
        return AttemptResult(tool, attempt, False, f"attempt error: {exc}", 124, fallback, elapsed)



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
    try:
        concurrency = get_concurrency()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
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
    raw_tools = describe.get("tools")
    describe_names: list[str] = sorted(tool_schema_name(t) for t in raw_tools if isinstance(t, Mapping)) if isinstance(raw_tools, list) else []
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

    print(f"Pi verification concurrency: {concurrency}")
    print(f"Tools: {len(tool_names)}")
    print(f"Attempts per tool: {repetitions}")
    print(f"Total Pi attempts: {len(tool_names) * repetitions}")
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path("data/verify") / batch
    cwd = Path.cwd()
    # Contain live side effects (e.g. thesis_create) in per-attempt dirs; never
    # the operator's durable store. Workers set STOCKBOT_DATA_DIR per attempt.
    durable = get_data_root()
    jobs = expand_jobs(tool_names, repetitions)
    verify_start = time.monotonic()
    def _worker(t: str, n: int) -> AttemptResult:
        return run_verification_attempt(t, n, case_args[t], root, cwd, durable, repetitions)
    ordered = run_matrix(jobs, _worker, concurrency)
    results: dict[str, list[dict[str, object]]] = {t: [] for t in tool_names}
    total = 0
    passed = 0
    for r in ordered:
        total += 1
        if r.ok:
            passed += 1
        results[r.tool].append({"attempt": r.attempt, "ok": r.ok, "reason": r.reason, "exit": r.exit, "db": r.db, "duration_seconds": r.duration_seconds})
        print(f"{r.tool} attempt {r.attempt}/{repetitions}: {'PASS' if r.ok else 'FAIL'} ({r.reason}) [{r.duration_seconds:.1f}s]")
        if r.model_config_failed:
            print("PI MODEL CONFIGURATION FAILED", file=sys.stderr)
    procs = len(ordered)
    failed_tools: list[str] = []
    for tool in tool_names:
        tool_recs = results[tool]
        if all(rec.get("ok") is True for rec in tool_recs):
            remove_successful_attempt_dirs(root, tool, tool_recs)
        else:
            failed_tools.append(tool)
            print(f"preserved DBs for {tool}: {root / tool}")
    passed_tools = len([t for t in tool_names if all(r.get("ok") is True for r in results[t])])
    coverage = f"{passed_tools}/{len(tool_names)} tools"
    wall = time.monotonic() - verify_start
    print(f"git: {sha} | tools {len(tool_names)} x {repetitions} = {total}")
    print(f"Coverage: {coverage} | processes: {procs} | passed: {passed}/{total}")
    print(f"Concurrency: {concurrency} | Wall time: {wall:.1f}s")
    print(f"RESULT: {'PASS' if not failed_tools else 'FAIL'}")
    if failed_tools:
        print(f"failed tools: {failed_tools}")
    summary = {"git_sha": sha, "tool_count": len(tool_names), "repetitions": repetitions, "results": results, "concurrency": concurrency, "wall_seconds": wall}
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if not failed_tools else 1


if __name__ == "__main__":
    sys.exit(main())
