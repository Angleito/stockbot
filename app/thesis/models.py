"""Minimal thesis domain model: frozen dataclasses with validation.

Conventions: frozen dataclasses + ``to_dict``/``from_dict`` + ``validate()``
raising ``ValueError`` with path/file-specific messages. IDs are Stockbot-owned
(``uuid.uuid4`` with prefixes); folder slugs are human-readable, never identity.
Unknown/unavailable optional values serialize as the literal ``"unknown"``.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


def validate_json_value(value: object, where: str = "<dict>") -> JSONValue:
    """Recursively normalize an object into a valid JSONValue (deep copy). Tuples normalize to lists; non-finite floats are rejected."""
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
                raise ValueError(f"{where}: dict key must be a string, got {type(k).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
            out[k] = validate_json_value(v, where)
        return out
    raise ValueError(f"{where}: not a JSON value, got {type(value).__name__}")


def validate_json_mapping(value: object, where: str = "<dict>") -> dict[str, JSONValue]:
    """Validate untrusted payload as a JSON object (narrowed dict for strict fields)."""
    validated = validate_json_value(value, where)
    if not isinstance(validated, dict):
        raise ValueError(f"{where}: must be a mapping, got {type(value).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return validated

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


def _req_str(d: Mapping[str, object], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _opt_unknown(v: object) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return UNKNOWN
    return str(v)


def _coerce_vocab(value: object, allowed: set[str], key: str, where: str) -> str:
    """Blank/None normalizes to unknown; other values must name a known primitive."""
    text = UNKNOWN if value in (None, "") else str(value)
    if text not in allowed:
        raise ValueError(f"{where}: {key!r} must be a known primitive or 'unknown', got {text!r}")
    return text


def _coerce_open_text(value: object, key: str, where: str) -> str:
    """Open-vocabulary text; must stay a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {key!r} must be a non-empty string (open vocabulary)")
    return value


def _coerce_id_list(value: object, key: str, where: str, noun: str = "IDs") -> list[str]:
    """List of string IDs; every entry must be a string."""
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError(f"{where}: {key!r} must be a list of {noun}")
    return list(value)


def _coerce_rule_type(value: object, where: str) -> str:
    """Semantic monitor name; must stay a non-empty name."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: 'rule_type' must be a non-empty semantic monitor name")
    return value


def _coerce_meta_mapping(value: object, where: str) -> dict[str, object]:
    """Trigger metadata mapping; None normalizes to {}."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where}: 'metadata' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _coerce_str_list(value: object, key: str, where: str) -> list[str]:
    """List of strings for thesis text fields."""
    if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
        raise ValueError(f"{where}: {key!r} must be a list of strings")
    return list(value)


def _coerce_optional_mapping(value: object, key: str, where: str) -> dict[str, object]:
    """Optional mapping field; None normalizes to {}."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where}: {key!r} must be a mapping, got {type(value).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _coerce_optional_str(value: object, key: str, where: str) -> str | None:
    """Optional string-or-null field."""
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{where}: {key!r} must be a string or null")
    return value


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

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ThesisClaim:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: claim must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
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
    leverage: dict[str, JSONValue] = field(default_factory=dict)
    parameters: dict[str, JSONValue] = field(default_factory=dict)
    deterministic_support: str = UNKNOWN
    status: str = ExpressionStatus.UNDECIDED.value

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> TradeExpression:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: expression must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: expression {d.get('expression_id', '?')}"
        instrument = _coerce_vocab(d.get("instrument", UNKNOWN), {e.value for e in Instrument}, "instrument", where)
        direction = _coerce_vocab(d.get("direction", UNKNOWN), {e.value for e in Direction}, "direction", where)
        leverage = _coerce_optional_mapping(d.get("leverage"), "leverage", where)
        parameters = _coerce_optional_mapping(d.get("parameters"), "parameters", where)
        return cls(
            expression_id=_req_str(d, "expression_id", where),
            intent=_opt_unknown(d.get("intent", UNKNOWN)),
            instrument=instrument,
            direction=direction,
            structure=_coerce_open_text(d.get("structure", UNKNOWN), "structure", where),
            horizon=_opt_unknown(d.get("horizon", UNKNOWN)),
            leverage=validate_json_mapping(leverage, f"{where}: 'leverage'"),
            parameters=validate_json_mapping(parameters, f"{where}: 'parameters'"),
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

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ExpressionRequirement:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: requirement must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: requirement {d.get('requirement_id', '?')}"
        rtype = _coerce_open_text(d.get("requirement_type"), "requirement_type", where)
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

    def to_dict(self) -> dict[str, JSONValue]:
        d = asdict(self)
        d["claim_ids"] = list(self.claim_ids)
        d["expression_ids"] = list(self.expression_ids)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> WatchRule:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: watch rule must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: rule {d.get('rule_id', '?')}"
        rtype = _coerce_rule_type(d.get("rule_type"), where)
        enabled = _coerce_rule_enabled(d.get("enabled", True), where)
        support = _coerce_enum(
            SupportStatus, d.get("support_status", SupportStatus.SUPPORTED.value), "support_status", where
        )
        _check_rule_known(rtype, enabled, support, where)
        claim_ids = _coerce_id_list(d.get("claim_ids", []), "claim_ids", where)
        expression_ids = _coerce_id_list(d.get("expression_ids", []), "expression_ids", where)
        return cls(
            rule_id=_req_str(d, "rule_id", where),
            rule_type=rtype,
            enabled=enabled,
            support_status=support,
            support_reason=str(d.get("support_reason", "")),
            claim_ids=tuple(claim_ids) if isinstance(claim_ids, list) else (),
            expression_ids=tuple(expression_ids) if isinstance(expression_ids, list) else (),
        )


def _coerce_rule_enabled(value: object, where: str) -> bool:
    """Enabled flag; must stay a bool."""
    if not isinstance(value, bool):
        raise ValueError(f"{where}: 'enabled' must be a bool")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _check_rule_known(rtype: str, enabled: bool, support: str, where: str) -> None:
    """Unknown rule types must stay disabled/unsupported, never live."""
    if rtype not in KNOWN_WATCH_TYPES and (enabled or support != SupportStatus.UNSUPPORTED.value):
        raise ValueError(
            f"{where}: unknown rule_type {rtype!r} must stay disabled/unsupported "
            "(enabled: false, support_status: unsupported)"
        )


def _watch_target_ids(ids: object) -> list[str]:
    """Non-empty string target IDs from a claim/expression ID field."""
    if not isinstance(ids, (list, tuple)):
        return []
    return [c for c in ids if isinstance(c, str) and c]


def _rule_target_lists(rule_like: WatchRule | Mapping[str, object]) -> tuple[object, object]:
    """(claim_ids, expression_ids) for a rule model or mapping."""
    if isinstance(rule_like, Mapping):
        return rule_like.get("claim_ids", []), rule_like.get("expression_ids", [])
    return rule_like.claim_ids, rule_like.expression_ids


def require_watch_targets(rule_like: WatchRule | Mapping[str, object], where: str) -> None:
    """Reject targetless watch rules: need >=1 non-empty claim or expression ID."""
    cids, eids = _rule_target_lists(rule_like)
    if not _watch_target_ids(cids) and not _watch_target_ids(eids):
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
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        d = asdict(self)
        d["claim_ids"] = list(self.claim_ids)
        d["expression_ids"] = list(self.expression_ids)
        d["canonical_refs"] = list(self.canonical_refs)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> Trigger:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: trigger must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: trigger {d.get('trigger_id', '?')}"
        claim_ids = _coerce_id_list(d.get("claim_ids", []), "claim_ids", where, "strings")
        expression_ids = _coerce_id_list(d.get("expression_ids", []), "expression_ids", where, "strings")
        canonical_refs = _coerce_id_list(d.get("canonical_refs", []), "canonical_refs", where, "strings")
        processed_at = _coerce_optional_str(d.get("processed_at"), "processed_at", where)
        run_id = _coerce_optional_str(d.get("run_id"), "run_id", where)
        meta = _coerce_meta_mapping(d.get("metadata", {}), where)
        return cls(
            trigger_id=_req_str(d, "trigger_id", where),
            thesis_id=_req_str(d, "thesis_id", where),
            created_at=_req_str(d, "created_at", where),
            status=_coerce_enum(TriggerStatus, d.get("status", TriggerStatus.PENDING.value), "status", where),
            trigger_type=_req_str(d, "trigger_type", where),
            importance=_coerce_enum(
                TriggerImportance, d.get("importance", TriggerImportance.MEDIUM.value), "importance", where
            ),
            claim_ids=tuple(claim_ids) if isinstance(claim_ids, list) else (),
            expression_ids=tuple(expression_ids) if isinstance(expression_ids, list) else (),
            canonical_refs=tuple(canonical_refs) if isinstance(canonical_refs, list) else (),
            summary=str(d.get("summary", "")),
            processed_at=processed_at,
            run_id=run_id,
            metadata=validate_json_mapping(meta or {}, f"{where}: 'metadata'"),
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
    def to_dict(self) -> dict[str, JSONValue]:
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
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> Thesis:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: thesis must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: thesis {d.get('thesis_id', '?')}"
        assumptions = _coerce_str_list(d.get("assumptions", []), "assumptions", where)
        invalidators = _coerce_str_list(d.get("invalidators", []), "invalidators", where)
        unknowns = _coerce_str_list(d.get("unknowns", []), "unknowns", where)
        claims = _thesis_claim_rows(d, _path, where)
        expressions = _thesis_expression_rows(d, _path, where)
        requirements = _thesis_requirement_rows(d, _path, where)
        thesis = cls(
            thesis_id=_req_str(d, "thesis_id", where),
            slug=_req_str(d, "slug", where),
            status=_coerce_enum(ThesisStatus, d.get("status", ThesisStatus.ACTIVE.value), "status", where),
            created_at=str(d.get("created_at", "") or ""),
            updated_at=str(d.get("updated_at", "") or ""),
            user_thesis=_req_str(d, "user_thesis", where),
            scope=_opt_unknown(d.get("scope", UNKNOWN)),
            claims=tuple(claims),
            assumptions=tuple(assumptions) if isinstance(assumptions, list) else (),
            invalidators=tuple(invalidators) if isinstance(invalidators, list) else (),
            unknowns=tuple(unknowns) if isinstance(unknowns, list) else (),
            expressions=tuple(expressions),
            requirements=tuple(requirements),
        )
        thesis.validate(_path)
        return thesis

    def validate(self, _path: str = "<thesis>") -> None:
        where = f"{_path}: thesis {self.thesis_id}"
        seen = _thesis_claim_ids(self, where)
        expr_ids = _thesis_expression_ids(self, where, seen)
        _thesis_requirement_links(self, where, seen, expr_ids)


def _row_mapping(row: object, _path: str, noun: str) -> Mapping[str, object]:
    """Narrow one thesis row to a mapping; raises with the model's own message."""
    if not isinstance(row, Mapping):
        raise ValueError(f"{_path}: {noun} must be a mapping, got {type(row).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return row


def _thesis_row_list(d: Mapping[str, object], key: str, where: str) -> list[object]:
    """Raw row list for a thesis collection field."""
    rows = d.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"{where}: {key!r} must be a list, got {type(rows).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return rows


def _thesis_claim_rows(d: Mapping[str, object], _path: str, where: str) -> list[ThesisClaim]:
    """Validated claim rows."""
    return [ThesisClaim.from_dict(_row_mapping(c, _path, "claim"), _path) for c in _thesis_row_list(d, "claims", where)]


def _thesis_expression_rows(d: Mapping[str, object], _path: str, where: str) -> list[TradeExpression]:
    """Validated expression rows."""
    return [TradeExpression.from_dict(_row_mapping(e, _path, "expression"), _path) for e in _thesis_row_list(d, "expressions", where)]


def _thesis_requirement_rows(d: Mapping[str, object], _path: str, where: str) -> list[ExpressionRequirement]:
    """Validated requirement rows."""
    return [ExpressionRequirement.from_dict(_row_mapping(r, _path, "requirement"), _path) for r in _thesis_row_list(d, "requirements", where)]


def _thesis_claim_ids(thesis: Thesis, where: str) -> set[str]:
    """Claim IDs; raises on duplicates."""
    seen: set[str] = set()
    for c in thesis.claims:
        if c.claim_id in seen:
            raise ValueError(f"{where}: duplicate ID {c.claim_id!r}")
        seen.add(c.claim_id)
    return seen


def _thesis_expression_ids(thesis: Thesis, where: str, seen: set[str]) -> set[str]:
    """Expression IDs folded into the ID set; raises on duplicates."""
    for e in thesis.expressions:
        if e.expression_id in seen:
            raise ValueError(f"{where}: duplicate ID {e.expression_id!r}")
        seen.add(e.expression_id)
    return {e.expression_id for e in thesis.expressions}


def _thesis_requirement_links(thesis: Thesis, where: str, seen: set[str], expr_ids: set[str]) -> None:
    """Requirement IDs unique plus linked to a live expression."""
    for r in thesis.requirements:
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
    claim_assessments: dict[str, JSONValue] = field(default_factory=dict)
    expression_assessments: dict[str, JSONValue] = field(default_factory=dict)
    def to_dict(self) -> dict[str, JSONValue]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}
    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ThesisState:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: state must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: state {d.get('thesis_id', '?')}"
        claim_assessments = d.get("claim_assessments", {})
        if not isinstance(claim_assessments, dict):
            raise ValueError(f"{where}: 'claim_assessments' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        expression_assessments = d.get("expression_assessments", {})
        if not isinstance(expression_assessments, dict):
            raise ValueError(f"{where}: 'expression_assessments' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        return cls(
            thesis_id=_req_str(d, "thesis_id", where),
            assessment=str(d.get("assessment", "unresolved")),
            claim_assessments=validate_json_mapping(claim_assessments, f"{where}: 'claim_assessments'"),
            expression_assessments=validate_json_mapping(expression_assessments, f"{where}: 'expression_assessments'"),
        )


@dataclass(frozen=True)
class ThesisQuestion:
    question_id: str
    text: str
    status: str = QuestionStatus.OPEN.value
    answer: str | None = None

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ThesisQuestion:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: question must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: question {d.get('question_id', '?')}"
        answer = d.get("answer")
        if answer is not None and not isinstance(answer, str):
            raise ValueError(f"{where}: 'answer' must be a string or null")
        return cls(
            question_id=_req_str(d, "question_id", where),
            text=_req_str(d, "text", where),
            status=_coerce_enum(QuestionStatus, d.get("status", QuestionStatus.OPEN.value), "status", where),
            answer=answer,
        )


@dataclass(frozen=True)
class ThesisMemory:
    memory_id: str
    text: str
    created_at: str = ""

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> ThesisMemory:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: memory must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
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

    def to_dict(self) -> dict[str, JSONValue]:
        return asdict(self)
    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> EvidenceRef:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: evidence ref must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
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
    sources: dict[str, JSONValue] = field(default_factory=dict)
    recent_hashes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, JSONValue]:
        return {"schema_version": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, d: Mapping[str, object], _path: str = "<dict>") -> Checkpoint:
        if not isinstance(d, dict):
            raise ValueError(f"{_path}: checkpoint must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{_path}: checkpoint {d.get('thesis_id', '?')}"
        sources = d.get("sources", {})
        if not isinstance(sources, dict):
            raise ValueError(f"{where}: 'sources' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        hashes = d.get("recent_hashes", [])
        if not isinstance(hashes, list) or not all(isinstance(h, str) for h in hashes):
            raise ValueError(f"{where}: 'recent_hashes' must be a list of strings")
        return cls(thesis_id=_req_str(d, "thesis_id", where), sources=validate_json_mapping(sources, f"{where}: 'sources'"),
                   recent_hashes=list(hashes))


def _snapshot_version(d: Mapping[str, object], where: str) -> int:
    """Snapshot version; must be an int >= 1."""
    version = d.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"{where}: 'version' must be an int >= 1, got {version!r}")
    return version


def _snapshot_thesis_id(d: Mapping[str, object], where: str) -> str:
    """Snapshot thesis ID; must be non-empty."""
    thesis_id = d.get("thesis_id")
    if not isinstance(thesis_id, str) or not thesis_id:
        raise ValueError(f"{where}: 'thesis_id' must be a non-empty string")
    return thesis_id


def _snapshot_moment(d: Mapping[str, object], key: str, where: str) -> str:
    """Parseable ISO-8601 moment for an effective/recorded timestamp."""
    from app.thesis.monitor import _as_dt  # local: avoid import cycle

    value = d.get(key)
    if not isinstance(value, str) or not value or _as_dt(value) is None:
        raise ValueError(f"{where}: {key!r} must be a parseable ISO-8601 timestamp, got {value!r}")
    return value


def _snapshot_reason(d: Mapping[str, object], where: str) -> str:
    """Snapshot reason; must be non-empty."""
    reason = d.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError(f"{where}: 'reason' must be a non-empty string")
    return reason


def _snapshot_head(d: Mapping[str, object], where: str) -> tuple[int, str, str, str, str]:
    """Scalar snapshot head: version, thesis_id, effective/recorded_at, reason."""
    if not isinstance(d, dict):
        raise ValueError(f"{where}: snapshot must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return (_snapshot_version(d, where), _snapshot_thesis_id(d, where),
            _snapshot_moment(d, "effective_at", where), _snapshot_moment(d, "recorded_at", where),
            _snapshot_reason(d, where))


def _snapshot_section(d: Mapping[str, object], key: str, where: str, thesis_id: str) -> dict[str, JSONValue]:
    """One snapshot section: mapping with a matching thesis_id."""
    payload = d.get(key, {})
    if not isinstance(payload, dict):
        raise ValueError(f"{where}: {key!r} must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    pid = payload.get("thesis_id")
    if pid != thesis_id:
        raise ValueError(f"{where}: {key!r} thesis_id {pid!r} != snapshot thesis_id {thesis_id!r}")
    return validate_json_mapping(payload, f"{where}: {key!r}")


def _snapshot_sections(d: Mapping[str, object], where: str, thesis_id: str) -> dict[str, dict[str, JSONValue]]:
    """All five snapshot sections validated as thesis-bound mappings."""
    return {key: _snapshot_section(d, key, where, thesis_id)
            for key in ("thesis", "state", "questions", "watch", "memory")}


def _snapshot_row_list(section: dict[str, JSONValue], key: str, where: str) -> list[Mapping[str, object]]:
    """Row list inside a snapshot section; entries must be mappings."""
    rows = section.get(key, [])
    if not isinstance(rows, list):
        raise ValueError(f"{where}: {key!r} must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    out: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{where}: {key!r} must be a list of mappings")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        out.append(row)
    return out


def _snapshot_validate(sections: dict[str, dict[str, JSONValue]], where: str) -> None:
    """Deep-validate every snapshot section with its model."""
    Thesis.from_dict(sections["thesis"], where)
    ThesisState.from_dict(sections["state"], where)
    for q in _snapshot_row_list(sections["questions"], "questions", where):
        ThesisQuestion.from_dict(q, where)
    for r in _snapshot_row_list(sections["watch"], "rules", where):
        WatchRule.from_dict(r, where)
    for m in _snapshot_row_list(sections["memory"], "memories", where):
        ThesisMemory.from_dict(m, where)


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
    thesis: dict[str, JSONValue] = field(default_factory=dict)
    state: dict[str, JSONValue] = field(default_factory=dict)
    questions: dict[str, JSONValue] = field(default_factory=dict)
    watch: dict[str, JSONValue] = field(default_factory=dict)
    memory: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
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
    def from_dict(cls, d: Mapping[str, object], where: str = "<dict>") -> ThesisStateSnapshot:
        version, thesis_id, effective_at, recorded_at, reason = _snapshot_head(d, where)
        try:
            sections = _snapshot_sections(d, where, thesis_id)
            _snapshot_validate(sections, where)
        except ValueError as e:
            if str(e).startswith(where):
                raise
            raise ValueError(f"{where}: {e}") from e
        return cls(
            thesis_id=thesis_id,
            version=version,
            effective_at=effective_at,
            recorded_at=recorded_at,
            reason=reason,
            run_id=str(d.get("run_id", "") or ""),
            trigger_id=str(d.get("trigger_id", "") or ""),
            thesis=sections["thesis"],
            state=sections["state"],
            questions=sections["questions"],
            watch=sections["watch"],
            memory=sections["memory"],
        )
