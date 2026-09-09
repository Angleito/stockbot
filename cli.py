"""Stockbot admin CLI — runs, data refresh, log server, login (no chat; Pi is the harness)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING

from app.config import configure_logging
from app.log_server import DEFAULT_LOG_SERVER_PORT, run_log_server
from app.robinhood.auth import DEFAULT_TOKEN_PATH
from app.services.mandate import load_mandate_file
from app.services.risk import evaluate_latest_mandate
from app.services.research_data import prepare_short_interest_data, replay_sec_facts_from_archive
from app.storage import duckdb
from app.tool_render import issue_to_prose
from app.tools import authorize_robinhood_browser
from app.storage.runs import (
    get_events,
    get_evidence,
    get_model_calls,
    get_run,
    get_security_events,
    get_security_summary,
    get_tool_calls,
    list_runs,
)

if TYPE_CHECKING:
    from app.thesis.models import JSONValue, Thesis
    from app.thesis.monitor import TickResult
    from app.thesis.repository import ThesisRepository

_LOG_SERVER_DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_LOG_SERVER_PORT}"
_SUBCOMMANDS = ("runs", "inspect", "refresh-data", "log-server", "robinhood-login",
                "backfill-sec", "resume-sec-backfill", "sec-coverage", "thesis", "google-data")


def _cmd_runs(limit: int) -> None:
    rows = list_runs(limit=limit)
    if not rows:
        print("No runs recorded.")
        return
    print(
        f"{'run_id':<38} {'started_at':<26} {'status':<16} {'duration_ms':>10} "
        f"{'cost':>9}  question"
    )
    for row in rows:
        duration_raw = row["duration_ms"]
        duration = duration_raw if isinstance(duration_raw, (int, float)) else 0.0
        cost_raw = row["estimated_total_cost"]
        cost = cost_raw if isinstance(cost_raw, (int, float)) else 0.0
        question_raw = row["question"]
        question = (question_raw if isinstance(question_raw, str) else "")[:60]
        started_raw = row["started_at"]
        started_local = (
            datetime.fromisoformat(started_raw).astimezone().isoformat()
            if isinstance(started_raw, str) and started_raw else ""
        )
        print(
            f"{row['run_id']:<38} {started_local[:26]:<26} "
            f"{(row['status'] or ''):<16} {duration:>10.0f} {cost:>9.6f}  {question}"
        )


def _cmd_refresh_data(settlement_date: str, tickers: list[str], ciks: list[int], data_root: str | Path | None = None) -> None:
    root = Path(data_root) if isinstance(data_root, str) else data_root
    summary = prepare_short_interest_data(settlement_date, tickers=tickers, ciks=ciks, data_root=root)
    print(json.dumps(summary, indent=2))
    from app.analytics.screens import materialize_short_interest_screen
    result = materialize_short_interest_screen(settlement_date, data_root=root)
    if result.get("error"):
        print(f"Leaderboard error: {result['error']}")
        return
    coverage_raw = result["coverage"]
    coverage: dict[str, object] = coverage_raw if isinstance(coverage_raw, dict) else {}
    finra_raw = coverage.get("finra_rows")
    finra_rows = finra_raw if isinstance(finra_raw, int) else 0
    mapped_raw = coverage.get("mapped_rows")
    mapped = mapped_raw if isinstance(mapped_raw, int) else 0
    shares_raw = coverage.get("shares_outstanding_rows")
    shares_covered = shares_raw if isinstance(shares_raw, int) else 0
    eligible_raw = coverage.get("eligible_rows")
    eligible = eligible_raw if isinstance(eligible_raw, int) else 0
    pct = 100.0 * eligible / finra_rows if finra_rows else 0.0
    print(f"FINRA securities:             {finra_rows:,}")
    print(f"Ticker mappings:              {mapped:,}")
    print(f"Shares-outstanding coverage:  {shares_covered:,}")
    print(f"Eligible screen universe:     {eligible:,}")
    print()
    print(f"Coverage: {pct:.1f}%")
    if summary["unresolved_tickers"]:
        print(f"Unresolved tickers (no SEC mapping, facts not fetched): {summary['unresolved_tickers']}")
    failed_raw = summary.get("failed_enrichments")
    fails: list[object] = failed_raw if isinstance(failed_raw, list) else []
    for fail in fails:
        if isinstance(fail, dict):
            print(f"Enrichment failed: ticker={fail['ticker']} cik={fail['cik']} error={fail['error']}")
    entries_raw = result.get("entries", [])
    entries: list[object] = entries_raw if isinstance(entries_raw, list) else []
    print(f"Leaderboard entries: {[e['ticker'] for e in entries if isinstance(e, dict)]}")


def _cmd_replay_sec_facts() -> None:
    summary = replay_sec_facts_from_archive()
    print(json.dumps(summary, indent=2))


def _cmd_refresh_obligations(ticker: str) -> None:
    from app import obligations

    result = obligations.get_obligations(ticker, persist=True)
    print(json.dumps(result, indent=2))
def _cmd_robinhood_login() -> None:
    print("Starting Robinhood authorization...")
    if authorize_robinhood_browser():
        print(f"Robinhood authorized. Tokens stored at {DEFAULT_TOKEN_PATH}")
    else:
        print("Robinhood authorization failed or was declined.")
        raise SystemExit(1)


def _cmd_log_server(port: int) -> None:
    try:
        run_log_server(port)
    except OSError as exc:
        print(f"error: cannot bind port {port}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _cmd_inspect(run_id: str) -> None:
    run = get_run(run_id)
    if run is None:
        print(f"error: no run found for {run_id}", file=sys.stderr)
        sys.exit(1)
    for key, value in run.items():
        if key in ("started_at", "completed_at") and isinstance(value, str) and value:
            value = datetime.fromisoformat(value).astimezone().isoformat()
        print(f"{key}: {value}")
    print()
    print("events (seq type round tool duration_ms summary):")
    for ev in get_events(run_id):
        result_raw = ev.get("result_summary")
        summary = (result_raw if isinstance(result_raw, str) else "").replace("\n", " ")[:80]
        duration = ev["duration_ms"] if ev.get("duration_ms") is not None else ""
        print(
            f"{ev['sequence']:>4} {ev['event_type']:<20} {str(ev.get('round')):<6} "
            f"{(ev.get('tool_name') or ''):<24} {str(duration):<10} {summary}"
        )
    print()
    print("tool calls:")
    for tc in get_tool_calls(run_id):
        print(
            f"  {tc['tool_call_id']} {tc['tool_name']} {tc['status']} "
            f"rows={tc['result_row_count']} bytes={tc['result_bytes']} "
            f"err={tc['error_type']} {tc['error_message'] or ''}"
        )
    print()
    print("search_web evidence:")
    for ev in get_evidence(run_id):
        if ev["tool_name"] == "search_web":
            rendered_raw = ev.get("rendered_text")
            snippet = (rendered_raw if isinstance(rendered_raw, str) else "").replace("\n", " ")[:200]
            print(f"  {ev['evidence_id']} {ev['tool_call_id']} {snippet}")
    print()
    print("model calls:")
    for mc in get_model_calls(run_id):
        print(
            f"  {mc['model_call_id']} {mc['provider']}/{mc['model']} "
            f"in={mc['input_tokens']} out={mc['output_tokens']} "
            f"cost={mc['estimated_cost']} finish={mc['finish_reason']} "
            f"req={mc['provider_request_id']}"
        )
    print()
    print("SECURITY:")
    summary = get_security_summary(run_id)
    print(
        f"  allowed={summary['allowed']} quarantined={summary['quarantined']} "
        f"blocked={summary['blocked']} action_blocked={summary['action_blocked']} "
        f"egress_blocked={summary['egress_blocked']} "
        f"response_stripped={summary['response_stripped']}"
    )
    for event in get_security_events(run_id):
        line = (
            f"  {event.get('source') or ''} | score={event.get('score')} | "
            f"{event.get('verdict') or ''} | rules={event.get('rule_ids') or ''} | "
            f"{event.get('decision')} | {event.get('reason') or ''} | "
            f"{event.get('created_at') or ''}"
        )
        if event.get("span_length") is not None:
            line += f" | stripped_span={event['span_length']} chars"
        print(line)


def _cmd_evaluate_mandate(mandate_path: Path, data_root: str | Path | None) -> None:
    """Evaluate a mandate against the latest persisted snapshot; report or exit 1."""
    try:
        root = Path(data_root) if isinstance(data_root, str) else data_root
        evaluation = evaluate_latest_mandate(mandate_path, data_root=root)
        mandate = load_mandate_file(mandate_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    units = {
        (limit.metric, limit.target): limit.unit
        for limit in mandate.limits
    }

    def fmt(value: Decimal | str | float | int | None, metric: str, target: str | None) -> str:
        if value is None:
            return ""
        if metric == "prohibited_assets" or units.get((metric, target)) == "dollars":
            return str(value)
        return f"{float(value) * 100:.1f}%"

    print(f"Mandate: {mandate_path}")
    print(f"Snapshot: {evaluation.snapshot_id} created {evaluation.created_at.astimezone().isoformat()}")
    if evaluation.sector_exposures:
        print(
            "Sector exposures: "
            + ", ".join(
                f"{sector} {float(weight) * 100:.1f}%"
                for sector, weight in evaluation.sector_exposures.items()
            )
        )
    if evaluation.breaches:
        print("Breaches:")
        for breach in evaluation.breaches:
            target = f" {breach.target}" if breach.target else ""
            line = (
                f"    [{breach.severity}] {breach.metric}{target}: "
                f"actual {fmt(breach.actual, breach.metric, breach.target)}, "
                f"limit {fmt(breach.limit, breach.metric, breach.target)}"
            )
            if breach.excess is not None:
                line += f", excess {fmt(breach.excess, breach.metric, breach.target)}"
            print(line)
    else:
        print("No breaches.")
    if evaluation.issues:
        print("Not evaluable:")
        for issue in evaluation.issues:
            print(f"    - {issue_to_prose(issue)}")

def _cmd_backfill_sec(source: str | None, forms: list[str], from_date: str,
                      to_date: str, batch_size: int,
                      data_root: str | None) -> None:
    """Enqueue bounded quarterly/form jobs for the range, then drain inline."""
    from app.sec import store as sec_store
    from app.sec.discovery.service import (
        BACKFILL_SOURCE,
        _quarter_dates,
        _quarters_for_range,
        drain_backfill_queue,
    )
    if not forms:
        print("error: --form is required (e.g. --form 10-K)", file=sys.stderr)
        raise SystemExit(2)
    try:
        quarters, _ = _quarters_for_range(from_date, to_date, cap=10_000)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not quarters:
        print("no quarterly partitions in range "
              "(before 1993 global indexes or current quarter only); "
              "nothing to backfill")
        return
    ids: list[str] = []
    for form in forms:
        for year, quarter in quarters:
            qs, qe = _quarter_dates(year, quarter)
            try:
                ids.append(sec_store.enqueue_backfill_job(
                    source or BACKFILL_SOURCE, form, qs, qe,
                    batch_size=batch_size, root=data_root))
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                raise SystemExit(2)
    print(f"queued {len(ids)} job(s): {ids}")
    summary = drain_backfill_queue(data_root)
    print(json.dumps({"jobs": ids, **summary}, indent=2))


def _cmd_resume_sec_backfill(job_id: str | None,
                             data_root: str | None) -> None:
    """Requeue one (or all) interrupted jobs and drain the queue inline."""
    from app.sec import store as sec_store
    from app.sec.discovery.service import drain_backfill_queue
    if job_id:
        if sec_store.get_job(job_id, root=data_root) is None:
            print(f"error: no backfill job {job_id!r}", file=sys.stderr)
            raise SystemExit(1)
        sec_store.requeue_job(job_id, root=data_root)
        print(f"requeued {job_id}")
    else:
        failed = sec_store.list_jobs(status="failed", root=data_root)
        for job in failed:
            job_id_raw = job.get("id")
            if isinstance(job_id_raw, str):
                sec_store.requeue_job(job_id_raw, root=data_root)
        print(f"requeued {len(failed)} failed job(s)")
    print(json.dumps(drain_backfill_queue(data_root), indent=2))


def _cmd_sec_coverage(source: str | None, form: str | None,
                      from_date: str | None, to_date: str | None,
                      data_root: str | None) -> None:
    """Show ingestion coverage rows plus pending backfill jobs."""
    from app.sec import store as sec_store
    for label, value in (("from", from_date), ("to", to_date)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                print(f"error: --{label} must be YYYY-MM-DD, got {value!r}",
                      file=sys.stderr)
                raise SystemExit(2)
    rows = sec_store.query_coverage(source=source, form=form, root=data_root)
    def _coverage_date(r: dict[str, object]) -> str:
        seen = r.get("coverage_date")
        return seen[:10] if isinstance(seen, str) else ""
    if from_date:
        rows = [r for r in rows if _coverage_date(r) >= from_date]
    if to_date:
        rows = [r for r in rows if _coverage_date(r) <= to_date]
    for row in rows:
        print(f"{row.get('source')} {row.get('form')} "
              f"{row.get('date_partition')} {row.get('status')} "
              f"count={row.get('accession_count')} last={row.get('last_key')}")
    if not rows:
        print("no coverage rows")
    pending = [j for j in sec_store.list_jobs(root=data_root)
               if j["status"] in ("queued", "running", "failed")]
    if pending:
        print(f"pending jobs: {[j['id'] for j in pending]}")
    else:
        print("no pending backfill jobs")



def _thesis_repo(args: argparse.Namespace) -> ThesisRepository:
    """Thesis root via the existing data-root mechanism (<root>/thesis)."""
    from app.config import get_data_root
    from app.thesis.repository import ThesisRepository
    raw_root = getattr(args, "data_root", None)
    override = raw_root if isinstance(raw_root, str) else None
    base = Path(override) if override else get_data_root()
    return ThesisRepository(base / "thesis")


def _thesis_load(repo: ThesisRepository, id_or_slug: str) -> Thesis:
    try:
        return repo.load_thesis(id_or_slug)
    except KeyError as exc:
        raise SystemExit(f"thesis: {exc}") from exc


def _thesis_list(args: argparse.Namespace) -> None:
    repo = _thesis_repo(args)
    rows = repo.list_theses()
    if not rows:
        print("No theses.")
        return
    for t in rows:
        one = t.user_thesis.splitlines()[0][:100] if t.user_thesis else ""
        print(f"{t.thesis_id} {t.slug} [{t.status}] {t.updated_at} {one}")


def _thesis_show(args: argparse.Namespace) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, str(args.id))
    state = repo.load_state(thesis.thesis_id)
    print(f"{thesis.thesis_id} ({thesis.slug}) [{thesis.status}] updated {thesis.updated_at}")
    print(f"Thesis: {thesis.user_thesis}")
    print(f"Scope: {thesis.scope}")
    for c in thesis.claims:
        print(f"- claim [{c.status}]: {c.statement}")
    for key in ("assumptions", "invalidators", "unknowns"):
        vals = getattr(thesis, key)
        if vals:
            print(f"{key.capitalize()}: {'; '.join(vals)}")
    print(f"Assessment: {state.assessment}")
    print("Expressions:")
    if not thesis.expressions:
        print("  (none)")
    for e in thesis.expressions:
        print(f"  - {e.intent} {e.instrument}/{e.direction} "
              f"({e.structure}, {e.horizon}) [{e.status}]")


def _thesis_status(args: argparse.Namespace) -> None:
    repo = _thesis_repo(args)
    op = {"pause": repo.pause_thesis, "resume": repo.resume_thesis,
          "close": repo.close_thesis}[args.thesis_command]
    try:
        updated = op(str(args.id))
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"thesis: {exc}") from exc
    print(f"{updated.thesis_id} ({updated.slug}) [{updated.status}]")


def _thesis_inspect(args: argparse.Namespace) -> None:
    from app.thesis.models import Checkpoint, Thesis, ThesisMemory, ThesisQuestion, ThesisState, WatchRule
    from app.thesis.yaml import load_raw_yaml, load_yaml
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, str(args.id))
    d = repo.root / thesis.slug
    problems: list[str] = []

    def check(name: str, fn: Callable[[], object]) -> None:
        try:
            fn()
            print(f"{name}: ok")
        except Exception as exc:
            print(f"{name}: INVALID ({exc})")
            problems.append(name)

    def owned(name: str) -> dict[str, JSONValue]:
        raw = load_raw_yaml(d / name)
        if raw.get("thesis_id") != thesis.thesis_id:
            raise ValueError(f"{d / name}: thesis_id mismatch")
        return raw

    def check_questions() -> None:
        items = owned("questions.yaml").get("questions", [])
        if not isinstance(items, list):
            raise ValueError(f"{d / 'questions.yaml'}: 'questions' must be a list")
        for q in items:
            if not isinstance(q, dict):
                raise ValueError(f"{d / 'questions.yaml'}: question entry must be a mapping")
            ThesisQuestion.from_dict(q, str(d / "questions.yaml"))

    def check_watch() -> None:
        items = owned("watch.yaml").get("rules", [])
        if not isinstance(items, list):
            raise ValueError(f"{d / 'watch.yaml'}: 'rules' must be a list")
        for r in items:
            if not isinstance(r, dict):
                raise ValueError(f"{d / 'watch.yaml'}: rule entry must be a mapping")
            WatchRule.from_dict(r, str(d / "watch.yaml"))

    def check_memory() -> None:
        items = owned("memory.yaml").get("memories", [])
        if not isinstance(items, list):
            raise ValueError(f"{d / 'memory.yaml'}: 'memories' must be a list")
        for m in items:
            if not isinstance(m, dict):
                raise ValueError(f"{d / 'memory.yaml'}: memory entry must be a mapping")
            ThesisMemory.from_dict(m, str(d / "memory.yaml"))

    check("thesis.yaml", lambda: load_yaml(d / "thesis.yaml", Thesis))
    check("state.yaml", lambda: load_yaml(d / "state.yaml", ThesisState))
    check("questions.yaml", check_questions)
    check("watch.yaml", check_watch)
    check("memory.yaml", check_memory)
    check("checkpoint.yaml", lambda: load_yaml(d / "checkpoint.yaml", Checkpoint))
    triggers = repo.load_triggers(thesis.thesis_id)
    pending = sum(1 for t in triggers if t.status == "pending")
    print(f"triggers: {pending} pending / {len(triggers)} total")
    for name in ("evidence", "journal"):
        sub = d / name
        n = len([p for p in sub.glob("*.md" if name == "journal" else "*") if p.is_file()]) if sub.is_dir() else 0
        print(f"{name} files: {n}")
    if problems:
        raise SystemExit(f"thesis inspect: invalid files: {', '.join(problems)}")


def _thesis_inbox(args: argparse.Namespace) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, str(args.id))
    triggers = repo.load_triggers(thesis.thesis_id)
    if not triggers:
        print("No triggers.")
        return
    for t in triggers:
        print(f"{t.trigger_id} [{t.status}] {t.trigger_type} ({t.importance}) {t.created_at}")
        if t.summary:
            print(f"  {t.summary.splitlines()[0][:120]}")


def _journal_head(path: Path) -> tuple[str, str, str]:
    entry_id, created, title = path.stem, "", ""
    try:
        with open(path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i > 15:
                    break
                s = line.strip()
                if s.startswith("entry_id:"):
                    entry_id = s.split(":", 1)[1].strip()
                elif s.startswith("created_at:"):
                    created = s.split(":", 1)[1].strip()
                elif s.startswith("# "):
                    title = s[2:].strip()
    except OSError:
        pass
    return entry_id, created, title


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


def _thesis_journal(args: argparse.Namespace) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, str(args.id))
    jdir = repo.root / thesis.slug / "journal"
    files: list[Path] = sorted((p for p in jdir.glob("*.md") if p.is_file()),
                   key=_mtime, reverse=True) if jdir.is_dir() else []
    sel_raw = getattr(args, "entry", None)
    sel = sel_raw if isinstance(sel_raw, str) else None
    if sel:
        norm = sel.replace(":", "_")  # filenames store entry IDs with ':' -> '_'
        match = next((p for p in files if p.stem == norm or norm in p.stem or sel in p.stem), None)
        if match is None:
            raise SystemExit(f"thesis journal: no entry matching {sel!r} ({len(files)} entries)")
        print(match.read_text(encoding="utf-8"), end="")
        return
    if not files:
        print("No journal entries.")
        return
    for p in files:
        entry_id, created, title = _journal_head(p)
        print(f"{entry_id} {created} {title}".rstrip())


def _thesis_runtime(args: argparse.Namespace, what: str = "thesis tick"):
    """Shared tick/monitor wiring: repo, thesis ID, source services."""
    from app.thesis import monitor

    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, str(args.id))
    targets = monitor.targets_for_thesis(thesis)
    services = {
        "sec_filings": monitor.SecFilingsService(targets, since_default=thesis.created_at),
        "material_events": monitor.MaterialEventsService(targets, since_default=thesis.created_at),
        "finra_short_interest": monitor.FinraShortInterestService(targets),
    }
    return repo, thesis.thesis_id, services


def _thesis_tick(args: argparse.Namespace) -> None:
    """One deterministic monitor tick; exit 0 always (pauses/no-ops print a message)."""
    from datetime import datetime, timezone

    from app.thesis import monitor

    repo, thesis_id, services = _thesis_runtime(args)
    known_at = getattr(args, "known_at", None) or datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = monitor.tick(repo, thesis_id, services, known_at=known_at)
    if result.no_op:
        print("no meaningful change" + (f": {result.no_op_reason}" if result.no_op_reason else ""))
        return
    print(f"triggers created: {len(result.triggers_created)}"
          + (f" ({', '.join(result.triggers_created)})" if result.triggers_created else ""))
    print(f"runs: {len(result.runs)}"
          + (f" ({', '.join(r.run_id for r in result.runs)})" if result.runs else ""))

def _thesis_monitor(args: argparse.Namespace) -> None:
    """Loop ticks until stopped or closed; paused sleeps without querying, closed exits 0."""
    import signal
    import threading

    from app.thesis.worker import monitor_loop

    interval = args.interval_seconds
    if interval <= 0:
        print("thesis monitor: --interval-seconds must be > 0", file=sys.stderr)
        raise SystemExit(2)
    repo, thesis_id, services = _thesis_runtime(args, what="thesis monitor")
    fixed_known_at = getattr(args, "known_at", None)
    stop = threading.Event()

    def _stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    def _report(outcome: TickResult) -> None:
        if outcome.no_op:
            print("no meaningful change" + (f": {outcome.no_op_reason}" if outcome.no_op_reason else ""),
                  flush=True)
            return
        print(f"triggers created: {len(outcome.triggers_created)}"
              + (f" ({', '.join(outcome.triggers_created)})" if outcome.triggers_created else ""),
              flush=True)
        print(f"runs: {len(outcome.runs)}"
              + (f" ({', '.join(r.run_id for r in outcome.runs)})" if outcome.runs else ""),
              flush=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    monitor_loop(repository=repo, thesis_id=thesis_id, interval_seconds=interval,
                 source_services=services,
                 known_at_fn=(lambda: fixed_known_at) if fixed_known_at else None,
                 stop_event=stop, on_tick=_report)



def _cmd_thesis(args: argparse.Namespace) -> None:
    cmd = getattr(args, "thesis_command", None)
    if cmd == "list":
        _thesis_list(args)
    elif cmd == "show":
        _thesis_show(args)
    elif cmd in ("pause", "resume", "close"):
        _thesis_status(args)
    elif cmd == "inspect":
        _thesis_inspect(args)
    elif cmd == "inbox":
        _thesis_inbox(args)
    elif cmd == "journal":
        _thesis_journal(args)
    elif cmd == "tick":
        _thesis_tick(args)
    elif cmd == "monitor":
        _thesis_monitor(args)
    else:
        raise SystemExit(
            "thesis: choose from list, show, pause, resume, close, "
            "inspect, inbox, journal, tick, monitor"
        )


def _cmd_google_data(args: argparse.Namespace) -> None:
    """Manual Google public-data collection; per-source status, never raises."""
    if getattr(args, "google_data_command", None) != "collect":
        raise SystemExit("google-data: choose 'collect' (e.g. google-data collect --source trends ...)")
    geos = args.geo or ["US"]
    limit = max(1, min(args.limit or 25, 1000))
    out: dict[str, object] = {}
    sources = ("trends", "patents", "macro", "geo", "stackoverflow") if args.source == "all" else (args.source,)
    for src in sources:
        try:
            if src == "trends":
                from app.google_data import trends as _trends
                out[src] = _trends.collect_trends(
                    start_date=args.start_date, end_date=args.end_date,
                    geos=list(geos), limit=limit, data_root=args.data_root or None)
            elif src == "patents":
                if not args.company:
                    out[src] = {"status": "error", "source": "patents",
                                "error": "company id required (--company) for patents collection"}
                else:
                    from app.google_data import patents as _patents
                    out[src] = _patents.search_company_patents(
                        args.company, start_date=args.start_date, end_date=args.end_date,
                        limit=min(limit, 20))
            elif src == "macro":
                from app.google_data import datacommons as _dc
                out[src] = _dc.get_macro_context(
                    list(geos), list(args.variable or []),
                    start_date=args.start_date, end_date=args.end_date, limit=min(limit, 100))
            elif src == "geo":
                from app.google_data import geo_context as _geo
                out[src] = _geo.get_geo_context(
                    list(geos), variables=list(args.variable or []),
                    start_date=args.start_date, end_date=args.end_date,
                    limit=min(limit, 100))
            elif src == "stackoverflow":
                from app.google_data import stackoverflow as _so
                out[src] = _so.get_tag_activity(
                    list(args.tag or []), start_date=args.start_date,
                    end_date=args.end_date, limit=min(limit, 100))
        except Exception as exc:
            out[src] = {"status": "error", "source": src, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps({"sources": out}, indent=2, default=str))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stockbot — AI investment research assistant")
    subparsers = parser.add_subparsers(dest="command")
    parser.add_argument(
        "--log-server",
        nargs="?",
        const=_LOG_SERVER_DEFAULT_URL,
        help="stream all logs to this log server URL (default: http://127.0.0.1:8765)",
    )

    runs_parser = subparsers.add_parser("runs", help="list recent runs")
    runs_parser.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    inspect_parser = subparsers.add_parser("inspect", help="show one run's record")
    inspect_parser.add_argument("run_id", help="run id, e.g. run:20260829T123456789012")
    refresh_parser = subparsers.add_parser("refresh-data", help="fetch + normalize SEC/FINRA research data into the Parquet store")
    refresh_parser.add_argument("--settlement-date", required=True, help="FINRA settlement date YYYY-MM-DD")
    refresh_parser.add_argument("--ticker", action="append", default=[], help="enrich SEC facts for this ticker (repeatable; optional)")
    refresh_parser.add_argument("--cik", type=int, action="append", default=[], help="enrich SEC facts for this CIK (repeatable; optional)")
    refresh_parser.add_argument("--data-root", default=None, help="data root directory (default: $STOCKBOT_DATA_DIR or repo data/)")
    subparsers.add_parser("replay-sec-facts", help="replay archived SEC companyfacts payloads into the Parquet store (offline)")
    obligations_parser = subparsers.add_parser("refresh-obligations", help="extract obligations for a ticker and persist events/evidence into the store")
    obligations_parser.add_argument("ticker", help="ticker, e.g. NVDA")
    mandate_parser = subparsers.add_parser("evaluate-mandate", help="evaluate the mandate JSON against the latest portfolio snapshot")
    mandate_parser.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    log_server_parser = subparsers.add_parser(
        "log-server", help="receive and print log lines from CLI/Pi-bridge clients (Ctrl-C to stop)"
    )
    log_server_parser.add_argument(
        "--port", type=int, default=DEFAULT_LOG_SERVER_PORT,
        help=f"port to listen on (default {DEFAULT_LOG_SERVER_PORT})",
    )
    subparsers.add_parser("robinhood-login", help="authorize Robinhood OAuth deliberately (opens browser)")
    backfill_parser = subparsers.add_parser(
        "backfill-sec",
        help="enqueue bounded SEC quarterly/form backfill jobs, then drain inline (dates required; no all-history default)")
    backfill_parser.add_argument("--source", default="sec-global", help="coverage source (default sec-global)")
    backfill_parser.add_argument("--form", action="append", default=[], help="SEC form, e.g. 10-K (repeatable; required)")
    backfill_parser.add_argument("--from", dest="from_date", required=True, help="range start YYYY-MM-DD (required)")
    backfill_parser.add_argument("--to", dest="to_date", required=True, help="range end YYYY-MM-DD (required)")
    backfill_parser.add_argument("--batch-size", type=int, default=50, help="filings per job batch (default 50)")
    backfill_parser.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    resume_parser = subparsers.add_parser(
        "resume-sec-backfill",
        help="requeue interrupted SEC backfill jobs and drain the queue inline")
    resume_parser.add_argument("job_id", nargs="?", default=None, help="one job ID to resume (default: all queued/failed)")
    resume_parser.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    coverage_parser = subparsers.add_parser(
        "sec-coverage", help="show SEC ingestion coverage plus pending backfill jobs")
    coverage_parser.add_argument("--source", default=None, help="filter by coverage source")
    coverage_parser.add_argument("--form", default=None, help="filter by SEC form")
    coverage_parser.add_argument("--from", dest="from_date", default=None, help="coverage on/after YYYY-MM-DD")
    coverage_parser.add_argument("--to", dest="to_date", default=None, help="coverage on/before YYYY-MM-DD")
    coverage_parser.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    gd_parser = subparsers.add_parser(
        "google-data", help="manual Google public-data collection (optional; never affects SEC/FINRA)")
    gd_sub = gd_parser.add_subparsers(dest="google_data_command")
    gd_collect = gd_sub.add_parser("collect", help="collect one Google source (manual only)")
    gd_collect.add_argument("--source", required=True,
                            choices=["trends", "patents", "macro", "geo", "stackoverflow", "all"],
                            help="source to collect")
    gd_collect.add_argument("--start-date", default=None, help="range start YYYY-MM-DD")
    gd_collect.add_argument("--end-date", default=None, help="range end YYYY-MM-DD")
    gd_collect.add_argument("--geo", action="append", default=None,
                            help="geography, e.g. US (repeatable; default US)")
    gd_collect.add_argument("--company", default=None,
                            help="documented assignee for --source patents")
    gd_collect.add_argument("--variable", action="append", default=[],
                            help="Data Commons variable ID for --source macro, census column or weather hint for --source geo (repeatable)")
    gd_collect.add_argument("--tag", action="append", default=[],
                            help="Stack Overflow tag for --source stackoverflow (repeatable)")
    gd_collect.add_argument("--limit", type=int, default=25,
                            help="max rows (default 25, capped at 1000)")
    gd_collect.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    thesis_common = argparse.ArgumentParser(add_help=False)
    thesis_common.add_argument("--data-root", default=argparse.SUPPRESS,
                               help="data root directory (default: $STOCKBOT_DATA_DIR or repo data/)")
    thesis_parser = subparsers.add_parser("thesis", parents=[thesis_common],
                                          help="persistent thesis management")
    thesis_sub = thesis_parser.add_subparsers(dest="thesis_command")
    thesis_sub.add_parser("list", parents=[thesis_common], help="list theses")
    show_parser = thesis_sub.add_parser("show", parents=[thesis_common],
                                        help="show thesis and assessment")
    show_parser.add_argument("id", help="thesis ID or slug")
    for _name in ("pause", "resume", "close"):
        _p = thesis_sub.add_parser(_name, parents=[thesis_common], help=f"{_name} a thesis")
        _p.add_argument("id", help="thesis ID or slug")
    inspect_parser = thesis_sub.add_parser("inspect", parents=[thesis_common],
                                           help="validate thesis files and show counts")
    inspect_parser.add_argument("id", help="thesis ID or slug")
    inbox_parser = thesis_sub.add_parser("inbox", parents=[thesis_common],
                                         help="list triggers and their state")
    inbox_parser.add_argument("id", help="thesis ID or slug")
    journal_parser = thesis_sub.add_parser("journal", parents=[thesis_common],
                                           help="list journal entries (newest first)")
    journal_parser.add_argument("id", help="thesis ID or slug")
    journal_parser.add_argument("entry", nargs="?", default=None,
                                help="print one entry without loading all")
    tick_parser = thesis_sub.add_parser("tick", parents=[thesis_common],
                                        help="run one deterministic monitor tick")
    tick_parser.add_argument("id", help="thesis ID or slug")
    tick_parser.add_argument("--known-at", default=None,
                             help="PIT upper bound ISO timestamp (default: now UTC)")
    monitor_parser = thesis_sub.add_parser("monitor", parents=[thesis_common],
                                           help="loop ticks until stopped; paused sleeps without "
                                           "querying, closed exits 0")
    monitor_parser.add_argument("id", help="thesis ID or slug")
    monitor_parser.add_argument("--interval-seconds", type=int, default=900,
                                help="seconds between ticks (default 900; must be > 0)")
    monitor_parser.add_argument("--known-at", default=None,
                                help="PIT upper bound ISO timestamp (default: now UTC per tick)")
    return parser


def _rewrite_bare_log_server(argv: list[str]) -> list[str]:
    """A bare --log-server directly before the subcommand (cli.py --log-server runs)
    would be consumed by nargs='?' as its value; rewrite it to the explicit default
    URL so the subcommand still parses and dispatches."""
    for i, arg in enumerate(argv[:-1]):
        if arg == "--log-server" and argv[i + 1] in _SUBCOMMANDS:
            return argv[:i] + [f"--log-server={_LOG_SERVER_DEFAULT_URL}"] + argv[i + 1:]
    return argv


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args(_rewrite_bare_log_server(sys.argv[1:]))
    stream_url = (
        None if args.command == "log-server"
        else args.log_server or os.getenv("STOCKBOT_LOG_SERVER") or None
    )
    configure_logging(stream_url=stream_url)

    if args.command == "runs":
        _cmd_runs(args.limit)
    elif args.command == "robinhood-login":
        _cmd_robinhood_login()
    elif args.command == "inspect":
        _cmd_inspect(args.run_id)
    elif args.command == "refresh-data":
        _cmd_refresh_data(args.settlement_date, args.ticker, args.cik, args.data_root or None)
    elif args.command == "replay-sec-facts":
        _cmd_replay_sec_facts()
    elif args.command == "refresh-obligations":
        _cmd_refresh_obligations(args.ticker)
    elif args.command == "evaluate-mandate":
        data_root = args.data_root or None
        mandate_path = (
            Path(args.mandate) if args.mandate
            else Path(duckdb.DEFAULT_DATA_ROOT) / "mandate.json"
        )
        _cmd_evaluate_mandate(mandate_path, data_root)
    elif args.command == "log-server":
        _cmd_log_server(args.port)
    elif args.command == "backfill-sec":
        _cmd_backfill_sec(args.source, args.form, args.from_date, args.to_date,
                          args.batch_size, args.data_root or None)
    elif args.command == "resume-sec-backfill":
        _cmd_resume_sec_backfill(args.job_id, args.data_root or None)
    elif args.command == "sec-coverage":
        _cmd_sec_coverage(args.source, args.form, args.from_date, args.to_date,
                          args.data_root or None)
    elif args.command == "thesis":
        _cmd_thesis(args)
    elif args.command == "google-data":
        _cmd_google_data(args)
    else:
        parser.error(
            "unknown command (choose from runs, inspect, refresh-data, replay-sec-facts, "
            "refresh-obligations, evaluate-mandate, log-server, robinhood-login, "
            "backfill-sec, resume-sec-backfill, sec-coverage, thesis, google-data)"
        )


if __name__ == "__main__":
    main()
