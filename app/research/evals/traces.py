"""Complete trace persistence for research/eval runs: SQLite + JSONL, never stdout-only.

Every research session records model, prompt version, discovery steps, tool
calls + args, evidence IDs, job-parent linkage, failures, retries,
conclusions, and durations here. Evaluators, regression fixtures, and viewer
projections read the same tables. stdlib only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from app.config import get_data_root

logger = logging.getLogger(__name__)

Scalar = Union[str, int, float, bool, None]
# ponytail: flat string-map payloads only; nest via a JSON-encoded string value if it matters.
Payload = dict[str, Scalar]

HARNESS_VERSION = "mvp-1"
TRACE_DB_NAME = "research_traces.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_traces (
  trace_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  wave_id INTEGER NOT NULL,
  provider TEXT NOT NULL DEFAULT 'fake',
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  harness_version TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  duration_ms REAL,
  conclusion TEXT,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_trace_events (
  event_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  duration_ms REAL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_events_trace ON eval_trace_events(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_traces_session ON eval_traces(session_id);
"""


@dataclass(frozen=True)
class TraceHeader:
    trace_id: str
    session_id: str
    wave_id: int
    provider: str
    model: str
    prompt_version: str
    harness_version: str
    git_sha: str
    started_at: str
    completed_at: str | None
    duration_ms: float | None
    conclusion: str | None
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "wave_id", _coerce_wave(self.wave_id))

@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    trace_id: str
    seq: int
    event_type: str
    started_at: str
    completed_at: str | None
    duration_ms: float | None
    payload: Payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(data_root: Path | None) -> Path:
    root = data_root if data_root is not None else get_data_root()
    return root / TRACE_DB_NAME


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(eval_traces)")}
        if "provider" not in cols:
            conn.execute("ALTER TABLE eval_traces ADD COLUMN provider TEXT NOT NULL DEFAULT 'fake'")
    except Exception:
        pass
    return conn


def _as_str(value: object) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _as_opt_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_opt_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _coerce_wave(wave_id: object) -> int:
    if isinstance(wave_id, bool):
        raise ValueError(f"trace: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        if wave_id < 1:
            raise ValueError(f"trace: 'wave_id' must be >= 1, got {wave_id!r}")
        return wave_id
    if isinstance(wave_id, str):
        text = wave_id.strip()
        if text.isdigit():
            value = int(text)
            if value < 1:
                raise ValueError(f"trace: 'wave_id' must be >= 1, got {wave_id!r}")
            return value
    raise ValueError(f"trace: 'wave_id' must be an int >= 1, got {wave_id!r}")


def _payload_from_json(raw: str) -> Payload:
    decoded: object = json.loads(raw)
    out: Payload = {}
    if isinstance(decoded, dict):
        for key, value in decoded.items():
            if not isinstance(key, str):
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                out[key] = value
            else:
                out[key] = json.dumps(value, sort_keys=True)
    return out


@dataclass
class TraceRecorder:
    """Append-only recorder for one trace.

    Never raises: observability must not break research, so persistence
    failures warn and continue (same policy as app/storage/runs.py).
    """

    trace_id: str
    session_id: str
    wave_id: int
    db_path: Path
    jsonl_path: Path
    _seq: int = 0
    _start_perf: float = 0.0
    _start_iso: str = ""
    _closed: bool = False
    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave(self.wave_id)


    def record(
        self,
        event_type: str,
        payload: Payload | None = None,
        duration_ms: float | None = None,
    ) -> str:
        """Append one event (discovery/call/evidence/job/parent/fail/retry); returns its event_id."""
        event_id = f"tev:{uuid.uuid4().hex[:16]}"
        body: Payload = dict(payload) if payload else {}
        started = _now()
        self._seq += 1
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO eval_trace_events"
                    " (event_id, trace_id, seq, event_type, started_at, completed_at,"
                    " duration_ms, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        self.trace_id,
                        self._seq,
                        event_type,
                        started,
                        started if duration_ms is not None else None,
                        duration_ms,
                        json.dumps(body, sort_keys=True),
                    ),
                )
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event_id": event_id,
                            "trace_id": self.trace_id,
                            "seq": self._seq,
                            "event_type": event_type,
                            "started_at": started,
                            "payload": body,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except Exception as exc:
            logger.warning("trace record failed for %s: %s", self.trace_id, exc)
        return event_id

    def finish(self, conclusion: str, status: str = "completed") -> None:
        """Close the trace with its conclusion; idempotent."""
        if self._closed:
            return
        self._closed = True
        completed = _now()
        duration_ms = (time.perf_counter() - self._start_perf) * 1000.0 if self._start_perf else None
        try:
            with _connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE eval_traces SET completed_at = ?, duration_ms = ?,"
                    " conclusion = ?, status = ? WHERE trace_id = ?",
                    (completed, duration_ms, conclusion, status, self.trace_id),
                )
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event_id": f"tev:{uuid.uuid4().hex[:16]}",
                            "trace_id": self.trace_id,
                            "event_type": "conclusion",
                            "started_at": completed,
                            "payload": {"conclusion": conclusion, "status": status},
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except Exception as exc:
            logger.warning("trace finish failed for %s: %s", self.trace_id, exc)


def create_trace(
    *,
    session_id: str,
    wave_id: int | str,
    provider: str = "fake",
    model: str = "fake",
    prompt_version: str = "v1",
    git_sha: str,
    data_root: Path | None = None,
    job_parent: str | None = None,
) -> TraceRecorder:
    """Open a trace row + JSONL file and return its recorder."""
    wave = _coerce_wave(wave_id)
    trace_id = f"tr:{uuid.uuid4().hex[:16]}"
    db = _db_path(data_root)
    root = data_root if data_root is not None else get_data_root()
    jsonl_path = root / "traces" / f"{trace_id}.jsonl"
    recorder = TraceRecorder(
        trace_id=trace_id,
        session_id=session_id,
        wave_id=wave,
        db_path=db,
        jsonl_path=jsonl_path,
        _start_perf=time.perf_counter(),
        _start_iso=_now(),
    )
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        with _connect(db) as conn:
            conn.execute(
                "INSERT INTO eval_traces (trace_id, session_id, wave_id, provider, model,"
                " prompt_version, harness_version, git_sha, started_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trace_id,
                    session_id,
                    wave,
                    provider,
                    model,
                    prompt_version,
                    HARNESS_VERSION,
                    git_sha,
                    recorder._start_iso,
                    "open",
                ),
            )
    except Exception as exc:
        logger.warning("trace create failed for session %s: %s", session_id, exc)
    if job_parent is not None:
        recorder.record("parent", {"job_parent": job_parent})
    return recorder


def _header_from_row(row: tuple[object, ...]) -> TraceHeader:
    return TraceHeader(
        trace_id=_as_str(row[0]),
        session_id=_as_str(row[1]),
        wave_id=_coerce_wave(row[2]),
        provider=_as_str(row[3]) if len(row) > 12 else "fake",
        model=_as_str(row[4]) if len(row) > 12 else _as_str(row[3]),
        prompt_version=_as_str(row[5]) if len(row) > 12 else _as_str(row[4]),
        harness_version=_as_str(row[6]) if len(row) > 12 else _as_str(row[5]),
        git_sha=_as_str(row[7]) if len(row) > 12 else _as_str(row[6]),
        started_at=_as_str(row[8]) if len(row) > 12 else _as_str(row[7]),
        completed_at=_as_opt_str(row[9]) if len(row) > 12 else _as_opt_str(row[8]),
        duration_ms=_as_opt_float(row[10]) if len(row) > 12 else _as_opt_float(row[9]),
        conclusion=_as_opt_str(row[11]) if len(row) > 12 else _as_opt_str(row[10]),
        status=_as_str(row[12]) if len(row) > 12 else _as_str(row[11]),
    )


def get_trace(trace_id: str, data_root: Path | None = None) -> TraceHeader | None:
    """Fetch one trace header; None when absent."""
    with _connect(_db_path(data_root)) as conn:
        row = conn.execute(
            "SELECT trace_id, session_id, wave_id, provider, model, prompt_version,"
            " harness_version, git_sha, started_at, completed_at, duration_ms,"
            " conclusion, status FROM eval_traces WHERE trace_id = ?",
            (trace_id,),
        ).fetchone()
    if row is None:
        return None
    return _header_from_row(tuple(row))


def list_traces(
    session_id: str | None = None, data_root: Path | None = None, limit: int = 50
) -> list[TraceHeader]:
    """Newest-first trace headers, optionally filtered to one session."""
    with _connect(_db_path(data_root)) as conn:
        if session_id is None:
            rows = conn.execute(
                "SELECT trace_id, session_id, wave_id, provider, model, prompt_version,"
                " harness_version, git_sha, started_at, completed_at, duration_ms,"
                " conclusion, status FROM eval_traces ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT trace_id, session_id, wave_id, provider, model, prompt_version,"
                " harness_version, git_sha, started_at, completed_at, duration_ms,"
                " conclusion, status FROM eval_traces WHERE session_id = ?"
                " ORDER BY started_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
    return [_header_from_row(tuple(row)) for row in rows]


def get_trace_events(trace_id: str, data_root: Path | None = None) -> list[TraceEvent]:
    """All events for one trace in sequence order."""
    with _connect(_db_path(data_root)) as conn:
        rows = conn.execute(
            "SELECT event_id, trace_id, seq, event_type, started_at, completed_at,"
            " duration_ms, payload_json FROM eval_trace_events WHERE trace_id = ?"
            " ORDER BY seq ASC",
            (trace_id,),
        ).fetchall()
    out: list[TraceEvent] = []
    for row in rows:
        cells = tuple(row)
        out.append(
            TraceEvent(
                event_id=_as_str(cells[0]),
                trace_id=_as_str(cells[1]),
                seq=_as_int(cells[2]),
                event_type=_as_str(cells[3]),
                started_at=_as_str(cells[4]),
                completed_at=_as_opt_str(cells[5]),
                duration_ms=_as_opt_float(cells[6]),
                payload=_payload_from_json(_as_str(cells[7])),
            )
        )
    return out
