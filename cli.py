"""Stockbot admin CLI — runs, data refresh, log server, login (no chat; Pi is the harness)."""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

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
    get_run,
    get_security_events,
    get_security_summary,
    get_tool_calls,
    list_runs,
)

_LOG_SERVER_DEFAULT_URL = f"http://127.0.0.1:{DEFAULT_LOG_SERVER_PORT}"
_SUBCOMMANDS = ("runs", "inspect", "refresh-data", "log-server", "robinhood-login",
                "backfill-sec", "resume-sec-backfill", "sec-coverage", "thesis")


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
        duration = row["duration_ms"] if row["duration_ms"] is not None else 0.0
        cost = row["estimated_total_cost"] if row["estimated_total_cost"] is not None else 0.0
        question = (row["question"] or "")[:60]
        started_local = (
            datetime.fromisoformat(row["started_at"]).astimezone().isoformat()
            if row["started_at"] else ""
        )
        print(
            f"{row['run_id']:<38} {started_local[:26]:<26} "
            f"{(row['status'] or ''):<16} {duration:>10.0f} {cost:>9.6f}  {question}"
        )


def _cmd_refresh_data(settlement_date: str, tickers: list[str], ciks: list[int], data_root: str | None = None) -> None:
    summary = prepare_short_interest_data(settlement_date, tickers=tickers, ciks=ciks, data_root=data_root)
    print(json.dumps(summary, indent=2))
    from app.analytics.screens import materialize_short_interest_screen
    result = materialize_short_interest_screen(settlement_date, data_root=data_root)
    if result.get("error"):
        print(f"Leaderboard error: {result['error']}")
        return
    coverage = result["coverage"]
    finra_rows = coverage["finra_rows"]
    mapped = coverage["mapped_rows"]
    shares_covered = coverage["shares_outstanding_rows"]
    eligible = coverage["eligible_rows"]
    pct = 100.0 * eligible / finra_rows if finra_rows else 0.0
    print(f"FINRA securities:             {finra_rows:,}")
    print(f"Ticker mappings:              {mapped:,}")
    print(f"Shares-outstanding coverage:  {shares_covered:,}")
    print(f"Eligible screen universe:     {eligible:,}")
    print()
    print(f"Coverage: {pct:.1f}%")
    if summary["unresolved_tickers"]:
        print(f"Unresolved tickers (no SEC mapping, facts not fetched): {summary['unresolved_tickers']}")
    for fail in summary["failed_enrichments"]:
        print(f"Enrichment failed: ticker={fail['ticker']} cik={fail['cik']} error={fail['error']}")
    print(f"Leaderboard entries: {[e['ticker'] for e in result.get('entries', [])]}")


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
        if key in ("started_at", "completed_at") and value:
            value = datetime.fromisoformat(value).astimezone().isoformat()
        print(f"{key}: {value}")
    print()
    print("events (seq type round tool duration_ms summary):")
    for ev in get_events(run_id):
        summary = (ev.get("result_summary") or "").replace("\n", " ")[:80]
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
            snippet = (ev.get("rendered_text") or "").replace("\n", " ")[:200]
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


def _cmd_evaluate_mandate(mandate_path: Path, data_root: str | None) -> None:
    """Evaluate a mandate against the latest persisted snapshot; report or exit 1."""
    try:
        evaluation = evaluate_latest_mandate(mandate_path, data_root=data_root)
        mandate = load_mandate_file(mandate_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    units = {
        (limit.metric, limit.target): limit.unit
        for limit in mandate.limits
    }

    def fmt(value, metric: str, target: str | None) -> str:
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
    ids = []
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
            sec_store.requeue_job(job["id"], root=data_root)
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
    if from_date:
        rows = [r for r in rows if (r.get("coverage_date") or "")[:10] >= from_date]
    if to_date:
        rows = [r for r in rows if (r.get("coverage_date") or "")[:10] <= to_date]
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



def _thesis_repo(args):
    """Thesis root via the existing data-root mechanism (<root>/thesis)."""
    from app.config import get_data_root
    from app.thesis.repository import ThesisRepository
    override = getattr(args, "data_root", None) or None
    base = Path(override) if override else get_data_root()
    return ThesisRepository(base / "thesis")


def _thesis_ctx():
    from app.policy import LOCAL_CONTEXT
    return LOCAL_CONTEXT


def _thesis_load(repo, id_or_slug):
    try:
        return repo.load_thesis(id_or_slug)
    except KeyError as exc:
        raise SystemExit(f"thesis: {exc}") from exc


def _print_proposal(proposal) -> None:
    print(f"Thesis: {proposal.user_thesis}")
    print(f"Scope: {proposal.scope}")
    for c in proposal.claims:
        print(f"- claim [{c['status']}]: {c['statement']}")
    for e in proposal.expressions:
        print(f"- expression {e['instrument']}/{e['direction']} "
              f"({e['structure']}, {e['horizon']}): {e['intent']} [{e['status']}]")
    for q in proposal.questions:
        print(f"- question ({q.question_id}): {q.question}")
    if proposal.unknowns:
        print(f"Unknowns: {', '.join(proposal.unknowns)}")


def _thesis_create(args) -> None:
    from app.thesis.intake import interpret_idea
    idea = args.idea
    if not idea:
        if sys.stdin.isatty():
            raise SystemExit("thesis create: provide IDEA as an argument or pipe it on stdin")
        idea = sys.stdin.read().strip()
    if not idea:
        raise SystemExit("thesis create: empty idea")
    proposal = interpret_idea(idea, None, None, _thesis_ctx())
    if proposal.questions:
        answers = {}
        for q in proposal.questions:
            try:
                answers[q.question_id] = input(f"{q.question}\n> ").strip()
            except EOFError:
                answers[q.question_id] = ""
        proposal = interpret_idea(idea, answers, None, _thesis_ctx())
    _print_proposal(proposal)
    try:
        ok = input("Create this thesis? [y/N] ").strip().lower()
    except EOFError:
        ok = ""
    if not ok.startswith("y"):
        print("Not created.")
        return
    repo = _thesis_repo(args)
    thesis = repo.create_thesis(
        user_thesis=proposal.user_thesis,
        scope=proposal.scope,
        claims=[dict(c) for c in proposal.claims],
        assumptions=list(proposal.assumptions),
        invalidators=list(proposal.invalidators),
        unknowns=list(proposal.unknowns),
        expressions=[dict(e) for e in proposal.expressions],
        requirements=[dict(r) for r in proposal.requirements],
    )
    print(f"Created {thesis.thesis_id} ({thesis.slug})")


def _thesis_list(args) -> None:
    repo = _thesis_repo(args)
    rows = repo.list_theses()
    if not rows:
        print("No theses.")
        return
    for t in rows:
        one = t.user_thesis.splitlines()[0][:100] if t.user_thesis else ""
        print(f"{t.thesis_id} {t.slug} [{t.status}] {t.updated_at} {one}")


def _thesis_show(args) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
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


def _thesis_refine(args) -> None:
    from app.thesis.intake import interpret_idea
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
    clarification = args.clarification
    if not clarification:
        if sys.stdin.isatty():
            try:
                clarification = input("Clarification:\n> ").strip()
            except EOFError:
                clarification = ""
        else:
            clarification = sys.stdin.read().strip()
    if not clarification:
        raise SystemExit("thesis refine: provide CLARIFICATION as an argument or on stdin")
    proposal = interpret_idea(f"{thesis.user_thesis}\n{clarification}", None, None, _thesis_ctx())
    old_claims = {c.statement for c in thesis.claims}
    claims = [c.to_dict() for c in thesis.claims]
    added_claims = [dict(c) for c in proposal.claims if c["statement"] not in old_claims]
    old_expr = {(e.intent, e.instrument, e.direction, e.structure, e.horizon)
                for e in thesis.expressions}
    expressions = [e.to_dict() for e in thesis.expressions]
    added_expr = [dict(e) for e in proposal.expressions
                  if (e["intent"], e["instrument"], e["direction"],
                      e["structure"], e["horizon"]) not in old_expr]
    new_ids = {e["expression_id"] for e in added_expr}
    requirements = [r.to_dict() for r in thesis.requirements]
    added_reqs = [dict(r) for r in proposal.requirements if r["expression_id"] in new_ids]
    merged = {
        "user_thesis": proposal.user_thesis,
        "scope": proposal.scope if proposal.scope != "unknown" else thesis.scope,
        "claims": claims + added_claims,
        "assumptions": list(dict.fromkeys([*thesis.assumptions, *proposal.assumptions])),
        "invalidators": list(dict.fromkeys([*thesis.invalidators, *proposal.invalidators])),
        "unknowns": list(dict.fromkeys([*thesis.unknowns, *proposal.unknowns])),
        "expressions": expressions + added_expr,
        "requirements": requirements + added_reqs,
    }
    print(f"+{len(added_claims)} claims, +{len(added_expr)} expressions, +{len(added_reqs)} requirements")
    for c in added_claims:
        print(f"- claim: {c['statement']}")
    for e in added_expr:
        print(f"- expression {e['instrument']}/{e['direction']} ({e['structure']}): {e['intent']}")
    if not added_claims and not added_expr and merged["user_thesis"] == thesis.user_thesis:
        print("No changes.")
        return
    try:
        ok = input("Apply refinement? [y/N] ").strip().lower()
    except EOFError:
        ok = ""
    if not ok.startswith("y"):
        print("Not applied.")
        return
    updated = repo.update_thesis(args.id, **merged)
    print(f"Refined {updated.thesis_id} ({updated.slug})")


def _thesis_status(args) -> None:
    repo = _thesis_repo(args)
    op = {"pause": repo.pause_thesis, "resume": repo.resume_thesis,
          "close": repo.close_thesis}[args.thesis_command]
    try:
        updated = op(args.id)
    except (KeyError, ValueError) as exc:
        raise SystemExit(f"thesis: {exc}") from exc
    print(f"{updated.thesis_id} ({updated.slug}) [{updated.status}]")


def _thesis_inspect(args) -> None:
    from app.thesis.models import Checkpoint, Thesis, ThesisMemory, ThesisQuestion, ThesisState, WatchRule
    from app.thesis.yaml import load_raw_yaml, load_yaml
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
    d = repo.root / thesis.slug
    problems = []

    def check(name, fn):
        try:
            fn()
            print(f"{name}: ok")
        except Exception as exc:
            print(f"{name}: INVALID ({exc})")
            problems.append(name)

    def owned(name):
        raw = load_raw_yaml(d / name)
        if raw.get("thesis_id") != thesis.thesis_id:
            raise ValueError(f"{d / name}: thesis_id mismatch")
        return raw

    def check_questions():
        for q in owned("questions.yaml").get("questions", []):
            ThesisQuestion.from_dict(q, str(d / "questions.yaml"))

    def check_watch():
        for r in owned("watch.yaml").get("rules", []):
            WatchRule.from_dict(r, str(d / "watch.yaml"))

    def check_memory():
        for m in owned("memory.yaml").get("memories", []):
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


def _thesis_inbox(args) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
    triggers = repo.load_triggers(thesis.thesis_id)
    if not triggers:
        print("No triggers.")
        return
    for t in triggers:
        print(f"{t.trigger_id} [{t.status}] {t.trigger_type} ({t.importance}) {t.created_at}")
        if t.summary:
            print(f"  {t.summary.splitlines()[0][:120]}")


def _journal_head(path):
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


def _thesis_journal(args) -> None:
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
    jdir = repo.root / thesis.slug / "journal"
    files = sorted((p for p in jdir.glob("*.md") if p.is_file()),
                   key=lambda p: p.stat().st_mtime, reverse=True) if jdir.is_dir() else []
    sel = getattr(args, "entry", None)
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


class _UnconfiguredGateway:
    """Placeholder research gateway: deterministic ticks work, Pi runs wait for wiring."""

    def complete_research(self, prompt, *, request_context, tools):
        raise RuntimeError(
            "thesis tick: Pi research gateway is not wired yet; trigger left pending")


def _thesis_runtime(args, what="thesis tick"):
    """Shared tick/monitor wiring: repo, thesis ID, request context, source services."""
    from app.policy import Capability, RequestContext
    from app.thesis import monitor
    from app.thesis.runner import capabilities_for_grants

    try:
        extra = capabilities_for_grants(list(getattr(args, "grants", None) or []))
    except ValueError as exc:
        raise SystemExit(f"{what}: {exc}") from exc
    repo = _thesis_repo(args)
    thesis = _thesis_load(repo, args.id)
    ctx = RequestContext(
        principal_id=_thesis_ctx().principal_id,
        capabilities=frozenset({Capability.RESEARCH}) | extra,
    )
    targets = monitor.targets_for_thesis(thesis)
    services = {
        "sec_filings": monitor.SecFilingsService(targets, since_default=thesis.created_at),
        "material_events": monitor.MaterialEventsService(targets, since_default=thesis.created_at),
        "finra_short_interest": monitor.FinraShortInterestService(targets),
    }
    return repo, thesis.thesis_id, ctx, services


def _thesis_tick(args) -> None:
    """One deterministic monitor tick; exit 0 always (pauses/no-ops print a message)."""
    from datetime import datetime, timezone

    from app.thesis import monitor

    repo, thesis_id, ctx, services = _thesis_runtime(args)
    known_at = getattr(args, "known_at", None) or datetime.now(timezone.utc).isoformat(timespec="seconds")
    result = monitor.tick(repo, thesis_id, services, _UnconfiguredGateway(), ctx,
                          known_at=known_at)
    if result.no_op:
        print("no meaningful change" + (f": {result.no_op_reason}" if result.no_op_reason else ""))
        return
    print(f"triggers created: {len(result.triggers_created)}"
          + (f" ({', '.join(result.triggers_created)})" if result.triggers_created else ""))
    print(f"runs: {len(result.runs)}"
          + (f" ({', '.join(r.run_id for r in result.runs)})" if result.runs else ""))

def _thesis_monitor(args) -> None:
    """Loop ticks until stopped or closed; paused sleeps without querying, closed exits 0."""
    import signal
    import threading

    from app.thesis.worker import monitor_loop

    interval = getattr(args, "interval_seconds", 900)
    if interval <= 0:
        print("thesis monitor: --interval-seconds must be > 0", file=sys.stderr)
        raise SystemExit(2)
    repo, thesis_id, ctx, services = _thesis_runtime(args, what="thesis monitor")
    fixed_known_at = getattr(args, "known_at", None)
    stop = threading.Event()

    def _stop(signum, frame):
        stop.set()

    def _report(outcome):
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
                 gateway=_UnconfiguredGateway(), request_context=ctx,
                 source_services=services,
                 known_at_fn=(lambda: fixed_known_at) if fixed_known_at else None,
                 stop_event=stop, on_tick=_report)



def _cmd_thesis(args) -> None:
    cmd = getattr(args, "thesis_command", None)
    if cmd == "create":
        _thesis_create(args)
    elif cmd == "list":
        _thesis_list(args)
    elif cmd == "show":
        _thesis_show(args)
    elif cmd == "refine":
        _thesis_refine(args)
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
            "thesis: choose from create, list, show, refine, pause, resume, close, "
            "inspect, inbox, journal, tick, monitor"
        )


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
    thesis_common = argparse.ArgumentParser(add_help=False)
    thesis_common.add_argument("--data-root", default=None,
                               help="data root directory (default: $STOCKBOT_DATA_DIR or repo data/)")
    thesis_parser = subparsers.add_parser("thesis", parents=[thesis_common],
                                          help="persistent thesis management")
    thesis_sub = thesis_parser.add_subparsers(dest="thesis_command")
    create_parser = thesis_sub.add_parser("create", parents=[thesis_common],
                                          help="normalize an idea and persist a thesis")
    create_parser.add_argument("idea", nargs="?", default=None,
                               help="thesis idea (default: read stdin)")
    thesis_sub.add_parser("list", parents=[thesis_common], help="list theses")
    show_parser = thesis_sub.add_parser("show", parents=[thesis_common],
                                        help="show thesis and assessment")
    show_parser.add_argument("id", help="thesis ID or slug")
    refine_parser = thesis_sub.add_parser("refine", parents=[thesis_common],
                                          help="refine a thesis with extra clarification")
    refine_parser.add_argument("id", help="thesis ID or slug")
    refine_parser.add_argument("clarification", nargs="?", default=None,
                               help="extra natural-language clarification (default: stdin)")
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
    tick_parser.add_argument("--grant", dest="grants", action="append", default=[],
                             help="explicit read grant for this tick only "
                             "(repeatable: broker-market-read | portfolio-read)")
    tick_parser.add_argument("--known-at", default=None,
                             help="PIT upper bound ISO timestamp (default: now UTC)")
    monitor_parser = thesis_sub.add_parser("monitor", parents=[thesis_common],
                                           help="loop ticks until stopped; paused sleeps without "
                                           "querying, closed exits 0")
    monitor_parser.add_argument("id", help="thesis ID or slug")
    monitor_parser.add_argument("--interval-seconds", type=int, default=900,
                                help="seconds between ticks (default 900; must be > 0)")
    monitor_parser.add_argument("--grant", dest="grants", action="append", default=[],
                                help="explicit read grant for this monitor process only "
                                "(repeatable: broker-market-read | portfolio-read)")
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
    else:
        parser.error(
            "unknown command (choose from runs, inspect, refresh-data, replay-sec-facts, "
            "refresh-obligations, evaluate-mandate, log-server, robinhood-login, "
            "backfill-sec, resume-sec-backfill, sec-coverage, thesis)"
        )


if __name__ == "__main__":
    main()
