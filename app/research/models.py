"""Research kernel domain models: frozen dataclasses, stdlib only.

One authoritative ResearchSession; no private state copies. All datetimes
are UTC (naive inputs normalize to UTC). Missing timestamps stay None,
never invented. JSON helpers mirror app.thesis.models locally so the
kernel stands alone.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


def validate_json_value(value: object, where: str = "<dict>") -> JSONValue:
    """Recursively normalize an object into a JSONValue (deep copy)."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: non-finite float not allowed, got {value!r}")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [validate_json_value(v, where) for v in value]
    if isinstance(value, dict):
        out: dict[str, JSONValue] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{where}: dict key must be a string, got {type(k).__name__}")
            out[k] = validate_json_value(v, where)
        return out
    raise ValueError(f"{where}: not a JSON value, got {type(value).__name__}")


def validate_json_mapping(value: object, where: str = "<dict>") -> dict[str, JSONValue]:
    """Validate untrusted payload as a JSON object."""
    validated = validate_json_value(value, where)
    if not isinstance(validated, dict):
        raise ValueError(f"{where}: must be a mapping, got {type(value).__name__}")
    return validated


def utcnow() -> datetime:
    """Current UTC timestamp."""
    return datetime.now(timezone.utc)


def normalize_time(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; aware values pass through."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def new_session_id() -> str:
    """Mint a Stockbot-owned research session id."""
    return f"rs:{uuid.uuid4()}"


def new_job_id() -> str:
    """Mint a Stockbot-owned research job id."""
    return f"job:{uuid.uuid4()}"


def new_event_id() -> str:
    """Mint a Stockbot-owned journal event id."""
    return f"jev:{uuid.uuid4()}"


class SessionStatus(StrEnum):
    """ResearchSession lifecycle states (forward flow + terminal states)."""

    CREATED = "created"
    PLANNING = "planning"
    RESEARCHING = "researching"
    FREEZING = "freezing"
    ANALYZING = "analyzing"
    TARGETED_RESEARCH = "targeted_research"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    """Worker roles; models decide relevance, infra decides budgets."""

    SOURCE_AGENT = "source_agent"
    SCOUT = "scout"
    STOCKBOT = "stockbot"
    BULLBOT = "bullbot"
    BEARBOT = "bearbot"
    SYNTHESIS = "synthesis"


class JobStatus(StrEnum):
    """Job execution states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class FailureCategory(StrEnum):
    """Closed failure vocabulary (15 values); free text lives in message."""

    TIMEOUT = "timeout"
    TOKEN_BUDGET_EXHAUSTED = "token_budget_exhausted"
    TOOL_BUDGET_EXHAUSTED = "tool_budget_exhausted"
    JOB_BUDGET_EXHAUSTED = "job_budget_exhausted"
    WAVE_BUDGET_EXHAUSTED = "wave_budget_exhausted"
    PARALLELISM_EXCEEDED = "parallelism_exceeded"
    DEPTH_EXCEEDED = "depth_exceeded"
    POLICY_DENIED = "policy_denied"
    TOOL_ERROR = "tool_error"
    MODEL_ERROR = "model_error"
    NO_EVIDENCE = "no_evidence"
    PIT_VIOLATION = "pit_violation"
    FREEZE_MISMATCH = "freeze_mismatch"
    COMMITTEE_DEADLOCK = "committee_deadlock"
    SYNTHESIS_FAILED = "synthesis_failed"


# ponytail: single nested defaults dict; per-job overrides only via explicit
# create_job kwargs. Add sections when a new job type needs children/tools.
DEFAULT_BUDGETS: dict[str, JSONValue] = {
    "research": {"max_runtime": 600, "max_total_jobs": 20, "max_parallel": 6, "max_waves": 2},
    "source": {"max_children": 4, "max_tool": 30},
    "scout": {"max_children": 0, "max_tool": 12, "max_runtime": 120},
    "committee": {"max_parallel": 3},
}


def default_policy() -> dict[str, JSONValue]:
    """Fresh copy of the full nested policy (research/source/scout/committee)."""
    out = validate_json_value(DEFAULT_BUDGETS, "<defaults>")
    assert isinstance(out, dict)
    return out


def default_budget() -> dict[str, JSONValue]:
    """Fresh copy of the session budget (mirrors the research section)."""
    out = validate_json_value(DEFAULT_BUDGETS["research"], "<defaults>")
    assert isinstance(out, dict)
    return out


DEFAULT_POLICY: dict[str, JSONValue] = default_policy()
DEFAULT_BUDGET: dict[str, JSONValue] = default_budget()

STATUS_VALUES = frozenset(e.value for e in SessionStatus)
JOB_TYPE_VALUES = frozenset(e.value for e in JobType)
JOB_STATUS_VALUES = frozenset(e.value for e in JobStatus)
FAILURE_CATEGORY_VALUES = frozenset(e.value for e in FailureCategory)

def _json_str_list(values: list[str]) -> list[JSONValue]:
    """Copy a string list into a JSON list (works around list invariance)."""
    out: list[JSONValue] = []
    for value in values:
        out.append(value)
    return out


def _req_str(d: Mapping[str, object], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _opt_str(v: object, key: str, where: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError(f"{where}: '{key}' must be a string or null, got {type(v).__name__}")
    return v


def _opt_int(v: object, key: str, where: str) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{where}: '{key}' must be an int or null, got {v!r}")
    return v


def _coerce_enum(allowed: frozenset[str], v: object, key: str, where: str) -> str:
    if isinstance(v, StrEnum):
        v = v.value
    if isinstance(v, str) and v in allowed:
        return v
    raise ValueError(f"{where}: '{key}' must be one of {sorted(allowed)}, got {v!r}")


def _coerce_time(v: object, key: str, where: str) -> datetime | None:
    """Parse an ISO-8601 string or datetime (naive normalizes to UTC); None stays None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return normalize_time(v)
    if isinstance(v, str) and v.strip():
        try:
            return normalize_time(datetime.fromisoformat(v.strip()))
        except ValueError:
            raise ValueError(f"{where}: '{key}' must be ISO-8601, got {v!r}") from None
    raise ValueError(f"{where}: '{key}' must be an ISO-8601 string, datetime, or null")


def _req_time(d: Mapping[str, object], key: str, where: str) -> datetime:
    out = _coerce_time(d.get(key), key, where)
    if out is None:
        raise ValueError(f"{where}: '{key}' must be a timestamp, got null")
    return out


def _req_list_str(d: Mapping[str, object], key: str, where: str) -> list[str]:
    v = d.get(key, [])
    if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
        raise ValueError(f"{where}: '{key}' must be a list of strings")
    return list(v)


def pit_violated(as_of: datetime | str | None, known_at: datetime | str | None) -> bool:
    """True when known_at > as_of. None on either side never violates."""
    start = _coerce_time(as_of, "as_of", "<pit>")
    known = _coerce_time(known_at, "known_at", "<pit>")
    if start is None or known is None:
        return False
    return known > start


@dataclass(frozen=True)
class Failure:
    """Categorized failure; category is closed vocabulary, detail is free text."""

    category: str
    message: str

    def validate(self, where: str = "<failure>") -> None:
        """Raise ValueError unless category is known and message non-empty."""
        _coerce_enum(FAILURE_CATEGORY_VALUES, self.category, "category", where)
        if not self.message:
            raise ValueError(f"{where}: 'message' must be a non-empty string")

    def to_dict(self) -> dict[str, JSONValue]:
        """Serialize to plain JSON-compatible dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> Failure:
        """Parse and validate; raises ValueError on malformed input."""
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: failure must be a mapping, got {type(d).__name__}")
        where = f"{_path}: failure"
        out = cls(
            category=_coerce_enum(FAILURE_CATEGORY_VALUES, d.get("category"), "category", where),
            message=_req_str(d, "message", where),
        )
        out.validate(where)
        return out


@dataclass(frozen=True)
class ResearchSession:
    """Authoritative session record; the only state holder, never copied privately."""

    session_id: str
    created_at: datetime
    updated_at: datetime
    query: str
    objective: str
    as_of: datetime | None = None
    status: str = SessionStatus.CREATED.value
    current_wave: int = 0
    policy: dict[str, JSONValue] = field(default_factory=default_policy)
    budget: dict[str, JSONValue] = field(default_factory=default_budget)
    job_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    freeze_ids: list[str] = field(default_factory=list)
    dossier_ids: list[str] = field(default_factory=list)
    committee_runs: list[JSONValue] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    final_result: dict[str, JSONValue] | None = None
    failure: Failure | None = None

    def validate(self, where: str = "<session>") -> None:
        """Raise ValueError on any contract violation."""
        if not self.session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        if not self.query:
            raise ValueError(f"{where}: 'query' must be a non-empty string")
        if not self.objective:
            raise ValueError(f"{where}: 'objective' must be a non-empty string")
        _coerce_enum(STATUS_VALUES, self.status, "status", where)
        if isinstance(self.current_wave, bool) or not isinstance(self.current_wave, int):
            raise ValueError(f"{where}: 'current_wave' must be an int")
        if self.current_wave < 0:
            raise ValueError(f"{where}: 'current_wave' must be >= 0, got {self.current_wave}")
        if self.failure is not None:
            self.failure.validate(f"{where}: failure")

    def to_dict(self) -> dict[str, JSONValue]:
        """Serialize (datetimes as ISO-8601, failure nested as dict or None)."""
        d: dict[str, JSONValue] = {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "query": self.query,
            "objective": self.objective,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "status": self.status,
            "current_wave": self.current_wave,
            "policy": validate_json_mapping(self.policy, "<session>: 'policy'"),
            "budget": validate_json_mapping(self.budget, "<session>: 'budget'"),
            "job_ids": _json_str_list(self.job_ids),
            "evidence_ids": _json_str_list(self.evidence_ids),
            "freeze_ids": _json_str_list(self.freeze_ids),
            "dossier_ids": _json_str_list(self.dossier_ids),
            "committee_runs": [validate_json_value(x, "<session>: 'committee_runs'") for x in self.committee_runs],
            "unresolved_questions": _json_str_list(self.unresolved_questions),
            "final_result": validate_json_mapping(self.final_result, "<session>: 'final_result'") if self.final_result is not None else None,
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ResearchSession:
        """Parse and validate; raises ValueError on malformed input."""
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: session must be a mapping, got {type(d).__name__}")
        where = f"{_path}: session {d.get('session_id', '?')}"
        raw_failure = d.get("failure")
        failure: Failure | None = None
        if raw_failure is not None:
            if not isinstance(raw_failure, dict):
                raise ValueError(f"{where}: 'failure' must be a mapping or null")
            failure = Failure.from_dict(raw_failure, where)
        raw_result = d.get("result", d.get("final_result"))
        final_result: dict[str, JSONValue] | None = None
        if raw_result is not None:
            final_result = validate_json_mapping(raw_result, f"{where}: 'final_result'")
        raw_runs = d.get("committee_runs", [])
        if not isinstance(raw_runs, list):
            raise ValueError(f"{where}: 'committee_runs' must be a list")
        wave = d.get("current_wave", 0)
        if isinstance(wave, bool) or not isinstance(wave, int):
            raise ValueError(f"{where}: 'current_wave' must be an int")
        out = cls(
            session_id=_req_str(d, "session_id", where),
            created_at=_req_time(d, "created_at", where),
            updated_at=_req_time(d, "updated_at", where),
            query=_req_str(d, "query", where),
            objective=_req_str(d, "objective", where),
            as_of=_coerce_time(d.get("as_of"), "as_of", where),
            status=_coerce_enum(STATUS_VALUES, d.get("status", SessionStatus.CREATED.value), "status", where),
            current_wave=wave,
            policy=validate_json_mapping(d.get("policy", {}), f"{where}: 'policy'"),
            budget=validate_json_mapping(d.get("budget", {}), f"{where}: 'budget'"),
            job_ids=_req_list_str(d, "job_ids", where),
            evidence_ids=_req_list_str(d, "evidence_ids", where),
            freeze_ids=_req_list_str(d, "freeze_ids", where),
            dossier_ids=_req_list_str(d, "dossier_ids", where),
            committee_runs=[validate_json_value(x, f"{where}: 'committee_runs'") for x in raw_runs],
            unresolved_questions=_req_list_str(d, "unresolved_questions", where),
            final_result=final_result,
            failure=failure,
        )
        out.validate(where)
        return out


@dataclass(frozen=True)
class Job:
    """One unit of delegated work; budgets enforced at creation from policy."""

    job_id: str
    session_id: str
    wave_id: int
    parent_job_id: str | None
    job_type: str
    owner: str
    source_domain: str | None = None
    status: str = JobStatus.QUEUED.value
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    deadline: datetime | None = None
    model: str | None = None
    token_budget: int | None = None
    tool_budget: int | None = None
    child_budget: int = 0
    result: dict[str, JSONValue] | None = None
    diagnostics: dict[str, JSONValue] = field(default_factory=dict)
    failure: Failure | None = None

    def validate(self, where: str = "<job>") -> None:
        """Raise ValueError on any contract violation."""
        if not self.job_id:
            raise ValueError(f"{where}: 'job_id' must be a non-empty string")
        if not self.session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        if isinstance(self.wave_id, bool) or not isinstance(self.wave_id, int):
            raise ValueError(f"{where}: 'wave_id' must be an int")
        if self.wave_id < 1:
            raise ValueError(f"{where}: 'wave_id' must be >= 1, got {self.wave_id}")
        _coerce_enum(JOB_TYPE_VALUES, self.job_type, "job_type", where)
        _coerce_enum(JOB_STATUS_VALUES, self.status, "status", where)
        if not self.owner:
            raise ValueError(f"{where}: 'owner' must be a non-empty string")
        if isinstance(self.child_budget, bool) or not isinstance(self.child_budget, int):
            raise ValueError(f"{where}: 'child_budget' must be an int")
        if self.child_budget < 0:
            raise ValueError(f"{where}: 'child_budget' must be >= 0, got {self.child_budget}")
        if self.failure is not None:
            self.failure.validate(f"{where}: failure")

    def to_dict(self) -> dict[str, JSONValue]:
        """Serialize (datetimes as ISO-8601, failure/result nested or None)."""
        return {
            "job_id": self.job_id,
            "session_id": self.session_id,
            "wave_id": self.wave_id,
            "parent_job_id": self.parent_job_id,
            "job_type": self.job_type,
            "owner": self.owner,
            "source_domain": self.source_domain,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at is not None else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at is not None else None,
            "deadline": self.deadline.isoformat() if self.deadline is not None else None,
            "model": self.model,
            "token_budget": self.token_budget,
            "tool_budget": self.tool_budget,
            "child_budget": self.child_budget,
            "result": validate_json_mapping(self.result, "<job>: 'result'") if self.result is not None else None,
            "diagnostics": validate_json_mapping(self.diagnostics, "<job>: 'diagnostics'"),
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> Job:
        """Parse and validate; raises ValueError on malformed input."""
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: job must be a mapping, got {type(d).__name__}")
        where = f"{_path}: job {d.get('job_id', '?')}"
        raw_failure = d.get("failure")
        failure: Failure | None = None
        if raw_failure is not None:
            if not isinstance(raw_failure, dict):
                raise ValueError(f"{where}: 'failure' must be a mapping or null")
            failure = Failure.from_dict(raw_failure, where)
        raw_result = d.get("result")
        result: dict[str, JSONValue] | None = None
        if raw_result is not None:
            result = validate_json_mapping(raw_result, f"{where}: 'result'")
        children = d.get("child_budget", 0)
        if isinstance(children, bool) or not isinstance(children, int):
            raise ValueError(f"{where}: 'child_budget' must be an int")
        out = cls(
            job_id=_req_str(d, "job_id", where),
            session_id=_req_str(d, "session_id", where),
            wave_id=_req_time_wave(d, where),
            parent_job_id=_opt_str(d.get("parent_job_id"), "parent_job_id", where),
            job_type=_coerce_enum(JOB_TYPE_VALUES, d.get("job_type"), "job_type", where),
            owner=_req_str(d, "owner", where),
            source_domain=_opt_str(d.get("source_domain"), "source_domain", where),
            status=_coerce_enum(JOB_STATUS_VALUES, d.get("status", JobStatus.QUEUED.value), "status", where),
            created_at=_req_time(d, "created_at", where),
            started_at=_coerce_time(d.get("started_at"), "started_at", where),
            completed_at=_coerce_time(d.get("completed_at"), "completed_at", where),
            deadline=_coerce_time(d.get("deadline"), "deadline", where),
            model=_opt_str(d.get("model"), "model", where),
            token_budget=_opt_int(d.get("token_budget"), "token_budget", where),
            tool_budget=_opt_int(d.get("tool_budget"), "tool_budget", where),
            child_budget=children,
            result=result,
            diagnostics=validate_json_mapping(d.get("diagnostics", {}), f"{where}: 'diagnostics'"),
            failure=failure,
        )
        out.validate(where)
        return out


def _req_time_wave(d: Mapping[str, object], where: str) -> int:
    wave = d.get("wave_id")
    if isinstance(wave, bool) or not isinstance(wave, int):
        raise ValueError(f"{where}: 'wave_id' must be an int")
    return wave


@dataclass(frozen=True)
class JournalEvent:
    """One append-only journal record; sequence is per-session, 1-based."""

    event_id: str
    session_id: str
    sequence: int
    event_type: str
    timestamp: datetime
    actor_type: str
    actor_id: str
    payload: dict[str, JSONValue] = field(default_factory=dict)
    previous_state: str | None = None
    new_state: str | None = None

    def validate(self, where: str = "<journal>") -> None:
        """Raise ValueError on any contract violation."""
        if not self.event_id:
            raise ValueError(f"{where}: 'event_id' must be a non-empty string")
        if not self.session_id:
            raise ValueError(f"{where}: 'session_id' must be a non-empty string")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError(f"{where}: 'sequence' must be an int")
        if self.sequence < 1:
            raise ValueError(f"{where}: 'sequence' must be >= 1, got {self.sequence}")
        if not self.event_type:
            raise ValueError(f"{where}: 'event_type' must be a non-empty string")
        if not self.actor_type:
            raise ValueError(f"{where}: 'actor_type' must be a non-empty string")
        if not self.actor_id:
            raise ValueError(f"{where}: 'actor_id' must be a non-empty string")

    def to_dict(self) -> dict[str, JSONValue]:
        """Serialize to plain JSON-compatible dict."""
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "payload": validate_json_mapping(self.payload, "<journal>: 'payload'"),
            "previous_state": self.previous_state,
            "new_state": self.new_state,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> JournalEvent:
        """Parse and validate; raises ValueError on malformed input."""
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: journal event must be a mapping, got {type(d).__name__}")
        where = f"{_path}: event {d.get('event_id', '?')}"
        seq = d.get("sequence")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError(f"{where}: 'sequence' must be an int")
        out = cls(
            event_id=_req_str(d, "event_id", where),
            session_id=_req_str(d, "session_id", where),
            sequence=seq,
            event_type=_req_str(d, "event_type", where),
            timestamp=_req_time(d, "timestamp", where),
            actor_type=_req_str(d, "actor_type", where),
            actor_id=_req_str(d, "actor_id", where),
            payload=validate_json_mapping(d.get("payload", {}), f"{where}: 'payload'"),
            previous_state=_opt_str(d.get("previous_state"), "previous_state", where),
            new_state=_opt_str(d.get("new_state"), "new_state", where),
        )
        out.validate(where)
        return out
