"""SQLite run/event/tool/model observability store. stdlib only.

One RunRecorder per agent run appends rows to agent_runs/agent_events/
tool_calls/model_calls under data/runs.sqlite (or $RUNS_DB_PATH).
Observability must never break research: every recorder method swallows
its own errors, disables the recorder, and logs a single warning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ..redact import redact_json, redact_text
from ..runtime import EventType, ExecutionBudget

logger = logging.getLogger(__name__)

def model_error_category(exc: Exception) -> str:
    """Map an upstream exception to a coarse category for observability."""
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.HTTPError):
        return "http"
    if isinstance(exc, requests.ConnectionError):
        return "connection"
    if isinstance(exc, json.JSONDecodeError):
        return "parse"
    return "other"

# Default data root when the recorder is not given an explicit one.
DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

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
    source_names TEXT, source_freshness TEXT, as_of TEXT, error_type TEXT, error_message TEXT);
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
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started_at: str, completed_at: str) -> float:
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(completed_at)
    return (end - start).total_seconds() * 1000.0


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
        as_of: Optional[str],
        model: str,
        provider: str,
        model_parameters: dict,
        agent_version: str,
        prompt_version: str,
        tool_registry_version: str,
        git_sha: str,
        data_root: Optional[Path] = None,
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
        self._data_root = data_root or DEFAULT_DATA_ROOT
        self.max_result_bytes = max_result_bytes
        self.enabled = False
        self._conn: Optional[sqlite3.Connection] = None
        self._warned = False
        self.started_at: Optional[str] = None
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

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "RunRecorder":
        try:
            path = get_runs_db_path(self._data_root)
            os.makedirs(path.parent, exist_ok=True)
            conn = sqlite3.connect(str(path))
            conn.executescript(_SCHEMA)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
            if "returned_count" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN returned_count INTEGER")
            if "truncated" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN truncated INTEGER")
            if "as_of" not in cols:
                conn.execute("ALTER TABLE tool_calls ADD COLUMN as_of TEXT")
            mcols = {row[1] for row in conn.execute("PRAGMA table_info(model_calls)")}
            if "status" not in mcols:
                conn.execute(
                    "ALTER TABLE model_calls ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            if "error_type" not in mcols:
                conn.execute("ALTER TABLE model_calls ADD COLUMN error_type TEXT")
            if "error_category" not in mcols:
                conn.execute("ALTER TABLE model_calls ADD COLUMN error_category TEXT")
            ecols = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
            if "as_of" not in ecols:
                conn.execute("ALTER TABLE evidence ADD COLUMN as_of TEXT")
            scols = {row[1] for row in conn.execute("PRAGMA table_info(security_events)")}
            if "span_length" not in scols:
                conn.execute("ALTER TABLE security_events ADD COLUMN span_length INTEGER")
            conn.commit()
            self.started_at = _now()
            conn.execute(
                "INSERT INTO agent_runs (run_id, request_id, started_at, question,"
                " model_provider, model_name, model_parameters, agent_version,"
                " prompt_version, tool_registry_version, git_sha, as_of)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.run_id, self.request_id, self.started_at, redact_json(self.question),
                    self.provider, self.model, json.dumps(self.model_parameters),
                    self.agent_version, self.prompt_version,
                    self.tool_registry_version, self.git_sha, self.as_of,
                ),
            )
            conn.commit()
            self._conn = conn
            self.enabled = True
        except Exception as exc:  # pragma: no cover - filesystem dependent
            self._disable(exc)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except Exception:
                pass
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _disable(self, exc: Exception) -> None:
        self.enabled = False
        if not self._warned:
            self._warned = True
            logger.warning(
                "run recorder disabled (%s: %s); observability is off",
                type(exc).__name__, exc,
            )

    def _note_round(self, round: Optional[int]) -> None:
        if round is not None:
            self._max_round = max(self._max_round, round)

    # -- event/tool/model records ------------------------------------------

    def record_event(
        self,
        event_type: str,
        *,
        round: Optional[int] = None,
        model: Optional[str] = None,
        tool_name: Optional[str] = None,
        arguments: Optional[Any] = None,
        result_summary: Optional[str] = None,
        success: Optional[bool] = None,
        error_type: Optional[str] = None,
        evidence_ids: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        duration_ms: Optional[float] = None,
    ) -> Optional[str]:
        """Append one agent_events row; returns the event_id (None when disabled).

        duration_ms: explicit wall-clock duration (preferred); when omitted
        and both timestamps are given, it is derived from them.
        """
        if not self.enabled:
            return None
        try:
            self._note_round(round)
            started = started_at or _now()
            completed = completed_at or _now()
            if duration_ms is None and started_at and completed_at:
                duration_ms = _duration_ms(started_at, completed_at)
            sequence = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_events WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()[0]
            event_id = f"{self.run_id}:ev:{sequence:04d}"
            summary = None
            if result_summary is not None:
                summary = redact_json(str(result_summary))
                if len(summary) > self.max_result_bytes:
                    summary = summary[: self.max_result_bytes] + "...[truncated]"
            self._conn.execute(
                "INSERT INTO agent_events (event_id, run_id, sequence, event_type,"
                " started_at, completed_at, duration_ms, round, model, tool_name,"
                " arguments, result_summary, success, error_type, evidence_ids, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, self.run_id, sequence, str(event_type),
                    started, completed,
                    duration_ms,
                    round, model, tool_name,
                    redact_json(json.dumps(arguments)) if arguments is not None else None,
                    summary,
                    (1 if success else 0) if success is not None else None,
                    error_type,
                    json.dumps(evidence_ids) if evidence_ids is not None else None,
                    redact_json(json.dumps(metadata)) if metadata is not None else None,
                ),
            )
            self._conn.commit()
            if event_type == EventType.EVIDENCE_ADDED:
                self.evidence_tokens += len(summary or "") // 4
            return event_id
        except Exception as exc:
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
        returned_count: Optional[int],
        truncated: bool,
        result_bytes: int,
        result_hash: str,
        source_names: str,
        source_freshness: str,
        as_of: Optional[str],
        error_type: Optional[str],
        error_message: Optional[str],
    ) -> None:
        if not self.enabled:
            return
        try:
            self._note_round(round)
            message = redact_text(error_message)[:2000] if error_message is not None else None
            self._conn.execute(
                "INSERT INTO tool_calls (tool_call_id, run_id, round, tool_name,"
                " tool_version, arguments_json, started_at, completed_at, duration_ms,"
                " status, result_row_count, returned_count, truncated, result_bytes,"
                " result_hash, source_names, source_freshness, as_of, error_type, error_message)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_call_id, self.run_id, round, tool_name,
                    self.tool_registry_version,
                    redact_json(arguments_json), started_at, completed_at,
                    _duration_ms(started_at, completed_at),
                    status, result_row_count, returned_count,
                    (1 if truncated else 0), result_bytes, result_hash,
                    source_names, source_freshness, as_of, error_type, message,
                ),
            )
            self._conn.commit()
        except Exception as exc:
            self._disable(exc)

    def record_evidence(
        self,
        *,
        evidence_id: str,
        run_id: str,
        tool_call_id: str,
        round: Optional[int],
        tool_name: str,
        rendered_hash: str,
        rendered_bytes: int,
        estimated_tokens: int,
        source_names: str,
        source_freshness: str,
        as_of: Optional[str],
        rendered_text: str,
    ) -> None:
        """Persist one rendered-evidence record (what the model received)."""
        if not self.enabled:
            return
        try:
            self._note_round(round)
            self._conn.execute(
                "INSERT INTO evidence (evidence_id, run_id, tool_call_id, round,"
                " tool_name, rendered_hash, rendered_bytes, estimated_tokens,"
                " source_names, source_freshness, as_of, rendered_text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, run_id, tool_call_id, round, tool_name,
                    rendered_hash, rendered_bytes, estimated_tokens,
                    source_names, source_freshness, as_of, rendered_text,
                ),
            )
            self._conn.commit()
        except Exception as exc:
            self._disable(exc)

    def record_security_event(
        self,
        *,
        source: str,
        sha256: str,
        score: Optional[int],
        verdict: Optional[str],
        rule_ids: Optional[list[str]],
        decision: str,
        reason: Optional[str] = None,
        span_length: Optional[int] = None,
    ) -> Optional[str]:
        """Append one security_events row; returns the event_id (None when
        disabled). Hash-only storage: events never carry full content;
        response_stripped events record only the stripped span's length."""
        if not self.enabled:
            return None
        try:
            created = _now()
            sequence = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM security_events"
                " WHERE run_id = ?",
                (self.run_id,),
            ).fetchone()[0]
            event_id = f"{self.run_id}:se:{sequence:04d}"
            self._conn.execute(
                "INSERT INTO security_events (event_id, run_id, sequence,"
                " created_at, source, sha256, score, verdict, rule_ids,"
                " decision, reason, span_length)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id, self.run_id, sequence, created, source, sha256,
                    score, verdict,
                    json.dumps(rule_ids) if rule_ids is not None else None,
                    decision, reason, span_length,
                ),
            )
            self._conn.commit()
            logger.info(
                "security event: run=%s decision=%s source=%s rules=%s reason=%s span_length=%s",
                self.run_id, decision, source, rule_ids, reason, span_length,
            )
            return event_id
        except Exception as exc:
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
        usage: Optional[dict] = None,
        finish_reason: Optional[str] = None,
        tool_call_count: int = 0,
        provider_request_id: Optional[str] = None,
        status: str = "completed",
        error_type: Optional[str] = None,
        error_category: Optional[str] = None,
    ) -> float:
        """Record one model completion; returns the estimated USD cost."""
        if not self.enabled:
            return 0.0
        try:
            self._note_round(round)
            self.model_calls += 1
            usage = usage or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
            reasoning_tokens = int(usage.get("reasoning_tokens", 0))
            cached_tokens = int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            )
            cost = self._estimate_cost(model, input_tokens, output_tokens, usage)
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._total_tokens += int(usage.get("total_tokens", 0))
            self._estimated_model_cost += cost
            self._conn.execute(
                "INSERT INTO model_calls (model_call_id, run_id, round, provider,"
                " model, started_at, completed_at, duration_ms, input_tokens,"
                " output_tokens, reasoning_tokens, cached_tokens, estimated_cost,"
                " finish_reason, tool_call_count, provider_request_id,"
                " status, error_type, error_category)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{self.run_id}:mc:{self.model_calls}", self.run_id, round,
                    provider, model, started_at, completed_at,
                    _duration_ms(started_at, completed_at),
                    input_tokens, output_tokens, reasoning_tokens, cached_tokens,
                    cost, finish_reason, tool_call_count, provider_request_id,
                    status, error_type, error_category,
                ),
            )
            self._conn.commit()
            return cost
        except Exception as exc:
            self._disable(exc)
            return 0.0

    def complete(
        self,
        *,
        status: str,
        answer: str,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Close out the agent_runs summary row; emits RUN_COMPLETED unless failed."""
        if not self.enabled:
            return
        try:
            completed_at = _now()
            message = redact_text(error_message)[:2000] if error_message is not None else None
            answer_hash = hashlib.sha256(answer.encode()).hexdigest() if answer else None
            duration = (
                _duration_ms(self.started_at, completed_at) if self.started_at else None
            )
            self._conn.execute(
                "UPDATE agent_runs SET completed_at = ?, duration_ms = ?, status = ?,"
                " round_count = ?, model_call_count = ?, tool_call_count = ?,"
                " input_tokens = ?, output_tokens = ?, total_tokens = ?,"
                " estimated_model_cost = ?, estimated_total_cost = ?, final_answer_hash = ?,"
                " error_type = ?, error_message = ? WHERE run_id = ?",
                (
                    completed_at, duration, status, self._max_round, self.model_calls,
                    self._tool_seq, self._input_tokens, self._output_tokens,
                    self._total_tokens, self._estimated_model_cost, self._estimated_model_cost, answer_hash, error_type, message,
                    self.run_id,
                ),
            )
            if status != "failed":
                self.record_event(EventType.RUN_COMPLETED, round=self.current_round)
            self._conn.commit()
        except Exception as exc:
            self._disable(exc)

    def next_tool_seq(self) -> int:
        """Recorder-internal tool-call sequence; also the run's tool-call count."""
        self._tool_seq += 1
        return self._tool_seq

    def next_evidence_seq(self) -> int:
        """Recorder-internal evidence sequence (never raises)."""
        self._evidence_seq += 1
        return self._evidence_seq

    @staticmethod
    def _estimate_cost(
        model: str, input_tokens: int, output_tokens: int, usage: dict
    ) -> float:
        """Provider-reported usage.cost wins; else the static list-price table."""
        cost = usage.get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        rates = MODEL_COST_PER_1M.get(model)
        if rates is None:
            return 0.0
        return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


# -- current-recorder contextvar (nested model calls inside tools) ----------

_current_recorder: ContextVar[Optional[RunRecorder]] = ContextVar(
    "current_recorder", default=None
)


def get_current_recorder() -> Optional[RunRecorder]:
    return _current_recorder.get()


def set_current_recorder(recorder: RunRecorder) -> Token:
    return _current_recorder.set(recorder)


def reset_current_recorder(token: Token) -> None:
    _current_recorder.reset(token)


def record_model_call_from_current(
    *,
    provider: str,
    model: str,
    started_at: str,
    completed_at: str,
    usage: Optional[dict] = None,
    finish_reason: Optional[str] = None,
    tool_call_count: int = 0,
    provider_request_id: Optional[str] = None,
    status: str = "completed",
    error_type: Optional[str] = None,
    error_category: Optional[str] = None,
) -> float:
    """Record a nested model call against the active recorder, if any."""
    recorder = get_current_recorder()
    if recorder is None:
        return 0.0
    return recorder.record_model_call(
        round=recorder.current_round,
        provider=provider,
        model=model,
        started_at=started_at,
        completed_at=completed_at,
        usage=usage,
        finish_reason=finish_reason,
        tool_call_count=tool_call_count,
        provider_request_id=provider_request_id,
        status=status,
        error_type=error_type,
        error_category=error_category,
    )


# -- current-budget contextvar (reserve-before-call inside nested helpers) ---

_current_budget: ContextVar[Optional[ExecutionBudget]] = ContextVar(
    "current_budget", default=None
)


def get_current_budget() -> Optional[ExecutionBudget]:
    return _current_budget.get()


def set_current_budget(budget: ExecutionBudget) -> Token:
    return _current_budget.set(budget)


def reset_current_budget(token: Token) -> None:
    _current_budget.reset(token)


def reserve_model_call_from_current() -> bool:
    """Reserve one model call against the active budget, if any. No active
    budget (standalone tool use) always succeeds."""
    budget = get_current_budget()
    if budget is None:
        return True
    return budget.reserve_model_call()


# -- read-side query helpers -------------------------------------------------

def _query_conn() -> Optional[sqlite3.Connection]:
    path = get_runs_db_path(DEFAULT_DATA_ROOT)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def list_runs(limit: int = 20) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_run(run_id: str) -> Optional[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def get_events(run_id: str) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_tool_calls(run_id: str) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_model_calls(run_id: str) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM model_calls WHERE run_id = ? ORDER BY started_at", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_security_events(run_id: str) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM security_events WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def get_security_summary(run_id: str) -> dict:
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
        decision = event.get("decision") or "unknown"
        counts[decision] = counts.get(decision, 0) + 1
    return counts


def get_evidence(run_id: str) -> list[dict]:
    try:
        conn = _query_conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY evidence_id", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []
