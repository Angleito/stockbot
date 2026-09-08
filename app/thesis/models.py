"""Minimal thesis domain model: frozen dataclasses with validation.

Conventions: frozen dataclasses + ``to_dict``/``from_dict`` + ``validate()``
raising ``ValueError`` with path/file-specific messages. IDs are Stockbot-owned
(``uuid.uuid4`` with prefixes); folder slugs are human-readable, never identity.
Unknown/unavailable optional values serialize as the literal ``"unknown"``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = 1
UNKNOWN = "unknown"

_RESERVED_SLUGS = {".", "..", ""}


class ThesisStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class ClaimStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    SUPPORTED = "supported"
    CHALLENGED = "challenged"
    INVALIDATED = "invalidated"
    UNRESOLVED = "unresolved"


class ExpressionStatus(StrEnum):
    UNDECIDED = "undecided"
    ACTIVE = "active"
    FLAGGED = "flagged"
    CLOSED = "closed"


class Instrument(StrEnum):
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"
    BOND = "bond"
    CASH = "cash"
    UNKNOWN = "unknown"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class TriggerStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"


class TriggerImportance(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


# Semantic monitor names (never tool names). Unknown values stay
# disabled/unsupported rather than being rejected outright.
KNOWN_WATCH_TYPES = frozenset(
    {
        "new_filing",
        "filing_change",
        "new_material_event",
        "new_short_interest_cycle",
        "material_short_interest_change",
        "new_external_evidence",
        "explicit_thesis_invalidator",
        "scheduled_deep_review",
    }
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _req_str(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _opt_unknown(v: object) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return UNKNOWN
    return str(v)


def _coerce_enum(enum_cls: type[StrEnum], v: object, key: str, where: str) -> str:
    if isinstance(v, enum_cls):
        return v.value
    if isinstance(v, str) and v in {e.value for e in enum_cls}:
        return v
    raise ValueError(f"{where}: '{key}' must be one of {[e.value for e in enum_cls]}, got {v!r}")


def new_thesis_id() -> str:
    return f"thesis:{uuid.uuid4()}"


def new_claim_id() -> str:
    return f"claim:{uuid.uuid4()}"


def new_expression_id() -> str:
    return f"expr:{uuid.uuid4()}"


def new_requirement_id() -> str:
    return f"req:{uuid.uuid4()}"


def new_rule_id() -> str:
    return f"rule:{uuid.uuid4()}"


def new_trigger_id() -> str:
    return f"trigger:{uuid.uuid4()}"


def new_question_id() -> str:
    return f"q:{uuid.uuid4()}"


def new_memory_id() -> str:
    return f"mem:{uuid.uuid4()}"


def new_evidence_id() -> str:
    return f"ev:{uuid.uuid4()}"


def new_journal_id() -> str:
    return f"journal:{uuid.uuid4()}"


def slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumeric runs to one hyphen, trim hyphens."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug or slug in _RESERVED_SLUGS:
        raise ValueError(f"cannot slugify to a usable value: {text!r}")
    return slug


@dataclass(frozen=True)
class ThesisClaim:
    claim_id: str
    statement: str
    status: str = ClaimStatus.UNVALIDATED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> ThesisClaim:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: claim must be a mapping, got {type(d).__name__}")
        where = f"{_path}: claim {d.get('claim_id', '?')}"
        return cls(
            claim_id=_req_str(d, "claim_id", where),
            statement=_req_str(d, "statement", where),
            status=_coerce_enum(ClaimStatus, d.get("status", ClaimStatus.UNVALIDATED.value), "status", where),
        )


@dataclass(frozen=True)
class TradeExpression:
    expression_id: str
    intent: str = UNKNOWN
    instrument: str = Instrument.UNKNOWN.value
    direction: str = Direction.UNKNOWN.value
    structure: str = UNKNOWN  # open vocabulary, kept unchanged
    horizon: str = UNKNOWN
    leverage: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    deterministic_support: str = UNKNOWN
    status: str = ExpressionStatus.UNDECIDED.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> TradeExpression:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: expression must be a mapping, got {type(d).__name__}")
        where = f"{_path}: expression {d.get('expression_id', '?')}"
        instrument = d.get("instrument", UNKNOWN)
        instrument = UNKNOWN if instrument in (None, "") else str(instrument)
        if instrument not in {e.value for e in Instrument}:
            raise ValueError(f"{where}: 'instrument' must be a known primitive or 'unknown', got {instrument!r}")
        direction = d.get("direction", UNKNOWN)
        direction = UNKNOWN if direction in (None, "") else str(direction)
        if direction not in {e.value for e in Direction}:
            raise ValueError(f"{where}: 'direction' must be a known primitive or 'unknown', got {direction!r}")
        structure = d.get("structure", UNKNOWN)
        if not isinstance(structure, str) or not structure.strip():
            raise ValueError(f"{where}: 'structure' must be a non-empty string (open vocabulary)")
        for key in ("leverage", "parameters"):
            if key in d and d[key] is not None and not isinstance(d[key], dict):
                raise ValueError(f"{where}: '{key}' must be a mapping, got {type(d[key]).__name__}")
        return cls(
            expression_id=_req_str(d, "expression_id", where),
            intent=_opt_unknown(d.get("intent", UNKNOWN)),
            instrument=instrument,
            direction=direction,
            structure=structure,  # accepted unchanged
            horizon=_opt_unknown(d.get("horizon", UNKNOWN)),
            leverage=dict(d.get("leverage") or {}),
            parameters=dict(d.get("parameters") or {}),
            deterministic_support=_opt_unknown(d.get("deterministic_support", UNKNOWN)),
            status=_coerce_enum(ExpressionStatus, d.get("status", ExpressionStatus.UNDECIDED.value), "status", where),
        )


@dataclass(frozen=True)
class ExpressionRequirement:
    requirement_id: str
    expression_id: str  # parent
    requirement_type: str  # open vocabulary
    statement: str
    status: str = QuestionStatus.OPEN.value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> ExpressionRequirement:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: requirement must be a mapping, got {type(d).__name__}")
        where = f"{_path}: requirement {d.get('requirement_id', '?')}"
        rtype = d.get("requirement_type")
        if not isinstance(rtype, str) or not rtype.strip():
            raise ValueError(f"{where}: 'requirement_type' must be a non-empty string (open vocabulary)")
        return cls(
            requirement_id=_req_str(d, "requirement_id", where),
            expression_id=_req_str(d, "expression_id", where),
            requirement_type=rtype,
            statement=_req_str(d, "statement", where),
            status=_coerce_enum(QuestionStatus, d.get("status", QuestionStatus.OPEN.value), "status", where),
        )


@dataclass(frozen=True)
class WatchRule:
    rule_id: str
    rule_type: str  # semantic monitor name, never a tool name
    enabled: bool = True
    support_status: str = SupportStatus.SUPPORTED.value
    support_reason: str = ""
    claim_ids: tuple[str, ...] = ()
    expression_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["claim_ids"] = list(self.claim_ids)
        d["expression_ids"] = list(self.expression_ids)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> WatchRule:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: watch rule must be a mapping, got {type(d).__name__}")
        where = f"{_path}: rule {d.get('rule_id', '?')}"
        rtype = d.get("rule_type")
        if not isinstance(rtype, str) or not rtype.strip():
            raise ValueError(f"{where}: 'rule_type' must be a non-empty semantic monitor name")
        enabled = d.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{where}: 'enabled' must be a bool")
        support = _coerce_enum(
            SupportStatus, d.get("support_status", SupportStatus.SUPPORTED.value), "support_status", where
        )
        if rtype not in KNOWN_WATCH_TYPES and (enabled or support != SupportStatus.UNSUPPORTED.value):
            raise ValueError(
                f"{where}: unknown rule_type {rtype!r} must stay disabled/unsupported "
                "(enabled: false, support_status: unsupported)"
            )
        for key in ("claim_ids", "expression_ids"):
            ids = d.get(key, [])
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise ValueError(f"{where}: '{key}' must be a list of IDs")
        return cls(
            rule_id=_req_str(d, "rule_id", where),
            rule_type=rtype,
            enabled=enabled,
            support_status=support,
            support_reason=str(d.get("support_reason", "")),
            claim_ids=tuple(d.get("claim_ids", [])),
            expression_ids=tuple(d.get("expression_ids", [])),
        )


def require_watch_targets(rule_like: Any, where: str) -> None:
    """Reject targetless watch rules: need >=1 non-empty claim or expression ID."""
    cids = getattr(rule_like, "claim_ids", None)
    eids = getattr(rule_like, "expression_ids", None)
    if cids is None or eids is None:
        if isinstance(rule_like, dict):
            cids, eids = rule_like.get("claim_ids", []), rule_like.get("expression_ids", [])
        else:
            cids, eids = list[str](), list[str]()
    if not [c for c in (cids or []) if isinstance(c, str) and c]:
        if not [e for e in (eids or []) if isinstance(e, str) and e]:
            raise ValueError(f"{where}: watch rule must name at least one claim_id or expression_id")


@dataclass(frozen=True)
class Trigger:
    trigger_id: str
    thesis_id: str
    created_at: str
    status: str = TriggerStatus.PENDING.value
    trigger_type: str = "new_external_evidence"  # semantic, never a tool name
    importance: str = TriggerImportance.MEDIUM.value
    claim_ids: tuple[str, ...] = ()
    expression_ids: tuple[str, ...] = ()
    canonical_refs: tuple[str, ...] = ()
    summary: str = ""
    processed_at: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["claim_ids"] = list(self.claim_ids)
        d["expression_ids"] = list(self.expression_ids)
        d["canonical_refs"] = list(self.canonical_refs)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> Trigger:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: trigger must be a mapping, got {type(d).__name__}")
        where = f"{_path}: trigger {d.get('trigger_id', '?')}"
        for key in ("claim_ids", "expression_ids", "canonical_refs"):
            vals = d.get(key, [])
            if not isinstance(vals, list) or not all(isinstance(i, str) for i in vals):
                raise ValueError(f"{where}: '{key}' must be a list of strings")
        meta = d.get("metadata", {})
        if meta is not None and not isinstance(meta, dict):
            raise ValueError(f"{where}: 'metadata' must be a mapping")
        return cls(
            trigger_id=_req_str(d, "trigger_id", where),
            thesis_id=_req_str(d, "thesis_id", where),
            created_at=_req_str(d, "created_at", where),
            status=_coerce_enum(TriggerStatus, d.get("status", TriggerStatus.PENDING.value), "status", where),
            trigger_type=_req_str(d, "trigger_type", where),
            importance=_coerce_enum(
                TriggerImportance, d.get("importance", TriggerImportance.MEDIUM.value), "importance", where
            ),
            claim_ids=tuple(d.get("claim_ids", [])),
            expression_ids=tuple(d.get("expression_ids", [])),
            canonical_refs=tuple(d.get("canonical_refs", [])),
            summary=str(d.get("summary", "")),
            processed_at=d.get("processed_at"),
            run_id=d.get("run_id"),
            metadata=dict(meta or {}),
        )


@dataclass(frozen=True)
class Thesis:
    thesis_id: str
    slug: str
    status: str = ThesisStatus.ACTIVE.value
    created_at: str = ""
    updated_at: str = ""
    user_thesis: str = ""
    scope: str = UNKNOWN
    claims: tuple[ThesisClaim, ...] = ()
    assumptions: tuple[str, ...] = ()
    invalidators: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    expressions: tuple[TradeExpression, ...] = ()
    requirements: tuple[ExpressionRequirement, ...] = ()
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        d["claims"] = [c.to_dict() if isinstance(c, ThesisClaim) else c for c in self.claims]
        d["expressions"] = [e.to_dict() if isinstance(e, TradeExpression) else e for e in self.expressions]
        d["requirements"] = [r.to_dict() if isinstance(r, ExpressionRequirement) else r for r in self.requirements]
        d["assumptions"] = list(self.assumptions)
        d["invalidators"] = list(self.invalidators)
        d["unknowns"] = list(self.unknowns)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> Thesis:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: thesis must be a mapping, got {type(d).__name__}")
        where = f"{_path}: thesis {d.get('thesis_id', '?')}"
        for key in ("assumptions", "invalidators", "unknowns"):
            vals = d.get(key, [])
            if not isinstance(vals, list) or not all(isinstance(i, str) for i in vals):
                raise ValueError(f"{where}: '{key}' must be a list of strings")
        claims = [ThesisClaim.from_dict(c, _path) for c in d.get("claims", [])]
        expressions = [TradeExpression.from_dict(e, _path) for e in d.get("expressions", [])]
        requirements = [ExpressionRequirement.from_dict(r, _path) for r in d.get("requirements", [])]
        thesis = cls(
            thesis_id=_req_str(d, "thesis_id", where),
            slug=_req_str(d, "slug", where),
            status=_coerce_enum(ThesisStatus, d.get("status", ThesisStatus.ACTIVE.value), "status", where),
            created_at=str(d.get("created_at", "") or ""),
            updated_at=str(d.get("updated_at", "") or ""),
            user_thesis=_req_str(d, "user_thesis", where),
            scope=_opt_unknown(d.get("scope", UNKNOWN)),
            claims=tuple(claims),
            assumptions=tuple(d.get("assumptions", [])),
            invalidators=tuple(d.get("invalidators", [])),
            unknowns=tuple(d.get("unknowns", [])),
            expressions=tuple(expressions),
            requirements=tuple(requirements),
        )
        thesis.validate(_path)
        return thesis

    def validate(self, _path: str = "<thesis>") -> None:
        where = f"{_path}: thesis {self.thesis_id}"
        seen: set[str] = set()
        for c in self.claims:
            if c.claim_id in seen:
                raise ValueError(f"{where}: duplicate ID {c.claim_id!r}")
            seen.add(c.claim_id)
        for e in self.expressions:
            if e.expression_id in seen:
                raise ValueError(f"{where}: duplicate ID {e.expression_id!r}")
            seen.add(e.expression_id)
        expr_ids = {e.expression_id for e in self.expressions}
        for r in self.requirements:
            if r.requirement_id in seen:
                raise ValueError(f"{where}: duplicate ID {r.requirement_id!r}")
            seen.add(r.requirement_id)
            if r.expression_id not in expr_ids:
                raise ValueError(f"{where}: requirement {r.requirement_id!r} references absent expression {r.expression_id!r}")


# --- Nested current-state records (only what the six files need) ---


@dataclass(frozen=True)
class ThesisState:
    thesis_id: str
    assessment: str = "unresolved"
    claim_assessments: dict[str, Any] = field(default_factory=dict)
    expression_assessments: dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}
    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> ThesisState:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: state must be a mapping")
        where = f"{_path}: state {d.get('thesis_id', '?')}"
        for key in ("claim_assessments", "expression_assessments"):
            if key in d and not isinstance(d[key], dict):
                raise ValueError(f"{where}: '{key}' must be a mapping")
        return cls(
            thesis_id=_req_str(d, "thesis_id", where),
            assessment=str(d.get("assessment", "unresolved")),
            claim_assessments=dict(d.get("claim_assessments", {})),
            expression_assessments=dict(d.get("expression_assessments", {})),
        )


@dataclass(frozen=True)
class ThesisQuestion:
    question_id: str
    text: str
    status: str = QuestionStatus.OPEN.value
    answer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> ThesisQuestion:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: question must be a mapping")
        where = f"{_path}: question {d.get('question_id', '?')}"
        return cls(
            question_id=_req_str(d, "question_id", where),
            text=_req_str(d, "text", where),
            status=_coerce_enum(QuestionStatus, d.get("status", QuestionStatus.OPEN.value), "status", where),
            answer=d.get("answer"),
        )


@dataclass(frozen=True)
class ThesisMemory:
    memory_id: str
    text: str
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> ThesisMemory:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: memory must be a mapping")
        where = f"{_path}: memory {d.get('memory_id', '?')}"
        return cls(
            memory_id=_req_str(d, "memory_id", where),
            text=_req_str(d, "text", where),
            created_at=str(d.get("created_at", "") or ""),
        )


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    thesis_id: str
    canonical_ref: str
    summary: str = ""
    known_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> EvidenceRef:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: evidence ref must be a mapping")
        where = f"{_path}: evidence {d.get('evidence_id', '?')}"
        return cls(
            evidence_id=_req_str(d, "evidence_id", where),
            thesis_id=_req_str(d, "thesis_id", where),
            canonical_ref=_req_str(d, "canonical_ref", where),
            summary=str(d.get("summary", "")),
            known_at=str(d.get("known_at", "") or ""),
        )


@dataclass(frozen=True)
class Checkpoint:
    thesis_id: str
    sources: dict[str, Any] = field(default_factory=dict)
    recent_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, d: dict[str, Any], _path: str = "<dict>") -> Checkpoint:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: checkpoint must be a mapping")
        where = f"{_path}: checkpoint {d.get('thesis_id', '?')}"
        if "sources" in d and not isinstance(d["sources"], dict):
            raise ValueError(f"{where}: 'sources' must be a mapping")
        hashes = d.get("recent_hashes", [])
        if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
            raise ValueError(f"{where}: 'recent_hashes' must be a list of strings")
        return cls(thesis_id=_req_str(d, "thesis_id", where), sources=dict(d.get("sources", {})),
                   recent_hashes=list(hashes))


class HistoricalStateUnavailable(ValueError):
    """Raised when no state snapshot exists at or before a requested cutoff."""


@dataclass(frozen=True)
class ThesisStateSnapshot:
    thesis_id: str
    version: int
    effective_at: str
    recorded_at: str
    reason: str
    run_id: str = ""
    trigger_id: str = ""
    thesis: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    questions: dict[str, Any] = field(default_factory=dict)
    watch: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "thesis_id": self.thesis_id,
            "version": self.version,
            "effective_at": self.effective_at,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
            "run_id": self.run_id,
            "trigger_id": self.trigger_id,
            "thesis": self.thesis,
            "state": self.state,
            "questions": self.questions,
            "watch": self.watch,
            "memory": self.memory,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "<dict>") -> ThesisStateSnapshot:
        from app.thesis.monitor import _as_dt  # local: avoid import cycle

        if not isinstance(d, dict):
            raise ValueError(f"{where}: snapshot must be a mapping, got {type(d).__name__}")
        version = d.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"{where}: 'version' must be an int >= 1, got {version!r}")
        thesis_id = d.get("thesis_id")
        if not isinstance(thesis_id, str) or not thesis_id:
            raise ValueError(f"{where}: 'thesis_id' must be a non-empty string")
        for key in ("effective_at", "recorded_at"):
            v = d.get(key)
            if not isinstance(v, str) or not v or _as_dt(v) is None:
                raise ValueError(f"{where}: '{key}' must be a parseable ISO-8601 timestamp, got {v!r}")
        reason = d.get("reason")
        if not isinstance(reason, str) or not reason:
            raise ValueError(f"{where}: 'reason' must be a non-empty string")
        try:
            thesis = d.get("thesis", {})
            state = d.get("state", {})
            questions = d.get("questions", {})
            watch = d.get("watch", {})
            memory = d.get("memory", {})
            for key, payload in (
                ("thesis", thesis),
                ("state", state),
                ("questions", questions),
                ("watch", watch),
                ("memory", memory),
            ):
                if not isinstance(payload, dict):
                    raise ValueError(f"{where}: '{key}' must be a mapping")
                pid = payload.get("thesis_id")
                if pid != thesis_id:
                    raise ValueError(f"{where}: '{key}' thesis_id {pid!r} != snapshot thesis_id {thesis_id!r}")
            Thesis.from_dict(dict(thesis), where)
            ThesisState.from_dict(dict(state), where)
            for q in questions.get("questions", []):
                ThesisQuestion.from_dict(q, where)
            for r in watch.get("rules", []):
                WatchRule.from_dict(r, where)
            for m in memory.get("memories", []):
                ThesisMemory.from_dict(m, where)
        except ValueError as e:
            if str(e).startswith(where):
                raise
            raise ValueError(f"{where}: {e}") from e
        return cls(
            thesis_id=thesis_id,
            version=version,
            effective_at=d["effective_at"],
            recorded_at=d["recorded_at"],
            reason=reason,
            run_id=str(d.get("run_id", "") or ""),
            trigger_id=str(d.get("trigger_id", "") or ""),
            thesis=dict(thesis),
            state=dict(state),
            questions=dict(questions),
            watch=dict(watch),
            memory=dict(memory),
        )
