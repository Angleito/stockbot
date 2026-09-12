#!/usr/bin/env python3
"""Live Pi tool verification: every describe-visible tool invoked 3/3 by Pi's configured default model.

Fail-closed at every step. Verdict comes only from per-attempt recorder DBs.
Pi configuration is authoritative for which model runs; Stockbot asserts only that non-empty model telemetry exists.
"""

from __future__ import annotations

import argparse
import hashlib
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
from enum import Enum
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOLS, TOOL_DISCOVERY_REGISTRY, build_prerequisite_graph_from_tool_metadata, execute_tool  # noqa: E402
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
_TRANSIENT_MESSAGE_RE = re.compile(r"(?i)\btimed?\s*-?\s*out\b|deadline exceeded|drain.?timeout")
TRANSIENT_RETRY_CAP = 2
DEFAULT_CONCURRENCY = 3
POLL_S = 2
THESIS_ID_PLACEHOLDER = "thesis-placeholder"
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})
FINRA_SEED_TOOLS = frozenset({"get_short_interest_leaderboard"})
# Prerequisite edges derive from TOOL_DISCOVERY_REGISTRY (single source with
# app/tools.py); per-edge description citations enforced by
# tests/test_verify_pi_tools.py::test_prereq_chains_are_documented_in_descriptions.
PREREQ_CHAINS: dict[str, frozenset[str]] = build_prerequisite_graph_from_tool_metadata()
# Discovery primitives: first-class citizens on attempts 1-2, never strays.
DISCOVERY_TOOLS = frozenset({"browse_tools", "call_tool", "list_tool_domains", "search_tools", "describe_tool"})
# Dispatch primitives: schema-only discovery/dispatch surface (no natural fixture;
# call_tool has required args). Plus list_tool_domains/describe_tool: not
# model-visible in TS and forbidden as call_tool inner names, so unreachable by
# construction. None are ever live-matrix targets.
_DISPATCH_PRIMITIVES = frozenset({"browse_tools", "call_tool"})
_MATRIX_EXCLUDED = _DISPATCH_PRIMITIVES | frozenset({"list_tool_domains", "describe_tool"})
# Routing vs reachability split: routing is intentional dispatch (strict),
# reachability is eventual navigation (relaxed). Caps: 4-6 meaningful
# wandering threshold on 47 research tools; 12 is egregious-loop backstop only.
ROUTING_DISCOVERY_CAP = 6
REACHABILITY_DISCOVERY_CAP = 12
# PREREQ_CHAINS is currently empty (no explicit map); routing therefore allows
# only the expected tool itself (zero prerequisites).


class RoutingFailureCategory(str, Enum):
    SELECTION_FAILURE = "SELECTION_FAILURE"
    DISCOVERY_FAILURE = "DISCOVERY_FAILURE"
    DISPATCH_FAILURE = "DISPATCH_FAILURE"
    ARGUMENT_FAILURE = "ARGUMENT_FAILURE"
    EXECUTION_FAILURE = "EXECUTION_FAILURE"


HOLDOUT_SHA256 = "bae0fff4977720ab5a07af26c78adf7ad0855a78e838ff0d1c26b68e9ac6f4eb"


def _surfaced_names(conn: sqlite3.Connection) -> set[str] | None:
    """Discovered tool names from routing_metrics; None when telemetry absent (assume surfaced)."""
    try:
        payload = _routing_metrics_event(conn)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("discovered_tools", "discoveredTools"):
        val = payload.get(key)
        if isinstance(val, list):
            out: set[str] = set()
            for v in val:
                if isinstance(v, str) and v:
                    out.add(v)
            return out
    # Legacy payloads without name lists: fall back to count signal.
    if payload.get("discovered_tool_count") is not None:
        return set()
    return None


def _expected_args_mismatch(conn: sqlite3.Connection, expected_tool: str, expected_args: Mapping[str, object] | None) -> bool:
    if expected_args is None:
        return False
    try:
        want = _normalize_args(dict(expected_args))
    except Exception:
        return False
    try:
        rows = conn.execute(
            "SELECT arguments_json FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (expected_tool,)
        ).fetchall()
    except Exception:
        return False
    try:
        have = {_normalize_args(r[0]) for r in rows}
    except Exception:
        return False
    return bool(rows) and want not in have


def _expected_has_invalid(conn: sqlite3.Connection, expected_tool: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT error_type FROM tool_calls WHERE tool_name = ?", (expected_tool,)
        ).fetchall()
    except Exception:
        return False
    return any(isinstance(r[0], str) and r[0] == "invalid_tool_arguments" for r in rows)


def classify_routing_failure(db_path: Path, expected_tool: str, *, attempt: int, expected_args: Mapping[str, object] | None = None) -> RoutingFailureCategory | None:
    """Earliest pipeline stage that failed; None on pass or infra-transient."""
    try:
        conn = __import__("sqlite3").connect(str(db_path))
    except Exception:
        return None
    try:
        # Infra-transient stays orthogonal: never relabel as ontology failure.
        try:
            if _row_errors_transient(conn, [expected_tool]):
                return None
            unexpected_all = _unexpected_tools(conn, expected_tool)
            if unexpected_all and _row_errors_transient(conn, unexpected_all):
                # If the only signal is transient stray noise, treat as infra.
                pass
        except Exception:
            pass
        is_explicit = attempt >= 3
        if not is_explicit:
            surfaced = _surfaced_names(conn)
            if surfaced is not None and expected_tool not in surfaced:
                return RoutingFailureCategory.DISCOVERY_FAILURE
        try:
            unexpected = _unexpected_tools(conn, expected_tool)
            allowed = set(PREREQ_CHAINS.get(expected_tool, frozenset()))
            clean_strays = [
                x for x in unexpected
                if x not in allowed and _routing_tool_success(conn, x, attempt=attempt) is None
            ]
            if clean_strays:
                return RoutingFailureCategory.SELECTION_FAILURE
        except Exception:
            pass
        try:
            if _call_tool_has_unparseable(conn):
                return RoutingFailureCategory.ARGUMENT_FAILURE
            if expected_tool in _rejected_tools(conn):
                return RoutingFailureCategory.ARGUMENT_FAILURE
        except Exception:
            pass
        try:
            if not _target_dispatched(conn, expected_tool):
                return RoutingFailureCategory.DISPATCH_FAILURE
        except Exception:
            pass
        try:
            if _expected_has_invalid(conn, expected_tool):
                return RoutingFailureCategory.ARGUMENT_FAILURE
            if expected_args is not None and _expected_args_mismatch(conn, expected_tool, expected_args):
                return RoutingFailureCategory.ARGUMENT_FAILURE
        except Exception:
            pass
        try:
            problem = _routing_tool_success(conn, expected_tool, attempt=attempt)
            if problem is not None and "failed execution present" in problem:
                return RoutingFailureCategory.EXECUTION_FAILURE
            # Any errored expected call that survived argument checks is execution.
            try:
                n_err = conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NOT NULL", (expected_tool,)
                ).fetchone()[0]
                if n_err:
                    return RoutingFailureCategory.EXECUTION_FAILURE
            except Exception:
                pass
        except Exception:
            pass
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


class ConfusionCase(TypedDict):
    expected_tool: str
    prompt: str
    arguments: dict[str, object]
    pair: list[str]

def generate_confusion_cases() -> list[ConfusionCase]:
    """Two directed cases per undirected conflicts_with edge, from registry semantics."""
    seen: set[frozenset[str]] = set()
    for name, meta in TOOL_DISCOVERY_REGISTRY.items():
        for peer in meta.conflicts_with:
            seen.add(frozenset({name, peer}))
    cases: list[ConfusionCase] = []
    for pair in sorted(sorted(p) for p in seen):
        a, b = pair[0], pair[1]
        for expected in (a, b):
            meta = TOOL_DISCOVERY_REGISTRY[expected]
            if not meta.choose_when:
                raise ValueError(f"missing choose_when for confusion case {expected!r}")
            fixture = VERIFY_CASES.get(expected)
            if fixture is None or not isinstance(fixture.get("arguments"), dict):
                raise ValueError(f"missing verification fixture for confusion edge {expected!r}")
            prompt = meta.choose_when[0]
            cases.append({
                "expected_tool": expected,
                "prompt": prompt,
                "arguments": dict(fixture["arguments"]),
                "pair": list(pair),
            })
    return cases


def _normalize_args(raw: object) -> str:
    """Canonical args key: sorted-keys JSON so whitespace/key order never fails."""
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, dict):
        try:
            return json.dumps(raw, sort_keys=True, default=str)
        except Exception:
            return str(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw.strip()
        if isinstance(parsed, dict):
            return json.dumps(parsed, sort_keys=True, default=str)
        return json.dumps(parsed, sort_keys=True, default=str)
    try:
        return json.dumps(raw, sort_keys=True, default=str)
    except Exception:
        return str(raw)


def _call_tool_start_count(conn: sqlite3.Connection) -> int:
    """Outer call_tool dispatches in this trace (agent_events lifecycle)."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_started' AND tool_name = 'call_tool'").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _trace_metrics(conn: sqlite3.Connection, expected_tool: str) -> dict[str, object]:
    """Efficiency accounting, always logged even on pass."""
    try:
        disc = _discovery_attempt_count(conn)
    except sqlite3.Error:
        disc = 0
    try:
        failed_disc = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains') AND error_type IS NOT NULL").fetchone()[0]
    except sqlite3.Error:
        failed_disc = 0
    try:
        names = get_registry_sets()["schemas"]
    except Exception:
        names = set()
    research_names = [n for n in names if n not in DISCOVERY_TOOLS]
    research_calls = failed_research = direct_tool_calls = 0
    if research_names:
        ph = ",".join("?" for _ in research_names)
        try:
            research_calls = int(conn.execute(f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph})", tuple(research_names)).fetchone()[0] or 0)
            failed_research = int(conn.execute(f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph}) AND error_type IS NOT NULL", tuple(research_names)).fetchone()[0] or 0)
            # Direct executions complete under their own name; call_tool fallback
            # completes under 'call_tool'. Counts both routing styles.
            direct_tool_calls = int(conn.execute(f"SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name IN ({ph})", tuple(research_names)).fetchone()[0] or 0)
        except sqlite3.Error:
            pass
    try:
        rejected_disc = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_failed' AND tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains')").fetchone()[0]
    except sqlite3.Error:
        rejected_disc = 0
    return {"discovery_calls": int(disc or 0), "failed_discovery_calls": int(failed_disc or 0), "rejected_discovery_calls": int(rejected_disc or 0), "research_calls": int(research_calls or 0), "failed_research_calls": int(failed_research or 0), "target_dispatched": expected_tool in _call_tool_dispatched_names(conn) or _direct_tool_completed(conn, expected_tool), "call_tool_count": _call_tool_start_count(conn), "direct_tool_calls": int(direct_tool_calls or 0)}


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

def _batch_id() -> str:
    """Process-unique batch ID: UTC microseconds plus PID so parallel probes never share a directory."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-p{os.getpid()}"


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
    payload = json.dumps(args, sort_keys=True)
    if tool == "search_tools":
        return (
            f"You are verifying Stockbot tool wiring. Call the `search_tools` tool "
            f"with exactly these arguments: {payload}. "
            f"Then summarize the result in one sentence. End your reply with "
            f"`TOOL_CHECK: PASS` if you called `search_tools` or `TOOL_CHECK: FAIL` otherwise."
        )
    return (
        f"You may use browse_tools, search_tools, or describe_tool to locate the tool, then Call call_tool exactly once with name=\"{tool}\" and arguments={payload}. "
        f"Then summarize the result in one sentence. End your reply with TOOL_CHECK: PASS if that call completed, or TOOL_CHECK: FAIL otherwise."
        )


# Ordered natural-discovery path for routing attempts 1-2: one search, at most
# one browse, then exactly one call_tool dispatch. Browse with name
# only (never combine name with domain/family); copy required_arguments and
# schema keys verbatim into call_tool. Also states the unsupported-request
# shape (clean zero-match search, no research call, plain-language limitation
# answer) per evaluate_completion_attempt.
_NATURAL_ROUTING_GUIDANCE = (
    " To answer, first call search_tools once with a single query, then call "
    "browse_tools with name only (never combine name with domain or family) "
    "at most once to confirm required_arguments, then call call_tool exactly "
    "once with the single best-matching research tool, copying required argument "
    "keys verbatim from the discovery card. Dispatch research tools "
    "only via call_tool; never chain a second research tool (dispatch an update "
    "directly instead of reading the record first). If search_tools returns "
    "zero matches, call no research tool and answer plainly that the request "
    "is unsupported."
)

_SEARCH_ONLY_GUIDANCE = (
    " To answer, call search_tools once with a single query, then summarize "
    "its matches in one sentence. Do not call call_tool, browse_tools, or any "
    "research tool; the discovery matches are the answer."
)

def _natural_prompt_v1(tool: str, args: Mapping[str, object]) -> str:
    """Ordinary user wording for attempt 1; never names the tool."""
    case = VERIFY_CASES.get(tool)
    if case is None or not case.get("natural_v1"):
        raise KeyError(f"missing v1 prompt for tool '{tool}'")
    natural = case["natural_v1"]
    if THESIS_ID_PLACEHOLDER in natural and "id" in args:
        natural = natural.replace(THESIS_ID_PLACEHOLDER, str(args["id"]))
    if tool == "search_tools":
        return natural + _SEARCH_ONLY_GUIDANCE
    return natural + _NATURAL_ROUTING_GUIDANCE


def _natural_prompt_v2(tool: str, args: Mapping[str, object]) -> str:
    """Clearer but still natural wording for attempt 2; never names the tool."""
    case = VERIFY_CASES.get(tool)
    if case is None or not case.get("natural_v2"):
        raise KeyError(f"missing v2 prompt for tool '{tool}'")
    natural = case["natural_v2"]
    if THESIS_ID_PLACEHOLDER in natural and "id" in args:
        natural = natural.replace(THESIS_ID_PLACEHOLDER, str(args["id"]))
    if tool == "search_tools":
        return natural + _SEARCH_ONLY_GUIDANCE
    return natural + _NATURAL_ROUTING_GUIDANCE


def build_attempt_prompt(tool: str, args: Mapping[str, object], attempt: int) -> str:
    # Attempts 1-2 are natural routing prompts (search_tools -> target); only
    # they can pass. Attempt 3+ is an explicit diagnostic fallback.
    if attempt == 1:
        return _natural_prompt_v1(tool, args)
    if attempt == 2:
        return _natural_prompt_v2(tool, args)
    return build_explicit_prompt(tool, args)

def build_routing_explicit_prompt(tool: str, args: Mapping[str, object]) -> str:
    """Strict attempt-3: exact direct dispatch, no discovery."""
    payload = json.dumps(args, sort_keys=True)
    if tool == "search_tools":
        return build_explicit_prompt(tool, args)
    return (
        f"Do not call browse_tools, search_tools, describe_tool, or list_tool_domains. "
        f"Call call_tool exactly once with name=\"{tool}\" and arguments={payload}. "
        f"Then summarize the result in one sentence. End your reply with TOOL_CHECK: PASS if that call completed, or TOOL_CHECK: FAIL otherwise."
    )


def build_routing_attempt_prompt(tool: str, args: Mapping[str, object], attempt: int) -> str:
    """Routing prompts: attempts 1-2 are natural traces carrying the ordered
    search -> browse/describe -> call_tool path plus the zero-match
    unsupported-request shape; attempt 3+ is strict direct dispatch."""
    if attempt == 1:
        return _natural_prompt_v1(tool, args)
    if attempt == 2:
        return _natural_prompt_v2(tool, args)
    return build_routing_explicit_prompt(tool, args)


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
    Rejected discovery primitives (browse/search/describe/list-domains) never
    fail: they are free exploration, not dispatch attempts.
    """
    rows = conn.execute(
        "SELECT DISTINCT e.tool_name FROM agent_events e "
        "WHERE e.event_type = 'tool_failed' AND NOT EXISTS "
        "(SELECT 1 FROM tool_calls c WHERE c.tool_name = e.tool_name "
        "AND c.error_type IS NOT NULL)"
    ).fetchall()
    allowed_names = get_registry_sets()["schemas"]
    ignored = frozenset({"browse_tools", "search_tools", "describe_tool", "list_tool_domains"})
    return sorted(r[0] for r in rows if r[0] in allowed_names and r[0] not in ignored)


def _unexpected_tools(conn: sqlite3.Connection, required_tool: str) -> list[str]:
    """Dispatched Stockbot tools other than the discovery set and the required target.

    tool_calls rows are written only by execute_pi_tool (app/pi_gateway.py:399),
    so every name here is a Stockbot-dispatched call; Pi built-ins never appear.
    """
    names = get_registry_sets()["schemas"]
    rows = conn.execute("SELECT DISTINCT tool_name FROM tool_calls").fetchall()
    return sorted(r[0] for r in rows if r[0] in names and r[0] not in DISCOVERY_TOOLS and r[0] != required_tool)


def _call_tool_dispatched_names(conn: sqlite3.Connection) -> set[str]:
    """Inner names Pi requested via outer `call_tool` lifecycle events.

    The gateway records the inner canonical name in `tool_calls` but writes no
    outer row; Pi records only the outer `call_tool` lifecycle in
    `agent_events`. The `tool_started` event keeps the outer arguments, which
    carry the inner name. Lenient on shape; unparseable rows are ignored.
    """
    names: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT arguments FROM agent_events WHERE event_type = 'tool_started' AND tool_name = 'call_tool'"
        ).fetchall()
    except sqlite3.Error:
        return names
    for (raw,) in rows:
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        inner = payload.get("name")
        if isinstance(inner, str) and inner:
            names.add(inner)
            continue
        wrapped = payload.get("arguments")
        if isinstance(wrapped, dict):
            inner_wrapped = wrapped.get("name")
            if isinstance(inner_wrapped, str) and inner_wrapped:
                names.add(inner_wrapped)
    return names


def _direct_tool_completed(conn: sqlite3.Connection, name: str) -> bool:
    """True iff `name` executed directly: a clean tool_calls row plus a direct agent_events tool_completed row.

    The call_tool fallback writes the inner name to tool_calls but completes
    under 'call_tool' in agent_events, so the direct completed row separates
    direct routing from fallback dispatch.
    """
    try:
        clean = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)).fetchone()[0]
        if not clean:
            return False
        direct = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)).fetchone()[0]
    except sqlite3.Error:
        return False
    return bool(direct)


def _target_dispatched(conn: sqlite3.Connection, name: str) -> bool:
    """Intentional dispatch by either route: direct completion or call_tool inner dispatch."""
    return _direct_tool_completed(conn, name) or name in _call_tool_dispatched_names(conn)


def _call_tool_has_unparseable(conn: sqlite3.Connection) -> bool:
    """True iff any outer `call_tool` row carries unparseable/missing inner args."""
    try:
        rows = conn.execute(
            "SELECT arguments FROM agent_events WHERE event_type = 'tool_started' AND tool_name = 'call_tool'"
        ).fetchall()
    except sqlite3.Error:
        return False
    for (raw,) in rows:
        if not isinstance(raw, str) or not raw:
            return True
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return True
        if not isinstance(payload, dict):
            return True
        inner = payload.get("name")
        if isinstance(inner, str) and inner:
            continue
        wrapped = payload.get("arguments")
        if isinstance(wrapped, dict) and isinstance(wrapped.get("name"), str) and wrapped.get("name"):
            continue
        return True
    return False


def _tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """None when `name` has a clean success; otherwise a short failure reason."""
    ok_calls = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)
    ).fetchone()[0]
    if ok_calls < 1:
        return "absent"
    failed = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NOT NULL", (name,)
    ).fetchone()[0]
    if failed > 0:
        # Strict: any errored execution fails, even with different arguments.
        return "failed execution present"
    completed = conn.execute(
        "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)
    ).fetchone()[0]
    if completed >= 1:
        return None
    # Generic-dispatch path: inner success in `tool_calls`, outer `call_tool`
    # lifecycle in `agent_events`. Correlate via the started event's inner name.
    if name in _call_tool_dispatched_names(conn):
        outer_completed = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = 'call_tool'"
        ).fetchone()[0]
        if outer_completed >= 1:
            return None
    return "no completed event"

def _reachability_tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """Relaxed: same-tool different-args probe forgiven; same-args failure still fails."""
    try:
        rows = conn.execute("SELECT arguments_json, error_type FROM tool_calls WHERE tool_name = ?", (name,)).fetchall()
    except sqlite3.Error:
        return "absent"
    clean = {_normalize_args(r[0]) for r in rows if r[1] is None}
    if not clean:
        return "absent"
    failed = [_normalize_args(r[0]) for r in rows if r[1] is not None]
    if any(f in clean for f in failed):
        return "failed execution present"
    completed = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)).fetchone()[0]
    if completed >= 1:
        return None
    if name in _call_tool_dispatched_names(conn):
        outer_completed = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = 'call_tool'").fetchone()[0]
        if outer_completed >= 1:
            return None
    return "no completed event"


def _routing_tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """Strict alias: any errored execution fails, even with different arguments."""
    return _tool_success(conn, name, attempt=attempt)


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
def _discovery_attempt_count(conn: sqlite3.Connection) -> int:
    """Any attempted discovery call (clean or errored) across the four primitives."""
    row = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains')").fetchone()
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

def evaluate_reachability_attempt(db_path: Path, required_tool: str, exit_code: int, timed_out: bool, *, completed_override: bool = False, attempt: int = 3) -> tuple[bool, str]:
    """Relaxed reachability: can the model eventually navigate catalog and dispatch target via call_tool."""
    if not db_path.is_file():
        if timed_out and not completed_override:
            return False, "transient: pi timeout before terminal state"
        return False, f"missing recorder DB: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            discovery_count = _discovery_attempt_count(conn)
            if required_tool != "search_tools" and discovery_count > REACHABILITY_DISCOVERY_CAP:
                return False, f"too-many-discovery:{discovery_count}"
            rejected = list(_rejected_tools(conn))
            if rejected:
                return False, f"routing failed: harness-rejected call to {', '.join(rejected)}"
            if _call_tool_has_unparseable(conn):
                return False, "routing failed: unparseable call_tool dispatch args"
            unexpected = _unexpected_tools(conn, required_tool)
            if unexpected:
                errored = [t for t in unexpected if _reachability_tool_success(conn, t, attempt=attempt) is not None]
                if errored and _row_errors_transient(conn, errored):
                    return False, f"transient: stray tool(s) transient error: {', '.join(errored)}"
                if errored:
                    return False, f"routing failed: unexpected research tool call(s): {', '.join(errored)}"
            _early_target = _reachability_tool_success(conn, required_tool, attempt=attempt)
            if _early_target is not None and _row_errors_transient(conn, [required_tool]):
                return False, f"transient: target '{required_tool}' {_early_target} (transient error)"
            if required_tool != "search_tools":
                if attempt in (1, 2):
                    discovery_ok = any(_reachability_tool_success(conn, t, attempt=attempt) is None for t in ("browse_tools", "search_tools", "describe_tool", "list_tool_domains"))
                    if not discovery_ok:
                        return False, "routing failed: no clean discovery before call_tool"
                    if not _target_dispatched(conn, required_tool):
                        return False, f"routing failed: '{required_tool}' not dispatched via call_tool"
                    if not _discovery_before_dispatch(conn, required_tool):
                        return False, f"routing failed: discovery did not precede call_tool dispatch of '{required_tool}'"
                else:
                    if required_tool not in _call_tool_dispatched_names(conn):
                        return False, f"routing failed: '{required_tool}' not dispatched via call_tool"
            if timed_out and not completed_override:
                return False, "transient: pi timeout before terminal state"
            if exit_code != 0 and not completed_override:
                return False, f"pi exit {exit_code}"
            target_problem = _reachability_tool_success(conn, required_tool, attempt=attempt)
            if target_problem is not None:
                if _row_errors_transient(conn, [required_tool]):
                    return False, f"transient: target '{required_tool}' {target_problem} (transient error)"
                return False, f"target '{required_tool}' absent ({target_problem})"
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


def evaluate_routing_attempt(db_path: Path, required_tool: str, exit_code: int, timed_out: bool, *, completed_override: bool = False, attempt: int = 3, expected_args: Mapping[str, object] | None = None) -> tuple[bool, str]:
    """Strict routing precision: intentional dispatch without unrelated research execution."""
    if not db_path.is_file():
        if timed_out and not completed_override:
            return False, "transient: pi timeout before terminal state"
        return False, f"missing recorder DB: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            discovery_count = _discovery_attempt_count(conn)
            if required_tool != "search_tools" and discovery_count > ROUTING_DISCOVERY_CAP:
                return False, f"too-many-discovery:{discovery_count}"
            rejected = list(_rejected_tools(conn))
            if rejected:
                return False, f"routing failed: harness-rejected call to {', '.join(rejected)}"
            if _call_tool_has_unparseable(conn):
                return False, "routing failed: unparseable call_tool dispatch args"
            unexpected = _unexpected_tools(conn, required_tool)
            if unexpected:
                allowed = set(PREREQ_CHAINS.get(required_tool, frozenset()))
                clean_strays = [t for t in unexpected if t not in allowed and _routing_tool_success(conn, t, attempt=attempt) is None]
                if clean_strays:
                    return False, f"routing failed: unrelated-research-success: unexpected research tool call(s): {', '.join(clean_strays)}"
                errored_strays = [t for t in unexpected if t not in allowed]
                if errored_strays:
                    if _row_errors_transient(conn, errored_strays):
                        return False, f"transient: stray tool(s) transient error: {', '.join(errored_strays)}"
                    return False, f"routing failed: failed-research-call: unexpected research tool call(s): {', '.join(errored_strays)}"
            _early_target = _routing_tool_success(conn, required_tool, attempt=attempt)
            if _early_target is not None and _row_errors_transient(conn, [required_tool]):
                return False, f"transient: target '{required_tool}' {_early_target} (transient error)"
            if _early_target is not None and "failed execution present" in _early_target:
                return False, f"routing failed: failed-research-call for '{required_tool}' (failed execution present)"
            if required_tool != "search_tools":
                if attempt in (1, 2):
                    discovery_ok = any(_routing_tool_success(conn, t, attempt=attempt) is None for t in ("browse_tools", "search_tools", "describe_tool", "list_tool_domains"))
                    if not discovery_ok:
                        return False, "routing failed: no clean discovery before call_tool"
                    if not _target_dispatched(conn, required_tool):
                        return False, f"routing failed: target-never-dispatched: '{required_tool}' not dispatched via call_tool"
                    if not _discovery_before_dispatch(conn, required_tool):
                        return False, f"routing failed: discovery did not precede call_tool dispatch of '{required_tool}'"
                else:
                    if discovery_count > 0:
                        return False, f"routing failed: attempt 3 must not browse/search/describe; call call_tool exactly once (discovery_calls={discovery_count})"
                    n_call = _call_tool_start_count(conn)
                    if n_call != 1:
                        return False, f"routing failed: attempt 3 must call call_tool exactly once (got {n_call})"
                    if required_tool not in _call_tool_dispatched_names(conn):
                        return False, f"routing failed: target-never-dispatched: '{required_tool}' not dispatched via call_tool"
                    if expected_args is not None:
                        try:
                            want = _normalize_args(dict(expected_args))
                        except Exception:
                            want = ""
                        try:
                            have_rows: list[tuple[object]] = conn.execute("SELECT arguments_json FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (required_tool,)).fetchall()
                        except sqlite3.Error:
                            have_rows = []
                        have = {_normalize_args(r[0]) for r in have_rows}
                        if want not in have:
                            return False, f"routing failed: attempt 3 args mismatch for '{required_tool}'"
            if timed_out and not completed_override:
                return False, "transient: pi timeout before terminal state"
            if exit_code != 0 and not completed_override:
                return False, f"pi exit {exit_code}"
            target_problem = _routing_tool_success(conn, required_tool, attempt=attempt)
            if target_problem is not None:
                if _row_errors_transient(conn, [required_tool]):
                    return False, f"transient: target '{required_tool}' {target_problem} (transient error)"
                if "failed execution present" in target_problem:
                    return False, f"routing failed: failed-research-call for '{required_tool}' (failed execution present)"
                return False, f"target '{required_tool}' absent ({target_problem})"
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


def evaluate_attempt(db_path: Path, required_tool: str, exit_code: int, timed_out: bool, *, completed_override: bool = False, attempt: int = 3) -> tuple[bool, str]:
    """Back-compat alias: strict routing verdict."""
    return evaluate_routing_attempt(db_path, required_tool, exit_code, timed_out, completed_override=completed_override, attempt=attempt)


def evaluate_reachability_tool(attempt_results: Collection[object]) -> bool:
    """Reachability 3/3: every attempt passed under reachability rules."""
    for a in attempt_results:
        if isinstance(a, dict):
            ok = a.get("reach_ok", a.get("ok"))
            if ok is None:
                ok = a.get("ok")
        else:
            ok = getattr(a, "reach_ok", None)
            if ok is None:
                ok = getattr(a, "ok", None)
        if ok is not True:
            return False
    return True


def evaluate_routing_tool(attempt_results: Collection[object]) -> bool:
    """Routing 3/3: every attempt passed under routing rules."""
    for a in attempt_results:
        if isinstance(a, dict):
            ok = a.get("routing_ok", a.get("ok"))
            if ok is None:
                ok = a.get("ok")
        else:
            ok = getattr(a, "routing_ok", None)
            if ok is None:
                ok = getattr(a, "ok", None)
        if ok is not True:
            return False
    return True


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
    reach_ok: bool | None = None
    reach_reason: str = ""
    routing_ok: bool | None = None
    routing_reason: str = ""
    discovery_calls: int = 0
    research_calls: int = 0
    direct_tool_calls: int = 0
    completion_ok: bool | None = None
    completion_reason: str = ""
    routing_metrics: dict[str, object] | None = None
    terminal_completed: bool = False
    failure_category: RoutingFailureCategory | None = None
_INFRA_RE = re.compile(r"ratelimit|rate-limit|rate limit|429|timeout|timed out|latency|deadline exceeded|drain.?timeout", re.IGNORECASE)
# Explicit routing-failure markers from evaluate_attempt; these take precedence
# over infra keywords (a routing reason mentioning "timeout"/"429" is routing).
_ROUTING_RE = re.compile(r"routing failed|harness-rejected|unexpected research|unrelated-research-success|failed-research-call|target-never-dispatched|errored discovery|too-many-searches|too-many-discovery|absent|no model telemetry|agent_runs not completed|pi exit|missing recorder DB|DB read failed", re.IGNORECASE)


def is_routing_failure(reason: str) -> bool:
    """True iff reason is an explicit routing failure (never infra)."""
    return bool(_ROUTING_RE.search(reason or ""))


def is_infra_failure(reason: str) -> bool:
    """True iff reason looks like ratelimit/latency infra (still a gate failure, never a pass)."""
    if not reason or is_routing_failure(reason):
        return False
    return bool(_INFRA_RE.search(reason))


def tool_passes(attempts: Collection[object]) -> bool:
    """Strict 3/3 routing: tool passes iff every routing attempt passed."""
    return evaluate_routing_tool(attempts)


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


def _metrics_for_db(db_path: Path, tool: str) -> tuple[int, int]:
    """Best-effort efficiency counts for AttemptResult; (0, 0) when DB unreadable."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            m = _trace_metrics(conn, tool)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0
    disc = m.get("discovery_calls", 0)
    resc = m.get("research_calls", 0)
    return (disc if isinstance(disc, int) else 0), (resc if isinstance(resc, int) else 0)


def _direct_count_for_db(db_path: Path, tool: str) -> int:
    """Best-effort direct-execution count for AttemptResult; 0 when DB unreadable."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            m = _trace_metrics(conn, tool)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    direct = m.get("direct_tool_calls", 0)
    return direct if isinstance(direct, int) else 0


def _routing_metrics_for_db(db_path: Path | str) -> dict[str, object] | None:
    """Best-effort routing_metrics event payload; None when DB unreadable or absent."""
    if not db_path:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(path))
        try:
            return _routing_metrics_event(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _median(values: list[int]) -> float:
    """Median of ints; 0.0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _metric_int(metrics: dict[str, object] | None, key: str) -> int:
    value = metrics.get(key, 0) if metrics else 0
    return value if isinstance(value, int) else 0


def _agg_section(aggregates: dict[str, object], key: str) -> dict[str, object]:
    """One nested aggregate section as a dict; {} when missing or malformed."""
    section = aggregates.get(key)
    if isinstance(section, dict):
        return dict(section)
    return {}


def _agg_float(section: dict[str, object], key: str) -> float:
    """One numeric aggregate value; 0.0 when missing or malformed."""
    value = section.get(key, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def aggregate_results(results: list[AttemptResult]) -> dict[str, object]:
    """Attempt-level aggregates over terminal attempts after transient retries.

    Population is every terminal AttemptResult (one per tool x attempt after the
    retry loop); each rate carries its numerator/denominator so the formula is
    auditable. Completion covers only attempts with a completion verdict
    (natural attempts and agent-loop cases, never explicit attempt-3 runs).
    """
    pop = [r for r in results if r.terminal_completed]
    total = len(pop)
    routing_ok = sum(1 for r in pop if (r.routing_ok if r.routing_ok is not None else r.ok))
    reach_ok = sum(1 for r in pop if (r.reach_ok if r.reach_ok is not None else r.ok))
    with_discovery = [r for r in pop if _metric_int(r.routing_metrics, "discovered_tool_count") > 0]
    with_research = [r for r in with_discovery if r.research_calls > 0]
    premature = [r for r in pop if (r.routing_metrics or {}).get("premature_stop_detected") is True]
    injected = [r for r in pop if (r.routing_metrics or {}).get("continuation_injected") is True]
    recovered = [r for r in injected if (r.routing_metrics or {}).get("continuation_succeeded") is True]
    invalid_attempts = [r for r in pop if _metric_int(r.routing_metrics, "invalid_tool_calls") > 0]
    unrelated_total = sum(_metric_int(r.routing_metrics, "unrelated_research_calls") for r in pop)
    invalid_total = sum(_metric_int(r.routing_metrics, "invalid_tool_calls") for r in pop)
    routed_total = sum(_metric_int(r.routing_metrics, "discovery_calls") + _metric_int(r.routing_metrics, "call_tool_count") + _metric_int(r.routing_metrics, "direct_tool_calls") for r in pop)
    invalid_rate = (invalid_total / routed_total) if routed_total else 0.0
    invalid_rate = min(1.0, max(0.0, invalid_rate))
    evaluated = [r for r in pop if r.completion_ok is not None]
    completed = [r for r in evaluated if r.completion_ok]
    failure_counts: dict[str, int] = {c.value: sum(1 for r in pop if r.failure_category == c) for c in RoutingFailureCategory}
    return {
        "population": total,
        "failure_category_counts": failure_counts,
        "routing_precision": {"passed": routing_ok, "total": total, "rate": (routing_ok / total) if total else 0.0},
        "reachability": {"passed": reach_ok, "total": total, "rate": (reach_ok / total) if total else 0.0},
        "discovery_to_research_conversion": {"with_research": len(with_research), "with_discovery": len(with_discovery), "rate": (len(with_research) / len(with_discovery)) if with_discovery else 0.0},
        "premature_stop_rate": {"premature": len(premature), "total": total, "rate": (len(premature) / total) if total else 0.0},
        "continuation_recovery": {"recovered": len(recovered), "injected": len(injected), "rate": (len(recovered) / len(injected)) if injected else 1.0},
        "median_discovery_calls": _median([r.discovery_calls for r in pop]),
        "median_research_calls": _median([r.research_calls for r in pop]),
        "invalid_tool_call_rate": {"invalid_calls": invalid_total, "routed_calls": routed_total, "invalid_attempts": len(invalid_attempts), "total": total, "rate": invalid_rate},
        "unrelated_research_calls": unrelated_total,
        "completion": {"passed": len(completed), "evaluated": len(evaluated), "rate": (len(completed) / len(evaluated)) if evaluated else None},
    }


def run_verification_attempt(tool: str, attempt: int, base_args: Mapping[str, object], batch_root: Path, cwd: Path, durable: Path, repetitions: int, *, prompt_override: str | None = None) -> AttemptResult:
    """Own one Pi attempt end to end: isolated store/DB/fixture, then dual-evaluate. Bounded same-prompt transient retries."""
    start = time.monotonic()
    try:
        for retry in range(TRANSIENT_RETRY_CAP + 1):
            db_path, store_dir = attempt_dirs(batch_root, tool, attempt, retry=retry)
            store_dir.mkdir(parents=True, exist_ok=True)
            args = dict(base_args)
            if tool in FINRA_SEED_TOOLS:
                seed_finra_fixture(store_dir, durable)
            if tool in THESIS_ID_TOOLS:
                fixture_id = ensure_thesis_fixture(store_dir.resolve())
                for key, value in args.items():
                    if value == THESIS_ID_PLACEHOLDER:
                        args[key] = fixture_id
            if prompt_override is not None:
                prompt = prompt_override
            elif attempt >= 3:
                prompt = build_routing_attempt_prompt(tool, args, attempt)
            else:
                prompt = build_attempt_prompt(tool, args, attempt)
            code, timed_out, _out, err_text, saw_complete = run_pi(prompt, db_path, cwd, store_dir)
            base_config_failed = code != 0 and not saw_complete and "model" in err_text.lower()
            reach_ok, reach_reason = evaluate_reachability_attempt(db_path, tool, code, timed_out, completed_override=saw_complete, attempt=attempt)
            routing_ok, routing_reason = evaluate_routing_attempt(db_path, tool, code, timed_out, completed_override=saw_complete, attempt=attempt, expected_args=args)
            ok, reason = routing_ok, routing_reason
            model_config_failed = (not is_routing_failure(reason)) and (base_config_failed or is_infra_failure(reason) or is_infra_failure(err_text) or is_infra_failure(reach_reason))
            duration_seconds = time.monotonic() - start
            transient_reason = ""
            if routing_reason.startswith("transient: "):
                transient_reason = routing_reason
            elif reach_reason.startswith("transient: ") and not is_routing_failure(routing_reason):
                transient_reason = reach_reason
            if transient_reason:
                TRANSIENT_RETRIES.append({"tool": tool, "attempt": attempt, "retry": retry, "reason": transient_reason, "db": str(db_path)})
                if retry < TRANSIENT_RETRY_CAP:
                    continue
                disc, resc = _metrics_for_db(db_path, tool)
                return AttemptResult(tool, attempt, False, f"transient budget exhausted: {transient_reason}", code, str(db_path), duration_seconds, model_config_failed, False, transient_reason, False, transient_reason, disc, resc, _direct_count_for_db(db_path, tool), None, "", _routing_metrics_for_db(db_path))
            disc, resc = _metrics_for_db(db_path, tool)
            comp_ok, comp_reason = (evaluate_completion_attempt(db_path, tool, code, timed_out, completed_override=saw_complete) if attempt in (1, 2) else (None, ""))
            try:
                failure_cat = None if ok else classify_routing_failure(db_path, tool, attempt=attempt, expected_args=args)
            except Exception:
                failure_cat = None
            return AttemptResult(tool, attempt, ok, reason, code, str(db_path), duration_seconds, model_config_failed, reach_ok, reach_reason, routing_ok, routing_reason, disc, resc, _direct_count_for_db(db_path, tool), comp_ok, comp_reason, _routing_metrics_for_db(db_path), terminal_completed=bool(saw_complete), failure_category=failure_cat)
        return AttemptResult(tool, attempt, False, "transient budget exhausted: retry loop fell through", 124, "", time.monotonic() - start, False, False, "transient budget exhausted", False, "transient budget exhausted")
    except Exception as exc:
        elapsed = time.monotonic() - start
        try:
            fallback = str(attempt_dirs(batch_root, tool, attempt)[0])
        except Exception:
            fallback = ""
        return AttemptResult(tool, attempt, False, f"attempt error: {exc}", 124, fallback, elapsed)

def _discovery_before_dispatch(conn: sqlite3.Connection, expected_tool: str) -> bool:
    """True iff a clean discovery call started before the expected inner dispatch."""
    try:
        disc = conn.execute(
            "SELECT MIN(started_at) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains') AND error_type IS NULL"
        ).fetchone()[0]
        inner = conn.execute(
            "SELECT MIN(started_at) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (expected_tool,)
        ).fetchone()[0]
    except sqlite3.Error:
        return False
    return bool(disc) and bool(inner) and str(disc) < str(inner)


def evaluate_holdout_attempt(db_path: Path, expected_tool: str) -> tuple[bool, str]:
    """Holdout routing verdict: attempt-1 routing branch."""
    return evaluate_routing_attempt(db_path, expected_tool, 0, False, attempt=1)


def evaluate_holdout_reachability_attempt(db_path: Path, expected_tool: str) -> tuple[bool, str]:
    """Holdout reachability verdict: attempt-1 reachability branch."""
    return evaluate_reachability_attempt(db_path, expected_tool, 0, False, attempt=1)

def _routing_continuation_count(conn: sqlite3.Connection) -> int:
    """Queued hidden routing continuations in this trace (at most one per request)."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM agent_events WHERE event_type = 'routing_continuation'").fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _routing_continuation_started_at(conn: sqlite3.Connection) -> str | None:
    """Earliest routing_continuation timestamp; None when no continuation was queued."""
    try:
        row = conn.execute("SELECT MIN(started_at) FROM agent_events WHERE event_type = 'routing_continuation'").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _clean_tool_started_at(conn: sqlite3.Connection, name: str) -> str | None:
    """Earliest clean tool_calls timestamp for `name`; None when never cleanly called."""
    try:
        row = conn.execute("SELECT MIN(started_at) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _research_call_count(conn: sqlite3.Connection) -> int:
    """Every research tool_calls row (clean or errored) outside the discovery set."""
    try:
        names = get_registry_sets()["schemas"]
    except Exception:
        return 0
    research_names = [n for n in names if n not in DISCOVERY_TOOLS]
    if not research_names:
        return 0
    ph = ",".join("?" for _ in research_names)
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph})", tuple(research_names)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _final_answer_present(conn: sqlite3.Connection) -> bool:
    """True iff agent_runs carries a nonempty final answer (hash when text is not persisted)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    except sqlite3.Error:
        return False
    column = "final_answer" if "final_answer" in cols else ("final_answer_hash" if "final_answer_hash" in cols else None)
    if column is None:
        return False
    try:
        rows = conn.execute(f"SELECT {column} FROM agent_runs").fetchall()
    except sqlite3.Error:
        return False
    return any(isinstance(r[0], str) and r[0].strip() for r in rows)


def _routing_metrics_event(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Latest routing_metrics metadata payload; None when the request emitted none."""
    try:
        row = conn.execute("SELECT metadata FROM agent_events WHERE event_type = 'routing_metrics' ORDER BY sequence DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def evaluate_completion_attempt(db_path: Path, expected_tool: str | None, exit_code: int = 0, timed_out: bool = False, *, completed_override: bool = False) -> tuple[bool, str]:
    """Agent-loop completion: discovery -> research -> nonempty answer, with at most one continuation.

    Supported (expected_tool set) passes only on clean discovery before a clean
    expected research tool plus a nonempty final answer, with either no
    continuation or exactly one continuation that recovered to research.
    Unsupported (expected_tool None) passes only on a clean zero-match
    search_tools trace with zero research calls, zero continuations, and a
    nonempty limitation answer. Two continuations always fail.
    """
    if not db_path.is_file():
        if timed_out and not completed_override:
            return False, "transient: pi timeout before terminal state"
        return False, f"missing recorder DB: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            continuations = _routing_continuation_count(conn)
            if continuations >= 2:
                return False, "routing failed: multiple routing continuations (expected at most one)"
            if expected_tool is None:
                if continuations != 0:
                    return False, "routing failed: unexpected routing continuation for unsupported request"
                if _routing_tool_success(conn, "search_tools", attempt=1) is not None:
                    return False, "routing failed: no clean zero-match search_tools trace"
                try:
                    zero_rows: list[tuple[object]] = conn.execute("SELECT result_row_count FROM tool_calls WHERE tool_name = 'search_tools' AND error_type IS NULL").fetchall()
                except sqlite3.Error:
                    zero_rows = []
                if not zero_rows or any(not isinstance(r[0], int) or r[0] != 0 for r in zero_rows):
                    return False, "routing failed: no clean zero-match search_tools trace"
                if _research_call_count(conn) != 0:
                    return False, "routing failed: unexpected research tool call(s) for unsupported request"
                if timed_out and not completed_override:
                    return False, "transient: pi timeout before terminal state"
                if exit_code != 0 and not completed_override:
                    return False, f"pi exit {exit_code}"
                if not _final_answer_present(conn):
                    return False, "routing failed: empty final answer for unsupported request"
                return True, "pass"
            discovery_ok = any(_routing_tool_success(conn, t, attempt=1) is None for t in ("browse_tools", "search_tools", "describe_tool", "list_tool_domains"))
            if not discovery_ok:
                return False, "routing failed: no clean discovery before research"
            target_problem = _routing_tool_success(conn, expected_tool, attempt=1)
            if target_problem is not None:
                return False, f"target '{expected_tool}' absent ({target_problem})"
            if not _discovery_before_dispatch(conn, expected_tool):
                return False, f"routing failed: discovery did not precede research dispatch of '{expected_tool}'"
            if timed_out and not completed_override:
                return False, "transient: pi timeout before terminal state"
            if exit_code != 0 and not completed_override:
                return False, f"pi exit {exit_code}"
            if not _final_answer_present(conn):
                return False, "routing failed: empty final answer after research"
            if continuations == 1:
                cont_at = _routing_continuation_started_at(conn)
                inner_at = _clean_tool_started_at(conn, expected_tool)
                if not (cont_at and inner_at and str(cont_at) < str(inner_at)):
                    return False, "routing failed: routing continuation did not recover to research"
            return True, "pass"
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"DB read failed: {exc}"


class _AgentLoopCase(TypedDict):
    prompt: str
    expected_tool: str | None


AGENT_LOOP_CASES: list[_AgentLoopCase] = [
    {"prompt": "Why did GoPro stock shoot up over the last 30 days?", "expected_tool": "search_web"},
    {"prompt": "What is NVDA EPS?", "expected_tool": "get_fundamentals"},
    {"prompt": "What's GME short interest?", "expected_tool": "get_short_interest"},
    {"prompt": "What changed at AMD recently?", "expected_tool": "get_material_events"},
    {"prompt": "How do I bake sourdough bread at home?", "expected_tool": None},
]


def run_agent_loop_case(case: _AgentLoopCase, batch_root: Path, cwd: Path, index: int) -> AttemptResult:
    """Run one agent-loop case through run_pi with an isolated DB/store, then completion-evaluate."""
    start = time.monotonic()
    label = case["expected_tool"] or "unsupported"
    db_path, _store_dir = attempt_dirs(batch_root, f"loop-{label}", index)
    _store_dir.mkdir(parents=True, exist_ok=True)
    code, timed_out, _out, _err, saw_complete = run_pi(case["prompt"], db_path, cwd, _store_dir)
    comp_ok, comp_reason = evaluate_completion_attempt(db_path, case["expected_tool"], code, timed_out, completed_override=saw_complete)
    disc, resc = _metrics_for_db(db_path, label)
    duration_seconds = time.monotonic() - start
    return AttemptResult(label, index, comp_ok, comp_reason if not comp_ok else "pass", code, str(db_path), duration_seconds, False, None, "", None, "", disc, resc, _direct_count_for_db(db_path, label), comp_ok, comp_reason, _routing_metrics_for_db(db_path), terminal_completed=bool(saw_complete))


def run_agent_loop_main() -> int:
    """Live agent-loop gate: each AGENT_LOOP_CASES prompt must complete discovery -> research -> answer."""
    batch = _batch_id()
    root = Path("data/verify") / batch / "agent-loop"
    cwd = Path.cwd()
    loop_start = time.monotonic()
    results = run_agent_loop(root, cwd)
    failed = 0
    for r, case in zip(results, AGENT_LOOP_CASES):
        mark = "PASS" if r.ok else "FAIL"
        print(f"{mark} {case['prompt']!r} -> {case['expected_tool'] or 'unsupported'} ({r.reason}) [{r.duration_seconds:.1f}s]")
        if not r.ok:
            failed += 1
    wall = time.monotonic() - loop_start
    aggregates = aggregate_results(results)
    print(f"Agent-loop completion: {len(results) - failed}/{len(results)} cases complete [{wall:.1f}s]")
    summary = {"cases": [{"prompt": case["prompt"], "expected_tool": case["expected_tool"], "ok": r.ok, "reason": r.reason, "completion_ok": r.completion_ok, "completion_reason": r.completion_reason, "db": r.db, "duration_seconds": r.duration_seconds, "discovery_calls": r.discovery_calls, "research_calls": r.research_calls, "direct_tool_calls": r.direct_tool_calls, "routing_metrics": r.routing_metrics} for r, case in zip(results, AGENT_LOOP_CASES)], "aggregates": aggregates, "wall_seconds": wall}
    _fc = aggregates.get("failure_category_counts")
    summary["failure_category_counts"] = _fc if isinstance(_fc, dict) else {}
    summary["aggregates"] = aggregates
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if failed == 0 else 1


def run_agent_loop(batch_root: Path, cwd: Path) -> list[AttemptResult]:
    """Run every AGENT_LOOP_CASES prompt once; each case is an independent request."""
    return [run_agent_loop_case(case, batch_root, cwd, i + 1) for i, case in enumerate(AGENT_LOOP_CASES)]


def run_confusion() -> int:
    """Generated confusion benchmark: two directed cases per undirected conflict edge."""
    try:
        cases = generate_confusion_cases()
    except ValueError as exc:
        print(f"confusion preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"confusion cases: {len(cases)} directed ({len(cases)//2} undirected edges)")
    batch = _batch_id()
    root = Path("data/verify") / batch / "confusion"
    cwd = Path.cwd()
    durable = get_data_root()
    # group by undirected pair for per-pair accuracy
    from collections import defaultdict
    by_pair: dict[tuple[str, str], list[ConfusionCase]] = defaultdict(list)
    for c in cases:
        _raw_pair = c.get("pair")
        assert isinstance(_raw_pair, list)
        pair = tuple(sorted(str(x) for x in _raw_pair))
        by_pair[(pair[0], pair[1])].append(c)
    total = 0
    correct = 0
    cat_totals: dict[str, int] = {c.value: 0 for c in RoutingFailureCategory}
    pair_lines: list[str] = []
    summary_cases: list[dict[str, object]] = []
    for pair in sorted(by_pair):
        pair_total = 0
        pair_ok = 0
        for c in by_pair[pair]:
            expected = str(c["expected_tool"])
            base_args = dict(c["arguments"])
            prompt = str(c["prompt"])
            result = run_verification_attempt(expected, 1, base_args, root / f"{pair[0]}-vs-{pair[1]}", cwd, durable, 1, prompt_override=prompt)
            db_path = Path(result.db) if result.db else root / expected / "attempt-1" / "runs.sqlite"
            # Selection accuracy: discovery/selection/dispatch/arguments correct even on EXECUTION_FAILURE.
            try:
                cat = None if result.ok else classify_routing_failure(db_path, expected, attempt=1, expected_args=base_args)
            except Exception:
                cat = None
            sel_ok = result.ok or cat == RoutingFailureCategory.EXECUTION_FAILURE
            pair_total += 1
            total += 1
            if sel_ok:
                pair_ok += 1
                correct += 1
            if cat is not None:
                cat_totals[cat.value] += 1
            # evidence for audit
            surfaced: set[str] | None = None
            selected: list[str] = []
            try:
                conn = sqlite3.connect(str(db_path))
                try:
                    surfaced = _surfaced_names(conn)
                    selected = sorted(_call_tool_dispatched_names(conn))
                finally:
                    conn.close()
            except Exception:
                surfaced: set[str] | None = None
                selected: list[str] = []
            summary_cases.append({
                "pair": list(pair), "expected_tool": expected, "prompt": prompt,
                "selection_ok": sel_ok, "failure_category": cat.value if cat else None,
                "surfaced": sorted(surfaced) if surfaced is not None else None,
                "dispatched": selected, "db": str(db_path),
            })
            print(f"{'PASS' if sel_ok else 'FAIL'}[confusion] {prompt!r} -> {expected} (cat={cat.value if cat else 'none'})")
        pair_lines.append(f"{pair[0]} vs {pair[1]}: {pair_ok}/{pair_total}")
    print("metadata/conflict consistency pair selection accuracy:")
    for line in pair_lines:
        print(f"  {line}")
    print(f"metadata/conflict consistency selection: {correct}/{total}")
    print(f"metadata/conflict consistency categories: " + ", ".join(f"{k}={v}" for k, v in sorted(cat_totals.items())))
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps({"cases": summary_cases, "pair_accuracy": pair_lines, "selection": {"correct": correct, "total": total}, "categories": cat_totals}, indent=2))
    return 0 if correct == total else 1


def run_holdout(holdout_path: str) -> int:
    """Frozen 20-prompt holdout, dual-reported: reachability + routing precision."""
    try:
        blob = Path(holdout_path).read_bytes()
    except OSError as exc:
        print(f"holdout read failed: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(blob).hexdigest()
    print(f"holdout sha256: {digest}")
    if digest != HOLDOUT_SHA256:
        print(f"holdout hash mismatch: expected {HOLDOUT_SHA256}, got {digest}", file=sys.stderr)
        return 1
    try:
        raw = json.loads(blob.decode())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"holdout read failed: {exc}", file=sys.stderr)
        return 1
    if not isinstance(raw, list) or not raw:
        print(f"holdout {holdout_path} must be a non-empty list", file=sys.stderr)
        return 1
    schemas = tool_schemas()
    batch = _batch_id()
    root = Path("data/verify") / batch / "holdout"
    cwd = Path.cwd()
    durable = get_data_root()
    failed_reach = failed_routing = 0
    for i, case in enumerate(raw):
        if not isinstance(case, dict) or not isinstance(case.get("prompt"), str) or not isinstance(case.get("expected_tool"), str):
            print(f"FAIL case {i}: needs string prompt + expected_tool", file=sys.stderr)
            failed_reach += 1
            failed_routing += 1
            continue
        tool = str(case["expected_tool"])
        try:
            base_args = dict(case["arguments"]) if isinstance(case.get("arguments"), dict) else resolve_arguments(tool, schemas)
        except LookupError as exc:
            print(f"FAIL {case['prompt']!r}: {exc}", file=sys.stderr)
            failed_reach += 1
            failed_routing += 1
            continue
        result = run_verification_attempt(tool, 1, base_args, root, cwd, durable, 1, prompt_override=str(case["prompt"]))
        db_path = Path(result.db) if result.db else root / tool / "attempt-1" / "runs.sqlite"
        if result.reach_ok is None or result.routing_ok is None:
            holdout_reach_ok, holdout_reach_reason = evaluate_holdout_reachability_attempt(db_path, tool)
            holdout_routing_ok, holdout_routing_reason = evaluate_holdout_attempt(db_path, tool)
        else:
            holdout_reach_ok, holdout_reach_reason = bool(result.reach_ok), result.reach_reason
            holdout_routing_ok, holdout_routing_reason = bool(result.routing_ok), result.routing_reason
        print(f"{'PASS' if holdout_reach_ok else 'FAIL'}[reach] {'PASS' if holdout_routing_ok else 'FAIL'}[route] {case['prompt']!r} -> {tool} (reach: {holdout_reach_reason}; route: {holdout_routing_reason}) [{result.duration_seconds:.1f}s]")
        if not holdout_reach_ok:
            failed_reach += 1
        if not holdout_routing_ok:
            failed_routing += 1
    print(f"holdout reachability: {len(raw) - failed_reach}/{len(raw)} prompts discover-then-dispatch cleanly")
    print(f"holdout routing precision: {len(raw) - failed_routing}/{len(raw)} prompts dispatch intentionally")
    return 0 if (failed_reach == 0 and failed_routing == 0) else 1

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default=None, help="verify one tool only (debug mode)")
    parser.add_argument("--holdout", nargs="?", const="evals/holdout_discovery.json", default=None, help="run the discovery holdout only (no live matrix): each prompt must browse-or-search then call_tool its expected tool")
    parser.add_argument("--agent-loop", action="store_true", help="run the natural agent-loop cases only (no live matrix): discovery -> research -> answer per AGENT_LOOP_CASES")
    parser.add_argument("--confusion", action="store_true", help="run generated confusion benchmark only (no live matrix)")
    args = parser.parse_args()
    if args.confusion:
        return run_confusion()
    if args.holdout is not None:
        return run_holdout(args.holdout)
    if args.agent_loop:
        return run_agent_loop_main()
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
        if args.tool in _MATRIX_EXCLUDED:
            print(f"tool '{args.tool}' is not a live-matrix target", file=sys.stderr)
            return 1
        tool_names = [args.tool]
    else:
        tool_names = [t for t in describe_names if t not in _MATRIX_EXCLUDED]

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
    batch = _batch_id()
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
    passed_routing = 0
    passed_reach = 0
    for r in ordered:
        total += 1
        r_reach = r.reach_ok if r.reach_ok is not None else r.ok
        r_route = r.routing_ok if r.routing_ok is not None else r.ok
        if r_route:
            passed_routing += 1
        if r_reach:
            passed_reach += 1
        rec: dict[str, object] = {"attempt": r.attempt, "ok": r.ok, "reason": r.reason, "reach_ok": r_reach, "reach_reason": r.reach_reason, "routing_ok": r_route, "routing_reason": r.routing_reason, "exit": r.exit, "db": r.db, "duration_seconds": r.duration_seconds, "discovery_calls": r.discovery_calls, "research_calls": r.research_calls, "direct_tool_calls": r.direct_tool_calls, "completion_ok": r.completion_ok, "completion_reason": r.completion_reason, "routing_metrics": r.routing_metrics, "failure_category": r.failure_category.value if r.failure_category else None}
        if not r.ok:
            rec["searchQueries"] = _search_queries_for_db(r.db)
        results[r.tool].append(rec)
        comp_mark = "n/a" if r.completion_ok is None else ("PASS" if r.completion_ok else "FAIL")
        print(f"{r.tool} attempt {r.attempt}/{repetitions}: routing {'PASS' if r_route else 'FAIL'} ({r.routing_reason or r.reason}) | reachability {'PASS' if r_reach else 'FAIL'} ({r.reach_reason or r.reason}) | completion {comp_mark} ({r.completion_reason}) [direct={r.direct_tool_calls} discovery={r.discovery_calls} research={r.research_calls}] [{r.duration_seconds:.1f}s]")
        if r.model_config_failed:
            print("PI MODEL CONFIGURATION FAILED", file=sys.stderr)
    procs = len(ordered)
    failed_routing_tools: list[str] = []
    failed_reach_tools: list[str] = []
    for tool in tool_names:
        tool_recs = results[tool]
        reach_pass = evaluate_reachability_tool([{"reach_ok": rec.get("reach_ok", rec.get("ok"))} for rec in tool_recs])
        route_pass = evaluate_routing_tool([{"routing_ok": rec.get("routing_ok", rec.get("ok"))} for rec in tool_recs])
        if reach_pass and route_pass:
            remove_successful_attempt_dirs(root, tool, tool_recs)
        else:
            if not reach_pass:
                failed_reach_tools.append(tool)
            if not route_pass:
                failed_routing_tools.append(tool)
            print(f"preserved DBs for {tool}: {root / tool}")
            by_tool = [r for r in ordered if r.tool == tool and not r.ok]
            if by_tool and not any(is_routing_failure(r.reason) for r in by_tool) and all(r.model_config_failed or is_infra_failure(r.reason) for r in by_tool):
                print(f"infra-only failure for {tool} (still FAIL)")
    passed_reach_tools = len(tool_names) - len(failed_reach_tools)
    passed_routing_tools = len(tool_names) - len(failed_routing_tools)
    wall = time.monotonic() - verify_start
    print(f"git: {sha} | tools {len(tool_names)} x {repetitions} = {total}")
    print(f"Reachability: {passed_reach_tools}/{len(tool_names)} tools 3/3")
    print(f"Routing precision: {passed_routing_tools}/{len(tool_names)} tools 3/3")
    print(f"Coverage: {passed_routing_tools}/{len(tool_names)} tools | processes: {procs} | routing passed: {passed_routing}/{total} | reachability passed: {passed_reach}/{total}")
    aggregates = aggregate_results(ordered)
    conv = _agg_section(aggregates, "discovery_to_research_conversion")
    prem = _agg_section(aggregates, "premature_stop_rate")
    recov = _agg_section(aggregates, "continuation_recovery")
    comp = _agg_section(aggregates, "completion")
    print(f"Discovery-to-research conversion: {conv.get('with_research', 0)}/{conv.get('with_discovery', 0)} ({_agg_float(conv, 'rate'):.0%}) | premature stops: {prem.get('premature', 0)}/{prem.get('total', 0)} ({_agg_float(prem, 'rate'):.0%}) | continuation recovery: {recov.get('recovered', 0)}/{recov.get('injected', 0)} ({_agg_float(recov, 'rate'):.0%})")
    print(f"Median discovery calls: {_agg_float(aggregates, 'median_discovery_calls'):.1f} | median research calls: {_agg_float(aggregates, 'median_research_calls'):.1f} | unrelated research calls: {aggregates.get('unrelated_research_calls', 0)} | completion: {comp.get('passed', 0)}/{comp.get('evaluated', 0)}")
    _raw_cats = aggregates.get("failure_category_counts")
    cats: dict[str, int] = dict(_raw_cats) if isinstance(_raw_cats, dict) else {}
    if cats:
        print("Failure categories: " + ", ".join(f"{k}={cats.get(k, 0)}" for k in sorted(cats)))
    else:
        print("Failure categories: none")
    print(f"Concurrency: {concurrency} | Wall time: {wall:.1f}s")
    loop_results: list[AttemptResult] = [] if debug else run_agent_loop(root / "agent-loop", cwd)
    loop_failed = 0
    for r, case in zip(loop_results, AGENT_LOOP_CASES):
        mark = "PASS" if r.ok else "FAIL"
        print(f"{mark}[agent-loop] {case['prompt']!r} -> {case['expected_tool'] or 'unsupported'} ({r.reason}) [{r.duration_seconds:.1f}s]")
        if not r.ok:
            loop_failed += 1
    if debug:
        print("Agent-loop completion: skipped (debug mode)")
    else:
        print(f"Agent-loop completion: {len(loop_results) - loop_failed}/{len(loop_results)} cases complete")
    wall = time.monotonic() - verify_start
    failed_tools = sorted(set(failed_reach_tools) | set(failed_routing_tools))
    print(f"RESULT: {'PASS' if not failed_tools and not loop_failed else 'FAIL'}")
    if failed_tools:
        print(f"failed tools: {failed_tools}")
    summary = {"git_sha": sha, "tool_count": len(tool_names), "repetitions": repetitions, "results": results, "reachability": {"passed_tools": passed_reach_tools, "tool_count": len(tool_names), "failed_tools": failed_reach_tools}, "routing_precision": {"passed_tools": passed_routing_tools, "tool_count": len(tool_names), "failed_tools": failed_routing_tools}, "agent_loop": {"passed": len(loop_results) - loop_failed, "total": len(loop_results), "failed": loop_failed, "cases": [{"prompt": case["prompt"], "expected_tool": case["expected_tool"], "ok": r.ok, "reason": r.reason, "db": r.db, "completion_ok": r.completion_ok, "completion_reason": r.completion_reason, "discovery_calls": r.discovery_calls, "research_calls": r.research_calls, "direct_tool_calls": r.direct_tool_calls, "routing_metrics": r.routing_metrics} for r, case in zip(loop_results, AGENT_LOOP_CASES)]}, "aggregates": aggregates, "transient_retries": list(TRANSIENT_RETRIES), "concurrency": concurrency, "wall_seconds": wall}
    _fc = aggregates.get("failure_category_counts")
    summary["failure_category_counts"] = _fc if isinstance(_fc, dict) else {}
    summary["aggregates"] = aggregates
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if not failed_tools and not loop_failed else 1


if __name__ == "__main__":
    sys.exit(main())
