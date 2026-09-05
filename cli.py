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
                "backfill-sec", "resume-sec-backfill", "sec-coverage")


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


def _cmd_refresh_data(settlement_date: str, tickers: list[str], ciks: list[int]) -> None:
    summary = prepare_short_interest_data(settlement_date, tickers=tickers, ciks=ciks)
    print(json.dumps(summary, indent=2))
    from app.analytics.screens import materialize_short_interest_screen
    result = materialize_short_interest_screen(settlement_date)
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
    subparsers.add_parser("replay-sec-facts", help="replay archived SEC companyfacts payloads into the Parquet store (offline)")
    obligations_parser = subparsers.add_parser("refresh-obligations", help="extract obligations for a ticker and persist events/evidence into the store")
    obligations_parser.add_argument("ticker", help="ticker, e.g. NVDA")
    mandate_parser = subparsers.add_parser("evaluate-mandate", help="evaluate the mandate JSON against the latest portfolio snapshot")
    mandate_parser.add_argument("--data-root", default=None, help="data root directory (default: repo data/)")
    log_server_parser = subparsers.add_parser(
        "log-server", help="receive and print log lines from chat/API clients (Ctrl-C to stop)"
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
    return parser


def _rewrite_bare_log_server(argv: list[str]) -> list[str]:
    """A bare --log-server directly before the subcommand (cli.py --log-server chat)
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
        _cmd_refresh_data(args.settlement_date, args.ticker, args.cik)
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
    else:
        parser.error(
            "unknown command (choose from runs, inspect, refresh-data, replay-sec-facts, "
            "refresh-obligations, evaluate-mandate, log-server, robinhood-login, "
            "backfill-sec, resume-sec-backfill, sec-coverage)"
        )


if __name__ == "__main__":
    main()
