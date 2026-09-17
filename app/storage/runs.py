"""SQLite run/event/tool/model observability store. stdlib only.

One RunRecorder per Pi run appends rows to agent_runs/agent_events/
tool_calls/model_calls under data/runs.sqlite (or $RUNS_DB_PATH).
Pi emits model telemetry through scripts/pi_bridge.py; there are no nested
Python completions. Observability must never break research: every recorder
method swallows its own errors, disables the recorder, and logs a warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from ..config import get_data_root
from ..redact import redact_json, redact_text
from ..runtime import EventType

logger = logging.getLogger(__name__)

# Default data root when the recorder is not given an explicit one.
DEFAULT_DATA_ROOT = get_data_root()

# Approximate USD per 1M input/output tokens (as of 2025-06); used only when
# the provider does not report usage.cost.
MODEL_COST_PER_1M = {
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-pro": (1.25, 10.00),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, started_at TEXT NOT NULL,
  completed_at TEXT, duration_ms REAL, status TEXT, question TEXT NOT NULL,
  model_provider TEXT, model_name TEXT, model_parameters TEXT,
  agent_version TEXT, prompt_version TEXT, tool_registry_version TEXT, git_sha TEXT,
  as_of TEXT, round_count INTEGER, model_call_count INTEGER, tool_call_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
  estimated_model_cost REAL, estimated_total_cost REAL,
  final_answer_hash TEXT, error_type TEXT, error_message TEXT);
CREATE TABLE IF NOT EXISTS agent_events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
  duration_ms REAL, round INTEGER, model TEXT, tool_name TEXT, arguments TEXT,
  result_summary TEXT, success INTEGER, error_type TEXT, evidence_ids TEXT, metadata TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (
  tool_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, round INTEGER,
  tool_name TEXT NOT NULL, tool_version TEXT, arguments_json TEXT,
  started_at TEXT NOT NULL, completed_at TEXT, duration_ms REAL, status TEXT,
  result_row_count INTEGER, returned_count INTEGER, truncated INTEGER,
  result_bytes INTEGER, result_hash TEXT,
    source_names TEXT, source_freshness TEXT, as_of TEXT, error_type TEXT, error_message TEXT,
  protocol_id TEXT, bridge_queue_ms REAL, handler_ms REAL, cache_hit INTEGER, cache_type TEXT);
CREATE TABLE IF NOT EXISTS model_calls (
  model_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, round INTEGER,
  provider TEXT NOT NULL, model TEXT NOT NULL, started_at TEXT NOT NULL,
  completed_at TEXT, duration_ms REAL, input_tokens INTEGER, output_tokens INTEGER,
  reasoning_tokens INTEGER, cached_tokens INTEGER, estimated_cost REAL,
    finish_reason TEXT, tool_call_count INTEGER, provider_request_id TEXT,
  status TEXT NOT NULL DEFAULT 'completed', error_type TEXT, error_category TEXT);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
  round INTEGER, tool_name TEXT, rendered_hash TEXT NOT NULL,
  rendered_bytes INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL,
    source_names TEXT, source_freshness TEXT, as_of TEXT, rendered_text TEXT);
CREATE TABLE IF NOT EXISTS security_events (
  event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  created_at TEXT NOT NULL, source TEXT, sha256 TEXT, score INTEGER, verdict TEXT,
  rule_ids TEXT, decision TEXT NOT NULL, reason TEXT, span_length INTEGER);
CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_run ON model_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_security_events_run ON security_events(run_id, sequence);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _duration_ms(started_at: str, completed_at: str) -> float:
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(completed_at)
    return (end - start).total_seconds() * 1000.0


def _usage_int(value: object) -> int:
    """Narrow a provider usage counter to int; raises on malformed."""
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        return int(value)
    raise TypeError(f"malformed usage counter: {value!r}")


def get_runs_db_path(data_root: Path) -> Path:
    """Resolve the runs DB path: $RUNS_DB_PATH wins, else data_root/runs.sqlite."""
    env = os.environ.get("RUNS_DB_PATH")
    if env:
        return Path(env)
    return data_root / "runs.sqlite"


class RunRecorder:
    """Records one agent run's rows. Degrades to a no-op on any failure."""

    def __init__(
        self,
        *,
        run_id: str,
        request_id: str,
        question: str,
        as_of: str | None,
        model: str,
        provider: str,
        model_parameters: dict[str, object],
        agent_version: str,
        prompt_version: str,
        tool_registry_version: str,
        git_sha: str,
        data_root: Path | None = None,
        max_result_bytes: int = 64 * 1024,
    ) -> None:
        self.run_id = run_id
        self.request_id = request_id
        self.question = question
        self.as_of = as_of
        self.model = model
        self.provider = provider
        self.model_parameters = model_parameters
        self.agent_version = agent_version
        self.prompt_version = prompt_version
        self.tool_registry_version = tool_registry_version
        self.git_sha = git_sha
        self._data_root = Path(data_root) if data_root else get_data_root()
        self.max_result_bytes = max_result_bytes
        self.enabled = False
        self._conn: sqlite3.Connection | None = None
        self._warned = False
        self.started_at: str | None = None
        # Live counters exposed to the loop.
        self.current_round = 0
        self.model_calls = 0
        self.evidence_tokens = 0
        # Accumulators for the agent_runs summary row.
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._estimated_model_cost = 0.0
        self._max_round = 0
        self._tool_seq = 0
        self._evidence_seq = 0
        # ponytail: single RLock serializes sequence/counter/DB mutations;
        # per-table locks if bridge throughput ever contends here.
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)

    @classmethod
    def _migrate_tool_calls(cls, conn: sqlite3.Connection) -> None:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
        for column, ddl in (
            ("returned_count", "ALTER TABLE tool_calls ADD COLUMN returned_count INTEGER"),
            ("truncated", "ALTER TABLE tool_calls ADD COLUMN truncated INTEGER"),
            ("as_of", "ALTER TABLE tool_calls ADD COLUMN as_of TEXT"),
            ("protocol_id", "ALTER TABLE tool_calls ADD COLUMN protocol_id TEXT"),
            ("bridge_queue_ms", "ALTER TABLE tool_calls ADD COLUMN bridge_queue_ms REAL"),
            ("handler_ms", "ALTER TABLE tool_calls ADD COLUMN handler_ms REAL"),
            ("cache_hit", "ALTER TABLE tool_calls ADD COLUMN cache_hit INTEGER"),
            ("cache_type", "ALTER TABLE tool_calls ADD COLUMN cache_type TEXT"),
        ):
            if column not in cols:
                conn.execute(ddl)

    @classmethod
    def _migrate_model_calls(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(
            conn,
            "model_calls",
            "status",
            "ALTER TABLE model_calls ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
        )
        cls._ensure_column(conn, "model_calls", "error_type", "ALTER TABLE model_calls ADD COLUMN error_type TEXT")
        cls._ensure_column(
            conn,
            "model_calls",
            "error_category",
            "ALTER TABLE model_calls ADD COLUMN error_category TEXT",
        )

    @classmethod
    def _migrate_evidence_security(cls, conn: sqlite3.Connection) -> None:
        cls._ensure_column(conn, "evidence", "as_of", "ALTER TABLE evidence ADD COLUMN as_of TEXT")
        cls._ensure_column(
            conn,
            "security_events",
            "span_length",
            "ALTER TABLE security_events ADD COLUMN span_length INTEGER",
        )

    @classmethod
    def _migrate_schema(cls, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        cls._migrate_tool_calls(conn)
        cls._migrate_model_calls(conn)
        cls._migrate_evidence_security(conn)
        conn.commit()

    def _insert_run_row(self, conn: sqlite3.Connection) -> None:
        self.started_at = _now()
        conn.execute(
            "INSERT INTO agent_runs (run_id, request_id, started_at, question,"
            " model_provider, model_name, model_parameters, agent_version,"
            " prompt_version, tool_registry_version, git_sha, as_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                self.request_id,
                self.started_at,
                redact_json(self.question),
                self.provider,
                self.model,
                json.dumps(self.model_parameters),
                self.agent_version,
                self.prompt_version,
                self.tool_registry_version,
                self.git_sha,
                self.as_of,
            ),
        )
        conn.commit()

    def __enter__(self) -> Self:
        with self._lock:
            try:
                path = get_runs_db_path(self._data_root)
                os.makedirs(path.parent, exist_ok=True)
                conn = sqlite3.connect(str(path), check_same_thread=False)
                self._migrate_schema(conn)
                self._insert_run_row(conn)
                self._conn = conn
                self.enabled = True
            except Exception as exc:  # pragma: no cover - filesystem dependent  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)
            return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
                    pass
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
                    pass
                self._conn = None

    def _disable(self, exc: Exception) -> None:
        self.enabled = False
        if not self._warned:
            self._warned = True
            logger.warning(
                "run recorder disabled (%s: %s); observability is off",
                type(exc).__name__,
                exc,
            )

    def _note_round(self, round: int | None) -> None:
        if round is not None:
            self._max_round = max(self._max_round, round)

    @staticmethod
    def _event_times(
        started_at: str | None,
        completed_at: str | None,
        duration_ms: float | None,
    ) -> tuple[str, str, float | None]:
        started = started_at or _now()
        completed = completed_at or _now()
        if duration_ms is None and started_at and completed_at:
            duration_ms = _duration_ms(started_at, completed_at)
        return started, completed, duration_ms

    def _event_summary(self, result_summary: str | None) -> str | None:
        if result_summary is None:
            return None
        summary = redact_json(result_summary)
        if len(summary) > self.max_result_bytes:
            summary = summary[: self.max_result_bytes] + "...[truncated]"
        return summary

    def _insert_event_row(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        sequence: int,
        event_type: str,
        started: str,
        completed: str,
        duration_ms: float | None,
        round: int | None,
        model: str | None,
        tool_name: str | None,
        arguments: object | None,
        summary: str | None,
        success: bool | None,
        error_type: str | None,
        evidence_ids: list[str] | None,
        metadata: dict[str, object] | None,
    ) -> None:
        conn.execute(
            "INSERT INTO agent_events (event_id, run_id, sequence, event_type,"
            " started_at, completed_at, duration_ms, round, model, tool_name,"
            " arguments, result_summary, success, error_type, evidence_ids, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                self.run_id,
                sequence,
                event_type,
                started,
                completed,
                duration_ms,
                round,
                model,
                tool_name,
                redact_json(json.dumps(arguments)) if arguments is not None else None,
                summary,
                (1 if success else 0) if success is not None else None,
                error_type,
                json.dumps(evidence_ids) if evidence_ids is not None else None,
                redact_json(json.dumps(metadata)) if metadata is not None else None,
            ),
        )
        conn.commit()

    def _next_event_id(self, conn: sqlite3.Connection) -> tuple[str, int]:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_events WHERE run_id = ?",
            (self.run_id,),
        ).fetchone()[0]
        return f"{self.run_id}:ev:{sequence:04d}", sequence

    def record_event(
        self,
        event_type: str,
        *,
        round: int | None = None,
        model: str | None = None,
        tool_name: str | None = None,
        arguments: object | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error_type: str | None = None,
        evidence_ids: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        duration_ms: float | None = None,
    ) -> str | None:
        """Append one agent_events row; returns the event_id (None when disabled).

        duration_ms: explicit wall-clock duration (preferred); when omitted
        and both timestamps are given, it is derived from them.
        """
        with self._lock:
            if not self.enabled:
                return None
            try:
                assert self._conn is not None
                self._note_round(round)
                started, completed, duration_ms = self._event_times(started_at, completed_at, duration_ms)
                event_id, sequence = self._next_event_id(self._conn)
                summary = self._event_summary(result_summary)
                self._insert_event_row(
                    self._conn,
                    event_id,
                    sequence,
                    event_type,
                    started,
                    completed,
                    duration_ms,
                    round,
                    model,
                    tool_name,
                    arguments,
                    summary,
                    success,
                    error_type,
                    evidence_ids,
                    metadata,
                )
                if event_type == EventType.EVIDENCE_ADDED:
                    self.evidence_tokens += len(summary or "") // 4
                return event_id
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)
                return None

    def record_tool_call(
        self,
        *,
        tool_call_id: str,
        round: int,
        tool_name: str,
        arguments_json: str,
        started_at: str,
        completed_at: str,
        status: str,
        result_row_count: int,
        returned_count: int | None,
        truncated: bool,
        result_bytes: int,
        result_hash: str,
        source_names: str,
        source_freshness: str,
        as_of: str | None,
        error_type: str | None,
        error_message: str | None,
        protocol_id: str | None = None,
        bridge_queue_ms: float | None = None,
        handler_ms: float | None = None,
        cache_hit: bool | None = None,
        cache_type: str | None = None,
    ) -> None:
        with self._lock:
            if not self.enabled:
                return
            try:
                assert self._conn is not None
                self._note_round(round)
                message = redact_text(error_message)[:2000] if error_message is not None else None
                self._conn.execute(
                    "INSERT INTO tool_calls (tool_call_id, run_id, round, tool_name,"
                    " tool_version, arguments_json, started_at, completed_at, duration_ms,"
                    " status, result_row_count, returned_count, truncated, result_bytes,"
                    " result_hash, source_names, source_freshness, as_of, error_type, error_message,"
                    " protocol_id, bridge_queue_ms, handler_ms, cache_hit, cache_type)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tool_call_id,
                        self.run_id,
                        round,
                        tool_name,
                        self.tool_registry_version,
                        redact_json(arguments_json),
                        started_at,
                        completed_at,
                        _duration_ms(started_at, completed_at),
                        status,
                        result_row_count,
                        returned_count,
                        (1 if truncated else 0),
                        result_bytes,
                        result_hash,
                        source_names,
                        source_freshness,
                        as_of,
                        error_type,
                        message,
                        protocol_id,
                        bridge_queue_ms,
                        handler_ms,
                        (1 if cache_hit else 0) if cache_hit is not None else None,
                        cache_type,
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)

    def record_evidence(
        self,
        *,
        evidence_id: str,
        run_id: str,
        tool_call_id: str,
        round: int | None,
        tool_name: str,
        rendered_hash: str,
        rendered_bytes: int,
        estimated_tokens: int,
        source_names: str,
        source_freshness: str,
        as_of: str | None,
        rendered_text: str,
    ) -> None:
        """Persist one rendered-evidence record (what the model received)."""
        with self._lock:
            if not self.enabled:
                return
            try:
                self._note_round(round)
                assert self._conn is not None
                self._conn.execute(
                    "INSERT INTO evidence (evidence_id, run_id, tool_call_id, round,"
                    " tool_name, rendered_hash, rendered_bytes, estimated_tokens,"
                    " source_names, source_freshness, as_of, rendered_text)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        evidence_id,
                        run_id,
                        tool_call_id,
                        round,
                        tool_name,
                        rendered_hash,
                        rendered_bytes,
                        estimated_tokens,
                        source_names,
                        source_freshness,
                        as_of,
                        rendered_text,
                    ),
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)

    def record_security_event(
        self,
        *,
        source: str,
        sha256: str,
        score: int | None,
        verdict: str | None,
        rule_ids: list[str] | None,
        decision: str,
        reason: str | None = None,
        span_length: int | None = None,
    ) -> str | None:
        """Append one security_events row; returns the event_id (None when
        disabled). Hash-only storage: events never carry full content;
        response_stripped events record only the stripped span's length."""
        with self._lock:
            if not self.enabled:
                return None
            try:
                created = _now()
                assert self._conn is not None
                sequence = self._conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM security_events WHERE run_id = ?",
                    (self.run_id,),
                ).fetchone()[0]
                event_id = f"{self.run_id}:se:{sequence:04d}"
                self._conn.execute(
                    "INSERT INTO security_events (event_id, run_id, sequence,"
                    " created_at, source, sha256, score, verdict, rule_ids,"
                    " decision, reason, span_length)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        self.run_id,
                        sequence,
                        created,
                        source,
                        sha256,
                        score,
                        verdict,
                        json.dumps(rule_ids) if rule_ids is not None else None,
                        decision,
                        reason,
                        span_length,
                    ),
                )
                self._conn.commit()
                logger.info(
                    "security event: run=%s decision=%s source=%s rules=%s reason=%s span_length=%s",
                    self.run_id,
                    decision,
                    source,
                    rule_ids,
                    reason,
                    span_length,
                )
                return event_id
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)
                return None

    def record_model_call(
        self,
        *,
        round: int,
        provider: str,
        model: str,
        started_at: str,
        completed_at: str,
        usage: dict[str, object] | None = None,
        finish_reason: str | None = None,
        tool_call_count: int = 0,
        provider_request_id: str | None = None,
        status: str = "completed",
        error_type: str | None = None,
        error_category: str | None = None,
    ) -> float:
        """Record one model completion; returns the estimated USD cost."""
        with self._lock:
            if not self.enabled:
                return 0.0
            try:
                self._note_round(round)
                self.model_calls += 1
                assert self._conn is not None
                usage = usage or {}
                input_tokens = _usage_int(usage.get("prompt_tokens", 0))
                output_tokens = _usage_int(usage.get("completion_tokens", 0))
                reasoning_tokens = _usage_int(usage.get("reasoning_tokens", 0))
                details_raw = usage.get("prompt_tokens_details")
                details: dict[str, object] = details_raw if isinstance(details_raw, dict) else {}
                cached_tokens = _usage_int(details.get("cached_tokens", 0))
                cost = self._estimate_cost(model, input_tokens, output_tokens, usage)
                self._input_tokens += input_tokens
                self._output_tokens += output_tokens
                self._total_tokens += _usage_int(usage.get("total_tokens", 0))
                self._estimated_model_cost += cost
                self._conn.execute(
                    "INSERT INTO model_calls (model_call_id, run_id, round, provider,"
                    " model, started_at, completed_at, duration_ms, input_tokens,"
                    " output_tokens, reasoning_tokens, cached_tokens, estimated_cost,"
                    " finish_reason, tool_call_count, provider_request_id,"
                    " status, error_type, error_category)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{self.run_id}:mc:{self.model_calls}",
                        self.run_id,
                        round,
                        provider,
                        model,
                        started_at,
                        completed_at,
                        _duration_ms(started_at, completed_at),
                        input_tokens,
                        output_tokens,
                        reasoning_tokens,
                        cached_tokens,
                        cost,
                        finish_reason,
                        tool_call_count,
                        provider_request_id,
                        status,
                        error_type,
                        error_category,
                    ),
                )
                self._conn.commit()
                return cost
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)
                return 0.0

    def complete(
        self,
        *,
        status: str,
        answer: str,
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Close out the agent_runs summary row; emits RUN_COMPLETED unless failed."""
        with self._lock:
            if not self.enabled:
                return
            try:
                completed_at = _now()
                message = redact_text(error_message)[:2000] if error_message is not None else None
                assert self._conn is not None
                answer_hash = hashlib.sha256(answer.encode()).hexdigest() if answer else None
                duration = _duration_ms(self.started_at, completed_at) if self.started_at else None
                self._conn.execute(
                    "UPDATE agent_runs SET completed_at = ?, duration_ms = ?, status = ?,"
                    " round_count = ?, model_call_count = ?, tool_call_count = ?,"
                    " input_tokens = ?, output_tokens = ?, total_tokens = ?,"
                    " estimated_model_cost = ?, estimated_total_cost = ?, final_answer_hash = ?,"
                    " error_type = ?, error_message = ? WHERE run_id = ?",
                    (
                        completed_at,
                        duration,
                        status,
                        self._max_round,
                        self.model_calls,
                        self._tool_seq,
                        self._input_tokens,
                        self._output_tokens,
                        self._total_tokens,
                        self._estimated_model_cost,
                        self._estimated_model_cost,
                        answer_hash,
                        error_type,
                        message,
                        self.run_id,
                    ),
                )
                if status != "failed":
                    self.record_event(EventType.RUN_COMPLETED, round=self.current_round)
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                self._disable(exc)

    def next_tool_seq(self) -> int:
        """Recorder-internal tool-call sequence; also the run's tool-call count."""
        with self._lock:
            self._tool_seq += 1
            return self._tool_seq

    def next_evidence_seq(self) -> int:
        """Recorder-internal evidence sequence (never raises)."""
        with self._lock:
            self._evidence_seq += 1
            return self._evidence_seq

    @staticmethod
    def _estimate_cost(model: str, input_tokens: int, output_tokens: int, usage: dict[str, object]) -> float:
        """Provider-reported usage.cost wins; else the static list-price table."""
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        rates = MODEL_COST_PER_1M.get(model)
        if rates is None:
            return 0.0
        return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


def finalize_failed_run(run_id: str, *, error_type: str, error_message: str) -> bool:
    """Terminalize an orphaned agent_runs row as failed (fail-stop, UPDATE-only).
    Reads started_at/completed_at via a short-lived connection and updates
    completed_at, duration_ms, status, error_type, redacted/truncated
    error_message, plus the summary aggregates reconstructed from durable
    child rows (same contract as RunRecorder.complete) for
    WHERE run_id = ? AND completed_at IS NULL. Preserves evidence, tool rows,
    and already-terminal runs. Returns True when this call updated the row or
    found it already terminal, False when no row exists or storage fails;
    storage errors are swallowed (observability never breaks research).
    """
    try:
        path = get_runs_db_path(DEFAULT_DATA_ROOT)
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT started_at, completed_at FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            started_at, completed_at = row
            if completed_at is not None:
                return True
            now = _now()
            try:
                duration = _duration_ms(started_at, now) if started_at else None
            except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
                duration = None
            round_count = conn.execute(
                "SELECT COALESCE(MAX(round), 0) FROM ("
                " SELECT round FROM model_calls WHERE run_id = ?"
                " UNION ALL SELECT round FROM tool_calls WHERE run_id = ?"
                " UNION ALL SELECT round FROM agent_events WHERE run_id = ?)",
                (run_id, run_id, run_id),
            ).fetchone()[0]
            model_row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(input_tokens), 0),"
                " COALESCE(SUM(output_tokens), 0), COALESCE(SUM(estimated_cost), 0.0)"
                " FROM model_calls WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            model_call_count, input_tokens, output_tokens, estimated_cost = model_row
            tool_call_count = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id = ?", (run_id,)).fetchone()[0]
            # model_calls persists input/output/reasoning/cached but not usage.total_tokens,
            # so total mirrors input + output instead of the live accumulator.
            message = redact_text(error_message)[:2000]
            cur = conn.execute(
                "UPDATE agent_runs SET completed_at = ?, duration_ms = ?, status = ?,"
                " round_count = ?, model_call_count = ?, tool_call_count = ?,"
                " input_tokens = ?, output_tokens = ?, total_tokens = ?,"
                " estimated_model_cost = ?, estimated_total_cost = ?,"
                " error_type = ?, error_message = ?"
                " WHERE run_id = ? AND completed_at IS NULL",
                (
                    now,
                    duration,
                    "failed",
                    round_count,
                    model_call_count,
                    tool_call_count,
                    input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    estimated_cost,
                    estimated_cost,
                    error_type,
                    message,
                    run_id,
                ),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
                pass
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("finalize_failed_run dropped (%s: %s)", type(exc).__name__, exc)
        return False


# -- current-recorder contextvar (Pi bridge + gateway share one recorder) ----------

_current_recorder: ContextVar[RunRecorder | None] = ContextVar("current_recorder", default=None)


def get_current_recorder() -> RunRecorder | None:
    return _current_recorder.get()


def set_current_recorder(recorder: RunRecorder) -> Token[RunRecorder | None]:
    return _current_recorder.set(recorder)


def reset_current_recorder(token: Token[RunRecorder | None]) -> None:
    _current_recorder.reset(token)


# -- read-side query helpers -------------------------------------------------


def _query_conn() -> sqlite3.Connection | None:
    path = get_runs_db_path(DEFAULT_DATA_ROOT)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    """Run one read query; empty on missing DB or storage error (existing boundary)."""
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _fetch_one(sql: str, params: tuple[object, ...]) -> dict[str, object] | None:
    """Run one single-row read query (existing boundary)."""
    rows = _fetch_all(sql, params)
    return rows[0] if rows else None


def _normalize_decision(value: object) -> str:
    """Security decision label with unknown fallback (existing boundary)."""
    return value if isinstance(value, str) and value else "unknown"


def list_runs(limit: int = 20) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,))


def get_run(run_id: str) -> dict[str, object] | None:
    return _fetch_one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))


def get_events(run_id: str) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM agent_events WHERE run_id = ? ORDER BY sequence", (run_id,))


def get_tool_calls(run_id: str) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at", (run_id,))


def get_model_calls(run_id: str) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM model_calls WHERE run_id = ? ORDER BY started_at", (run_id,))


def get_security_events(run_id: str) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM security_events WHERE run_id = ? ORDER BY sequence", (run_id,))


def get_security_summary(run_id: str) -> dict[str, int]:
    """Counts of security events grouped by decision."""
    counts: dict[str, int] = {
        "allowed": 0,
        "quarantined": 0,
        "blocked": 0,
        "action_blocked": 0,
        "egress_blocked": 0,
        "response_stripped": 0,
    }
    for event in get_security_events(run_id):
        decision = _normalize_decision(event.get("decision"))
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def get_evidence(run_id: str) -> list[dict[str, object]]:
    return _fetch_all("SELECT * FROM evidence WHERE run_id = ? ORDER BY evidence_id", (run_id,))
