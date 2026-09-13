"""SQLite persistence for the research kernel: sessions/jobs/journal/evidence/freezes.

Database is data/research.sqlite ($RESEARCH_DB_PATH wins). Resume loads
state without writing anything, so completed jobs are never duplicated.
stdlib sqlite3 + json + hashlib only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_data_root
from . import journal as journal_log
from .models import (
    JSONValue,
    Job,
    JournalEvent,
    ResearchSession,
    utcnow,
    validate_json_mapping,
    validate_json_value,
)

__all__ = [
    "ResearchRepository",
    "ResumeState",
    "get_research_db_path",
    "pending_next_action",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  query TEXT NOT NULL, objective TEXT NOT NULL, as_of TEXT,
  status TEXT NOT NULL, current_wave INTEGER NOT NULL,
  policy TEXT NOT NULL, budget TEXT NOT NULL,
  job_ids TEXT NOT NULL, evidence_ids TEXT NOT NULL,
  freeze_ids TEXT NOT NULL, dossier_ids TEXT NOT NULL,
  committee_runs TEXT NOT NULL, unresolved_questions TEXT NOT NULL,
  final_result TEXT, failure TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, wave_id INTEGER NOT NULL,
  parent_job_id TEXT, job_type TEXT NOT NULL, owner TEXT NOT NULL,
  source_domain TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, deadline TEXT,
  model TEXT, token_budget INTEGER, tool_budget INTEGER, child_budget INTEGER NOT NULL,
  result TEXT, diagnostics TEXT NOT NULL, failure TEXT);
CREATE TABLE IF NOT EXISTS journal (
  event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL, timestamp TEXT NOT NULL,
  actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, payload TEXT NOT NULL,
  previous_state TEXT, new_state TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_journal_session_seq ON journal(session_id, sequence);
CREATE INDEX IF NOT EXISTS ix_jobs_session ON jobs(session_id);
CREATE INDEX IF NOT EXISTS ix_journal_session ON journal(session_id, sequence);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  known_at TEXT, as_of TEXT, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_evidence_session ON evidence(session_id);
CREATE TABLE IF NOT EXISTS freezes (
  freeze_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  created_at TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_freezes_session ON freezes(session_id);
CREATE TABLE IF NOT EXISTS dossiers (
  dossier_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  created_at TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_dossiers_session ON dossiers(session_id);
"""


def get_research_db_path(data_root: Path | None = None) -> Path:
    """Resolve the research DB path: $RESEARCH_DB_PATH wins, else data_root/research.sqlite."""
    env = (os.environ.get("RESEARCH_DB_PATH") or "").strip()
    if env:
        return Path(env)
    root = data_root if data_root is not None else get_data_root()
    return root / "research.sqlite"


def _jsonable(value: object) -> object:
    """Convert datetimes to ISO strings so records stay JSON-serializable."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _record_json(record: Mapping[str, object], where: str) -> str:
    validated = validate_json_value(_jsonable(dict(record)), where)
    return json.dumps(validated, sort_keys=True)


def _iso_or_none(value: object, key: str, where: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ValueError(f"{where}: '{key}' must be ISO-8601, got {value!r}") from None
        aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        return aware.isoformat()
    raise ValueError(f"{where}: '{key}' must be ISO-8601, datetime, or null")


def _chain_hash(prev_hash: str, event: JournalEvent) -> str:
    body = "|".join(
        (prev_hash, event.event_id, str(event.sequence), event.timestamp.isoformat(),
         json.dumps(event.to_dict()["payload"], sort_keys=True))
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _job_id(job: Job) -> str:
    return job.job_id


def pending_next_action(session: ResearchSession, jobs: list[Job]) -> str:
    """Deterministic next step from status + open jobs (read-only)."""
    mine = sorted((j for j in jobs if j.session_id == session.session_id), key=_job_id)
    running = [j.job_id for j in mine if j.status == "running"]
    queued = [j.job_id for j in mine if j.status == "queued"]
    if session.status in ("completed", "failed", "cancelled"):
        return f"none: session {session.status}"
    if running:
        return f"wait: {len(running)} running job(s): {', '.join(running)}"
    if queued:
        return f"dispatch: {len(queued)} queued job(s): {', '.join(queued)}"
    return {
        "created": "plan",
        "planning": "plan",
        "researching": f"dispatch wave {session.current_wave + 1} research",
        "freezing": "freeze evidence",
        "analyzing": "analyze frozen evidence",
        "targeted_research": "dispatch targeted research",
        "synthesizing": "synthesize",
    }.get(session.status, f"none: unknown status {session.status!r}")


@dataclass(frozen=True)
class ResumeState:
    """Resume snapshot: session + wave + budgets + next action. No writes performed."""

    session: ResearchSession
    wave: int
    budgets: dict[str, JSONValue] = field(default_factory=dict)
    pending_next_action: str = ""
    open_job_ids: list[str] = field(default_factory=list)


class ResearchRepository:
    """Per-operation SQLite connections; no shared state.

    ponytail: one connection per call, add pooling if write volume matters.
    """

    def __init__(self, path: Path | str | None = None, *, data_root: Path | None = None) -> None:
        """Bind to an explicit path, or resolve via get_research_db_path."""
        self._path = Path(path) if path is not None else get_research_db_path(data_root)

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    # -- sessions ------------------------------------------------------

    def save_session(self, session: ResearchSession) -> None:
        """Upsert one session row."""
        session.validate("<research.sqlite>")
        policy = json.dumps(validate_json_mapping(session.policy, "<session>"), sort_keys=True)
        budget = json.dumps(validate_json_mapping(session.budget, "<session>"), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, created_at, updated_at, query, objective,"
                " as_of, status, current_wave, policy, budget, job_ids, evidence_ids, freeze_ids,"
                " dossier_ids, committee_runs, unresolved_questions, final_result, failure)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.session_id, session.created_at.isoformat(), session.updated_at.isoformat(),
                    session.query, session.objective,
                    session.as_of.isoformat() if session.as_of is not None else None,
                    session.status, session.current_wave, policy, budget,
                    json.dumps(session.to_dict()["job_ids"], sort_keys=True),
                    json.dumps(session.to_dict()["evidence_ids"], sort_keys=True),
                    json.dumps(session.to_dict()["freeze_ids"], sort_keys=True),
                    json.dumps(session.to_dict()["dossier_ids"], sort_keys=True),
                    json.dumps(session.to_dict()["committee_runs"], sort_keys=True),
                    json.dumps(session.to_dict()["unresolved_questions"], sort_keys=True),
                    json.dumps(session.to_dict()["final_result"], sort_keys=True)
                    if session.final_result is not None else None,
                    json.dumps(session.to_dict()["failure"], sort_keys=True)
                    if session.failure is not None else None,
                ),
            )

    def get_session(self, session_id: str) -> ResearchSession:
        """Load one session; raises KeyError when absent."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown session_id: {session_id!r}")
        return self._row_to_session(row)

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> ResearchSession:
        doc: dict[str, object] = {
            "session_id": row["session_id"], "created_at": row["created_at"], "updated_at": row["updated_at"],
            "query": row["query"], "objective": row["objective"], "as_of": row["as_of"],
            "status": row["status"], "current_wave": row["current_wave"],
        }
        for key in ("policy", "budget", "job_ids", "evidence_ids", "freeze_ids",
                    "dossier_ids", "committee_runs", "unresolved_questions"):
            raw: object = json.loads(str(row[key]))
            doc[key] = raw
        for key in ("final_result", "failure"):
            value = row[key]
            raw_opt: object = json.loads(str(value)) if value is not None else None
            doc[key] = raw_opt
        return ResearchSession.from_dict(doc, "<research.sqlite>")

    # -- jobs ----------------------------------------------------------

    def save_job(self, job: Job) -> None:
        """Upsert one job row."""
        job.validate("<research.sqlite>")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, session_id, wave_id, parent_job_id, job_type, owner,"
                " source_domain, status, created_at, started_at, completed_at, deadline, model,"
                " token_budget, tool_budget, child_budget, result, diagnostics, failure)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.job_id, job.session_id, job.wave_id, job.parent_job_id, job.job_type, job.owner,
                    job.source_domain, job.status, job.created_at.isoformat(),
                    job.started_at.isoformat() if job.started_at is not None else None,
                    job.completed_at.isoformat() if job.completed_at is not None else None,
                    job.deadline.isoformat() if job.deadline is not None else None,
                    job.model, job.token_budget, job.tool_budget, job.child_budget,
                    json.dumps(job.to_dict()["result"], sort_keys=True) if job.result is not None else None,
                    json.dumps(job.to_dict()["diagnostics"], sort_keys=True),
                    json.dumps(job.to_dict()["failure"], sort_keys=True) if job.failure is not None else None,
                ),
            )

    def get_job(self, job_id: str) -> Job:
        """Load one job; raises KeyError when absent."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job_id: {job_id!r}")
        return self._row_to_job(row)

    def list_jobs(self, session_id: str) -> list[Job]:
        """All jobs for one session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at, rowid", (session_id,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        doc: dict[str, object] = {
            "job_id": row["job_id"], "session_id": row["session_id"], "wave_id": row["wave_id"],
            "parent_job_id": row["parent_job_id"], "job_type": row["job_type"], "owner": row["owner"],
            "source_domain": row["source_domain"], "status": row["status"], "created_at": row["created_at"],
            "started_at": row["started_at"], "completed_at": row["completed_at"], "deadline": row["deadline"],
            "model": row["model"], "token_budget": row["token_budget"], "tool_budget": row["tool_budget"],
            "child_budget": row["child_budget"],
        }
        for key in ("result", "diagnostics", "failure"):
            value = row[key]
            raw_opt: object = json.loads(str(value)) if value is not None else None
            doc[key] = raw_opt if raw_opt is not None else ({} if key == "diagnostics" else None)
        return Job.from_dict(doc, "<research.sqlite>")

    # -- journal --------------------------------------------------------

    def save_event(self, event: JournalEvent) -> JournalEvent:
        """Append one journal row with a hash chain; duplicates raise ValueError."""
        event.validate("<research.sqlite>")
        with self._connect() as conn:
            last = conn.execute(
                "SELECT hash FROM journal WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
                (event.session_id,),
            ).fetchone()
            prev = str(last["hash"]) if last is not None else "GENESIS"
            digest = _chain_hash(prev, event)
            try:
                conn.execute(
                    "INSERT INTO journal (event_id, session_id, sequence, event_type, timestamp,"
                    " actor_type, actor_id, payload, previous_state, new_state, prev_hash, hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id, event.session_id, event.sequence, event.event_type,
                        event.timestamp.isoformat(), event.actor_type, event.actor_id,
                        json.dumps(event.to_dict()["payload"], sort_keys=True),
                        event.previous_state, event.new_state, prev, digest,
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"<research.sqlite>: duplicate journal event {event.event_id!r}") from None
        return event

    def list_events(self, session_id: str) -> list[JournalEvent]:
        """Session events in sequence order; raises ValueError on a broken hash chain."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM journal WHERE session_id = ? ORDER BY sequence", (session_id,)
            ).fetchall()
        events: list[JournalEvent] = []
        prev = "GENESIS"
        for row in rows:
            payload_raw: object = json.loads(str(row["payload"]))
            if not isinstance(payload_raw, dict):
                raise ValueError(f"<research.sqlite>: event {row['event_id']!r} payload must be a mapping")
            event = JournalEvent.from_dict(
                {
                    "event_id": row["event_id"], "session_id": row["session_id"], "sequence": row["sequence"],
                    "event_type": row["event_type"], "timestamp": row["timestamp"],
                    "actor_type": row["actor_type"], "actor_id": row["actor_id"],
                    "payload": payload_raw, "previous_state": row["previous_state"],
                    "new_state": row["new_state"],
                },
                "<research.sqlite>",
            )
            if str(row["prev_hash"]) != prev or str(row["hash"]) != _chain_hash(prev, event):
                raise ValueError(f"<research.sqlite>: journal chain broken at {event.event_id!r}")
            prev = str(row["hash"])
            events.append(event)
        return events

    # -- evidence / freezes (records owned by evidence.py / freeze.py) --

    def save_evidence(self, record: Mapping[str, object]) -> str:
        """Insert one evidence record; duplicate ids raise ValueError."""
        if not isinstance(record, Mapping):
            raise ValueError("<research.sqlite>: evidence record must be a mapping")
        where = "<research.sqlite>: evidence"
        evidence_id = record.get("evidence_id")
        session_id = record.get("session_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError(f"{where}: 'evidence_id' must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO evidence (evidence_id, session_id, known_at, as_of, record)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        evidence_id, session_id,
                        _iso_or_none(record.get("known_at"), "known_at", where),
                        _iso_or_none(record.get("as_of"), "as_of", where),
                        _record_json(record, where),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"{where}: duplicate evidence_id {evidence_id!r}") from None
        return evidence_id

    def get_evidence(self, evidence_id: str) -> dict[str, JSONValue]:
        """Load one evidence record; raises KeyError when absent."""
        with self._connect() as conn:
            row = conn.execute("SELECT record FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown evidence_id: {evidence_id!r}")
        return validate_json_mapping(json.loads(str(row["record"])), "<research.sqlite>: evidence")

    def list_evidence(self, session_id: str) -> list[dict[str, JSONValue]]:
        """All evidence records for one session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record FROM evidence WHERE session_id = ? ORDER BY rowid", (session_id,)
            ).fetchall()
        return [validate_json_mapping(json.loads(str(r["record"])), "<research.sqlite>: evidence") for r in rows]

    def save_freeze(self, record: Mapping[str, object]) -> str:
        """Insert one freeze record; duplicate ids raise ValueError."""
        if not isinstance(record, Mapping):
            raise ValueError("<research.sqlite>: freeze record must be a mapping")
        where = "<research.sqlite>: freeze"
        freeze_id = record.get("freeze_id")
        session_id = record.get("session_id")
        if not isinstance(freeze_id, str) or not freeze_id:
            raise ValueError(f"{where}: 'freeze_id' must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        created = _iso_or_none(record.get("created_at"), "created_at", where) or utcnow().isoformat()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO freezes (freeze_id, session_id, created_at, record)"
                    " VALUES (?, ?, ?, ?)",
                    (freeze_id, session_id, created, _record_json(record, where)),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"{where}: duplicate freeze_id {freeze_id!r}") from None
        return freeze_id

    def get_freeze(self, freeze_id: str) -> dict[str, JSONValue]:
        """Load one freeze record; raises KeyError when absent."""
        with self._connect() as conn:
            row = conn.execute("SELECT record FROM freezes WHERE freeze_id = ?", (freeze_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown freeze_id: {freeze_id!r}")
        return validate_json_mapping(json.loads(str(row["record"])), "<research.sqlite>: freeze")

    def save_dossier(self, record: Mapping[str, object]) -> str:
        """Insert one immutable dossier record; duplicate ids raise ValueError."""
        if not isinstance(record, Mapping):
            raise ValueError("<research.sqlite>: dossier record must be a mapping")
        where = "<research.sqlite>: dossier"
        dossier_id = record.get("dossier_id")
        session_id = record.get("session_id")
        if not isinstance(dossier_id, str) or not dossier_id:
            raise ValueError(f"{where}: 'dossier_id' must be a non-empty string")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        created = _iso_or_none(record.get("created_at"), "created_at", where) or utcnow().isoformat()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO dossiers (dossier_id, session_id, created_at, record)"
                    " VALUES (?, ?, ?, ?)",
                    (dossier_id, session_id, created, _record_json(record, where)),
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"{where}: duplicate dossier_id {dossier_id!r}") from None
        return dossier_id

    def get_dossier(self, dossier_id: str) -> dict[str, JSONValue]:
        """Load one dossier record; raises KeyError when absent."""
        with self._connect() as conn:
            row = conn.execute("SELECT record FROM dossiers WHERE dossier_id = ?", (dossier_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown dossier_id: {dossier_id!r}")
        return validate_json_mapping(json.loads(str(row["record"])), "<research.sqlite>: dossier")

    def list_dossiers(self, session_id: str) -> list[dict[str, JSONValue]]:
        """All dossier records for one session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record FROM dossiers WHERE session_id = ? ORDER BY created_at, dossier_id",
                (session_id,),
            ).fetchall()
        return [validate_json_mapping(json.loads(str(r["record"])), "<research.sqlite>: dossier") for r in rows]

    def resource_stores(self, session_id: str) -> dict[str, dict[str, object]]:
        """id->record mappings for read_resource: evidence/freeze/dossier/job/research."""
        session = self.get_session(session_id)
        evidence: dict[str, object] = {str(r.get("evidence_id", "")): r for r in self.list_evidence(session_id)}
        freezes: dict[str, object] = {}
        for fid in session.freeze_ids:
            try:
                freezes[fid] = self.get_freeze(fid)
            except KeyError:
                continue
        dossiers: dict[str, object] = {}
        for dossier in self.list_dossiers(session_id):
            key = dossier.get("dossier_id")
            if isinstance(key, str):
                dossiers[key] = dossier
        jobs: dict[str, object] = {j.job_id: j.to_dict() for j in self.list_jobs(session_id)}
        return {"evidence": evidence, "freeze": freezes, "dossier": dossiers,
                "job": jobs, "research": {session_id: session.to_dict()}}
    # -- resume ----------------------------------------------------------

    def resume(self, session_id: str) -> ResumeState:
        """Load session + jobs + journal without writing; completed jobs are never recreated."""
        session = self.get_session(session_id)
        jobs = self.list_jobs(session_id)
        journal_log.hydrate(session_id, self.list_events(session_id))
        open_ids = sorted(j.job_id for j in jobs if j.status in ("queued", "running"))
        return ResumeState(
            session=session,
            wave=session.current_wave,
            budgets=dict(session.budget),
            pending_next_action=pending_next_action(session, jobs),
            open_job_ids=open_ids,
        )
