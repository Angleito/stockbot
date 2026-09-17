#!/usr/bin/env python3
"""Live Pi tool verification: every describe-visible tool invoked 3/3 by Pi's configured default model.

Fail-closed at every step. Verdict comes only from per-attempt recorder DBs.
Pi configuration is authoritative for which model runs; Stockbot asserts only that non-empty model telemetry exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_data_root
from app.policy import Capability, RequestContext
from app.tools import (
    TOOL_DISCOVERY_REGISTRY,
    TOOLS,
    build_prerequisite_graph_from_tool_metadata,
    execute_tool,
)
from scripts.verify_tool_registry import (
    get_registry_sets,
    registry_errors,
    tool_schema_function,
    tool_schema_name,
)

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
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal", "thesis_status"})
RESEARCH_SESSION_PLACEHOLDER = "research-session-placeholder"
RESEARCH_SESSION_TOOLS = frozenset({"research_resume", "research_status", "research_cancel", "research_read"})
FINRA_SEED_TOOLS = frozenset({"get_short_interest_leaderboard"})
# Prerequisite edges derive from TOOL_DISCOVERY_REGISTRY (single source with
# app/tools.py); per-edge description citations enforced by
# tests/test_verify_pi_tools.py::test_prereq_chains_are_documented_in_descriptions.
PREREQ_CHAINS: dict[str, frozenset[str]] = build_prerequisite_graph_from_tool_metadata()
# Discovery primitives: first-class citizens on attempts 1-2, never strays.
# None are ever live-matrix targets.
DISCOVERY_TOOLS = frozenset({"browse_tools", "call_tool", "list_tool_domains", "search_tools", "describe_tool"})
_MATRIX_EXCLUDED = DISCOVERY_TOOLS
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


def _surfaced_set_from_list(val: object) -> set[str]:
    """Names in a discovered-tools list payload; ignores non-string entries."""
    out: set[str] = set()
    if not isinstance(val, list):
        return out
    for item in val:
        if isinstance(item, str) and item:
            out.add(item)
    return out


def _surfaced_names_from_payload(payload: dict[str, object]) -> set[str] | None:
    """Name set from a routing_metrics payload; None when telemetry absent."""
    for key in ("discovered_tools", "discoveredTools"):
        val = payload.get(key)
        if isinstance(val, list):
            return _surfaced_set_from_list(val)
    if payload.get("discovered_tool_count") is not None:
        return set()
    return None


def _surfaced_names(conn: sqlite3.Connection) -> set[str] | None:
    """Discovered tool names from routing_metrics; None when telemetry absent (assume surfaced)."""
    try:
        payload = _routing_metrics_event(conn)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if not isinstance(payload, dict):
        return None
    return _surfaced_names_from_payload(payload)


def _wanted_args_key(expected_args: Mapping[str, object]) -> str | None:
    """Canonical key for the expected args; None when un-normalizable."""
    try:
        return _normalize_args(dict(expected_args))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _clean_arg_keys(conn: sqlite3.Connection, expected_tool: str) -> set[str] | None:
    """Canonical keys of clean executions; None when unreadable."""
    try:
        rows = conn.execute(
            "SELECT arguments_json FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (expected_tool,)
        ).fetchall()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    try:
        return {_normalize_args(r[0]) for r in rows}
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _expected_args_mismatch(
    conn: sqlite3.Connection, expected_tool: str, expected_args: Mapping[str, object] | None
) -> bool:
    if expected_args is None:
        return False
    want = _wanted_args_key(expected_args)
    if want is None:
        return False
    have = _clean_arg_keys(conn, expected_tool)
    if have is None:
        return False
    return bool(have) and want not in have


def _expected_has_invalid(conn: sqlite3.Connection, expected_tool: str) -> bool:
    try:
        rows = conn.execute("SELECT error_type FROM tool_calls WHERE tool_name = ?", (expected_tool,)).fetchall()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return False
    return any(isinstance(r[0], str) and r[0] == "invalid_tool_arguments" for r in rows)


def _classify_infra_transient(conn: sqlite3.Connection, expected_tool: str) -> bool:
    """True iff the failure is infra-transient (never an ontology failure)."""
    try:
        if _row_errors_transient(conn, [expected_tool]):
            return True
        unexpected_all = _unexpected_tools(conn, expected_tool)
        if unexpected_all and _row_errors_transient(conn, unexpected_all):
            pass
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    return False


def _classify_discovery(conn: sqlite3.Connection, expected_tool: str, attempt: int) -> RoutingFailureCategory | None:
    """DISCOVERY_FAILURE when the target never surfaced on a natural attempt."""
    if attempt >= 3:
        return None
    surfaced = _surfaced_names(conn)
    if surfaced is not None and expected_tool not in surfaced:
        return RoutingFailureCategory.DISCOVERY_FAILURE
    return None


def _classify_selection(conn: sqlite3.Connection, expected_tool: str, attempt: int) -> RoutingFailureCategory | None:
    """SELECTION_FAILURE on a clean stray research call outside prerequisites."""
    unexpected = _unexpected_tools(conn, expected_tool)
    allowed = set(PREREQ_CHAINS.get(expected_tool, frozenset()))
    clean_strays = [
        x for x in unexpected if x not in allowed and _routing_tool_success(conn, x, attempt=attempt) is None
    ]
    return RoutingFailureCategory.SELECTION_FAILURE if clean_strays else None


def _classify_arguments_shape(conn: sqlite3.Connection, expected_tool: str) -> RoutingFailureCategory | None:
    """ARGUMENT_FAILURE on unparseable dispatch or harness-rejected target args."""
    if _call_tool_has_unparseable(conn):
        return RoutingFailureCategory.ARGUMENT_FAILURE
    if expected_tool in _rejected_tools(conn):
        return RoutingFailureCategory.ARGUMENT_FAILURE
    return None


def _classify_dispatch(conn: sqlite3.Connection, expected_tool: str) -> RoutingFailureCategory | None:
    """DISPATCH_FAILURE when the target was never intentionally dispatched."""
    if not _target_dispatched(conn, expected_tool):
        return RoutingFailureCategory.DISPATCH_FAILURE
    return None


def _classify_expected_args(
    conn: sqlite3.Connection, expected_tool: str, expected_args: Mapping[str, object] | None
) -> RoutingFailureCategory | None:
    """ARGUMENT_FAILURE on invalid-arg executions or args mismatch."""
    if _expected_has_invalid(conn, expected_tool):
        return RoutingFailureCategory.ARGUMENT_FAILURE
    if expected_args is not None and _expected_args_mismatch(conn, expected_tool, expected_args):
        return RoutingFailureCategory.ARGUMENT_FAILURE
    return None


def _classify_execution(conn: sqlite3.Connection, expected_tool: str, attempt: int) -> RoutingFailureCategory | None:
    """EXECUTION_FAILURE on any errored expected call that survived arg checks."""
    problem = _routing_tool_success(conn, expected_tool, attempt=attempt)
    if problem is not None and "failed execution present" in problem:
        return RoutingFailureCategory.EXECUTION_FAILURE
    try:
        n_err = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NOT NULL", (expected_tool,)
        ).fetchone()[0]
        if n_err:
            return RoutingFailureCategory.EXECUTION_FAILURE
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    return None


def _classify_stages(
    conn: sqlite3.Connection, expected_tool: str, attempt: int, expected_args: Mapping[str, object] | None
) -> RoutingFailureCategory | None:
    """Earliest pipeline stage that failed, in ontology order."""
    for check in (
        lambda: _classify_discovery(conn, expected_tool, attempt),
        lambda: _classify_selection(conn, expected_tool, attempt),
        lambda: _classify_arguments_shape(conn, expected_tool),
        lambda: _classify_dispatch(conn, expected_tool),
        lambda: _classify_expected_args(conn, expected_tool, expected_args),
        lambda: _classify_execution(conn, expected_tool, attempt),
    ):
        try:
            hit = check()
        except Exception:  # noqa: BLE001, S112 - intentional best-effort boundary, never aborts
            continue
        if hit is not None:
            return hit
    return None


def classify_routing_failure(
    db_path: Path, expected_tool: str, *, attempt: int, expected_args: Mapping[str, object] | None = None
) -> RoutingFailureCategory | None:
    """Earliest pipeline stage that failed; None on pass or infra-transient."""
    try:
        conn = __import__("sqlite3").connect(str(db_path))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    try:
        # Infra-transient stays orthogonal: never relabel as ontology failure.
        if _classify_infra_transient(conn, expected_tool):
            return None
        return _classify_stages(conn, expected_tool, attempt, expected_args)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


class ConfusionCase(TypedDict):
    expected_tool: str
    prompt: str
    arguments: dict[str, object]
    pair: list[str]


def _confusion_edges() -> list[list[str]]:
    """Sorted undirected conflict edges from the tool discovery registry."""
    seen: set[frozenset[str]] = set()
    for name, meta in TOOL_DISCOVERY_REGISTRY.items():
        for peer in meta.conflicts_with:
            seen.add(frozenset({name, peer}))
    return sorted(sorted(p) for p in seen)


def _confusion_case(pair: list[str], expected: str) -> ConfusionCase:
    """One directed confusion case; raises when registry semantics are missing."""
    meta = TOOL_DISCOVERY_REGISTRY[expected]
    if not meta.choose_when:
        raise ValueError(f"missing choose_when for confusion case {expected!r}")
    fixture = VERIFY_CASES.get(expected)
    if fixture is None or not isinstance(fixture.get("arguments"), dict):
        raise ValueError(f"missing verification fixture for confusion edge {expected!r}")
    return {
        "expected_tool": expected,
        "prompt": meta.choose_when[0],
        "arguments": dict(fixture["arguments"]),
        "pair": list(pair),
    }


def generate_confusion_cases() -> list[ConfusionCase]:
    """Two directed cases per undirected conflicts_with edge, from registry semantics."""
    cases: list[ConfusionCase] = []
    for pair in _confusion_edges():
        for expected in (pair[0], pair[1]):
            cases.append(_confusion_case(pair, expected))
    return cases


def _normalize_jsonable(raw: object) -> str:
    """Sorted-keys JSON encoding with str() fallback for exotic values."""
    try:
        return json.dumps(raw, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return str(raw)


def _normalize_str_arg(raw: str) -> str:
    """Canonical key for a raw string arg: JSON-parse then re-encode, else strip."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return raw.strip()
    return _normalize_jsonable(parsed)


def _normalize_args(raw: object) -> str:
    """Canonical args key: sorted-keys JSON so whitespace/key order never fails."""
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, dict):
        return _normalize_jsonable(raw)
    if isinstance(raw, str):
        return _normalize_str_arg(raw)
    return _normalize_jsonable(raw)


def _call_tool_start_count(conn: sqlite3.Connection) -> int:
    """Outer call_tool dispatches in this trace (agent_events lifecycle)."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_started' AND tool_name = 'call_tool'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _count_where(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    """Scalar COUNT(*) query; 0 when the table is unreadable."""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] else 0


def _research_tool_names() -> list[str]:
    """Registry schema names outside the discovery set; [] when unreadable."""
    try:
        names = get_registry_sets()["schemas"]
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []
    return [n for n in names if n not in DISCOVERY_TOOLS]


def _research_counts(conn: sqlite3.Connection, research_names: list[str]) -> tuple[int, int, int]:
    """(research_calls, failed_research, direct_tool_calls) for the matrix tools."""
    if not research_names:
        return 0, 0, 0
    ph = ",".join("?" for _ in research_names)
    research_calls = _count_where(
        conn, f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph})", tuple(research_names)
    )
    failed_research = _count_where(
        conn,
        f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph}) AND error_type IS NOT NULL",
        tuple(research_names),
    )
    # Direct executions complete under their own name; call_tool fallback
    # completes under 'call_tool'. Counts both routing styles.
    direct_tool_calls = _count_where(
        conn,
        f"SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name IN ({ph})",
        tuple(research_names),
    )
    return research_calls, failed_research, direct_tool_calls


def _trace_metrics(conn: sqlite3.Connection, expected_tool: str) -> dict[str, object]:
    """Efficiency accounting, always logged even on pass."""
    disc = _count_where(
        conn,
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains')",
    )
    failed_disc = _count_where(
        conn,
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains') AND error_type IS NOT NULL",
    )
    research_calls, failed_research, direct_tool_calls = _research_counts(conn, _research_tool_names())
    rejected_disc = _count_where(
        conn,
        "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_failed' AND tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains')",
    )
    return {
        "discovery_calls": disc or 0,
        "failed_discovery_calls": failed_disc or 0,
        "rejected_discovery_calls": rejected_disc or 0,
        "research_calls": research_calls or 0,
        "failed_research_calls": failed_research or 0,
        "target_dispatched": expected_tool in _call_tool_dispatched_names(conn)
        or _direct_tool_completed(conn, expected_tool),
        "call_tool_count": _call_tool_start_count(conn),
        "direct_tool_calls": direct_tool_calls or 0,
    }


def get_concurrency() -> int:
    """Live Pi parallelism; PI_VERIFY_CONCURRENCY override, fail-closed on bad values."""
    raw = os.getenv("PI_VERIFY_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        value = int(raw or "")
    except ValueError, TypeError:
        raise ValueError("PI_VERIFY_CONCURRENCY must be an integer >= 1")
    if value < 1:
        raise ValueError("PI_VERIFY_CONCURRENCY must be an integer >= 1")
    return value


def _batch_id() -> str:
    """Process-unique batch ID: UTC microseconds plus PID so parallel probes never share a directory."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + f"-p{os.getpid()}"


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
    out = execute_tool(
        "thesis_create", {"user_thesis": "Verify wiring: NVDA AI demand stays strong."}, "verify", context=ctx
    )
    if not isinstance(out, dict) or not out.get("thesis_id"):
        raise RuntimeError(f"thesis fixture setup failed: {str(out)[:300]}")
    return str(out["thesis_id"])


def ensure_research_fixture(store: Path) -> str:
    """Create one verification research session in the batch store; returns its ID."""
    ctx = RequestContext(principal_id="verify", capabilities=frozenset({Capability.RESEARCH}), data_root=store)
    out = execute_tool(
        "research_start", {"question": "Verify wiring: NVDA AI demand stays strong."}, "verify", context=ctx
    )
    if not isinstance(out, dict) or not out.get("session_id"):
        raise RuntimeError(f"research fixture setup failed: {str(out)[:300]}")
    return str(out["session_id"])


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


def _ticker_ciks_from_sec(sec: object) -> dict[str, int]:
    """Ticker->CIK map from a refresh_sec_tickers payload; {} when malformed."""
    raw_map = sec.get("ticker_ciks") if isinstance(sec, dict) else None
    ticker_ciks: dict[str, int] = {}
    if not isinstance(raw_map, dict):
        return ticker_ciks
    for k, v in raw_map.items():
        if isinstance(k, str) and isinstance(v, int):
            ticker_ciks[k] = v
    return ticker_ciks


def _resolve_top_symbols(rows: object, ticker_ciks: dict[str, int]) -> tuple[list[str], list[tuple[str, int]]]:
    """(top symbols, resolved (symbol, cik)) from a top-symbols query result."""
    rows_list: list[object] = rows if isinstance(rows, list) else []
    symbols = [str(r["symbol_code"]).strip().upper() for r in rows_list if isinstance(r, dict) and r.get("symbol_code")]
    return symbols, [(s, ticker_ciks[s]) for s in symbols if s in ticker_ciks]


def _enrich_resolved(research_data: object, resolved: list[tuple[str, int]], durable: Path) -> list[dict[str, object]]:
    """Refresh company facts for resolved symbols; returns per-symbol failures."""
    failed: list[dict[str, object]] = []
    for sym, cik in resolved:
        try:
            getattr(research_data, "refresh_sec_company_facts")(cik, data_root=durable)  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            failed.append({"ticker": sym, "cik": cik, "error": f"{type(exc).__name__}: {exc}"})
    return failed


def _confirm_leaderboard_for(screens: object, durable: Path, settlement_date: str, resolved: int, failed: int) -> None:
    """Confirm the leaderboard serves entries for this durable store."""
    confirm = getattr(screens, "get_short_interest_leaderboard")(limit=5, data_root=durable)  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    entries = confirm.get("entries") if isinstance(confirm, dict) else None
    if not isinstance(confirm, dict) or "error" in confirm or not isinstance(entries, list) or not entries:
        err = confirm.get("error") if isinstance(confirm, dict) else None
        raise RuntimeError(
            f"verify fetch confirmation failed for settlement {settlement_date} "
            f"(resolved={resolved} failed={failed}): {err or 'zero entries'}"
        )
    print(f"verify fetch: confirmed {len(entries)} entries for {settlement_date}")


def fetch_finra_fixture(durable: Path, settlement_date: str) -> None:
    """Fetch missing durable datasets with the existing refresh functions; writes to durable only."""
    from app.analytics import screens
    from app.services import research_data
    from app.storage import duckdb

    sec = research_data.refresh_sec_tickers(data_root=durable)
    ticker_ciks = _ticker_ciks_from_sec(sec)
    print(f"verify fetch: tickers={len(ticker_ciks)}")
    research_data.refresh_finra_short_interest(settlement_date, data_root=durable)
    rows = duckdb.query(
        FETCH_TOP_SYMBOLS_SQL,
        params=[settlement_date],
        data_root=durable,
    )
    symbols, resolved = _resolve_top_symbols(rows, ticker_ciks)
    print(f"verify fetch: top={len(symbols)} resolved={len(resolved)} skipped={len(symbols) - len(resolved)}")
    failed = _enrich_resolved(research_data, resolved, durable)
    if failed:
        print(f"verify fetch: failed_enrichments={failed}")
    _confirm_leaderboard_for(screens, durable, settlement_date, len(resolved), len(failed))


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
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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
    "get_fundamentals": {
        "arguments": {"ticker": "AAPL", "metric": "eps"},
        "natural_v1": "What does Apple earn per share?",
        "natural_v2": "What is Apple's EPS, basic and diluted, including trailing twelve months?",
    },
    "find_sec_entities": {
        "arguments": {"query": "Apple"},
        "natural_v1": "Which SEC-registered entities correspond to Apple?",
        "natural_v2": "Which SEC entities match Apple?",
    },
    "search_sec_filings": {
        "arguments": {"query": "Apple", "limit": 5},
        "natural_v1": "What has Apple disclosed about risk factors in its recent filings?",
        "natural_v2": "What risk-factor language has Apple used in its recent SEC filings?",
    },
    "search_sec_relationships": {
        "arguments": {"entity": "AAPL"},
        "natural_v1": "What relationships does Apple disclose?",
        "natural_v2": "What ownership and transaction relationships does Apple disclose in its recent filings?",
    },
    "get_sec_search_coverage": {
        "arguments": {},
        "natural_v1": "What SEC filing types and years are covered?",
        "natural_v2": "Which filing types and years does your SEC coverage include?",
    },
    "list_sec_filings": {
        "arguments": {"identifier": "AAPL", "limit": 5},
        "natural_v1": "What has Apple filed with the SEC lately?",
        "natural_v2": "List Apple's recent SEC filings.",
    },
    "get_sec_filing": {
        "arguments": {"accession_no": "0000320193-25-000079"},
        "natural_v1": "What is in filing 0000320193-25-000079?",
        "natural_v2": "Can you pull up the filing record for 0000320193-25-000079?",
    },
    "list_sec_documents": {
        "arguments": {"accession_no": "0000320193-25-000079"},
        "natural_v1": "What documents are attached to filing 0000320193-25-000079?",
        "natural_v2": "What exhibits came with filing 0000320193-25-000079?",
    },
    "get_sec_document": {
        "arguments": {"accession_no": "0000320193-25-000079"},
        "natural_v1": "What does the main document in filing 0000320193-25-000079 say?",
        "natural_v2": "Can you show me the main document text for filing 0000320193-25-000079?",
    },
    "diff_sec_filings": {
        "arguments": {"current_accession": "0000320193-25-000079", "previous_accession": "0000320193-24-000123"},
        "natural_v1": "What changed between filings 0000320193-25-000079 and 0000320193-24-000123?",
        "natural_v2": "How does filing 0000320193-25-000079 differ from filing 0000320193-24-000123?",
    },
    "get_financial_statements": {
        "arguments": {"ticker": "MSFT", "statement_type": "income_statement"},
        "natural_v1": "How did Microsoft perform on revenue, expenses, and profit?",
        "natural_v2": "What does Microsoft's recent income statement show for revenue and profit?",
    },
    "get_xbrl_facts": {
        "arguments": {"ticker": "AAPL", "concept": "NetIncomeLoss"},
        "natural_v1": "What was Apple's net income?",
        "natural_v2": "What net income did Apple report?",
    },
    "get_material_events": {
        "arguments": {"ticker": "AAPL", "since": "2024-01-01"},
        "natural_v1": "What big events has Apple disclosed since 2024-01-01?",
        "natural_v2": "What material has Apple disclosed since 2024-01-01?",
    },
    "get_beneficial_ownership": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Who owns more than 5% of Apple?",
        "natural_v2": "What large ownership stakes in Apple have been disclosed?",
    },
    "get_ownership_changes": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Have Apple's big holders changed their stakes?",
        "natural_v2": "How have Apple's large holders changed their positions recently?",
    },
    "get_insider_activity": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Have Apple insiders been buying or selling?",
        "natural_v2": "What insider purchases and sales has Apple reported?",
    },
    "get_planned_insider_sales": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Are Apple insiders planning to sell shares?",
        "natural_v2": "What planned insider sales has Apple disclosed?",
    },
    "get_offering_history": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "What is Apple's offering history?",
        "natural_v2": "What offerings has Apple done?",
    },
    "get_dilution_profile": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "How diluted could Apple shareholders get?",
        "natural_v2": "What does Apple's dilution picture look like?",
    },
    "get_governance_events": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "What governance events has Apple had?",
        "natural_v2": "Has Apple had any board or governance changes?",
    },
    "get_transaction_status": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Is Apple involved in any deals or mergers?",
        "natural_v2": "What merger or tender-offer activity involves Apple?",
    },
    "get_short_pressure_profile": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Is Apple under much short pressure?",
        "natural_v2": "How much short pressure is on Apple right now?",
    },
    "search_tools": {
        "arguments": {"query": "short interest"},
        "natural_v1": "Which tools tell me what short sellers are doing in a stock?",
        "natural_v2": "What can show me what short sellers are doing?",
    },
    "list_tool_domains": {
        "arguments": {},
        "natural_v1": "What kinds of company and market data can you help with?",
        "natural_v2": "Can you list the groups of financial information you can look up, like earnings, filings, or ownership?",
    },
    "describe_tool": {
        "arguments": {"name": "get_analyst_estimates"},
        "natural_v1": "What can your analyst-estimates lookup do, and what does it need from me to run it for Apple?",
        "natural_v2": "Explain how your analyst expectations lookup works — what inputs it takes and what related lookups pair with it — before pulling numbers for Apple.",
    },
    "diff_risk_factors": {
        "arguments": {"ticker": "GOOGL"},
        "natural_v1": "What changed in Google's risk factors?",
        "natural_v2": "Compare Google's latest Risk Factors section with the prior filing.",
    },
    "get_recent_ownership_filings": {
        "arguments": {},
        "natural_v1": "What big ownership filings just came out?",
        "natural_v2": "Which SC 13D/G filings are most recent across the market?",
    },
    "get_threshold_securities": {
        "arguments": {},
        "natural_v1": "Which stocks are on the threshold list right now?",
        "natural_v2": "What securities are currently on the SHO threshold list?",
    },
    "get_short_interest": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "How much of Apple is sold short right now?",
        "natural_v2": "What is Apple's current short interest?",
    },
    "get_short_interest_leaderboard": {
        "arguments": {"limit": 5},
        "natural_v1": "Which stocks have the highest short interest as a share of total shares?",
        "natural_v2": "Which stocks are most heavily shorted right now?",
    },
    "get_reg_sho_volume": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "How much short-sale volume has Apple had lately?",
        "natural_v2": "What is Apple's recent Reg SHO volume?",
    },
    "get_analyst_estimates": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "What are analysts estimating for Apple?",
        "natural_v2": "What do analysts expect from Apple going forward?",
    },
    "get_sp500_weight": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "What is Apple's S&P 500 weight?",
        "natural_v2": "How big a part of the S&P 500 is Apple?",
    },
    "get_obligations": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "What contractual obligations does Apple have?",
        "natural_v2": "What commitments and obligations has Apple disclosed in its latest reports?",
    },
    "get_valuation_metrics": {
        "arguments": {"ticker": "AAPL"},
        "natural_v1": "Is Apple stock cheap or expensive right now?",
        "natural_v2": "How does Apple's valuation look on earnings multiples?",
    },
    "search_web": {
        "arguments": {"query": "Apple 10-K risk factors"},
        "natural_v1": "What are outside commentators saying this week about risks to Apple's business?",
        "natural_v2": "What are people saying about risks to Apple's business?",
    },
    "list_finra_datasets": {
        "arguments": {},
        "natural_v1": "What FINRA datasets can I pull?",
        "natural_v2": "Which FINRA datasets are available?",
    },
    "describe_finra_dataset": {
        "arguments": {"dataset_id": "otcMarket/consolidatedShortInterest"},
        "natural_v1": "What is in the FINRA short-interest dataset?",
        "natural_v2": "What fields and coverage does the FINRA consolidated short-interest dataset have?",
    },
    "get_finra_datapoints": {
        "arguments": {
            "dataset": "otcMarket/consolidatedShortInterest",
            "fields": ["settlementDate", "currentShortPositionQuantity"],
            "ticker": "AAPL",
            "limit": 5,
        },
        "natural_v1": "What are Apple's recent raw FINRA short-interest values (exact settlementDate + currentShortPositionQuantity rows)?",
        "natural_v2": "Show me Apple's latest raw FINRA short-position rows with exact field values.",
    },
    "query_finra": {
        "arguments": {"dataset": "otcMarket/consolidatedShortInterest", "ticker": "AAPL", "limit": 5},
        "natural_v1": "Has Apple's FINRA short interest trended up or down lately (analyzed briefing, no raw rows)?",
        "natural_v2": "How has Apple's short interest trended in FINRA data (deterministic metrics/trends only)?",
    },
    "find_alternative_signals": {
        "arguments": {},
        "natural_v1": "What alternative signals have been collected?",
        "natural_v2": "What alternative data signals are available?",
    },
    "get_trend_evidence": {
        "arguments": {"start_date": "2026-09-01", "end_date": "2026-09-02", "geos": ["US"], "limit": 25},
        "natural_v1": "What trends were picked up in the US around September 1st?",
        "natural_v2": "What search trends were collected for the US for September 1-2?",
    },
    "investigate_social_arbitrage_candidate": {
        "arguments": {"term": "Stanley"},
        "natural_v1": "What is the buzz around 'Stanley' as an investment idea?",
        "natural_v2": "Is 'Stanley' worth a closer look based on social signals?",
    },
    "get_macro_context": {
        "arguments": {"geos": ["geoId/06"], "variables": ["Count_Person"]},
        "natural_v1": "How many people live in California?",
        "natural_v2": "What is the latest population count for California?",
    },
    "search_company_patents": {
        "arguments": {"company_id": "Apple Inc.", "assignees": ["Apple Inc."], "limit": 5},
        "natural_v1": "What patents has Apple filed lately?",
        "natural_v2": "What has Apple patented recently?",
    },
    "thesis_create": {
        "arguments": {"user_thesis": "I think NVDA AI demand will stay strong."},
        "natural_v1": "I think NVDA AI demand will stay strong.",
        "natural_v2": "I believe demand for NVDA AI will stay strong. Please keep track of this view.",
    },
    "thesis_show": {
        "arguments": {"id": "thesis-placeholder"},
        "natural_v1": "What does thesis thesis-placeholder say?",
        "natural_v2": "Can you show me thesis thesis-placeholder?",
    },
    "thesis_refine": {
        "arguments": {"id": "thesis-placeholder", "clarification": "AI datacenter capex keeps growing."},
        "natural_v1": "AI datacenter capex keeps growing. Update thesis thesis-placeholder with that.",
        "natural_v2": "New info: AI datacenter capex keeps growing. Please update thesis thesis-placeholder.",
    },
    "thesis_watch": {
        "arguments": {"id": "thesis-placeholder"},
        "natural_v1": "What am I watching for thesis thesis-placeholder?",
        "natural_v2": "What are the watch rules for thesis thesis-placeholder?",
    },
    "thesis_journal": {
        "arguments": {"id": "thesis-placeholder", "body": "Operator note: still watching NVDA datacenter demand."},
        "natural_v1": "Still watching NVDA datacenter demand. Add that to thesis thesis-placeholder.",
        "natural_v2": "Please note for thesis thesis-placeholder: still watching NVDA datacenter demand.",
    },
    "research_start": {
        "arguments": {"question": "Will NVDA inference growth offset slowing training capex?"},
        "natural_v1": "Will NVDA inference growth offset slowing training capex?",
        "natural_v2": "Start a research session on whether NVDA inference growth will offset slowing training capex.",
    },
    "research_resume": {
        "arguments": {"session_id": "research-session-placeholder"},
        "natural_v1": "Continue my research session research-session-placeholder.",
        "natural_v2": "Resume research session research-session-placeholder where it left off.",
    },
    "research_status": {
        "arguments": {"session_id": "research-session-placeholder"},
        "natural_v1": "What is the state of my research session research-session-placeholder?",
        "natural_v2": "Show the status of research session research-session-placeholder.",
    },
    "research_cancel": {
        "arguments": {"session_id": "research-session-placeholder"},
        "natural_v1": "Cancel my research session research-session-placeholder.",
        "natural_v2": "Stop and cancel research session research-session-placeholder.",
    },
    "research_read": {
        "arguments": {
            "session_id": "research-session-placeholder",
            "kind": "research",
            "resource_id": "research-session-placeholder",
        },
        "natural_v1": "Read back my research session research-session-placeholder.",
        "natural_v2": "Show the stored record for research session research-session-placeholder.",
    },
    "thesis_status": {
        "arguments": {"id": "thesis-placeholder", "action": "pause"},
        "natural_v1": "Pause monitoring that thesis.",
        "natural_v2": "Please pause thesis thesis-placeholder monitoring.",
    },
}


def tool_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for raw in TOOLS:
        function = tool_schema_function(raw)
        parameters = function.get("parameters", {})
        schemas[tool_schema_name(raw)] = dict(parameters) if isinstance(parameters, Mapping) else {}
    return schemas


def _required_params(schemas: dict[str, dict[str, object]], tool: str) -> list[object]:
    """Required schema params for tool; [] when absent or malformed."""
    params = schemas.get(tool, {})
    required_raw = params.get("required")
    return list(required_raw) if isinstance(required_raw, list) else []


def _missing_fixture_error(tool: str, detail: object) -> LookupError:
    """LookupError for a missing verification fixture."""
    return LookupError(f"missing verification fixture for tool '{tool}' ({detail})")


def _case_args_or_raise(tool: str, required: list[object]) -> dict[str, object]:
    """Fixture args for tool; raises when the case is missing or incomplete."""
    case = VERIFY_CASES.get(tool)
    if case is None:
        if required:
            raise _missing_fixture_error(tool, f"required={required}")
        return {}
    args = dict(case.get("arguments", {}))
    missing = [k for k in required if k not in args]
    if missing:
        raise _missing_fixture_error(tool, f"missing={missing}")
    return args


def resolve_arguments(tool: str, schemas: dict[str, dict[str, object]] | None = None) -> dict[str, object]:
    schemas = schemas if schemas is not None else tool_schemas()
    return _case_args_or_raise(tool, _required_params(schemas, tool))


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
        f'You may use browse_tools, search_tools, or describe_tool to locate the tool, then Call call_tool exactly once with name="{tool}" and arguments={payload}. '
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


def _natural_case_text(tool: str, version: str) -> str:
    """Natural wording for tool at version; raises when the fixture is missing."""
    case = VERIFY_CASES.get(tool)
    if case is None:
        raise KeyError(f"missing {version} prompt for tool '{tool}'")
    if version == "v1":
        text = case["natural_v1"]
    elif version == "v2":
        text = case["natural_v2"]
    else:
        raise KeyError(f"missing {version} prompt for tool '{tool}'")
    if not isinstance(text, str) or not text:
        raise KeyError(f"missing {version} prompt for tool '{tool}'")
    return text


def _natural_prompt(tool: str, args: Mapping[str, object], version: str) -> str:
    """Natural prompt for version v1/v2: fixture text plus routing guidance."""
    natural = _natural_case_text(tool, version)
    if THESIS_ID_PLACEHOLDER in natural and "id" in args:
        natural = natural.replace(THESIS_ID_PLACEHOLDER, str(args["id"]))
    if tool == "search_tools":
        return natural + _SEARCH_ONLY_GUIDANCE
    return natural + _NATURAL_ROUTING_GUIDANCE


def _natural_prompt_v1(tool: str, args: Mapping[str, object]) -> str:
    """Ordinary user wording for attempt 1; never names the tool."""
    return _natural_prompt(tool, args, "v1")


def _natural_prompt_v2(tool: str, args: Mapping[str, object]) -> str:
    """Clearer but still natural wording for attempt 2; never names the tool."""
    return _natural_prompt(tool, args, "v2")


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
        f'Call call_tool exactly once with name="{tool}" and arguments={payload}. '
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


def _describe_tool_names(describe: Mapping[str, object]) -> list[str]:
    """Sorted describe-visible tool names; [] when the payload is malformed."""
    raw_tools = describe.get("tools")
    if not isinstance(raw_tools, list):
        return []
    return sorted(tool_schema_name(t) for t in raw_tools if isinstance(t, Mapping))


def _doctor_tool_names(doctor: Mapping[str, object]) -> list[str] | None:
    """Sorted doctor tool names; None when the payload is malformed."""
    raw_names = doctor.get("tool_names")
    if not isinstance(raw_names, list):
        return []
    return sorted(n for n in raw_names if isinstance(n, str))


def check_discovery(describe: Mapping[str, object], doctor: Mapping[str, object]) -> str | None:
    if doctor.get("bridge_ok") is not True:
        return "bridge doctor not ok"
    raw_tools = describe.get("tools")
    d_tools: list[Mapping[str, object]] = (
        [t for t in raw_tools if isinstance(t, Mapping)] if isinstance(raw_tools, list) else []
    )
    d_names = _describe_tool_names(describe)
    if doctor.get("tool_count") != len(d_tools):
        return f"doctor/describe count skew: doctor={doctor.get('tool_count')} describe={len(d_tools)}"
    if _doctor_tool_names(doctor) != d_names:
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


_CALL_TOOL_STARTED_SQL = (
    "SELECT arguments FROM agent_events WHERE event_type = 'tool_started' AND tool_name = 'call_tool'"
)


def _call_tool_started_rows(conn: sqlite3.Connection) -> list[tuple[object]]:
    """Raw outer call_tool started-event argument blobs; [] when unreadable."""
    try:
        return conn.execute(_CALL_TOOL_STARTED_SQL).fetchall()
    except sqlite3.Error:
        return []


def _call_tool_dispatched_names(conn: sqlite3.Connection) -> set[str]:
    """Inner names Pi requested via outer `call_tool` lifecycle events.

    The gateway records the inner canonical name in `tool_calls` but writes no
    outer row; Pi records only the outer `call_tool` lifecycle in
    `agent_events`. The `tool_started` event keeps the outer arguments, which
    carry the inner name. Lenient on shape; unparseable rows are ignored.
    """
    names: set[str] = set()
    for (raw,) in _call_tool_started_rows(conn):
        inner = _call_tool_inner_name(raw)
        if inner:
            names.add(inner)
    return names


def _decode_call_tool_payload(raw: object) -> dict[str, object] | None:
    """Outer call_tool row arguments as a dict; None when missing/unparseable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return None
    return payload if isinstance(payload, dict) else None


def _wrapped_inner_name(payload: dict[str, object]) -> str | None:
    """Inner name in the nested arguments envelope; None when absent."""
    wrapped = payload.get("arguments")
    if not isinstance(wrapped, dict):
        return None
    inner_wrapped = wrapped.get("name")
    return inner_wrapped if isinstance(inner_wrapped, str) and inner_wrapped else None


def _pick_inner_name(payload: dict[str, object]) -> str | None:
    """Inner dispatched name in a decoded payload; None when absent."""
    inner = payload.get("name")
    if isinstance(inner, str) and inner:
        return inner
    return _wrapped_inner_name(payload)


def _call_tool_inner_name(raw: object) -> str | None:
    """Inner dispatched name in one outer call_tool row; None when unparseable."""
    payload = _decode_call_tool_payload(raw)
    if payload is None:
        return None
    return _pick_inner_name(payload)


def _direct_tool_completed(conn: sqlite3.Connection, name: str) -> bool:
    """True iff `name` executed directly: a clean tool_calls row plus a direct agent_events tool_completed row.

    The call_tool fallback writes the inner name to tool_calls but completes
    under 'call_tool' in agent_events, so the direct completed row separates
    direct routing from fallback dispatch.
    """
    try:
        clean = conn.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)
        ).fetchone()[0]
        if not clean:
            return False
        direct = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)
        ).fetchone()[0]
    except sqlite3.Error:
        return False
    return bool(direct)


def _target_dispatched(conn: sqlite3.Connection, name: str) -> bool:
    """Intentional dispatch by either route: direct completion or call_tool inner dispatch."""
    return _direct_tool_completed(conn, name) or name in _call_tool_dispatched_names(conn)


def _call_tool_has_unparseable(conn: sqlite3.Connection) -> bool:
    """True iff any outer `call_tool` row carries unparseable/missing inner args."""
    rows = _call_tool_started_rows(conn)
    if not rows:
        # Distinguish "no call_tool rows" (clean) from "unreadable" via probe.
        try:
            conn.execute(_CALL_TOOL_STARTED_SQL).fetchall()
        except sqlite3.Error:
            return False
        return False
    return any(_call_tool_inner_name(raw) is None for (raw,) in rows)


def _has_any_tool_calls(conn: sqlite3.Connection, name: str) -> bool:
    """True iff any tool_calls row exists for name, clean or errored."""
    try:
        row = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name = ?", (name,)).fetchone()
    except sqlite3.Error:
        return False
    return int(row[0] or 0) > 0


def _clean_call_count(conn: sqlite3.Connection, name: str) -> int:
    """Clean tool_calls rows for name."""
    row = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)).fetchone()
    return int(row[0]) if row else 0


def _errored_call_count(conn: sqlite3.Connection, name: str) -> int:
    """Errored tool_calls rows for name."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name = ? AND error_type IS NOT NULL", (name,)
    ).fetchone()
    return int(row[0]) if row else 0


def _completed_event_count(conn: sqlite3.Connection, name: str) -> int:
    """tool_completed lifecycle rows for name."""
    row = conn.execute(
        "SELECT COUNT(*) FROM agent_events WHERE event_type = 'tool_completed' AND tool_name = ?", (name,)
    ).fetchone()
    return int(row[0]) if row else 0


def _completed_via_dispatch(conn: sqlite3.Connection, name: str) -> bool:
    """True iff inner success correlates with an outer call_tool completion."""
    if name not in _call_tool_dispatched_names(conn):
        return False
    return _completed_event_count(conn, "call_tool") >= 1


def _tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """None when `name` has a clean success; otherwise a short failure reason."""
    if _clean_call_count(conn, name) < 1:
        return "absent"
    if _errored_call_count(conn, name) > 0:
        # Strict: any errored execution fails, even with different arguments.
        return "failed execution present"
    if _completed_event_count(conn, name) >= 1:
        return None
    # Generic-dispatch path: inner success in `tool_calls`, outer `call_tool`
    # lifecycle in `agent_events`. Correlate via the started event's inner name.
    if _completed_via_dispatch(conn, name):
        return None
    return "no completed event"


def _tool_call_arg_sets(conn: sqlite3.Connection, name: str) -> tuple[set[str], list[str]] | None:
    """(clean arg keys, failed arg keys) for name; None when unreadable."""
    try:
        rows = conn.execute("SELECT arguments_json, error_type FROM tool_calls WHERE tool_name = ?", (name,)).fetchall()
    except sqlite3.Error:
        return None
    clean = {_normalize_args(r[0]) for r in rows if r[1] is None}
    failed = [_normalize_args(r[0]) for r in rows if r[1] is not None]
    return clean, failed


def _reachability_tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """Relaxed: same-tool different-args probe forgiven; same-args failure still fails."""
    sets = _tool_call_arg_sets(conn, name)
    if sets is None:
        return "absent"
    clean, failed = sets
    if not clean:
        return "absent"
    if any(f in clean for f in failed):
        return "failed execution present"
    if _completed_event_count(conn, name) >= 1:
        return None
    if _completed_via_dispatch(conn, name):
        return None
    return "no completed event"


def _routing_tool_success(conn: sqlite3.Connection, name: str, *, attempt: int) -> str | None:
    """Strict alias: any errored execution fails, even with different arguments."""
    return _tool_success(conn, name, attempt=attempt)


def _is_transient_error(error_type: object, error_message: object) -> bool:
    """True iff one error row is transient evidence (type or message)."""
    if (error_type or "") in TRANSIENT_ERROR_TYPES:
        return True
    return isinstance(error_message, str) and bool(_TRANSIENT_MESSAGE_RE.search(error_message))


def _error_rows(conn: sqlite3.Connection, name_list: list[str]) -> list[tuple[object, object]]:
    """Errored tool_calls rows for names; [] when the table is unreadable."""
    placeholders = ",".join("?" for _ in name_list)
    try:
        return conn.execute(
            f"SELECT error_type, error_message FROM tool_calls WHERE tool_name IN ({placeholders}) AND error_type IS NOT NULL",
            tuple(name_list),
        ).fetchall()
    except sqlite3.Error:
        return []


def _row_errors_transient(conn: sqlite3.Connection, names: Collection[str]) -> bool:
    """True iff `names` have errored calls and every one is transient evidence."""
    name_list = list(names)
    if not name_list:
        return False
    rows = _error_rows(conn, name_list)
    if not rows:
        return False
    return all(_is_transient_error(error_type, error_message) for error_type, error_message in rows)


def _discovery_attempt_count(conn: sqlite3.Connection) -> int:
    """Any attempted discovery call (clean or errored) across the four primitives."""
    row = conn.execute(
        "SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains')"
    ).fetchone()
    return int(row[0]) if row else 0


def _search_query_from_row(raw: object) -> str | None:
    """Query string in one search_tools arguments blob; None when absent."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        args: object = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return None
    if not isinstance(args, dict):
        return None
    query_value = args.get("query")
    return query_value if isinstance(query_value, str) else None


def _search_queries(conn: sqlite3.Connection) -> list[str]:
    """Ordered search_tools query strings for failure diagnostics."""
    rows = conn.execute(
        "SELECT arguments_json FROM tool_calls WHERE tool_name = 'search_tools' ORDER BY started_at, tool_call_id"
    ).fetchall()
    queries: list[str] = []
    for (raw,) in rows:
        query = _search_query_from_row(raw)
        if query is not None:
            queries.append(query)
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


_DISCOVERY_TOOLS_ORDERED = ("browse_tools", "search_tools", "describe_tool", "list_tool_domains")


def _eval_missing_db(db_path: Path, timed_out: bool, completed_override: bool) -> tuple[bool, str] | None:
    """Missing-DB verdict; None when the DB exists and evaluation continues."""
    if db_path.is_file():
        return None
    if timed_out and not completed_override:
        return False, "transient: pi timeout before terminal state"
    return False, f"missing recorder DB: {db_path}"


def _eval_rejected(conn: sqlite3.Connection) -> tuple[bool, str] | None:
    """Harness-rejection verdict; None when no rejected calls exist."""
    rejected = list(_rejected_tools(conn))
    if rejected:
        return False, f"routing failed: harness-rejected call to {', '.join(rejected)}"
    return None


def _eval_unparseable(conn: sqlite3.Connection) -> tuple[bool, str] | None:
    """Unparseable-dispatch verdict; None when every call_tool row parses."""
    if _call_tool_has_unparseable(conn):
        return False, "routing failed: unparseable call_tool dispatch args"
    return None


def _eval_transient_target(
    early_target: str | None, conn: sqlite3.Connection, required_tool: str
) -> tuple[bool, str] | None:
    """Transient-target verdict; None when the target shows no transient error."""
    if early_target is not None and _row_errors_transient(conn, [required_tool]):
        return False, f"transient: target '{required_tool}' {early_target} (transient error)"
    return None


def _eval_discovery_gate(
    conn: sqlite3.Connection, required_tool: str, discovery_count: int, cap: int
) -> tuple[bool, str] | None:
    """Discovery-cap verdict; None when within budget."""
    if required_tool != "search_tools" and discovery_count > cap:
        return False, f"too-many-discovery:{discovery_count}"
    return None


def _eval_timeout_exit(exit_code: int, timed_out: bool, completed_override: bool) -> tuple[bool, str] | None:
    """Timeout/exit verdict; None when the run terminal state is acceptable."""
    if timed_out and not completed_override:
        return False, "transient: pi timeout before terminal state"
    if exit_code != 0 and not completed_override:
        return False, f"pi exit {exit_code}"
    return None


def _model_telemetry_ok(conn: sqlite3.Connection) -> bool:
    """True iff model_calls carries a non-empty model ID."""
    models = conn.execute("SELECT model FROM model_calls").fetchall()
    return any((r[0] or "").strip() for r in models)


def _agent_runs_completed(conn: sqlite3.Connection) -> list[object] | None:
    """agent_runs statuses when every run completed; None otherwise."""
    rows = conn.execute("SELECT status FROM agent_runs").fetchall()
    if not rows or any((r[0] or "") != "completed" for r in rows):
        return None
    return [r[0] for r in rows]


def _eval_terminal(conn: sqlite3.Connection) -> tuple[bool, str] | None:
    """Model-telemetry + agent_runs terminal verdict; None on clean terminal."""
    if not _model_telemetry_ok(conn):
        return False, "no model telemetry: model_calls has no non-empty model ID"
    statuses = _agent_runs_completed(conn)
    if statuses is None:
        rows = conn.execute("SELECT status FROM agent_runs").fetchall()
        return False, f"agent_runs not completed: {[r[0] for r in rows]}"
    return None


_DiscoveryChecker = Callable[..., str | None]


def _discovery_ok(conn: sqlite3.Connection, checker: _DiscoveryChecker, attempt: int) -> bool:
    """True iff some discovery primitive has a clean success under checker."""
    return any(checker(conn, t, attempt=attempt) is None for t in _DISCOVERY_TOOLS_ORDERED)


def _errored_only_target(conn: sqlite3.Connection, required_tool: str, early_target: str | None) -> str | None:
    """'precede' | 'failed' | None: errored-only target state for absent-with-rows."""
    if early_target == "absent" and _has_any_tool_calls(conn, required_tool):
        if not _discovery_before_dispatch(conn, required_tool):
            return "precede"
        return "failed"
    return None


def _precede_message(required_tool: str) -> str:
    """Shared ordering-failure message for both evaluators."""
    return f"routing failed: discovery did not precede call_tool dispatch of '{required_tool}'"


def _reach_stray_verdict(conn: sqlite3.Connection, required_tool: str, attempt: int) -> tuple[bool, str] | None:
    """Stray-tool verdict for reachability; None when no errored strays exist."""
    unexpected = _unexpected_tools(conn, required_tool)
    if not unexpected:
        return None
    errored = [t for t in unexpected if _reachability_tool_success(conn, t, attempt=attempt) is not None]
    if errored and _row_errors_transient(conn, errored):
        return False, f"transient: stray tool(s) transient error: {', '.join(errored)}"
    if errored:
        return False, f"routing failed: unexpected research tool call(s): {', '.join(errored)}"
    return None


def _reach_natural_verdict(
    conn: sqlite3.Connection, required_tool: str, early_target: str | None, attempt: int
) -> tuple[bool, str] | None:
    """Attempt 1-2 reachability path: clean discovery precedes target dispatch."""
    if not _discovery_ok(conn, _reachability_tool_success, attempt):
        return False, "routing failed: no clean discovery before call_tool"
    state = _errored_only_target(conn, required_tool, early_target)
    if state == "precede":
        return False, _precede_message(required_tool)
    if state == "failed":
        return False, f"target '{required_tool}' failed-research-call (errored execution, no clean run)"
    if not _target_dispatched(conn, required_tool):
        return False, f"routing failed: '{required_tool}' not dispatched via call_tool"
    if not _discovery_before_dispatch(conn, required_tool):
        return False, f"routing failed: discovery did not precede call_tool dispatch of '{required_tool}'"
    return None


def _reach_explicit_verdict(conn: sqlite3.Connection, required_tool: str) -> tuple[bool, str] | None:
    """Attempt 3+ reachability path: direct call_tool dispatch of the target."""
    if required_tool not in _call_tool_dispatched_names(conn):
        return False, f"routing failed: '{required_tool}' not dispatched via call_tool"
    return None


def _reach_dispatch_verdict(
    conn: sqlite3.Connection, required_tool: str, early_target: str | None, attempt: int
) -> tuple[bool, str] | None:
    """Dispatch-shape verdict for reachability; None when the shape is valid."""
    if required_tool == "search_tools":
        return None
    if attempt in (1, 2):
        return _reach_natural_verdict(conn, required_tool, early_target, attempt)
    return _reach_explicit_verdict(conn, required_tool)


def _reach_target_verdict(conn: sqlite3.Connection, required_tool: str, attempt: int) -> tuple[bool, str] | None:
    """Final target verdict for reachability; None on clean target success."""
    target_problem = _reachability_tool_success(conn, required_tool, attempt=attempt)
    if target_problem is None:
        return None
    if _row_errors_transient(conn, [required_tool]):
        return False, f"transient: target '{required_tool}' {target_problem} (transient error)"
    if target_problem == "absent" and _has_any_tool_calls(conn, required_tool):
        return False, f"target '{required_tool}' failed-research-call (errored execution, no clean run)"
    return False, f"target '{required_tool}' absent ({target_problem})"


def _evaluate_reachability_conn(
    conn: sqlite3.Connection,
    required_tool: str,
    exit_code: int,
    timed_out: bool,
    completed_override: bool,
    attempt: int,
) -> tuple[bool, str]:
    """Reachability verdicts against an open attempt DB, in pipeline order."""
    discovery_count = _discovery_attempt_count(conn)
    for verdict in (
        lambda: _eval_discovery_gate(conn, required_tool, discovery_count, REACHABILITY_DISCOVERY_CAP),
        lambda: _eval_rejected(conn),
        lambda: _eval_unparseable(conn),
        lambda: _reach_stray_verdict(conn, required_tool, attempt),
    ):
        hit = verdict()
        if hit is not None:
            return hit
    early_target = _reachability_tool_success(conn, required_tool, attempt=attempt)
    hit = _eval_transient_target(early_target, conn, required_tool)
    if hit is not None:
        return hit
    hit = _reach_dispatch_verdict(conn, required_tool, early_target, attempt)
    if hit is not None:
        return hit
    hit = _eval_timeout_exit(exit_code, timed_out, completed_override)
    if hit is not None:
        return hit
    hit = _reach_target_verdict(conn, required_tool, attempt)
    if hit is not None:
        return hit
    hit = _eval_terminal(conn)
    if hit is not None:
        return hit
    return True, "pass"


def evaluate_reachability_attempt(
    db_path: Path,
    required_tool: str,
    exit_code: int,
    timed_out: bool,
    *,
    completed_override: bool = False,
    attempt: int = 3,
) -> tuple[bool, str]:
    """Relaxed reachability: can the model eventually navigate catalog and dispatch target via call_tool."""
    missing = _eval_missing_db(db_path, timed_out, completed_override)
    if missing is not None:
        return missing
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return _evaluate_reachability_conn(conn, required_tool, exit_code, timed_out, completed_override, attempt)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"DB read failed: {exc}"


def _out_of_allowance(required_tool: str, unexpected: list[str]) -> list[str]:
    """Unexpected tools outside the prerequisite allowance for the target."""
    allowed = set(PREREQ_CHAINS.get(required_tool, frozenset()))
    return [t for t in unexpected if t not in allowed]


def _clean_stray_verdict(conn: sqlite3.Connection, strays: list[str], attempt: int) -> tuple[bool, str] | None:
    """Clean-stray verdict; None when no stray has a clean success."""
    clean_strays = [t for t in strays if _routing_tool_success(conn, t, attempt=attempt) is None]
    if clean_strays:
        return (
            False,
            f"routing failed: unrelated-research-success: unexpected research tool call(s): {', '.join(clean_strays)}",
        )
    return None


def _errored_stray_verdict(conn: sqlite3.Connection, strays: list[str]) -> tuple[bool, str] | None:
    """Errored-stray verdict; None when no errored strays exist."""
    if not strays:
        return None
    if _row_errors_transient(conn, strays):
        return False, f"transient: stray tool(s) transient error: {', '.join(strays)}"
    return False, f"routing failed: failed-research-call: unexpected research tool call(s): {', '.join(strays)}"


def _route_stray_verdict(conn: sqlite3.Connection, required_tool: str, attempt: int) -> tuple[bool, str] | None:
    """Stray-tool verdict for strict routing; None when no out-of-allowance strays exist."""
    unexpected = _unexpected_tools(conn, required_tool)
    if not unexpected:
        return None
    strays = _out_of_allowance(required_tool, unexpected)
    hit = _clean_stray_verdict(conn, strays, attempt)
    if hit is not None:
        return hit
    return _errored_stray_verdict(conn, strays)


def _route_failed_target_verdict(early_target: str | None, required_tool: str) -> tuple[bool, str] | None:
    """Failed-execution verdict for strict routing; None when the target is clean/absent."""
    if early_target is not None and "failed execution present" in early_target:
        return False, f"routing failed: failed-research-call for '{required_tool}' (failed execution present)"
    return None


def _route_natural_verdict(
    conn: sqlite3.Connection, required_tool: str, early_target: str | None, attempt: int
) -> tuple[bool, str] | None:
    """Attempt 1-2 strict path: clean discovery precedes intentional dispatch."""
    if not _discovery_ok(conn, _routing_tool_success, attempt):
        return False, "routing failed: no clean discovery before call_tool"
    state = _errored_only_target(conn, required_tool, early_target)
    if state == "precede":
        return False, _precede_message(required_tool)
    if state == "failed":
        return False, f"routing failed: failed-research-call for '{required_tool}' (errored execution, no clean run)"
    if not _target_dispatched(conn, required_tool):
        return False, f"routing failed: target-never-dispatched: '{required_tool}' not dispatched via call_tool"
    if not _discovery_before_dispatch(conn, required_tool):
        return False, _precede_message(required_tool)
    return None


def _route_expected_args_set(conn: sqlite3.Connection, required_tool: str) -> set[str]:
    """Clean executed arg keys for the target on attempt 3; empty when unreadable."""
    try:
        have_rows: list[tuple[object]] = conn.execute(
            "SELECT arguments_json FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (required_tool,)
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {_normalize_args(r[0]) for r in have_rows}


def _route_args_match(
    conn: sqlite3.Connection, required_tool: str, expected_args: Mapping[str, object] | None
) -> tuple[bool, str] | None:
    """Attempt-3 args-equality verdict; None when args match or are unchecked."""
    if expected_args is None:
        return None
    try:
        want = _normalize_args(dict(expected_args))
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        want = ""
    if want not in _route_expected_args_set(conn, required_tool):
        return False, f"routing failed: attempt 3 args mismatch for '{required_tool}'"
    return None


def _route_explicit_verdict(
    conn: sqlite3.Connection, required_tool: str, discovery_count: int, expected_args: Mapping[str, object] | None
) -> tuple[bool, str] | None:
    """Attempt 3+ strict path: exactly one call_tool dispatch, no discovery."""
    if discovery_count > 0:
        return (
            False,
            f"routing failed: attempt 3 must not browse/search/describe; call call_tool exactly once (discovery_calls={discovery_count})",
        )
    n_call = _call_tool_start_count(conn)
    if n_call != 1:
        return False, f"routing failed: attempt 3 must call call_tool exactly once (got {n_call})"
    if required_tool not in _call_tool_dispatched_names(conn):
        return False, f"routing failed: target-never-dispatched: '{required_tool}' not dispatched via call_tool"
    return _route_args_match(conn, required_tool, expected_args)


def _route_dispatch_verdict(
    conn: sqlite3.Connection,
    required_tool: str,
    early_target: str | None,
    attempt: int,
    discovery_count: int,
    expected_args: Mapping[str, object] | None,
) -> tuple[bool, str] | None:
    """Dispatch-shape verdict for strict routing; None when the shape is valid."""
    if required_tool == "search_tools":
        return None
    if attempt in (1, 2):
        return _route_natural_verdict(conn, required_tool, early_target, attempt)
    return _route_explicit_verdict(conn, required_tool, discovery_count, expected_args)


def _route_target_verdict(conn: sqlite3.Connection, required_tool: str, attempt: int) -> tuple[bool, str] | None:
    """Final target verdict for strict routing; None on clean target success."""
    target_problem = _routing_tool_success(conn, required_tool, attempt=attempt)
    if target_problem is None:
        return None
    if _row_errors_transient(conn, [required_tool]):
        return False, f"transient: target '{required_tool}' {target_problem} (transient error)"
    if "failed execution present" in target_problem:
        return False, f"routing failed: failed-research-call for '{required_tool}' (failed execution present)"
    if target_problem == "absent" and _has_any_tool_calls(conn, required_tool):
        return False, f"routing failed: failed-research-call for '{required_tool}' (errored execution, no clean run)"
    return False, f"target '{required_tool}' absent ({target_problem})"


def _evaluate_routing_conn(
    conn: sqlite3.Connection,
    required_tool: str,
    exit_code: int,
    timed_out: bool,
    completed_override: bool,
    attempt: int,
    expected_args: Mapping[str, object] | None,
    discovery_count: int,
) -> tuple[bool, str]:
    """Strict routing verdicts against an open attempt DB, in pipeline order."""
    for verdict in (
        lambda: _eval_discovery_gate(conn, required_tool, discovery_count, ROUTING_DISCOVERY_CAP),
        lambda: _eval_rejected(conn),
        lambda: _eval_unparseable(conn),
        lambda: _route_stray_verdict(conn, required_tool, attempt),
    ):
        hit = verdict()
        if hit is not None:
            return hit
    early_target = _routing_tool_success(conn, required_tool, attempt=attempt)
    hit = _route_early_verdicts(conn, required_tool, early_target, attempt, discovery_count, expected_args)
    if hit is not None:
        return hit
    hit = _eval_timeout_exit(exit_code, timed_out, completed_override)
    if hit is not None:
        return hit
    hit = _route_target_verdict(conn, required_tool, attempt)
    if hit is not None:
        return hit
    hit = _eval_terminal(conn)
    if hit is not None:
        return hit
    return True, "pass"


def _route_early_verdicts(
    conn: sqlite3.Connection,
    required_tool: str,
    early_target: str | None,
    attempt: int,
    discovery_count: int,
    expected_args: Mapping[str, object] | None,
) -> tuple[bool, str] | None:
    """Pre-terminal strict verdicts: transient, failed-target, then dispatch shape."""
    hit = _eval_transient_target(early_target, conn, required_tool)
    if hit is not None:
        return hit
    hit = _route_failed_target_verdict(early_target, required_tool)
    if hit is not None:
        return hit
    return _route_dispatch_verdict(conn, required_tool, early_target, attempt, discovery_count, expected_args)


def evaluate_routing_attempt(
    db_path: Path,
    required_tool: str,
    exit_code: int,
    timed_out: bool,
    *,
    completed_override: bool = False,
    attempt: int = 3,
    expected_args: Mapping[str, object] | None = None,
) -> tuple[bool, str]:
    """Strict routing precision: intentional dispatch without unrelated research execution."""
    missing = _eval_missing_db(db_path, timed_out, completed_override)
    if missing is not None:
        return missing
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            discovery_count = _discovery_attempt_count(conn)
            return _evaluate_routing_conn(
                conn, required_tool, exit_code, timed_out, completed_override, attempt, expected_args, discovery_count
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"DB read failed: {exc}"


def evaluate_attempt(
    db_path: Path,
    required_tool: str,
    exit_code: int,
    timed_out: bool,
    *,
    completed_override: bool = False,
    attempt: int = 3,
) -> tuple[bool, str]:
    """Back-compat alias: strict routing verdict."""
    return evaluate_routing_attempt(
        db_path, required_tool, exit_code, timed_out, completed_override=completed_override, attempt=attempt
    )


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
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
                pass


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10, check=False
        )
        sha = (out.stdout or "").strip()
        return sha or "unknown"
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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


def _pi_env(db_path: Path, stockbot_store: Path | None) -> dict[str, str]:
    """Subprocess env for one Pi attempt with recorder + isolated store."""
    env = dict(os.environ, RUNS_DB_PATH=str(db_path))
    if stockbot_store is not None:
        env["STOCKBOT_DATA_DIR"] = str(stockbot_store.resolve())
    return env


def _pi_cmd(prompt: str) -> list[str]:
    """Pi CLI argv: pristine prompt mode with the stockbot extension only."""
    return [
        "pi",
        "-p",
        "--no-session",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--extension",
        EXTENSION,
        "--",
        prompt,
    ]


def _pi_logs(attempt_dir: Path) -> tuple[Path, Path]:
    """(stdout log, stderr log) paths for one attempt directory."""
    return attempt_dir / f"{attempt_dir.name}.pi.log", attempt_dir / f"{attempt_dir.name}.stderr.log"


def _wait_for_pi(proc: object, db_path: Path, deadline: float) -> tuple[int | None, bool]:
    """Wait for process exit or recorder completion; returns (code, saw_complete)."""
    saw_complete = False
    code: int | None = None
    while time.monotonic() < deadline:
        polled = getattr(proc, "poll")()  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        code = polled if isinstance(polled, int) else None
        if code is not None:
            break
        if db_terminal(db_path):
            saw_complete = True
            break
        time.sleep(POLL_S)
    else:
        polled = getattr(proc, "poll")()  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        code = polled if isinstance(polled, int) else None
    return code, saw_complete


def _reap_exited(proc: object, code: int | None) -> int | None:
    """Pass-through code for an already-exited Pi process."""
    polled = getattr(proc, "poll")()  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    return polled if code is None else code


def _kill_pi(proc: object) -> None:
    """SIGKILL the Pi process group; missing-process errors are terminal."""
    try:
        os.killpg(getattr(proc, "pid"), signal.SIGKILL)  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    except ProcessLookupError, PermissionError:
        pass


def _pi_wait_result(proc: object) -> int | None:
    """Wait result narrowed to int; None when the handle yields a non-int."""
    result = getattr(proc, "wait")(timeout=15)  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    return result if isinstance(result, int) else None


def _wait_pi_exit(proc: object) -> int | None:
    """Reap a killed Pi process; 124 when the wait itself fails."""
    try:
        return _pi_wait_result(proc)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return 124


def _reap_pi(proc: object, code: int | None) -> int | None:
    """Kill + reap a still-running Pi process; passes through an exited code."""
    if getattr(proc, "poll")() is not None:  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        return _reap_exited(proc, code)
    _kill_pi(proc)
    return _wait_pi_exit(proc)


def _pi_outcome(code: int | None, saw_complete: bool, deadline: float) -> tuple[int, bool]:
    """(exit code, timed_out) from the raw poll result and deadline."""
    resolved = code if code is not None else 124
    return resolved, not saw_complete and resolved != 0 and time.monotonic() >= deadline


def run_pi(
    prompt: str, db_path: Path, cwd: Path, stockbot_store: Path | None = None
) -> tuple[int, bool, str, str, bool]:
    """Run one Pi attempt. Pi 0.85.0 -p lingers after answering, so outputs go
    to files (never pipes) and completion is detected via the recorder DB;
    the process group is then killed. Returns (exit, timed_out, out, err, saw_complete)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_dir = db_path.parent
    out_log, err_log = _pi_logs(attempt_dir)
    with open(out_log, "w") as out_f, open(err_log, "w") as err_f:
        proc = subprocess.Popen(
            _pi_cmd(prompt),
            stdout=out_f,
            stderr=err_f,
            stdin=subprocess.DEVNULL,
            cwd=str(cwd),
            env=_pi_env(db_path, stockbot_store),
            start_new_session=True,
        )
        deadline = time.monotonic() + TIMEOUT_S
        code, saw_complete = _wait_for_pi(proc, db_path, deadline)
        code = _reap_pi(proc, code)
        exit_code, timed_out = _pi_outcome(code, saw_complete, deadline)
        if saw_complete and code not in (0, None):
            err_f.write("\nKILLED_AFTER_COMPLETE")
    out_text = out_log.read_text() if out_log.is_file() else ""
    err_text = err_log.read_text() if err_log.is_file() else ""
    return exit_code, timed_out, out_text, err_text, saw_complete


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


_INFRA_RE = re.compile(
    r"ratelimit|rate-limit|rate limit|429|timeout|timed out|latency|deadline exceeded|drain.?timeout", re.IGNORECASE
)
# Explicit routing-failure markers from evaluate_attempt; these take precedence
# over infra keywords (a routing reason mentioning "timeout"/"429" is routing).
_ROUTING_RE = re.compile(
    r"routing failed|harness-rejected|unexpected research|unrelated-research-success|failed-research-call|target-never-dispatched|errored discovery|too-many-searches|too-many-discovery|absent|no model telemetry|agent_runs not completed|pi exit|missing recorder DB|DB read failed",
    re.IGNORECASE,
)


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


def run_matrix(
    jobs: list[tuple[str, int]], worker: Callable[[str, int], AttemptResult], concurrency: int
) -> list[AttemptResult]:
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
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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


_VERIFIER_TOOL_RE = re.compile(r"^[a-z0-9_]+$")


def _verifier_unrelated_success(pop: list[AttemptResult]) -> dict[str, int]:
    """Verifier-side unrelated-success: attempts with unrelated-research-success reason plus distinct tool names."""
    attempts = 0
    tools: set[str] = set()
    for r in pop:
        reason = r.routing_reason or ""
        if "unrelated-research-success" not in reason:
            continue
        attempts += 1
        tail = reason.split("call(s):", 1)[1] if "call(s):" in reason else reason.rsplit(":", 1)[-1]
        for part in tail.split(","):
            name = part.strip().strip("'\"").rstrip(").")
            if name and _VERIFIER_TOOL_RE.fullmatch(name):
                tools.add(name)
    return {"attempts": attempts, "calls": len(tools)}


def _agg_routing_ok(pop: list[AttemptResult]) -> int:
    """Terminal attempts passing under routing rules."""
    return sum(1 for r in pop if (r.routing_ok if r.routing_ok is not None else r.ok))


def _agg_reach_ok(pop: list[AttemptResult]) -> int:
    """Terminal attempts passing under reachability rules."""
    return sum(1 for r in pop if (r.reach_ok if r.reach_ok is not None else r.ok))


def _agg_with_discovery(pop: list[AttemptResult]) -> list[AttemptResult]:
    """Terminal attempts whose routing_metrics show discovered tools."""
    return [r for r in pop if _metric_int(r.routing_metrics, "discovered_tool_count") > 0]


def _agg_flagged(pop: list[AttemptResult], key: str) -> list[AttemptResult]:
    """Terminal attempts with a routing_metrics boolean flag set."""
    return [r for r in pop if (r.routing_metrics or {}).get(key) is True]


def _agg_invalid_totals(pop: list[AttemptResult]) -> tuple[int, int, float]:
    """(invalid_total, routed_total, clamped invalid rate) for the population."""
    invalid_total = sum(_metric_int(r.routing_metrics, "invalid_tool_calls") for r in pop)
    routed_total = sum(
        _metric_int(r.routing_metrics, "discovery_calls")
        + _metric_int(r.routing_metrics, "call_tool_count")
        + _metric_int(r.routing_metrics, "direct_tool_calls")
        for r in pop
    )
    rate = (invalid_total / routed_total) if routed_total else 0.0
    return invalid_total, routed_total, min(1.0, max(0.0, rate))


def _agg_failure_counts(pop: list[AttemptResult]) -> dict[str, int]:
    """Terminal-attempt counts per routing failure category."""
    return {c.value: sum(1 for r in pop if r.failure_category == c) for c in RoutingFailureCategory}


def _agg_completion(pop: list[AttemptResult]) -> dict[str, object]:
    """Completion aggregate over attempts carrying a completion verdict."""
    evaluated = [r for r in pop if r.completion_ok is not None]
    completed = [r for r in evaluated if r.completion_ok]
    return {
        "passed": len(completed),
        "evaluated": len(evaluated),
        "rate": (len(completed) / len(evaluated)) if evaluated else None,
    }


def _rate(passed: int, total: int, empty: float = 0.0) -> float:
    """Auditable passed/total rate; empty default when the population is empty."""
    return (passed / total) if total else empty


def aggregate_results(results: list[AttemptResult]) -> dict[str, object]:
    """Attempt-level aggregates over terminal attempts after transient retries.

    Population is every terminal AttemptResult (one per tool x attempt after the
    retry loop); each rate carries its numerator/denominator so the formula is
    auditable. Completion covers only attempts with a completion verdict
    (natural attempts and agent-loop cases, never explicit attempt-3 runs).
    """
    pop = [r for r in results if r.terminal_completed]
    return _agg_report(pop)


def _agg_report(pop: list[AttemptResult]) -> dict[str, object]:
    """Full aggregate report dict for the terminal population."""
    total = len(pop)
    return {
        "population": total,
        "failure_category_counts": _agg_failure_counts(pop),
        "routing_precision": _agg_pass_section(pop, True),
        "reachability": _agg_pass_section(pop, False),
        "discovery_to_research_conversion": _agg_conversion(pop),
        "premature_stop_rate": _agg_flag_section(pop, "premature_stop_detected", "premature"),
        "continuation_recovery": _agg_recovery(pop),
        "median_discovery_calls": _median([r.discovery_calls for r in pop]),
        "median_research_calls": _median([r.research_calls for r in pop]),
        "invalid_tool_call_rate": _agg_invalid_section(pop),
        "unrelated_research_calls": sum(_metric_int(r.routing_metrics, "unrelated_research_calls") for r in pop),
        "verifier_unrelated_success": _verifier_unrelated_success(pop),
        "completion": _agg_completion(pop),
    }


def _agg_pass_section(pop: list[AttemptResult], routing: bool) -> dict[str, object]:
    """Passed/total/rate section for routing (True) or reachability (False)."""
    passed = _agg_routing_ok(pop) if routing else _agg_reach_ok(pop)
    return {"passed": passed, "total": len(pop), "rate": _rate(passed, len(pop))}


def _agg_conversion(pop: list[AttemptResult]) -> dict[str, object]:
    """Discovery-to-research conversion section."""
    with_discovery = _agg_with_discovery(pop)
    with_research = [r for r in with_discovery if r.research_calls > 0]
    return {
        "with_research": len(with_research),
        "with_discovery": len(with_discovery),
        "rate": _rate(len(with_research), len(with_discovery)),
    }


def _agg_flag_section(pop: list[AttemptResult], flag: str, name: str) -> dict[str, object]:
    """premature-stop style {name, total, rate} section for a routing flag."""
    flagged = _agg_flagged(pop, flag)
    return {name: len(flagged), "total": len(pop), "rate": _rate(len(flagged), len(pop))}


def _agg_recovery(pop: list[AttemptResult]) -> dict[str, object]:
    """Continuation-recovery section."""
    injected = _agg_flagged(pop, "continuation_injected")
    recovered = [r for r in injected if (r.routing_metrics or {}).get("continuation_succeeded") is True]
    return {"recovered": len(recovered), "injected": len(injected), "rate": _rate(len(recovered), len(injected), 1.0)}


def _agg_invalid_section(pop: list[AttemptResult]) -> dict[str, object]:
    """Invalid-tool-call-rate section with auditable numerator/denominator."""
    invalid_attempts = [r for r in pop if _metric_int(r.routing_metrics, "invalid_tool_calls") > 0]
    invalid_total, routed_total, invalid_rate = _agg_invalid_totals(pop)
    return {
        "invalid_calls": invalid_total,
        "routed_calls": routed_total,
        "invalid_attempts": len(invalid_attempts),
        "total": len(pop),
        "rate": invalid_rate,
    }


def _substitute_placeholder(args: dict[str, object], placeholder: str, real_id: str) -> None:
    """Replace placeholder arg values with the seeded fixture ID (in place)."""
    for key, value in args.items():
        if value == placeholder:
            args[key] = real_id


def _verification_args(tool: str, base_args: Mapping[str, object], store_dir: Path, durable: Path) -> dict[str, object]:
    """Per-attempt args with isolated fixtures seeded; never mutates base_args."""
    args = dict(base_args)
    if tool in FINRA_SEED_TOOLS:
        seed_finra_fixture(store_dir, durable)
    if tool in THESIS_ID_TOOLS:
        _substitute_placeholder(args, THESIS_ID_PLACEHOLDER, ensure_thesis_fixture(store_dir.resolve()))
    if tool in RESEARCH_SESSION_TOOLS:
        _substitute_placeholder(args, RESEARCH_SESSION_PLACEHOLDER, ensure_research_fixture(store_dir.resolve()))
    return args


def _verification_prompt(tool: str, args: Mapping[str, object], attempt: int, prompt_override: str | None) -> str:
    """Prompt for one attempt: override, explicit-dispatch debug, or natural."""
    if prompt_override is not None:
        return prompt_override
    if attempt >= 3:
        # Explicit-dispatch debug mode only; natural scenario path lives in verify_judge.py.
        return build_routing_explicit_prompt(tool, args)
    return build_explicit_prompt(tool, args)


def _verification_evaluate(
    db_path: Path,
    tool: str,
    code: int,
    timed_out: bool,
    saw_complete: bool,
    err_text: str,
    args: Mapping[str, object],
    attempt: int,
) -> tuple[bool, str, bool, str, bool]:
    """Dual-evaluate one finished run: (reach_ok, reach_reason, routing_ok, routing_reason, model_config_failed)."""
    reach_ok, reach_reason = evaluate_reachability_attempt(
        db_path, tool, code, timed_out, completed_override=saw_complete, attempt=attempt
    )
    routing_ok, routing_reason = evaluate_routing_attempt(
        db_path, tool, code, timed_out, completed_override=saw_complete, attempt=attempt, expected_args=args
    )
    base_config_failed = code != 0 and not saw_complete and "model" in err_text.lower()
    model_config_failed = (not is_routing_failure(routing_reason)) and (
        base_config_failed
        or is_infra_failure(routing_reason)
        or is_infra_failure(err_text)
        or is_infra_failure(reach_reason)
    )
    return reach_ok, reach_reason, routing_ok, routing_reason, model_config_failed


def _transient_reason_for(routing_reason: str, reach_reason: str) -> str:
    """Transient reason preferring routing, else reachability when routing is not a routing failure."""
    if routing_reason.startswith("transient: "):
        return routing_reason
    if reach_reason.startswith("transient: ") and not is_routing_failure(routing_reason):
        return reach_reason
    return ""


def _exhausted_result(
    tool: str,
    attempt: int,
    code: int,
    db_path: Path,
    duration_seconds: float,
    model_config_failed: bool,
    transient_reason: str,
) -> AttemptResult:
    """Terminal result when the transient retry budget is exhausted."""
    disc, resc = _metrics_for_db(db_path, tool)
    return AttemptResult(
        tool,
        attempt,
        False,
        f"transient budget exhausted: {transient_reason}",
        code,
        str(db_path),
        duration_seconds,
        model_config_failed,
        False,
        transient_reason,
        False,
        transient_reason,
        disc,
        resc,
        _direct_count_for_db(db_path, tool),
        None,
        "",
        _routing_metrics_for_db(db_path),
    )


def _finished_result(
    tool: str,
    attempt: int,
    code: int,
    timed_out: bool,
    db_path: Path,
    duration_seconds: float,
    model_config_failed: bool,
    reach_ok: bool,
    reach_reason: str,
    routing_ok: bool,
    routing_reason: str,
    args: Mapping[str, object],
    saw_complete: bool,
) -> AttemptResult:
    """Terminal result for a non-transient finished run, with completion + category."""
    disc, resc = _metrics_for_db(db_path, tool)
    comp_ok, comp_reason = (
        evaluate_completion_attempt(db_path, tool, code, timed_out, completed_override=saw_complete)
        if attempt in (1, 2)
        else (None, "")
    )
    try:
        failure_cat = (
            None if routing_ok else classify_routing_failure(db_path, tool, attempt=attempt, expected_args=args)
        )
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        failure_cat = None
    return AttemptResult(
        tool,
        attempt,
        routing_ok,
        routing_reason,
        code,
        str(db_path),
        duration_seconds,
        model_config_failed,
        reach_ok,
        reach_reason,
        routing_ok,
        routing_reason,
        disc,
        resc,
        _direct_count_for_db(db_path, tool),
        comp_ok,
        comp_reason,
        _routing_metrics_for_db(db_path),
        terminal_completed=saw_complete,
        failure_category=failure_cat,
    )


def _run_single_retry(
    tool: str,
    attempt: int,
    base_args: Mapping[str, object],
    batch_root: Path,
    cwd: Path,
    durable: Path,
    prompt_override: str | None,
    retry: int,
    start: float,
) -> AttemptResult | None:
    """One retry iteration; returns a terminal result or None to run the next retry."""
    db_path, store_dir = attempt_dirs(batch_root, tool, attempt, retry=retry)
    store_dir.mkdir(parents=True, exist_ok=True)
    args = _verification_args(tool, base_args, store_dir, durable)
    prompt = _verification_prompt(tool, args, attempt, prompt_override)
    code, timed_out, _out, err_text, saw_complete = run_pi(prompt, db_path, cwd, store_dir)
    reach_ok, reach_reason, routing_ok, routing_reason, model_config_failed = _verification_evaluate(
        db_path, tool, code, timed_out, saw_complete, err_text, args, attempt
    )
    duration_seconds = time.monotonic() - start
    transient_reason = _transient_reason_for(routing_reason, reach_reason)
    if transient_reason:
        TRANSIENT_RETRIES.append(
            {"tool": tool, "attempt": attempt, "retry": retry, "reason": transient_reason, "db": str(db_path)}
        )
        if retry < TRANSIENT_RETRY_CAP:
            return None
        return _exhausted_result(tool, attempt, code, db_path, duration_seconds, model_config_failed, transient_reason)
    return _finished_result(
        tool,
        attempt,
        code,
        timed_out,
        db_path,
        duration_seconds,
        model_config_failed,
        reach_ok,
        reach_reason,
        routing_ok,
        routing_reason,
        args,
        saw_complete,
    )


def _attempt_crash_result(tool: str, attempt: int, batch_root: Path, start: float, exc: Exception) -> AttemptResult:
    """Fail-closed result when the attempt harness itself raises."""
    elapsed = time.monotonic() - start
    try:
        fallback = str(attempt_dirs(batch_root, tool, attempt)[0])
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        fallback = ""
    return AttemptResult(tool, attempt, False, f"attempt error: {exc}", 124, fallback, elapsed)


def run_verification_attempt(
    tool: str,
    attempt: int,
    base_args: Mapping[str, object],
    batch_root: Path,
    cwd: Path,
    durable: Path,
    repetitions: int,
    *,
    prompt_override: str | None = None,
) -> AttemptResult:
    """Own one Pi attempt end to end: isolated store/DB/fixture, then dual-evaluate. Bounded same-prompt transient retries."""
    start = time.monotonic()
    try:
        for retry in range(TRANSIENT_RETRY_CAP + 1):
            result = _run_single_retry(
                tool, attempt, base_args, batch_root, cwd, durable, prompt_override, retry, start
            )
            if result is not None:
                return result
        return AttemptResult(
            tool,
            attempt,
            False,
            "transient budget exhausted: retry loop fell through",
            124,
            "",
            time.monotonic() - start,
            False,
            False,
            "transient budget exhausted",
            False,
            "transient budget exhausted",
        )
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return _attempt_crash_result(tool, attempt, batch_root, start, exc)


def _discovery_before_dispatch(conn: sqlite3.Connection, expected_tool: str) -> bool:
    try:
        disc = conn.execute(
            "SELECT MIN(started_at) FROM tool_calls WHERE tool_name IN ('browse_tools','search_tools','describe_tool','list_tool_domains') AND error_type IS NULL"
        ).fetchone()[0]
        inner = conn.execute("SELECT MIN(started_at) FROM tool_calls WHERE tool_name = ?", (expected_tool,)).fetchone()[
            0
        ]
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
        row = conn.execute(
            "SELECT MIN(started_at) FROM agent_events WHERE event_type = 'routing_continuation'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _clean_tool_started_at(conn: sqlite3.Connection, name: str) -> str | None:
    """Earliest clean tool_calls timestamp for `name`; None when never cleanly called."""
    try:
        row = conn.execute(
            "SELECT MIN(started_at) FROM tool_calls WHERE tool_name = ? AND error_type IS NULL", (name,)
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _registry_research_names() -> list[str] | None:
    """Registry schema names outside discovery; None when the registry is unreadable."""
    try:
        names = get_registry_sets()["schemas"]
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return [n for n in names if n not in DISCOVERY_TOOLS]


def _research_call_count(conn: sqlite3.Connection) -> int:
    """Every research tool_calls row (clean or errored) outside the discovery set."""
    research_names = _registry_research_names()
    if not research_names:
        return 0
    ph = ",".join("?" for _ in research_names)
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM tool_calls WHERE tool_name IN ({ph})", tuple(research_names)
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


def _final_answer_column(conn: sqlite3.Connection) -> str | None:
    """agent_runs final-answer column (text preferred, hash fallback); None when absent."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
    except sqlite3.Error:
        return None
    if "final_answer" in cols:
        return "final_answer"
    return "final_answer_hash" if "final_answer_hash" in cols else None


def _nonempty_answer_rows(conn: sqlite3.Connection, column: str) -> bool:
    """True iff any agent_runs row has a nonempty value in column."""
    try:
        rows = conn.execute(f"SELECT {column} FROM agent_runs").fetchall()
    except sqlite3.Error:
        return False
    return any(isinstance(r[0], str) and r[0].strip() for r in rows)


def _final_answer_present(conn: sqlite3.Connection) -> bool:
    """True iff agent_runs carries a nonempty final answer (hash when text is not persisted)."""
    column = _final_answer_column(conn)
    if column is None:
        return False
    return _nonempty_answer_rows(conn, column)


def _routing_metrics_event(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Latest routing_metrics metadata payload; None when the request emitted none."""
    try:
        row = conn.execute(
            "SELECT metadata FROM agent_events WHERE event_type = 'routing_metrics' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError, TypeError:
        return None
    return payload if isinstance(payload, dict) else None


def _completion_unsupported(
    conn: sqlite3.Connection, exit_code: int, timed_out: bool, completed_override: bool, continuations: int
) -> tuple[bool, str] | None:
    """Unsupported-request verdict; None only when called with expected_tool set."""
    if continuations != 0:
        return False, "routing failed: unexpected routing continuation for unsupported request"
    # Discovery is telemetry only: unsupported passes on terminal limitation
    # with no successful research, regardless of discovery.
    if _research_call_count(conn) != 0:
        return False, "routing failed: unexpected research tool call(s) for unsupported request"
    hit = _eval_timeout_exit(exit_code, timed_out, completed_override)
    if hit is not None:
        return hit
    if not _final_answer_present(conn):
        return False, "routing failed: empty final answer for unsupported request"
    return True, "pass"


def _completion_target(conn: sqlite3.Connection, expected_tool: str) -> tuple[bool, str] | None:
    """Target-research verdict for supported completion; None on clean target."""
    target_problem = _routing_tool_success(conn, expected_tool, attempt=1)
    if target_problem is None:
        return None
    if target_problem == "absent" and _has_any_tool_calls(conn, expected_tool):
        return False, f"target '{expected_tool}' failed-research-call (errored execution, no clean run)"
    return False, f"target '{expected_tool}' absent ({target_problem})"


def _completion_recovery(conn: sqlite3.Connection, expected_tool: str, continuations: int) -> tuple[bool, str] | None:
    """Continuation-recovery verdict; None when no continuation or it recovered."""
    if continuations != 1:
        return None
    cont_at = _routing_continuation_started_at(conn)
    inner_at = _clean_tool_started_at(conn, expected_tool)
    if not (cont_at and inner_at and cont_at < inner_at):
        return False, "routing failed: routing continuation did not recover to research"
    return None


def _completion_supported(
    conn: sqlite3.Connection,
    expected_tool: str,
    exit_code: int,
    timed_out: bool,
    completed_override: bool,
    continuations: int,
) -> tuple[bool, str]:
    """Supported-request verdict: research + answer (+ at most one recovery)."""
    # Discovery is telemetry only (WARNING); correctness is terminal completion + research + answer.
    if not _discovery_ok(conn, _routing_tool_success, 1):
        print("WARNING: no clean discovery before research (telemetry only)", file=sys.stderr)
    hit = _completion_target(conn, expected_tool)
    if hit is not None:
        return hit
    if not _discovery_before_dispatch(conn, expected_tool):
        print(f"WARNING: discovery did not precede dispatch of '{expected_tool}' (telemetry only)", file=sys.stderr)
    hit = _eval_timeout_exit(exit_code, timed_out, completed_override)
    if hit is not None:
        return hit
    if not _final_answer_present(conn):
        return False, "routing failed: empty final answer after research"
    hit = _completion_recovery(conn, expected_tool, continuations)
    if hit is not None:
        return hit
    return True, "pass"


def _evaluate_completion_conn(
    conn: sqlite3.Connection, expected_tool: str | None, exit_code: int, timed_out: bool, completed_override: bool
) -> tuple[bool, str]:
    """Completion verdicts against an open attempt DB, in pipeline order."""
    continuations = _routing_continuation_count(conn)
    if continuations >= 2:
        return False, "routing failed: multiple routing continuations (expected at most one)"
    if expected_tool is None:
        hit = _completion_unsupported(conn, exit_code, timed_out, completed_override, continuations)
        assert hit is not None
        return hit
    return _completion_supported(conn, expected_tool, exit_code, timed_out, completed_override, continuations)


def evaluate_completion_attempt(
    db_path: Path,
    expected_tool: str | None,
    exit_code: int = 0,
    timed_out: bool = False,
    *,
    completed_override: bool = False,
) -> tuple[bool, str]:
    """Agent-loop completion: discovery -> research -> nonempty answer, with at most one continuation.

    Supported (expected_tool set) passes only on clean discovery before a clean
    expected research tool plus a nonempty final answer, with either no
    continuation or exactly one continuation that recovered to research.
    Unsupported (expected_tool None) passes only on a clean zero-match
    search_tools trace with zero research calls, zero continuations, and a
    nonempty limitation answer. Two continuations always fail.
    """
    missing = _eval_missing_db(db_path, timed_out, completed_override)
    if missing is not None:
        return missing
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return _evaluate_completion_conn(conn, expected_tool, exit_code, timed_out, completed_override)
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
    comp_ok, comp_reason = evaluate_completion_attempt(
        db_path, case["expected_tool"], code, timed_out, completed_override=saw_complete
    )
    disc, resc = _metrics_for_db(db_path, label)
    duration_seconds = time.monotonic() - start
    return AttemptResult(
        label,
        index,
        comp_ok,
        comp_reason if not comp_ok else "pass",
        code,
        str(db_path),
        duration_seconds,
        False,
        None,
        "",
        None,
        "",
        disc,
        resc,
        _direct_count_for_db(db_path, label),
        comp_ok,
        comp_reason,
        _routing_metrics_for_db(db_path),
        terminal_completed=saw_complete,
    )


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
        print(
            f"{mark} {case['prompt']!r} -> {case['expected_tool'] or 'unsupported'} ({r.reason}) [{r.duration_seconds:.1f}s]"
        )
        if not r.ok:
            failed += 1
    wall = time.monotonic() - loop_start
    aggregates = aggregate_results(results)
    loop_verifier = _agg_section(aggregates, "verifier_unrelated_success")
    print(
        f"Agent-loop completion: {len(results) - failed}/{len(results)} cases complete [{wall:.1f}s] | extension unrelated research calls: {aggregates.get('unrelated_research_calls', 0)} | verifier unrelated-success: {loop_verifier.get('attempts', 0)} attempts / {loop_verifier.get('calls', 0)} calls"
    )
    summary = {
        "cases": [
            {
                "prompt": case["prompt"],
                "expected_tool": case["expected_tool"],
                "ok": r.ok,
                "reason": r.reason,
                "completion_ok": r.completion_ok,
                "completion_reason": r.completion_reason,
                "db": r.db,
                "duration_seconds": r.duration_seconds,
                "discovery_calls": r.discovery_calls,
                "research_calls": r.research_calls,
                "direct_tool_calls": r.direct_tool_calls,
                "routing_metrics": r.routing_metrics,
            }
            for r, case in zip(results, AGENT_LOOP_CASES)
        ],
        "aggregates": aggregates,
        "wall_seconds": wall,
    }
    _fc = aggregates.get("failure_category_counts")
    summary["failure_category_counts"] = _fc if isinstance(_fc, dict) else {}
    summary["aggregates"] = aggregates
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    return 0 if failed == 0 else 1


def run_agent_loop(batch_root: Path, cwd: Path) -> list[AttemptResult]:
    """Run every AGENT_LOOP_CASES prompt once; each case is an independent request."""
    return [run_agent_loop_case(case, batch_root, cwd, i + 1) for i, case in enumerate(AGENT_LOOP_CASES)]


def _group_confusion_by_pair(cases: list[ConfusionCase]) -> dict[tuple[str, str], list[ConfusionCase]]:
    """Group directed confusion cases by undirected tool pair."""
    from collections import defaultdict

    by_pair: dict[tuple[str, str], list[ConfusionCase]] = defaultdict(list)
    for c in cases:
        _raw_pair = c.get("pair")
        assert isinstance(_raw_pair, list)
        pair = tuple(sorted(x for x in _raw_pair))
        by_pair[(pair[0], pair[1])].append(c)
    return by_pair


def _confusion_db_evidence(db_path: Path) -> tuple[set[str] | None, list[str]]:
    """(surfaced, dispatched) evidence for one confusion case; defaults on DB errors."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return _surfaced_names(conn), sorted(_call_tool_dispatched_names(conn))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None, []


def _confusion_category(
    result: AttemptResult, db_path: Path, expected: str, base_args: dict[str, object]
) -> RoutingFailureCategory | None:
    """Failure category for a failed confusion case; None on pass or reclasify error."""
    if result.ok:
        return None
    try:
        return classify_routing_failure(db_path, expected, attempt=1, expected_args=base_args)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _run_confusion_case(
    pair: tuple[str, str], c: ConfusionCase, root: Path, cwd: Path, durable: Path
) -> dict[str, object]:
    """Run one directed confusion case; returns its summary record + tally fields."""
    expected = c["expected_tool"]
    base_args = dict(c["arguments"])
    prompt = c["prompt"]
    result = run_verification_attempt(
        expected, 1, base_args, root / f"{pair[0]}-vs-{pair[1]}", cwd, durable, 1, prompt_override=prompt
    )
    db_path = Path(result.db) if result.db else root / expected / "attempt-1" / "runs.sqlite"
    # Selection accuracy: discovery/selection/dispatch/arguments correct even on EXECUTION_FAILURE.
    cat = _confusion_category(result, db_path, expected, base_args)
    sel_ok = result.ok or cat == RoutingFailureCategory.EXECUTION_FAILURE
    surfaced, selected = _confusion_db_evidence(db_path)
    print(f"{'PASS' if sel_ok else 'FAIL'}[confusion] {prompt!r} -> {expected} (cat={cat.value if cat else 'none'})")
    return {
        "pair": list(pair),
        "expected_tool": expected,
        "prompt": prompt,
        "selection_ok": sel_ok,
        "failure_category": cat.value if cat else None,
        "surfaced": sorted(surfaced) if surfaced is not None else None,
        "dispatched": selected,
        "db": str(db_path),
    }


def _run_confusion_pair(
    pair: tuple[str, str],
    cases: list[ConfusionCase],
    root: Path,
    cwd: Path,
    durable: Path,
    summary_cases: list[dict[str, object]],
    cat_totals: dict[str, int],
) -> tuple[int, int]:
    """Run every directed case for one pair; returns (pair_ok, pair_total)."""
    pair_ok = pair_total = 0
    for c in cases:
        rec = _run_confusion_case(pair, c, root, cwd, durable)
        summary_cases.append(rec)
        pair_total += 1
        if rec["selection_ok"]:
            pair_ok += 1
        if rec["failure_category"] is not None:
            cat_totals[str(rec["failure_category"])] += 1
    return pair_ok, pair_total


def _print_confusion_report(pair_lines: list[str], correct: int, total: int, cat_totals: dict[str, int]) -> None:
    """Pair-accuracy + category report for the confusion benchmark."""
    print("metadata/conflict consistency pair selection accuracy:")
    for line in pair_lines:
        print(f"  {line}")
    print(f"metadata/conflict consistency selection: {correct}/{total}")
    print("metadata/conflict consistency categories: " + ", ".join(f"{k}={v}" for k, v in sorted(cat_totals.items())))


def _write_confusion_summary(
    root: Path,
    summary_cases: list[dict[str, object]],
    pair_lines: list[str],
    correct: int,
    total: int,
    cat_totals: dict[str, int],
) -> None:
    """Summary.json for the confusion benchmark."""
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(
        json.dumps(
            {
                "cases": summary_cases,
                "pair_accuracy": pair_lines,
                "selection": {"correct": correct, "total": total},
                "categories": cat_totals,
            },
            indent=2,
        )
    )


def run_confusion() -> int:
    """Generated confusion benchmark: two directed cases per undirected conflict edge."""
    try:
        cases = generate_confusion_cases()
    except ValueError as exc:
        print(f"confusion preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"confusion cases: {len(cases)} directed ({len(cases) // 2} undirected edges)")
    batch = _batch_id()
    root = Path("data/verify") / batch / "confusion"
    cwd = Path.cwd()
    durable = get_data_root()
    # group by undirected pair for per-pair accuracy
    by_pair = _group_confusion_by_pair(cases)
    total = correct = 0
    cat_totals: dict[str, int] = {c.value: 0 for c in RoutingFailureCategory}
    pair_lines: list[str] = []
    summary_cases: list[dict[str, object]] = []
    for pair in sorted(by_pair):
        pair_ok, pair_total = _run_confusion_pair(pair, by_pair[pair], root, cwd, durable, summary_cases, cat_totals)
        total += pair_total
        correct += pair_ok
        pair_lines.append(f"{pair[0]} vs {pair[1]}: {pair_ok}/{pair_total}")
    _print_confusion_report(pair_lines, correct, total, cat_totals)
    _write_confusion_summary(root, summary_cases, pair_lines, correct, total, cat_totals)
    return 0 if correct == total else 1


def _read_holdout_cases(holdout_path: str) -> list[object] | None:
    """Verified holdout case list; None after printing the failure reason."""
    try:
        blob = Path(holdout_path).read_bytes()
    except OSError as exc:
        print(f"holdout read failed: {exc}", file=sys.stderr)
        return None
    digest = hashlib.sha256(blob).hexdigest()
    print(f"holdout sha256: {digest}")
    if digest != HOLDOUT_SHA256:
        print(f"holdout hash mismatch: expected {HOLDOUT_SHA256}, got {digest}", file=sys.stderr)
        return None
    try:
        raw = json.loads(blob.decode())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"holdout read failed: {exc}", file=sys.stderr)
        return None
    if not isinstance(raw, list) or not raw:
        print(f"holdout {holdout_path} must be a non-empty list", file=sys.stderr)
        return None
    return raw


def _holdout_verdicts(result: AttemptResult, db_path: Path, tool: str) -> tuple[bool, str, bool, str]:
    """(reach_ok, reach_reason, routing_ok, routing_reason) for one holdout case."""
    if result.reach_ok is None or result.routing_ok is None:
        holdout_reach_ok, holdout_reach_reason = evaluate_holdout_reachability_attempt(db_path, tool)
        holdout_routing_ok, holdout_routing_reason = evaluate_holdout_attempt(db_path, tool)
        return holdout_reach_ok, holdout_reach_reason, holdout_routing_ok, holdout_routing_reason
    return result.reach_ok, result.reach_reason, result.routing_ok, result.routing_reason


def _holdout_lookup_args(
    case: dict[str, object], schemas: dict[str, dict[str, object]]
) -> tuple[str, str, dict[str, object]] | None:
    """(tool, prompt, base_args) resolving fixtures; None after printing LookupError."""
    tool = str(case["expected_tool"])
    try:
        base_args = (
            dict(case["arguments"]) if isinstance(case.get("arguments"), dict) else resolve_arguments(tool, schemas)
        )
    except LookupError as exc:
        print(f"FAIL {case['prompt']!r}: {exc}", file=sys.stderr)
        return None
    return tool, str(case["prompt"]), base_args


def _run_holdout_case(
    case: object, index: int, schemas: dict[str, dict[str, object]], root: Path, cwd: Path, durable: Path
) -> tuple[int, int]:
    """Run one holdout case; returns (reach_failed, routing_failed) as 0/1."""
    if (
        not isinstance(case, dict)
        or not isinstance(case.get("prompt"), str)
        or not isinstance(case.get("expected_tool"), str)
    ):
        print(f"FAIL case {index}: needs string prompt + expected_tool", file=sys.stderr)
        return 1, 1
    parsed = _holdout_lookup_args(case, schemas)
    if parsed is None:
        return 1, 1
    tool, prompt, base_args = parsed
    result = run_verification_attempt(tool, 1, base_args, root, cwd, durable, 1, prompt_override=prompt)
    db_path = Path(result.db) if result.db else root / tool / "attempt-1" / "runs.sqlite"
    holdout_reach_ok, holdout_reach_reason, holdout_routing_ok, holdout_routing_reason = _holdout_verdicts(
        result, db_path, tool
    )
    _report_holdout_case(
        prompt,
        tool,
        holdout_reach_ok,
        holdout_reach_reason,
        holdout_routing_ok,
        holdout_routing_reason,
        result.duration_seconds,
    )
    return (0 if holdout_reach_ok else 1), (0 if holdout_routing_ok else 1)


def _report_holdout_case(
    prompt: str, tool: str, reach_ok: bool, reach_reason: str, routing_ok: bool, routing_reason: str, duration: float
) -> None:
    """One-line dual verdict report for a holdout case."""
    print(
        f"{'PASS' if reach_ok else 'FAIL'}[reach] {'PASS' if routing_ok else 'FAIL'}[route] {prompt!r} -> {tool} (reach: {reach_reason}; route: {routing_reason}) [{duration:.1f}s]"
    )


def run_holdout(holdout_path: str) -> int:
    """Frozen 20-prompt holdout, dual-reported: reachability + routing precision."""
    raw = _read_holdout_cases(holdout_path)
    if raw is None:
        return 1
    schemas = tool_schemas()
    batch = _batch_id()
    root = Path("data/verify") / batch / "holdout"
    cwd = Path.cwd()
    durable = get_data_root()
    failed_reach = failed_routing = 0
    for i, case in enumerate(raw):
        reach_failed, routing_failed = _run_holdout_case(case, i, schemas, root, cwd, durable)
        failed_reach += reach_failed
        failed_routing += routing_failed
    print(f"holdout reachability: {len(raw) - failed_reach}/{len(raw)} prompts discover-then-dispatch cleanly")
    print(f"holdout routing precision: {len(raw) - failed_routing}/{len(raw)} prompts dispatch intentionally")
    return 0 if (failed_reach == 0 and failed_routing == 0) else 1


def _build_parser() -> argparse.ArgumentParser:
    """CLI parser for the verify entrypoint (modes + single-tool debug)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default=None, help="verify one tool only (debug mode)")
    parser.add_argument(
        "--routing",
        action="store_true",
        help="run live exact-routing benchmark only (opt-in benchmark, demoted from default gate)",
    )
    parser.add_argument(
        "--holdout",
        nargs="?",
        const="evals/holdout_discovery.json",
        default=None,
        help="run the discovery holdout only (no live matrix): each prompt must browse-or-search then call_tool its expected tool",
    )
    parser.add_argument(
        "--agent-loop",
        action="store_true",
        help="run the natural agent-loop cases only (no live matrix): discovery -> research -> answer per AGENT_LOOP_CASES",
    )
    parser.add_argument(
        "--confusion", action="store_true", help="run generated confusion benchmark only (no live matrix)"
    )
    return parser


def _parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """Parse CLI args with the shared verify parser."""
    return parser.parse_args()


def _dispatch_mode(args: argparse.Namespace) -> int | None:
    """Benchmark-mode dispatch; None when the live matrix path continues."""
    if args.confusion:
        return run_confusion()
    if args.holdout is not None:
        return run_holdout(args.holdout)
    if args.agent_loop:
        return run_agent_loop_main()
    return None


def _require_matrix_mode(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int | None:
    """Usage gate for the live matrix; None when matrix mode is selected."""
    if args.tool is None and not args.routing:
        parser.print_usage(sys.stderr)
        print(
            "live exact-routing matrix is opt-in: use --routing for full benchmark, --tool <name> for single-tool exact-dispatch debug, --confusion/--holdout for benchmarks, or scripts/verify_judge.py for outcome suite",
            file=sys.stderr,
        )
        return 2
    return None


def _debug_repetitions(debug: bool) -> int | None:
    """Repetition count; None after printing the validation failure."""
    repetitions = int(os.getenv("PI_VERIFY_REPETITIONS", str(DEFAULT_REPETITIONS))) if debug else DEFAULT_REPETITIONS
    if repetitions < 1:
        print("PI_VERIFY_REPETITIONS must be >= 1", file=sys.stderr)
        return None
    return repetitions


def _get_concurrency_or_report() -> int | None:
    """Concurrency or None after printing the validation failure."""
    try:
        return get_concurrency()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _discover_or_report() -> tuple[dict[str, object], dict[str, object]] | None:
    """(describe, doctor) payloads; None after printing discovery failure."""
    try:
        describe, doctor = discover()
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        print(f"discovery failed: {exc}", file=sys.stderr)
        return None
    err = check_discovery(describe, doctor)
    if err:
        print(f"discovery failed: {err}", file=sys.stderr)
        return None
    return describe, doctor


def _describe_names(describe: dict[str, object]) -> list[str]:
    """Sorted describe-visible tool names."""
    raw_tools = describe.get("tools")
    return (
        sorted(tool_schema_name(t) for t in raw_tools if isinstance(t, Mapping)) if isinstance(raw_tools, list) else []
    )


def _select_single_tool(tool: str, describe_names: list[str]) -> list[str] | None:
    """Single-tool debug selection; None after printing the failure."""
    if tool not in describe_names:
        print(f"unknown tool for --tool: {tool}", file=sys.stderr)
        return None
    if tool in _MATRIX_EXCLUDED:
        print(f"tool '{tool}' is not a live-matrix target", file=sys.stderr)
        return None
    return [tool]


def _select_matrix_tools(args: argparse.Namespace, describe_names: list[str]) -> list[str] | None:
    """Matrix tool list; None after printing the selection failure."""
    if args.tool is not None:
        return _select_single_tool(args.tool, describe_names)
    return [t for t in describe_names if t not in _MATRIX_EXCLUDED]


def _resolve_matrix_args(tool_names: list[str]) -> dict[str, dict[str, object]] | None:
    """Fixture args per matrix tool; None after printing the failure."""
    schemas = tool_schemas()
    try:
        return {t: resolve_arguments(t, schemas) for t in tool_names}
    except LookupError as exc:
        print(f"{exc}", file=sys.stderr)
        return None


def _matrix_context(tool_names: list[str]) -> tuple[Path, Path, Path] | int:
    """(batch root, cwd, durable) or 1 after a FINRA fixture failure."""
    batch = _batch_id()
    root = Path("data/verify") / batch
    cwd = Path.cwd()
    # Contain live side effects (e.g. thesis_create) in per-attempt dirs; never
    # the operator's durable store. Workers set STOCKBOT_DATA_DIR per attempt.
    durable = get_data_root()
    if ensure_finra_fixture(durable, tool_names):
        return 1
    return root, cwd, durable


def _run_matrix_jobs(
    tool_names: list[str],
    repetitions: int,
    case_args: dict[str, dict[str, object]],
    root: Path,
    cwd: Path,
    durable: Path,
    concurrency: int,
) -> list[AttemptResult]:
    """Execute every (tool, attempt) job with bounded parallelism."""
    jobs = expand_jobs(tool_names, repetitions)
    del TRANSIENT_RETRIES[:]

    def _worker(t: str, n: int) -> AttemptResult:
        return run_verification_attempt(t, n, case_args[t], root, cwd, durable, repetitions)

    return run_matrix(jobs, _worker, concurrency)


def _attempt_record(r: AttemptResult) -> tuple[dict[str, object], bool, bool]:
    """(record dict, route_passed, reach_passed) for one ordered attempt."""
    r_reach = r.reach_ok if r.reach_ok is not None else r.ok
    r_route = r.routing_ok if r.routing_ok is not None else r.ok
    rec: dict[str, object] = {
        "attempt": r.attempt,
        "ok": r.ok,
        "reason": r.reason,
        "reach_ok": r_reach,
        "reach_reason": r.reach_reason,
        "routing_ok": r_route,
        "routing_reason": r.routing_reason,
        "exit": r.exit,
        "db": r.db,
        "duration_seconds": r.duration_seconds,
        "discovery_calls": r.discovery_calls,
        "research_calls": r.research_calls,
        "direct_tool_calls": r.direct_tool_calls,
        "completion_ok": r.completion_ok,
        "completion_reason": r.completion_reason,
        "routing_metrics": r.routing_metrics,
        "failure_category": r.failure_category.value if r.failure_category else None,
    }
    if not r.ok:
        rec["searchQueries"] = _search_queries_for_db(r.db)
    return rec, bool(r_route), bool(r_reach)


def _completion_mark(r: AttemptResult) -> str:
    """PASS/FAIL/n/a mark for the completion verdict."""
    if r.completion_ok is None:
        return "n/a"
    return "PASS" if r.completion_ok else "FAIL"


def _report_attempt(r: AttemptResult, repetitions: int) -> None:
    """One-line per-attempt routing/reachability/completion report."""
    r_reach = r.reach_ok if r.reach_ok is not None else r.ok
    r_route = r.routing_ok if r.routing_ok is not None else r.ok
    print(
        f"{r.tool} attempt {r.attempt}/{repetitions}: routing {'PASS' if r_route else 'FAIL'} ({r.routing_reason or r.reason}) | reachability {'PASS' if r_reach else 'FAIL'} ({r.reach_reason or r.reason}) | completion {_completion_mark(r)} ({r.completion_reason}) [direct={r.direct_tool_calls} discovery={r.discovery_calls} research={r.research_calls}] [{r.duration_seconds:.1f}s]"
    )
    if r.model_config_failed:
        print("PI MODEL CONFIGURATION FAILED", file=sys.stderr)


def _collect_matrix_results(
    ordered: list[AttemptResult], repetitions: int
) -> tuple[dict[str, list[dict[str, object]]], int, int, int]:
    """(per-tool records, total, passed_routing, passed_reach) with per-attempt report."""
    results: dict[str, list[dict[str, object]]] = {}
    total = passed_routing = passed_reach = 0
    for r in ordered:
        rec, route_ok, reach_ok = _attempt_record(r)
        results.setdefault(r.tool, []).append(rec)
        total += 1
        passed_routing += 1 if route_ok else 0
        passed_reach += 1 if reach_ok else 0
        _report_attempt(r, repetitions)
    return results, total, passed_routing, passed_reach


def _tool_verdicts(tool_recs: list[dict[str, object]]) -> tuple[bool, bool]:
    """(reach_pass, route_pass) 3/3 verdicts for one tool's attempt records."""
    reach_pass = evaluate_reachability_tool([{"reach_ok": rec.get("reach_ok", rec.get("ok"))} for rec in tool_recs])
    route_pass = evaluate_routing_tool([{"routing_ok": rec.get("routing_ok", rec.get("ok"))} for rec in tool_recs])
    return reach_pass, route_pass


def _failed_attempts(tool: str, ordered: list[AttemptResult]) -> list[AttemptResult]:
    """Failed attempts for one matrix tool."""
    return [r for r in ordered if r.tool == tool and not r.ok]


def _all_infra(by_tool: list[AttemptResult]) -> bool:
    """True iff every failure is infra/config (never an explicit routing failure)."""
    return not any(is_routing_failure(r.reason) for r in by_tool) and all(
        r.model_config_failed or is_infra_failure(r.reason) for r in by_tool
    )


def _report_infra_only(tool: str, ordered: list[AttemptResult]) -> None:
    """Infra-only notice for a failed tool whose failures are all infra."""
    by_tool = _failed_attempts(tool, ordered)
    if by_tool and _all_infra(by_tool):
        print(f"infra-only failure for {tool} (still FAIL)")


def _sweep_one_tool(
    tool: str, tool_recs: list[dict[str, object]], ordered: list[AttemptResult], root: Path
) -> tuple[bool, bool]:
    """(reach_failed, routing_failed) for one tool; prunes DBs on full pass."""
    reach_pass, route_pass = _tool_verdicts(tool_recs)
    if reach_pass and route_pass:
        remove_successful_attempt_dirs(root, tool, tool_recs)
        return False, False
    print(f"preserved DBs for {tool}: {root / tool}")
    _report_infra_only(tool, ordered)
    return not reach_pass, not route_pass


def _sweep_matrix_tools(
    tool_names: list[str], results: dict[str, list[dict[str, object]]], ordered: list[AttemptResult], root: Path
) -> tuple[list[str], list[str]]:
    """(failed_routing_tools, failed_reach_tools) after per-tool sweep."""
    failed_routing_tools: list[str] = []
    failed_reach_tools: list[str] = []
    for tool in tool_names:
        reach_failed, route_failed = _sweep_one_tool(tool, results.get(tool, []), ordered, root)
        if reach_failed:
            failed_reach_tools.append(tool)
        if route_failed:
            failed_routing_tools.append(tool)
    return failed_routing_tools, failed_reach_tools


def _print_matrix_aggregates(aggregates: dict[str, object], concurrency: int, wall: float) -> None:
    """Aggregate report lines for the live matrix."""
    conv = _agg_section(aggregates, "discovery_to_research_conversion")
    prem = _agg_section(aggregates, "premature_stop_rate")
    recov = _agg_section(aggregates, "continuation_recovery")
    comp = _agg_section(aggregates, "completion")
    verifier_unrelated = _agg_section(aggregates, "verifier_unrelated_success")
    print(
        f"Discovery-to-research conversion: {conv.get('with_research', 0)}/{conv.get('with_discovery', 0)} ({_agg_float(conv, 'rate'):.0%}) | premature stops: {prem.get('premature', 0)}/{prem.get('total', 0)} ({_agg_float(prem, 'rate'):.0%}) | continuation recovery: {recov.get('recovered', 0)}/{recov.get('injected', 0)} ({_agg_float(recov, 'rate'):.0%})"
    )
    print(
        f"Median discovery calls: {_agg_float(aggregates, 'median_discovery_calls'):.1f} | median research calls: {_agg_float(aggregates, 'median_research_calls'):.1f} | extension unrelated research calls: {aggregates.get('unrelated_research_calls', 0)} | verifier unrelated-success: {verifier_unrelated.get('attempts', 0)} attempts / {verifier_unrelated.get('calls', 0)} calls | completion: {comp.get('passed', 0)}/{comp.get('evaluated', 0)}"
    )
    _raw_cats = aggregates.get("failure_category_counts")
    cats: dict[str, int] = dict(_raw_cats) if isinstance(_raw_cats, dict) else {}
    if cats:
        print("Failure categories: " + ", ".join(f"{k}={cats.get(k, 0)}" for k in sorted(cats)))
    else:
        print("Failure categories: none")
    print(f"Concurrency: {concurrency} | Wall time: {wall:.1f}s")


def _report_matrix_outcome(
    tool_names: list[str],
    ordered: list[AttemptResult],
    total: int,
    passed_routing: int,
    passed_reach: int,
    failed_routing_tools: list[str],
    failed_reach_tools: list[str],
    aggregates: dict[str, object],
    concurrency: int,
    verify_start: float,
    sha: str,
    repetitions: int,
) -> None:
    """Pass/fail + aggregate report lines for the live matrix."""
    passed_reach_tools = len(tool_names) - len(failed_reach_tools)
    passed_routing_tools = len(tool_names) - len(failed_routing_tools)
    wall = time.monotonic() - verify_start
    print(f"git: {sha} | tools {len(tool_names)} x {repetitions} = {total}")
    print(f"Reachability: {passed_reach_tools}/{len(tool_names)} tools 3/3")
    print(f"Routing precision: {passed_routing_tools}/{len(tool_names)} tools 3/3")
    print(
        f"Coverage: {passed_routing_tools}/{len(tool_names)} tools | processes: {len(ordered)} | routing passed: {passed_routing}/{total} | reachability passed: {passed_reach}/{total}"
    )
    _print_matrix_aggregates(aggregates, concurrency, wall)


def _matrix_summary(
    tool_names: list[str],
    repetitions: int,
    results: dict[str, list[dict[str, object]]],
    failed_reach_tools: list[str],
    failed_routing_tools: list[str],
    aggregates: dict[str, object],
    sha: str,
) -> dict[str, object]:
    """Summary.json payload for the live matrix run."""
    passed_reach_tools = len(tool_names) - len(failed_reach_tools)
    passed_routing_tools = len(tool_names) - len(failed_routing_tools)
    return {
        "git_sha": sha,
        "tool_count": len(tool_names),
        "repetitions": repetitions,
        "results": results,
        "reachability": {
            "passed_tools": passed_reach_tools,
            "tool_count": len(tool_names),
            "failed_tools": failed_reach_tools,
        },
        "routing_precision": {
            "passed_tools": passed_routing_tools,
            "tool_count": len(tool_names),
            "failed_tools": failed_routing_tools,
        },
        "aggregates": aggregates,
    }


def _report_matrix_result(failed_reach_tools: list[str], failed_routing_tools: list[str]) -> tuple[list[str], int]:
    """RESULT report lines; returns (failed_tools, loop_failed)."""
    loop_failed = 0
    print("Agent-loop completion: skipped (moved to scripts/verify_judge.py)")
    failed_tools = sorted(set(failed_reach_tools) | set(failed_routing_tools))
    print(f"RESULT: {'PASS' if not failed_tools and not loop_failed else 'FAIL'}")
    if failed_tools:
        print(f"failed tools: {failed_tools}")
    return failed_tools, loop_failed


def _loop_case_record(r: AttemptResult, case: _AgentLoopCase) -> dict[str, object]:
    """Summary record for one (skipped) agent-loop case."""
    return {
        "prompt": case["prompt"],
        "expected_tool": case["expected_tool"],
        "ok": r.ok,
        "reason": r.reason,
        "db": r.db,
        "completion_ok": r.completion_ok,
        "completion_reason": r.completion_reason,
        "discovery_calls": r.discovery_calls,
        "research_calls": r.research_calls,
        "direct_tool_calls": r.direct_tool_calls,
        "routing_metrics": r.routing_metrics,
    }


def _failure_counts_or_empty(aggregates: dict[str, object]) -> dict[str, object]:
    """Failure-category counts narrowed; empty mapping when absent."""
    counts = aggregates.get("failure_category_counts")
    if isinstance(counts, dict):
        return {str(k): v for k, v in counts.items()}
    empty: dict[str, object] = {}
    return empty


def _extend_matrix_summary(
    summary: dict[str, object],
    aggregates: dict[str, object],
    loop_results: list[AttemptResult],
    loop_failed: int,
    concurrency: int,
    wall: float,
) -> None:
    """Agent-loop + retry + wall-clock fields on the matrix summary (in place)."""
    summary["agent_loop"] = {
        "passed": len(loop_results) - loop_failed,
        "total": len(loop_results),
        "failed": loop_failed,
        "cases": [_loop_case_record(r, case) for r, case in zip(loop_results, AGENT_LOOP_CASES)],
    }
    summary["failure_category_counts"] = _failure_counts_or_empty(aggregates)
    summary["aggregates"] = aggregates
    summary["transient_retries"] = list(TRANSIENT_RETRIES)
    summary["concurrency"] = concurrency
    summary["wall_seconds"] = wall


def _write_matrix_summary(root: Path, summary: dict[str, object]) -> None:
    """Write summary.json for the live matrix run."""
    (root / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "summary.json").write_text(json.dumps(summary, indent=2))


def _setup_matrix(
    args: argparse.Namespace, concurrency: int, repetitions: int
) -> tuple[str, list[str], dict[str, dict[str, object]], Path, Path, Path] | int:
    """Discovery + selection + fixtures for the live matrix; int exit code on failure."""
    sha = git_sha()
    print(f"Extension: {EXTENSION} (Pi configured default model)")
    discovered = _discover_or_report()
    if discovered is None:
        return 1
    describe, _doctor = discovered
    describe_names = _describe_names(describe)
    tool_names = _select_matrix_tools(args, describe_names)
    if tool_names is None:
        return 1
    pre = check_pre_pi(describe_names)
    if pre:
        print(f"pre-Pi parity failed: {pre}", file=sys.stderr)
        return 1
    case_args = _resolve_matrix_args(tool_names)
    if case_args is None:
        return 1
    print(f"Pi verification concurrency: {concurrency}")
    print(f"Tools: {len(tool_names)}")
    print(f"Attempts per tool: {repetitions}")
    print(f"Total Pi attempts: {len(tool_names) * repetitions}")
    ctx = _matrix_context(tool_names)
    if ctx == 1:
        return 1
    assert not isinstance(ctx, int)
    root, cwd, durable = ctx
    return sha, tool_names, case_args, root, cwd, durable


def _pre_setup() -> tuple[argparse.Namespace, int, int] | int:
    """Parse + mode-gate + repetition/concurrency setup; int exit code on early exit."""
    parser = _build_parser()
    args = _parse_args(parser)
    mode_result = _dispatch_mode(args)
    if mode_result is not None:
        return mode_result
    gated = _require_matrix_mode(parser, args)
    if gated is not None:
        return gated
    debug = args.tool is not None
    if debug:
        print("DEBUG MODE — partial verification")
    repetitions = _debug_repetitions(debug)
    if repetitions is None:
        return 1
    concurrency = _get_concurrency_or_report()
    if concurrency is None:
        return 1
    return args, concurrency, repetitions


def _finalize_matrix(
    tool_names: list[str],
    repetitions: int,
    results: dict[str, list[dict[str, object]]],
    failed_reach_tools: list[str],
    failed_routing_tools: list[str],
    aggregates: dict[str, object],
    sha: str,
    concurrency: int,
    verify_start: float,
    root: Path,
) -> int:
    """Result report + summary.json for the live matrix; returns the exit code."""
    loop_results: list[AttemptResult] = []
    failed_tools, loop_failed = _report_matrix_result(failed_reach_tools, failed_routing_tools)
    wall = time.monotonic() - verify_start
    summary = _matrix_summary(
        tool_names, repetitions, results, failed_reach_tools, failed_routing_tools, aggregates, sha
    )
    _extend_matrix_summary(summary, aggregates, loop_results, loop_failed, concurrency, wall)
    _write_matrix_summary(root, summary)
    return 0 if not failed_tools and not loop_failed else 1


def main() -> int:
    pre = _pre_setup()
    if isinstance(pre, int):
        return pre
    args, concurrency, repetitions = pre
    setup = _setup_matrix(args, concurrency, repetitions)
    if isinstance(setup, int):
        return setup
    sha, tool_names, case_args, root, cwd, durable = setup
    verify_start = time.monotonic()
    ordered = _run_matrix_jobs(tool_names, repetitions, case_args, root, cwd, durable, concurrency)
    results, total, passed_routing, passed_reach = _collect_matrix_results(ordered, repetitions)
    failed_routing_tools, failed_reach_tools = _sweep_matrix_tools(tool_names, results, ordered, root)
    aggregates = aggregate_results(ordered)
    _report_matrix_outcome(
        tool_names,
        ordered,
        total,
        passed_routing,
        passed_reach,
        failed_routing_tools,
        failed_reach_tools,
        aggregates,
        concurrency,
        verify_start,
        sha,
        repetitions,
    )
    return _finalize_matrix(
        tool_names,
        repetitions,
        results,
        failed_reach_tools,
        failed_routing_tools,
        aggregates,
        sha,
        concurrency,
        verify_start,
        root,
    )


if __name__ == "__main__":
    sys.exit(main())
