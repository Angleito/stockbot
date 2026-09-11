#!/usr/bin/env python3
"""Live Pi tool verification: every describe-visible tool invoked 3/3 by Pi's configured default model.

Fail-closed at every step. Verdict comes only from per-attempt recorder DBs.
Pi configuration is authoritative for which model runs; Stockbot asserts only that non-empty model telemetry exists.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOLS, build_prerequisite_graph_from_tool_metadata, execute_tool  # noqa: E402
from app.config import get_data_root  # noqa: E402
from app.policy import Capability, RequestContext  # noqa: E402
from scripts.verify_tool_registry import get_registry_sets, registry_errors, tool_schema_function, tool_schema_name  # noqa: E402
EXTENSION = ".pi/extensions/stockbot.ts"
# Duration evidence (2026-09-09 matrix): every recorded tool handler completes
# in seconds (slowest singles: search_sec_filings 39s, search_sec_relationships
# 25s; SEC per-filing sweeps ~0.6s each warm). The 180s kills all landed
# mid-run with clean target calls and no failed events (e.g.
# get_ownership_changes attempt-2: 9 fast calls, run still open) — time goes
# to small-model rounds (~5-10s each), not tools. 300s fits ~30 rounds; the
# 120s per-tool-call cliff still bounds any single hung handler.
TIMEOUT_S = 300
DEFAULT_REPETITIONS = 3
TRANSIENT_ERROR_TYPES = frozenset({"rate_limited"})
_TRANSIENT_MESSAGE_RE = re.compile(r"(?i)\btimed?\s*-?\s*out\b|deadline exceeded|drain.?timeout|unreachable")
TRANSIENT_RETRY_CAP = 2
DEFAULT_CONCURRENCY = 6
POLL_S = 2
THESIS_ID_PLACEHOLDER = "thesis-placeholder"
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})
FINRA_SEED_TOOLS = frozenset({"get_short_interest_leaderboard"})
# Prerequisite edges derive from TOOL_DISCOVERY_REGISTRY (single source with
# app/tools.py); per-edge description citations enforced by
# tests/test_verify_pi_tools.py::test_prereq_chains_are_documented_in_descriptions.
PREREQ_CHAINS: dict[str, frozenset[str]] = build_prerequisite_graph_from_tool_metadata()
# Discovery primitives: first-class citizens on attempts 1-2, never strays.
DISCOVERY_TOOLS = frozenset({"list_tool_domains", "search_tools", "describe_tool"})


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


def attempt_dirs(batch_root: Path, tool: str, attempt: int, retry: int = 0) -> tuple[Path, Path]:
    """Per-attempt recorder DB and Stockbot store; keeps attempts mutually isolated."""
    if retry:
        attempt_dir = batch_root / tool / f"attempt-{attempt}-retry-{retry}"
    else:
        attempt_dir = batch_root / tool / f"attempt-{attempt}"
    return attempt_dir / "runs.sqlite", attempt_dir / "store"


TRANSIENT_RETRIES: list[dict[str, object]] = []


def remove_successful_attempt_dirs(batch_root: Path, tool: str, tool_recs: list[dict[str, object]]) -> None:
    """Delete whole attempt dirs for a fully-successful tool; prune tool dir when empty."""
    for rec in tool_recs:
        db_value = rec.get("db")
        if not isinstance(db_value, str) or not db_value:
            continue
        attempt_dir = Path(db_value).parent
        if not attempt_dir.name.startswith("attempt-"):
            continue
        if "-retry-" in attempt_dir.name:
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
FINRA_SETTLEMENT_ENV = "PI_VERIFY_SETTLEMENT_DATE"
FETCH_TOP_SYMBOLS_SQL = (
    "SELECT symbol_code, MAX(short_position) AS pos FROM short_interest "
    "WHERE settlement_date = ? GROUP BY symbol_code ORDER BY pos DESC NULLS LAST LIMIT 25"
)
_SETTLEMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def missing_finra_datasets(durable: Path) -> list[str]:
    """Durable FINRA seed datasets absent as directories."""
    return [n for n in FINRA_SEED_DATASETS if not (durable / "parquet" / n).is_dir()]


def fetch_finra_fixture(durable: Path, settlement_date: str) -> None:
    """Fetch missing durable datasets with the existing refresh functions; writes to durable only."""
    from app.analytics import screens
    from app.services import research_data
    from app.storage import duckdb

    sec = research_data.refresh_sec_tickers(data_root=durable)
    raw_map = sec.get("ticker_ciks") if isinstance(sec, dict) else None
    ticker_ciks: dict[str, int] = {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            if isinstance(k, str) and isinstance(v, int):
                ticker_ciks[k] = v
    print(f"verify fetch: tickers={len(ticker_ciks)}")
    research_data.refresh_finra_short_interest(settlement_date, data_root=durable)
    rows = duckdb.query(
        FETCH_TOP_SYMBOLS_SQL,
        params=[settlement_date],
        data_root=durable,
    )
    symbols = [str(r["symbol_code"]).strip().upper() for r in rows if r.get("symbol_code")]
    resolved = [(s, ticker_ciks[s]) for s in symbols if s in ticker_ciks]
    print(f"verify fetch: top={len(symbols)} resolved={len(resolved)} skipped={len(symbols) - len(resolved)}")
    failed: list[dict[str, object]] = []
    for sym, cik in resolved:
        try:
            research_data.refresh_sec_company_facts(cik, data_root=durable)
        except Exception as exc:
            failed.append({"ticker": sym, "cik": cik, "error": f"{type(exc).__name__}: {exc}"})
    if failed:
        print(f"verify fetch: failed_enrichments={failed}")
    confirm = screens.get_short_interest_leaderboard(limit=5, data_root=durable)
    entries = confirm.get("entries") if isinstance(confirm, dict) else None
    if not isinstance(confirm, dict) or "error" in confirm or not isinstance(entries, list) or not entries:
        err = confirm.get("error") if isinstance(confirm, dict) else None
        raise RuntimeError(
            f"verify fetch confirmation failed for settlement {settlement_date} "
            f"(resolved={len(resolved)} failed={len(failed)}): {err or 'zero entries'}"
        )
    print(f"verify fetch: confirmed {len(entries)} entries for {settlement_date}")


def ensure_finra_fixture(durable: Path, tool_names: list[str]) -> int:
    """Fetch-once gate for the verify matrix; 0 ok, 1 with stderr on missing date/fetch failure."""
    if not any(t in FINRA_SEED_TOOLS for t in tool_names):
        return 0
    missing = missing_finra_datasets(durable)
    if not missing:
        return 0
    settlement = (os.getenv(FINRA_SETTLEMENT_ENV, "") or "").strip()
    if not settlement or not _SETTLEMENT_RE.match(settlement):
        print(
            f"{FINRA_SETTLEMENT_ENV} is required to fetch missing durable datasets {missing} "
            f"(e.g. {FINRA_SETTLEMENT_ENV}=2026-08-14)",
            file=sys.stderr,
        )
        return 1
    try:
        fetch_finra_fixture(durable, settlement)
    except Exception as exc:
        print(f"verify fetch failed for settlement {settlement}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0




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
    natural_v1: str
    natural_v2: str

VERIFY_CASES: dict[str, _VerifyCase] = {
    "get_fundamentals": {"arguments": {"ticker": "AAPL", "metric": "eps"}, "natural_v1": "What does Apple earn per share?", "natural_v2": "What is Apple's EPS, basic and diluted, including trailing twelve months?"},
    "find_sec_entities": {"arguments": {"query": "Apple"}, "natural_v1": "Which SEC-registered entities correspond to Apple?", "natural_v2": "Which SEC entities match Apple?"},
    "search_sec_filings": {"arguments": {"query": "Apple", "limit": 5}, "natural_v1": "What has Apple disclosed about risk factors in its recent filings?", "natural_v2": "What risk-factor language has Apple used in its recent SEC filings?"},
    "search_sec_relationships": {"arguments": {"entity": "AAPL"}, "natural_v1": "What relationships does Apple disclose?", "natural_v2": "What ownership and transaction relationships does Apple disclose in its recent filings?"},
    "get_sec_search_coverage": {"arguments": {}, "natural_v1": "What SEC filing types and years are covered?", "natural_v2": "Which filing types and years does your SEC coverage include?"},
    "list_sec_filings": {"arguments": {"identifier": "AAPL", "limit": 5}, "natural_v1": "What has Apple filed with the SEC lately?", "natural_v2": "List Apple's recent SEC filings."},
    "get_sec_filing": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_v1": "What is in filing 0000320193-25-000079?", "natural_v2": "Can you pull up the filing record for 0000320193-25-000079?"},
    "list_sec_documents": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_v1": "What documents are attached to filing 0000320193-25-000079?", "natural_v2": "What exhibits came with filing 0000320193-25-000079?"},
    "get_sec_document": {"arguments": {"accession_no": "0000320193-25-000079"}, "natural_v1": "What does the main document in filing 0000320193-25-000079 say?", "natural_v2": "Can you show me the main document text for filing 0000320193-25-000079?"},
    "diff_sec_filings": {"arguments": {"current_accession": "0000320193-25-000079", "previous_accession": "0000320193-24-000123"}, "natural_v1": "What changed between filings 0000320193-25-000079 and 0000320193-24-000123?", "natural_v2": "How does filing 0000320193-25-000079 differ from filing 0000320193-24-000123?"},
    "get_financial_statements": {"arguments": {"ticker": "MSFT", "statement_type": "income_statement"}, "natural_v1": "How did Microsoft perform on revenue, expenses, and profit?", "natural_v2": "What does Microsoft's recent income statement show for revenue and profit?"},
    "get_xbrl_facts": {"arguments": {"ticker": "AAPL", "concept": "NetIncomeLoss"}, "natural_v1": "What was Apple's net income?", "natural_v2": "What net income did Apple report?"},
    "get_material_events": {"arguments": {"ticker": "AAPL", "since": "2024-01-01"}, "natural_v1": "What big events has Apple disclosed since 2024-01-01?", "natural_v2": "What material has Apple disclosed since 2024-01-01?"},
    "get_beneficial_ownership": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Who owns more than 5% of Apple?", "natural_v2": "What large ownership stakes in Apple have been disclosed?"},
    "get_ownership_changes": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Have Apple's big holders changed their stakes?", "natural_v2": "How have Apple's large holders changed their positions recently?"},
    "get_insider_activity": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Have Apple insiders been buying or selling?", "natural_v2": "What insider purchases and sales has Apple reported?"},
    "get_planned_insider_sales": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Are Apple insiders planning to sell shares?", "natural_v2": "What planned insider sales has Apple disclosed?"},
    "get_offering_history": {"arguments": {"ticker": "AAPL"}, "natural_v1": "What is Apple's offering history?", "natural_v2": "What offerings has Apple done?"},
    "get_dilution_profile": {"arguments": {"ticker": "AAPL"}, "natural_v1": "How diluted could Apple shareholders get?", "natural_v2": "What does Apple's dilution picture look like?"},
    "get_governance_events": {"arguments": {"ticker": "AAPL"}, "natural_v1": "What governance events has Apple had?", "natural_v2": "Has Apple had any board or governance changes?"},
    "get_transaction_status": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Is Apple involved in any deals or mergers?", "natural_v2": "What merger or tender-offer activity involves Apple?"},
    "get_short_pressure_profile": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Is Apple under much short pressure?", "natural_v2": "How much short pressure is on Apple right now?"},
    "search_tools": {"arguments": {"query": "short interest"}, "natural_v1": "Which tools tell me what short sellers are doing in a stock?", "natural_v2": "What can show me what short sellers are doing?"},
    "list_tool_domains": {"arguments": {}, "natural_v1": "What kinds of company and market data can you help with?", "natural_v2": "Can you list the groups of financial information you can look up, like earnings, filings, or ownership?"},
    "describe_tool": {"arguments": {"name": "get_analyst_estimates"}, "natural_v1": "What can your analyst-estimates lookup do, and what does it need from me to run it for Apple?", "natural_v2": "Explain how your analyst expectations lookup works — what inputs it takes and what related lookups pair with it — before pulling numbers for Apple."},
    "diff_risk_factors": {"arguments": {"ticker": "GOOGL"}, "natural_v1": "What changed in Google's risk factors?", "natural_v2": "Compare Google's latest Risk Factors section with the prior filing."},
    "get_recent_ownership_filings": {"arguments": {}, "natural_v1": "What big ownership filings just came out?", "natural_v2": "Which SC 13D/G filings are most recent across the market?"},
    "get_threshold_securities": {"arguments": {}, "natural_v1": "Which stocks are on the threshold list right now?", "natural_v2": "What securities are currently on the SHO threshold list?"},
    "get_short_interest": {"arguments": {"ticker": "AAPL"}, "natural_v1": "How much of Apple is sold short right now?", "natural_v2": "What is Apple's current short interest?"},
    "get_short_interest_leaderboard": {"arguments": {"limit": 5}, "natural_v1": "Which stocks have the highest short interest as a share of total shares?", "natural_v2": "Which stocks are most heavily shorted right now?"},
    "get_reg_sho_volume": {"arguments": {"ticker": "AAPL"}, "natural_v1": "How much short-sale volume has Apple had lately?", "natural_v2": "What is Apple's recent Reg SHO volume?"},
    "get_analyst_estimates": {"arguments": {"ticker": "AAPL"}, "natural_v1": "What are analysts estimating for Apple?", "natural_v2": "What do analysts expect from Apple going forward?"},
    "get_sp500_weight": {"arguments": {"ticker": "AAPL"}, "natural_v1": "What is Apple's S&P 500 weight?", "natural_v2": "How big a part of the S&P 500 is Apple?"},
    "get_obligations": {"arguments": {"ticker": "AAPL"}, "natural_v1": "What contractual obligations does Apple have?", "natural_v2": "What commitments and obligations has Apple disclosed in its latest reports?"},
    "get_valuation_metrics": {"arguments": {"ticker": "AAPL"}, "natural_v1": "Is Apple stock cheap or expensive right now?", "natural_v2": "How does Apple's valuation look on earnings multiples?"},
    "search_web": {"arguments": {"query": "Apple 10-K risk factors"}, "natural_v1": "What are outside commentators saying this week about risks to Apple's business?", "natural_v2": "What are people saying about risks to Apple's business?"},
    "list_finra_datasets": {"arguments": {}, "natural_v1": "What FINRA datasets can I pull?", "natural_v2": "Which FINRA datasets are available?"},
    "describe_finra_dataset": {"arguments": {"dataset_id": "otcMarket/consolidatedShortInterest"}, "natural_v1": "What is in the FINRA short-interest dataset?", "natural_v2": "What fields and coverage does the FINRA consolidated short-interest dataset have?"},
    "get_finra_datapoints": {"arguments": {"dataset": "otcMarket/consolidatedShortInterest", "fields": ["settlementDate", "currentShortPositionQuantity"], "ticker": "AAPL", "limit": 5}, "natural_v1": "What are Apple's recent short-interest values from FINRA?", "natural_v2": "Show me Apple's latest FINRA short-position figures."},
    "query_finra": {"arguments": {"dataset": "otcMarket/consolidatedShortInterest", "ticker": "AAPL", "limit": 5}, "natural_v1": "Has Apple's FINRA short interest changed lately?", "natural_v2": "How has Apple's short interest trended in FINRA data?"},
    "find_alternative_signals": {"arguments": {}, "natural_v1": "What alternative signals have been collected?", "natural_v2": "What alternative data signals are available?"},
    "get_trend_evidence": {"arguments": {"start_date": "2026-09-01", "end_date": "2026-09-02", "geos": ["US"], "limit": 25}, "natural_v1": "What trends were picked up in the US around September 1st?", "natural_v2": "What search trends were collected for the US for September 1-2?"},
    "investigate_social_arbitrage_candidate": {"arguments": {"term": "Stanley"}, "natural_v1": "What is the buzz around 'Stanley' as an investment idea?", "natural_v2": "Is 'Stanley' worth a closer look based on social signals?"},
    "get_macro_context": {"arguments": {"geos": ["geoId/06"], "variables": ["Count_Person"]}, "natural_v1": "How many people live in California?", "natural_v2": "What is the latest population count for California?"},
    "search_company_patents": {"arguments": {"company_id": "Apple Inc.", "assignees": ["Apple Inc."], "limit": 5}, "natural_v1": "What patents has Apple filed lately?", "natural_v2": "What has Apple patented recently?"},
    "thesis_create": {"arguments": {"user_thesis": "I think NVDA AI demand will stay strong."}, "natural_v1": "I think NVDA AI demand will stay strong.", "natural_v2": "I believe demand for NVDA AI will stay strong. Please keep track of this view."},
    "thesis_show": {"arguments": {"id": "thesis-placeholder"}, "natural_v1": "What does thesis thesis-placeholder say?", "natural_v2": "Can you show me thesis thesis-placeholder?"},
    "thesis_refine": {"arguments": {"id": "thesis-placeholder", "clarification": "AI datacenter capex keeps growing."}, "natural_v1": "AI datacenter capex keeps growing. Update thesis thesis-placeholder with that.", "natural_v2": "New info: AI datacenter capex keeps growing. Please update thesis thesis-placeholder."},
    "thesis_watch": {"arguments": {"id": "thesis-placeholder"}, "natural_v1": "What am I watching for thesis thesis-placeholder?", "natural_v2": "What are the watch rules for thesis thesis-placeholder?"},
    "thesis_journal": {"arguments": {"id": "thesis-placeholder", "body": "Operator note: still watching NVDA datacenter demand."}, "natural_v1": "Still watching NVDA datacenter demand. Add that to thesis thesis-placeholder.", "natural_v2": "Please note for thesis thesis-placeholder: still watching NVDA datacenter demand."},
}

class _RoutingCase(TypedDict):
    question: str
    expected_first_search_any: list[str]
    forbidden_before_expected: list[str]

# Deterministic top-3 routing gate derived from VERIFY_CASES (2 natural V1/V2
# questions per tool). No hand-curated list: every verify case must rank its
# intended target in matches[:MAX_ACTIVE_RESEARCH_TOOLS]. Forbidden-before-target
# lists from the legacy benchmark are retained where the same question text
# recurs; all other cases use an empty forbidden list plus the rank check.
# Cap shared with the extension activation path
# (.pi/extensions/stockbot.ts MAX_ACTIVE_RESEARCH_TOOLS): one search activates
# at most the top-3 ranked matches, so the gate asserts the same slice.
# ponytail: deterministic scorer check, not live-Pi sessions.
MAX_ACTIVE_RESEARCH_TOOLS = 3
_LEGACY_FORBIDDEN: dict[str, list[str]] = {
    "why did GPRO shoot up the past 30 days?": ["get_valuation_metrics", "get_offering_history", "get_recent_ownership_filings"],
    "What does Apple earn per share?": ["search_web", "get_beneficial_ownership"],
    "Is Apple stock cheap or expensive right now?": ["get_offering_history", "get_recent_ownership_filings"],
    "Who owns more than 5% of Apple?": ["get_valuation_metrics", "search_web"],
    "What insider purchases and sales has Apple reported?": ["get_valuation_metrics", "search_web"],
    "What big events has Apple disclosed since 2024-01-01?": ["get_valuation_metrics", "get_offering_history"],
    "List Apple recent SEC filings.": ["get_valuation_metrics", "thesis_show"],
    "What is Apple current short interest?": ["get_valuation_metrics", "search_web"],
    "Track my investment thesis on NVDA AI demand staying strong.": ["get_valuation_metrics", "search_web"],
    "What trends were picked up in the US around September 1st?": ["get_valuation_metrics", "search_web"],
    "What patents has Apple filed lately?": ["get_valuation_metrics", "search_web"],
    "What is California unemployment rate?": ["get_valuation_metrics", "get_xbrl_facts"],
    "Has Apple had any board or governance changes?": ["get_valuation_metrics", "search_web"],
}


def build_routing_cases() -> list[_RoutingCase]:
    """Derive routing cases from VERIFY_CASES: 2 natural prompts per tool.

    The discovery triple is permanently active and never search-routed, so its
    cases are excluded (the scorer only ranks TOOL_DISCOVERY_REGISTRY)."""
    cases: list[_RoutingCase] = []
    for tool, case in VERIFY_CASES.items():
        if tool in DISCOVERY_TOOLS:
            continue
        for question in (case["natural_v1"], case["natural_v2"]):
            if not question:
                continue
            cases.append(
                {
                    "question": question,
                    "expected_first_search_any": [tool],
                    "forbidden_before_expected": list(_LEGACY_FORBIDDEN.get(question, [])),
                }
            )
    expected_count = 2 * (len(VERIFY_CASES) - len(DISCOVERY_TOOLS))
    if len(cases) != expected_count:
        raise AssertionError(f"routing cases {len(cases)} != 2 per routable tool ({expected_count}); add the missing natural_v1/v2")
    return cases


def run_routing_benchmark(question_filter: str | None = None) -> int:
    """Top-3 routing gate over VERIFY_CASES-derived prompts. Returns 0 when every
    case ranks its intended target in matches[:MAX_ACTIVE_RESEARCH_TOOLS] with no
    forbidden tool above it; 1 otherwise."""
    from app.tools import _search_tools

    failed = 0
    ran = 0
    for case in build_routing_cases():
        if question_filter and question_filter.lower() not in case["question"].lower():
            continue
        ran += 1
        result = _search_tools({"query": case["question"]}, "benchmark")
        matches = result.get("matches")
        names: list[str] = [m["name"] for m in matches if isinstance(m, dict) and isinstance(m.get("name"), str)] if isinstance(matches, list) else []
        top3 = names[:MAX_ACTIVE_RESEARCH_TOOLS]
        expected = case["expected_first_search_any"]
        expected_ranks = [names.index(e) for e in expected if e in names]
        if not expected_ranks:
            print(f"FAIL {case['question']!r} expected {expected[0]!r} top-3 {top3} rank=absent")
            failed += 1
            continue
        first_expected = min(expected_ranks)
        stray = [n for n in names[:first_expected] if n in case["forbidden_before_expected"]]
        if first_expected >= MAX_ACTIVE_RESEARCH_TOOLS:
            print(f"FAIL {case['question']!r} expected {expected[0]!r} top-3 {top3} rank={first_expected}")
            failed += 1
        elif stray:
            print(f"FAIL {case['question']!r} expected {expected[0]!r} top-3 {top3} rank={first_expected} (forbidden {stray} before expected)")
            failed += 1
        else:
            print(f"PASS {case['question']!r} -> {top3} rank={first_expected}")
    if not ran:
        print("routing benchmark: no cases matched filter", file=sys.stderr)
        return 1
    print(f"routing benchmark: {ran - failed}/{ran} cases rank top-{MAX_ACTIVE_RESEARCH_TOOLS}")
    return 0 if not failed else 1

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


def _natural_prompt_v1(tool: str, args: Mapping[str, object]) -> str:
    """Ordinary user wording for attempt 1; never names the tool."""
    case = VERIFY_CASES.get(tool)
    if case is None or not case.get("natural_v1"):
        raise KeyError(f"missing v1 prompt for tool '{tool}'")
    natural = case["natural_v1"]
    if THESIS_ID_PLACEHOLDER in natural and "id" in args:
        natural = natural.replace(THESIS_ID_PLACEHOLDER, str(args["id"]))
    return natural


def _natural_prompt_v2(tool: str, args: Mapping[str, object]) -> str:
    """Clearer but still natural wording for attempt 2; never names the tool."""
    case = VERIFY_CASES.get(tool)
    if case is None or not case.get("natural_v2"):
        raise KeyError(f"missing v2 prompt for tool '{tool}'")
    natural = case["natural_v2"]
    if THESIS_ID_PLACEHOLDER in natural and "id" in args:
        natural = natural.replace(THESIS_ID_PLACEHOLDER, str(args["id"]))
    return natural


def build_attempt_prompt(tool: str, args: Mapping[str, object], attempt: int) -> str:
    # Attempts 1-2 are natural routing prompts (search_tools -> target); only
    # they can pass. Attempt 3+ is an explicit diagnostic fallback.
    if attempt == 1:
        return _natural_prompt_v1(tool, args)
    if attempt == 2:
        return _natural_prompt_v2(tool, args)
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


def _rejected_tools(conn: sqlite3.Connection) -> list[str]:
    """Tool names with a `tool_failed` event but no errored execution.

    A rejection with no `tool_calls` error row means the harness refused the
    call before dispatch (e.g. schema validation of a probe); a genuine
    execution failure always leaves an errored call row and is excluded here.
    Built-in Pi tools (read, grep, …) never appear in tool_calls and are excluded via the schema set.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.tool_name FROM agent_events e "
        "WHERE e.event_type = 'tool_failed' AND NOT EXISTS "
        "(SELECT 1 FROM tool_calls c WHERE c.tool_name = e.tool_name "
        "AND c.error_type IS NOT NULL)"
    ).fetchall()
    allowed_names = get_registry_sets()["schemas"]
    return sorted(r[0] for r in rows if r[0] in allowed_names)


def _unexpected_tools(conn: sqlite3.Connection, required_tool: str) -> list[str]:
    """Dispatched Stockbot tools other than the discovery triple and the required target.

    tool_calls rows are written only by execute_pi_tool (app/pi_gateway.py:399),
    so every name here is a Stockbot-dispatched call; Pi built-ins never appear.
    """
    names = get_registry_sets()["schemas"]
    rows = conn.execute("SELECT DISTINCT tool_name FROM tool_calls").fetchall()
    return sorted(r[0] for r in rows if r[0] in names and r[0] not in DISCOVERY_TOOLS and r[0] != required_tool)


def _tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """None when `name` has a clean success; otherwise a short failure reason."""
    ok_calls = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)
    ).fetchone()[0]
    if ok_calls < 1:
        return "absent"
    completed = conn.execute(
        "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)
    ).fetchone()[0]
    if completed < 1:
        return "no completed event"
    failed = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NOT NULL", (name,)
    ).fetchone()[0]
    if failed > 0:
        return "failed execution present"
    return None


def _row_errors_transient(conn: sqlite3.Connection, names: Collection[str]) -> bool:
    """True iff `names` have errored calls and every one is transient evidence."""
    name_list = list(names)
    if not name_list:
        return False
    placeholders = ",".join("?" for _ in name_list)
    rows = conn.execute(
        f"SELECT error_type, error_message FROM tool_calls WHERE tool_name IN ({placeholders}) AND error_type IS NOT NULL",
        tuple(name_list),
    ).fetchall()
    if not rows:
        return False
    for error_type, error_message in rows:
        if (error_type or "") in TRANSIENT_ERROR_TYPES:
            continue
        if _TRANSIENT_MESSAGE_RE.search(error_message or ""):
            continue
        return False
    return True


def _search_call_count(conn: sqlite3.Connection) -> int:
    """Number of search_tools invocations in this attempt's transcript."""
    row = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name = 'search_tools'").fetchone()
    return int(row[0]) if row else 0


def _search_queries(conn: sqlite3.Connection) -> list[str]:
    """Ordered search_tools query strings for failure diagnostics."""
    queries: list[str] = []
    rows = conn.execute(
        "SELECT arguments_json FROM tool_calls WHERE tool_name = 'search_tools' ORDER BY started_at, tool_call_id"
    ).fetchall()
    for (raw,) in rows:
        try:
            args: dict[str, object] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        query_value = args.get("query")
        if isinstance(query_value, str):
            queries.append(query_value)
    return queries


def _search_queries_for_db(db_path_str: str) -> list[str]:
    """Read ordered search queries from a finished attempt DB; [] when unavailable."""
    if not db_path_str:
        return []
    db_path = Path(db_path_str)
    if not db_path.is_file():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return _search_queries(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return []

def evaluate_attempt(db_path: Path, required_tool: str, exit_code: int, timed_out: bool, *, completed_override: bool = False, attempt: int = 3) -> tuple[bool, str]:
    # Pi 0.85.0 -p does not exit after answering in this environment; when the
    # recorder DB already shows terminal state, the kill is cleanup, not failure.
    # Routing benchmark: attempts 1-2 allow a clean discovery-triple call and a clean
    # required-target call only (PREREQ_CHAINS is empty: no prerequisite edges remain
    # in TOOL_DISCOVERY_REGISTRY). More than 2 search_tools calls fails the attempt
    # on every attempt (strict 3/3, no exemption).
    # Any other Stockbot tool fails, whether harness-rejected pre-dispatch,
    # dispatched-and-errored, or dispatched-and-successful. A called but unclean
    # discovery tool also fails unless transient-only. Attempt 3 uses an explicit
    # recovery prompt under the same presence checks, exempt from both
    # stray-tool gates. All three attempts must pass (see the all-ok gate in main).
    # Transient (rate_limited / timeout-message-only) never passes: it returns
    # ok=False with a "transient: " reason for bounded same-prompt retry.
    if not db_path.is_file():
        if timed_out and not completed_override:
            return False, f"transient: missing recorder DB: {db_path}"
        return False, f"missing recorder DB: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            search_count = _search_call_count(conn)
            # Searching IS the contract for the search_tools target itself.
            if required_tool != "search_tools" and search_count > 2:
                return False, f"too-many-searches:{search_count}"
            if attempt in (1, 2):
                rejected = list(_rejected_tools(conn))
                if rejected:
                    return False, f"routing failed: harness-rejected call to {', '.join(rejected)}"
                allowed_prereqs = set(PREREQ_CHAINS.get(required_tool, frozenset()))
                unexpected = _unexpected_tools(conn, required_tool)
                stray_wrong = [t for t in unexpected if t not in allowed_prereqs]
                if stray_wrong:
                    return False, f"routing failed: unexpected research tool call(s): {', '.join(stray_wrong)}"
                prereq_errored = [t for t in unexpected if t in allowed_prereqs and _tool_success(conn, t, attempt=attempt) is not None]
                if prereq_errored:
                    if _row_errors_transient(conn, prereq_errored):
                        return False, f"transient: prereq tool(s) transient error: {', '.join(prereq_errored)}"
                    return False, f"routing failed: unexpected research tool call(s): {', '.join(prereq_errored)}"
                dispatched = {r[0] for r in conn.execute("SELECT DISTINCT tool_name FROM tool_calls").fetchall()}
                bad_discovery = sorted(t for t in DISCOVERY_TOOLS if t in dispatched and t != required_tool and _tool_success(conn, t, attempt=attempt) is not None)
                if bad_discovery:
                    if _row_errors_transient(conn, bad_discovery):
                        return False, f"transient: discovery tool(s) transient error: {', '.join(bad_discovery)}"
                    return False, f"routing failed: errored discovery tool call(s): {', '.join(bad_discovery)}"
            if timed_out and not completed_override:
                return False, "transient: pi timeout before terminal state"
            if exit_code != 0 and not completed_override:
                return False, f"pi exit {exit_code}"
            if required_tool not in DISCOVERY_TOOLS:
                search_problem = _tool_success(conn, "search_tools", attempt=attempt)
                if search_problem is not None:
                    if _row_errors_transient(conn, ["search_tools"]):
                        return False, f"transient: search_tools {search_problem} (transient error)"
                    return False, f"routing failed: search_tools absent ({search_problem})"
            target_problem = _tool_success(conn, required_tool, attempt=attempt)
            if target_problem is not None:
                if _row_errors_transient(conn, [required_tool]):
                    return False, f"transient: target '{required_tool}' {target_problem} (transient error)"
                return False, f"target '{required_tool}' absent ({target_problem})"
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

_INFRA_RE = re.compile(r"ratelimit|rate limit|rate-limit|429|quota|too many requests|timeout|timed out|latency|deadline|temporarily|try again|overloaded|503|502|504", re.IGNORECASE)
# Explicit routing-failure markers from evaluate_attempt; these take precedence
# over infra keywords (a routing reason mentioning "timeout"/"429" is routing).
_ROUTING_RE = re.compile(r"routing failed|harness-rejected|unexpected research|errored discovery|too-many-searches|absent|no model telemetry|agent_runs not completed|pi exit|missing recorder DB|DB read failed", re.IGNORECASE)


def is_routing_failure(reason: str) -> bool:
    """True iff reason is an explicit routing failure (never infra)."""
    return bool(_ROUTING_RE.search(reason or ""))


def is_infra_failure(reason: str) -> bool:
    """True iff reason looks like ratelimit/latency infra (still a gate failure, never a pass)."""
    if not reason or is_routing_failure(reason):
        return False
    return bool(_INFRA_RE.search(reason))


def tool_passes(attempts: Collection[object]) -> bool:
    """Strict 3/3: tool passes iff every strict attempt passed."""
    for a in attempts:
        ok = a.get("ok") if isinstance(a, dict) else getattr(a, "ok", None)
        if ok is not True:
            return False
    return True


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
        base_config_failed = code != 0 and not saw_complete and "model" in err_text.lower()
        ok, reason = evaluate_attempt(db_path, tool, code, timed_out, completed_override=saw_complete, attempt=attempt)
        # Routing reasons take precedence over every infra signal, including "model" in stderr.
        model_config_failed = (not is_routing_failure(reason)) and (base_config_failed or is_infra_failure(reason) or is_infra_failure(err_text))
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
    parser.add_argument("--routing", action="store_true", help="run the natural routing benchmark only (no live Pi)")
    parser.add_argument("--routing-filter", default=None, help="run only routing cases containing this substring")
    args = parser.parse_args()
    if args.routing:
        return run_routing_benchmark(args.routing_filter)
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
    if ensure_finra_fixture(durable, tool_names):
        return 1
    jobs = expand_jobs(tool_names, repetitions)
    verify_start = time.monotonic()
    del TRANSIENT_RETRIES[:]
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
        rec: dict[str, object] = {"attempt": r.attempt, "ok": r.ok, "reason": r.reason, "exit": r.exit, "db": r.db, "duration_seconds": r.duration_seconds}
        if not r.ok:
            rec["searchQueries"] = _search_queries_for_db(r.db)
        results[r.tool].append(rec)
        print(f"{r.tool} attempt {r.attempt}/{repetitions}: {'PASS' if r.ok else 'FAIL'} ({r.reason}) [{r.duration_seconds:.1f}s]")
        if r.model_config_failed:
            print("PI MODEL CONFIGURATION FAILED", file=sys.stderr)
    procs = len(ordered)
    failed_tools: list[str] = []
    # Strict 3/3: tool passes iff all strict attempts pass; any single
    # routing failure fails the tool. Infra (ratelimit/latency) still fails
    # the gate but reports as infra, never as pass.
    for tool in tool_names:
        tool_recs = results[tool]
        if tool_passes(tool_recs):
            remove_successful_attempt_dirs(root, tool, tool_recs)
        else:
            failed_tools.append(tool)
            print(f"preserved DBs for {tool}: {root / tool}")
            by_tool = [r for r in ordered if r.tool == tool and not r.ok]
            if by_tool and not any(is_routing_failure(r.reason) for r in by_tool) and all(r.model_config_failed or is_infra_failure(r.reason) for r in by_tool):
                print(f"infra-only failure for {tool} (still FAIL)")
    passed_tools = len(tool_names) - len(failed_tools)
    coverage = f"{passed_tools}/{len(tool_names)} tools"
    wall = time.monotonic() - verify_start
    print(f"git: {sha} | tools {len(tool_names)} x {repetitions} = {total}")
    print(f"Coverage: {coverage} | processes: {procs} | passed: {passed}/{total}")
    print(f"Concurrency: {concurrency} | Wall time: {wall:.1f}s")
    print(f"RESULT: {'PASS' if not failed_tools else 'FAIL'}")
    if failed_tools:
        print(f"failed tools: {failed_tools}")
    summary = {"git_sha": sha, "tool_count": len(tool_names), "repetitions": repetitions, "results": results, "transient_retries": list(TRANSIENT_RETRIES), "concurrency": concurrency, "wall_seconds": wall}
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if not failed_tools else 1


if __name__ == "__main__":
    sys.exit(main())
