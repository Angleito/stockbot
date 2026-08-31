"""Terminal client — thin wrapper around app.agent.run_chat."""

import argparse
import json
import sys

from app.agent import run_chat
from app.config import get_default_model, get_local_chat_policy
from app.policy import LOCAL_CONTEXT
from app.services.research_data import prepare_short_interest_data
from app.storage.runs import (
    get_events,
    get_model_calls,
    get_run,
    get_tool_calls,
    list_runs,
)


def _chat(model: str) -> None:
    print(f"Stockbot — AI investment research assistant — model: {model}")
    print("Type your question (Ctrl-D or 'quit' to exit).\n")

    messages: list = []
    while True:
        try:
            user_input = input("you: ").strip()
        except EOFError:
            break
        if not user_input or user_input.lower() in ("quit", "exit"):
            break
        messages.append({"role": "user", "content": user_input})
        result = run_chat(
            messages,
            model,
            context=LOCAL_CONTEXT,
            policy=get_local_chat_policy(),
            return_result=True,
        )
        messages.append({"role": "assistant", "content": result.answer})
        print(f"\nassistant: {result.answer}\n")
        print(f"[run {result.run_id}]")


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
        print(
            f"{row['run_id']:<38} {(row['started_at'] or '')[:26]:<26} "
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


def _cmd_inspect(run_id: str) -> None:
    run = get_run(run_id)
    if run is None:
        print(f"error: no run found for {run_id}", file=sys.stderr)
        sys.exit(1)
    for key, value in run.items():
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
    print("model calls:")
    for mc in get_model_calls(run_id):
        print(
            f"  {mc['model_call_id']} {mc['provider']}/{mc['model']} "
            f"in={mc['input_tokens']} out={mc['output_tokens']} "
            f"cost={mc['estimated_cost']} finish={mc['finish_reason']} "
            f"req={mc['provider_request_id']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stockbot — AI investment research assistant")
    subparsers = parser.add_subparsers(dest="command")
    chat_parser = subparsers.add_parser("chat", help="interactive chat (default)")
    chat_parser.add_argument(
        "--model",
        default=get_default_model(),
        help=f"OpenRouter model string (default: {get_default_model()})",
    )
    runs_parser = subparsers.add_parser("runs", help="list recent runs")
    runs_parser.add_argument("--limit", type=int, default=20, help="max rows (default 20)")
    inspect_parser = subparsers.add_parser("inspect", help="show one run's record")
    inspect_parser.add_argument("run_id", help="run id, e.g. run:20260829T123456789012")
    refresh_parser = subparsers.add_parser("refresh-data", help="fetch + normalize SEC/FINRA research data into the Parquet store")
    refresh_parser.add_argument("--settlement-date", required=True, help="FINRA settlement date YYYY-MM-DD")
    refresh_parser.add_argument("--ticker", action="append", default=[], help="enrich SEC facts for this ticker (repeatable; optional)")
    refresh_parser.add_argument("--cik", type=int, action="append", default=[], help="enrich SEC facts for this CIK (repeatable; optional)")
    args = parser.parse_args()

    if args.command == "runs":
        _cmd_runs(args.limit)
    elif args.command == "inspect":
        _cmd_inspect(args.run_id)
    elif args.command == "refresh-data":
        _cmd_refresh_data(args.settlement_date, args.ticker, args.cik)
    else:
        model = getattr(args, "model", None) or get_default_model()
        _chat(model)


if __name__ == "__main__":
    main()
