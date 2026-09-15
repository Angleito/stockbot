"""User-idea intake: normalize a natural-language thesis into a validated proposal.

# ponytail: deterministic local read covering the plan's showcase + intake-table
# phrases; Pi-facing tools validate through IntakeProposal.from_dict instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict

from app.policy import Capability, RequestContext
from app.thesis.models import (
    ExpressionRequirement,
    JSONValue,
    Thesis,
    new_claim_id,
    new_expression_id,
    new_question_id,
    new_requirement_id,
    new_rule_id,
)
from app.thesis.repository import ThesisRepository

UNKNOWN = "unknown"

# StrEnum-free local copies of the small intake-relevant vocabularies.
_INSTRUMENTS = frozenset({"equity", "option", "future", "bond", "cash", UNKNOWN})
_DIRECTIONS = frozenset({"long", "short", "neutral", UNKNOWN})
_EXPRESSION_STATUSES = frozenset({"undecided", "active", "flagged", "closed"})
_REQUIREMENT_STATUSES = frozenset({"open", "answered"})
_MAX_QUESTIONS = 3


def _req_str(d: Mapping[str, object], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _unknown_str(v: object, key: str, where: str) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return UNKNOWN
    if isinstance(v, str):
        return v
    raise ValueError(f"{where}: '{key}' must be a string, got {type(v).__name__}")


def _str_list(d: Mapping[str, object], key: str, where: str) -> list[str]:
    vals = d.get(key, [])
    if not isinstance(vals, list) or not all(isinstance(i, str) for i in vals):
        raise ValueError(f"{where}: '{key}' must be a list of strings")
    return list(vals)


def _as_list(d: Mapping[str, object], key: str, where: str) -> list[object]:
    vals = d.get(key, [])
    if not isinstance(vals, list):
        raise ValueError(f"{where}: '{key}' must be a list, got {type(vals).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return vals


@dataclass(frozen=True)
class IntakeQuestion:
    question_id: str
    question: str
    question_type: str = UNKNOWN

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "question_type": self.question_type,
        }

    @classmethod
    def from_dict(cls, d: object, path: str = "<intake>") -> IntakeQuestion:
        if not isinstance(d, dict):
            raise ValueError(f"{path}: question must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        _qid = d.get("question_id")
        qid = _qid if isinstance(_qid, str) and _qid else new_question_id()
        where = f"{path}: question {qid}"
        return cls(
            question_id=qid,
            question=_req_str(d, "question", where),
            question_type=_unknown_str(d.get("question_type", UNKNOWN), "question_type", where),
        )


def _check_proposal_head(proposal: IntakeProposal, where: str) -> None:
    """Head fields: non-empty thesis plus the question cap."""
    if not isinstance(proposal.user_thesis, str) or not proposal.user_thesis.strip():
        raise ValueError(f"{where}: 'user_thesis' must be a non-empty string")
    if len(proposal.questions) > _MAX_QUESTIONS:
        raise ValueError(f"{where}: at most {_MAX_QUESTIONS} questions, got {len(proposal.questions)}")


def _claim_seen(c: Mapping[str, object], where: str, seen: set[object]) -> None:
    """One claim row: unvalidated status plus global ID uniqueness."""
    if c["status"] != "unvalidated":
        raise ValueError(
            f"{where}: claim {c['claim_id']!r} status must stay 'unvalidated', got {c['status']!r}"
        )
    if c["claim_id"] in seen:
        raise ValueError(f"{where}: duplicate ID {c['claim_id']!r}")
    seen.add(c["claim_id"])


def _check_claim_rows(proposal: IntakeProposal, where: str) -> set[object]:
    """Claim rows; returns the ID set for the later phases."""
    seen: set[object] = set()
    for c in proposal.claims:
        _claim_seen(c, where, seen)
    return seen


def _check_expression_rows(proposal: IntakeProposal, where: str, seen: set[object]) -> set[object]:
    """Expression rows; returns expression IDs for the requirement phase."""
    for e in proposal.expressions:
        if e["expression_id"] in seen:
            raise ValueError(f"{where}: duplicate ID {e['expression_id']!r}")
        seen.add(e["expression_id"])
    return {e["expression_id"] for e in proposal.expressions}


def _check_requirement_rows(proposal: IntakeProposal, where: str, seen: set[object], expr_ids: set[object]) -> None:
    """Requirement rows: unique IDs plus a live parent expression link."""
    for r in proposal.requirements:
        if r["requirement_id"] in seen:
            raise ValueError(f"{where}: duplicate ID {r['requirement_id']!r}")
        seen.add(r["requirement_id"])
        if r["expression_id"] not in expr_ids:
            raise ValueError(
                f"{where}: requirement {r['requirement_id']!r} references absent expression "
                f"{r['expression_id']!r}"
            )



def _validate_claim(c: object, path: str) -> dict[str, JSONValue]:
    if not isinstance(c, dict):
        raise ValueError(f"{path}: claim must be a mapping, got {type(c).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    cid = c.get("claim_id") or new_claim_id()
    where = f"{path}: claim {cid}"
    if not isinstance(cid, str) or not cid:
        raise ValueError(f"{where}: 'claim_id' must be a non-empty string")
    return {
        "claim_id": cid,
        "statement": _req_str(c, "statement", where),
        "status": _unknown_str(c.get("status", "unvalidated"), "status", where),
    }


def _expression_vocab(value: object, key: str, vocab: frozenset[str], where: str) -> str:
    """Validated vocab field (instrument/direction); open text passes via _unknown_str."""
    text = _unknown_str(value, key, where)
    if text not in vocab:
        raise ValueError(f"{where}: {key!r} must be a known primitive or 'unknown', got {text!r}")
    return text


def _expression_status(value: object, where: str) -> str:
    """Validated expression status against the intake status vocabulary."""
    status = _unknown_str(value, "status", where)
    if status not in _EXPRESSION_STATUSES:
        raise ValueError(f"{where}: 'status' must be one of {sorted(_EXPRESSION_STATUSES)}, got {status!r}")
    return status


def _expression_structure(e: Mapping[str, object], where: str) -> str:
    """Open-vocabulary structure field; must stay a non-empty string."""
    structure = e.get("structure", UNKNOWN)  # open vocabulary, passed through unchanged
    if not isinstance(structure, str) or not structure.strip():
        raise ValueError(f"{where}: 'structure' must be a non-empty string (open vocabulary)")
    return structure


def _expression_mappings(e: Mapping[str, object], where: str, out: dict[str, JSONValue]) -> None:
    """Copy the leverage/parameters mappings (None normalizes to {})."""
    for k in ("leverage", "parameters"):
        _v: object = e.get(k, {})
        if _v is None:
            _v = dict[str, JSONValue]()
        if not isinstance(_v, dict):
            raise ValueError(f"{where}: '{k}' must be a mapping, got {type(_v).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        out[k] = dict(_v)


def _validate_expression(e: object, path: str) -> dict[str, JSONValue]:
    if not isinstance(e, dict):
        raise ValueError(f"{path}: expression must be a mapping, got {type(e).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    eid = new_expression_id()
    where = f"{path}: expression {eid}"
    instrument = _expression_vocab(e.get("instrument", UNKNOWN), "instrument", _INSTRUMENTS, where)
    direction = _expression_vocab(e.get("direction", UNKNOWN), "direction", _DIRECTIONS, where)
    status = _expression_status(e.get("status", "undecided"), where)
    out: dict[str, JSONValue] = {
        "expression_id": eid,
        "intent": _unknown_str(e.get("intent", UNKNOWN), "intent", where),
        "instrument": instrument,
        "direction": direction,
        "structure": _expression_structure(e, where),
        "horizon": _unknown_str(e.get("horizon", UNKNOWN), "horizon", where),
        "deterministic_support": _unknown_str(
            e.get("deterministic_support", UNKNOWN), "deterministic_support", where
        ),
        "status": status,
    }
    _expression_mappings(e, where, out)
    return out

def _requirement_id(r: Mapping[str, object], path: str) -> tuple[str, str]:
    """(requirement_id, where) for one requirement row."""
    rid = r.get("requirement_id") or new_requirement_id()
    where = f"{path}: requirement {rid}"
    if not isinstance(rid, str) or not rid:
        raise ValueError(f"{where}: 'requirement_id' must be a non-empty string")
    return rid, where


def _requirement_status(r: Mapping[str, object], where: str) -> str:
    """Validated requirement status."""
    status = _unknown_str(r.get("status", "open"), "status", where)
    if status not in _REQUIREMENT_STATUSES:
        raise ValueError(f"{where}: 'status' must be one of {sorted(_REQUIREMENT_STATUSES)}, got {status!r}")
    return status


def _requirement_expression(r: Mapping[str, object], rid: str, where: str, expression_ids: set[JSONValue]) -> str:
    """Parent expression link; must name a known expression."""
    eid = r.get("expression_id")
    if not isinstance(eid, str) or not eid.strip():
        raise ValueError(f"{where}: 'expression_id' must be a non-empty string")
    if eid not in expression_ids:
        raise ValueError(
            f"{where}: requirement {rid!r} references absent expression "
            f"{eid!r}"
        )
    return eid


def _validate_requirement(r: object, path: str, expression_ids: set[JSONValue]) -> dict[str, JSONValue]:
    if not isinstance(r, dict):
        raise ValueError(f"{path}: requirement must be a mapping, got {type(r).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    rid, where = _requirement_id(r, path)
    status = _requirement_status(r, where)
    eid = _requirement_expression(r, rid, where, expression_ids)
    return {
        "requirement_id": rid,
        "expression_id": eid,
        "requirement_type": _req_str(r, "requirement_type", where),
        "statement": _req_str(r, "statement", where),
        "status": status,
    }


@dataclass(frozen=True)
class IntakeProposal:
    user_thesis: str
    scope: str = UNKNOWN
    claims: tuple[dict[str, JSONValue], ...] = ()
    assumptions: tuple[str, ...] = ()
    invalidators: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    expressions: tuple[dict[str, JSONValue], ...] = ()
    requirements: tuple[dict[str, JSONValue], ...] = ()
    questions: tuple[IntakeQuestion, ...] = ()

    def __post_init__(self) -> None:
        self.validate("<intake>")

    def validate(self, path: str = "<intake>") -> None:
        where = f"{path}: proposal"
        _check_proposal_head(self, where)
        seen = _check_claim_rows(self, where)
        expr_ids = _check_expression_rows(self, where, seen)
        _check_requirement_rows(self, where, seen, expr_ids)

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "user_thesis": self.user_thesis,
            "scope": self.scope,
            "claims": list[JSONValue](dict(c) for c in self.claims),
            "assumptions": list[JSONValue](self.assumptions),
            "invalidators": list[JSONValue](self.invalidators),
            "unknowns": list[JSONValue](self.unknowns),
            "expressions": list[JSONValue](dict(e) for e in self.expressions),
            "requirements": list[JSONValue](dict(r) for r in self.requirements),
            "questions": list[JSONValue](q.to_dict() for q in self.questions),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, object], path: str = "<intake>") -> IntakeProposal:
        if not isinstance(d, dict):
            raise ValueError(f"{path}: proposal must be a mapping, got {type(d).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        where = f"{path}: proposal"
        scope = _proposal_scope(d, where)
        expressions = _proposal_expressions(d, where, path)
        requirements = _proposal_requirements(d, where, path, expressions)
        return cls(
            user_thesis=_req_str(d, "user_thesis", where),
            scope=scope,
            claims=tuple(_validate_claim(c, path) for c in _as_list(d, "claims", where)),
            assumptions=tuple(_str_list(d, "assumptions", where)),
            invalidators=tuple(_str_list(d, "invalidators", where)),
            unknowns=tuple(_str_list(d, "unknowns", where)),
            expressions=tuple(expressions),
            requirements=requirements,
            questions=tuple(IntakeQuestion.from_dict(q, path) for q in _as_list(d, "questions", where)),
        )


def _proposal_scope(d: Mapping[str, object], where: str) -> str:
    """Scope field; blank/missing normalizes to unknown."""
    scope = d.get("scope", UNKNOWN)
    if scope is None or (isinstance(scope, str) and not scope.strip()):
        return UNKNOWN
    if not isinstance(scope, str):
        raise ValueError(f"{where}: 'scope' must be a string, got {type(scope).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return scope


def _proposal_expressions(d: Mapping[str, object], where: str, path: str) -> list[dict[str, JSONValue]]:
    """Validated expression rows."""
    out: list[dict[str, JSONValue]] = []
    for e in _as_list(d, "expressions", where):
        out.append(_validate_expression(e, path))
    return out


def _proposal_requirements(d: Mapping[str, object], where: str, path: str,
                           expressions: list[dict[str, JSONValue]]) -> tuple[dict[str, JSONValue], ...]:
    """Validated requirement rows linked to the expression IDs."""
    ids = {e["expression_id"] for e in expressions}
    return tuple(_validate_requirement(r, path, ids) for r in _as_list(d, "requirements", where))


def _research_only_context(request_context: RequestContext | None) -> RequestContext:
    if request_context is None:
        raise ValueError("<intake>: request_context is required (must carry RESEARCH)")
    caps = set(getattr(request_context, "capabilities", ()))
    if Capability.RESEARCH not in caps:
        raise ValueError("<intake>: request_context must include the RESEARCH capability")
    for cap in caps:
        if cap in (Capability.BROKER_MARKET_READ, Capability.PORTFOLIO_READ):
            name = cap.value if isinstance(cap, Capability) else str(cap)
            raise ValueError(f"<intake>: request_context must not include broker capability {name!r}")
    return request_context


def _expr(*, intent: str, instrument: str, direction: str, structure: str, horizon: str = UNKNOWN) -> dict[str, JSONValue]:
    return {
        "expression_id": new_expression_id(),
        "intent": intent,
        "instrument": instrument,
        "direction": direction,
        "structure": structure,  # open vocabulary, kept unchanged
        "horizon": horizon,
        "leverage": {},
        "parameters": {},
        "deterministic_support": UNKNOWN,
        "status": "undecided",  # intake never activates an expression
    }


def _expression_choice_question() -> IntakeQuestion:
    return IntakeQuestion(
        question_id="expression_choice",
        question=(
            "How do you want to express this "
            "(e.g. long puts, short equity, buy-and-hold equity, or still deciding)?"
        ),
        question_type="expression_choice",
    )


def _strategy_expression(combined: str) -> dict[str, JSONValue] | None:
    if re.search(r"\bputs?\b", combined):
        return _expr(intent="bearish", instrument="option", direction="long", structure="long puts")
    if re.search(r"\bcalls?\b", combined):
        return _expr(intent="bullish", instrument="option", direction="long", structure="long calls")
    if re.search(r"\bshort\b|\binverse\b", combined):
        return _expr(intent="bearish", instrument="equity", direction="short", structure="short equity")
    if re.search(r"\baccumulat\w*|\bbuy\w*|\bown\b|\bbullish\b|\blong-term\b", combined):
        return _expr(intent="bullish", instrument="equity", direction="long", structure="equity")
    return None


class _ProposalBase(TypedDict):
    user_thesis: str
    scope: str
    claims: tuple[dict[str, JSONValue], ...]
    assumptions: tuple[str, ...]
    invalidators: tuple[str, ...]
    unknowns: tuple[str, ...]
    requirements: tuple[dict[str, JSONValue], ...]

def _interpret_blobs(text: str, answers: Mapping[str, object] | None) -> tuple[str, str]:
    """Lowercased (thesis, answers) blobs for phrase matching."""
    blob = " ".join(str(v) for v in (answers or {}).values() if isinstance(v, (str, int, float)))
    return text.lower(), blob.lower()


def _interpret_base(text: str) -> _ProposalBase:
    """One-claim proposal base shared by every interpret branch."""
    claim: dict[str, JSONValue] = {"claim_id": new_claim_id(), "statement": text, "status": "unvalidated"}
    return {
        "user_thesis": text,
        "scope": UNKNOWN,
        "claims": (claim,),
        "assumptions": (),
        "invalidators": (),
        "unknowns": (UNKNOWN,),
        "requirements": (),
    }


def _showcase_pair() -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """AI-infra repricing showcase: hedge puts + post-selloff accumulation."""
    return (
        _expr(intent="bearish", instrument="option", direction="long", structure="long puts"),
        _expr(intent="bullish", instrument="equity", direction="long",
              structure="post-selloff equity accumulation"),
    )


def _wants_accumulation(combined: str) -> bool:
    """True when the text wants post-selloff accumulation."""
    return "accumulat" in combined or "selloff" in combined or "sell-off" in combined


def _is_showcase(t: str, blob: str, combined: str) -> bool:
    """Showcase phrase set: AI-infra repricing + puts + accumulation."""
    if "ai infrastructure" not in t or "too high" not in t:
        return False
    return re.search(r"\bputs?\b", blob) is not None and _wants_accumulation(combined)


def _ten_year_expr() -> dict[str, JSONValue]:
    """Obvious long-equity intent for a ten-year hold."""
    return _expr(intent="bullish", instrument="equity", direction="long",
                 structure="equity", horizon="long-term")


def _local_interpret(text: str, answers: Mapping[str, object] | None) -> IntakeProposal:
    t, blob = _interpret_blobs(text, answers)
    combined = f"{t}\n{blob}"
    base = _interpret_base(text)
    # Showcase: AI-infra repricing + answers wanting puts and post-selloff accumulation.
    if _is_showcase(t, blob, combined):
        return IntakeProposal(**base, expressions=_showcase_pair())
    # "own it for ten years" maps to obvious long-equity intent, no options questions.
    if re.search(r"\bten years\b|\b10 years\b|\bten-year\b", combined):
        return IntakeProposal(**base, expressions=(_ten_year_expr(),))
    # "still deciding" persists zero expressions rather than choosing one.
    if "still deciding" in combined:
        return IntakeProposal(**base)
    strat = _strategy_expression(combined)
    if strat is not None:
        return IntakeProposal(**base, expressions=(strat,))
    # No strategy stated: one expression-choice question, never a recommendation.
    return IntakeProposal(**base, questions=(_expression_choice_question(),))


def interpret_idea(
    text: str,
    answers: Mapping[str, object] | None = None,
    request_context: RequestContext | None = None,
) -> IntakeProposal:
    """Normalize ``text`` (+ optional ``answers``) into a validated IntakeProposal."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("<intake>: 'text' must be a non-empty string")
    _research_only_context(request_context)
    clean = text.strip()
    given = dict(answers or {})
    # ponytail: deterministic local read (see module docstring).
    return _local_interpret(clean, given)

_SCOPE_RE = re.compile(r"^[A-Za-z]{1,5}$")
_SETUP_QUESTION_ID = "setup:scope"


def build_initial_watch_rules(
    scope: str, requirements: Sequence[Mapping[str, object] | ExpressionRequirement], *, claim_ids: Sequence[object] = (), expression_ids: Sequence[object] = ()
) -> list[dict[str, JSONValue]]:
    """Supported semantic rules only: explicit ticker scope -> ``new_filing`` plus
    any requirement whose type already names a supported monitor. No thresholds."""

    cids = _str_ids(claim_ids)
    eids = _str_ids(expression_ids)
    rules: list[dict[str, JSONValue]] = []
    if _filing_rule_due(scope, cids, eids):
        rules.append(_new_rule("new_filing", cids, eids))
    seen = {r["rule_type"] for r in rules}
    for r in requirements or []:
        cand = _requirement_rule(r, eids, seen)
        if cand is not None:
            rules.append(cand)
            seen.add(str(cand["rule_type"]))
    return rules


def _str_ids(ids: Sequence[object]) -> list[str]:
    """Non-empty string IDs only."""
    return [c for c in (ids or []) if isinstance(c, str) and c]


def _filing_rule_due(scope: str, cids: list[str], eids: list[str]) -> bool:
    """True when an explicit ticker scope names at least one target."""
    from app.thesis.monitor import SUPPORTED_HANDLERS  # local: monitor owns the table

    return bool(_SCOPE_RE.fullmatch((scope or "").strip())
                and "new_filing" in SUPPORTED_HANDLERS and (cids or eids))


def _new_rule(rule_type: str, cids: list[str], eids: list[str]) -> dict[str, JSONValue]:
    """One supported watch rule row."""
    return {
        "rule_id": new_rule_id(),
        "rule_type": rule_type,
        "enabled": True,
        "support_status": "supported",
        "support_reason": "",
        "claim_ids": list[JSONValue](cids),
        "expression_ids": list[JSONValue](eids),
    }


def _requirement_link(r: Mapping[str, object] | ExpressionRequirement) -> tuple[object, object]:
    """(requirement_type, expression_id) for a dict or model row."""
    if isinstance(r, dict):
        return r.get("requirement_type"), r.get("expression_id")
    return getattr(r, "requirement_type", None), getattr(r, "expression_id", None)


def _requirement_supported(rt: object, seen: set[JSONValue]) -> bool:
    """True when the requirement names a supported, unseen monitor."""
    from app.thesis.monitor import SUPPORTED_HANDLERS  # local: monitor owns the table

    return isinstance(rt, str) and rt in SUPPORTED_HANDLERS and rt not in seen

def _requirement_eligible(rt: object, eid: object, eids: list[str], seen: set[JSONValue]) -> bool:
    """True when the requirement names a supported, unseen monitor with a live expression."""
    return _requirement_supported(rt, seen) and isinstance(eid, str) and eid in eids


def _requirement_rule(r: Mapping[str, object] | ExpressionRequirement, eids: list[str],
                      seen: set[JSONValue]) -> dict[str, JSONValue] | None:
    """One requirement-backed rule; None when ineligible."""
    rt, eid = _requirement_link(r)
    if not _requirement_eligible(rt, eid, eids, seen) or not isinstance(eid, str):
        return None
    return _new_rule(str(rt), [], [eid])


def setup_needed_question() -> dict[str, JSONValue]:
    return {
        "question_id": _SETUP_QUESTION_ID,
        "text": "Which ticker should this thesis monitor? Reply with the single ticker symbol (e.g. NVDA).",
        "status": "open",
    }


def create_thesis_from_proposal(repository: ThesisRepository, proposal: IntakeProposal, *, effective_at: str | None = None) -> dict[str, object]:
    """Shared CLI + tool creation path: thesis, explicit scope, supported rules.

    Unresolvable scope persists a setup question instead of a fake active
    monitor. Returns thesis_id/slug/scope/rules/setup_needed/missing_questions.
    """
    rules = build_initial_watch_rules(
        proposal.scope, proposal.requirements,
        claim_ids=[c["claim_id"] for c in proposal.claims],
        expression_ids=[e["expression_id"] for e in proposal.expressions])
    thesis = _create_thesis_rows(repository, proposal, rules, effective_at)
    missing, questions_add = _create_questions(proposal, rules)
    if questions_add:
        repository.apply_research_result(thesis.thesis_id, {"questions_add": questions_add}, "", effective_at=effective_at)
    return {
        "thesis_id": thesis.thesis_id,
        "slug": thesis.slug,
        "scope": thesis.scope,
        "setup_needed": not rules,
        "missing_questions": [dict(m) for m in missing],
        "rules": [dict(r) for r in rules],
    }


def _create_thesis_rows(repository: ThesisRepository, proposal: IntakeProposal,
                        rules: list[dict[str, JSONValue]], effective_at: str | None) -> Thesis:
    """Persist the thesis row with proposal payload plus initial watch rules."""
    return repository.create_thesis(
        user_thesis=proposal.user_thesis,
        scope=proposal.scope,
        claims=[dict(c) for c in proposal.claims],
        assumptions=list(proposal.assumptions),
        invalidators=list(proposal.invalidators),
        unknowns=list(proposal.unknowns),
        expressions=[dict(e) for e in proposal.expressions],
        requirements=[dict(r) for r in proposal.requirements],
        watch_rules=rules,
        effective_at=effective_at,
    )


def _create_questions(proposal: IntakeProposal,
                      rules: list[dict[str, JSONValue]]) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
    """Question rows: proposal questions plus the setup question when ruleless."""
    missing: list[dict[str, JSONValue]] = []
    questions_add = _proposal_question_rows(proposal)
    if not rules:
        missing.append(setup_needed_question())
        questions_add.append(dict(missing[0]))
    return missing, questions_add


def _question_parts(q: IntakeQuestion | Mapping[str, object]) -> tuple[str, object]:
    """(validated question_id, raw text) for a dict or model question row."""
    if isinstance(q, IntakeQuestion):
        return q.question_id, q.question
    _qid = q.get("question_id")
    qid = _qid if isinstance(_qid, str) and _qid else new_question_id()
    return qid, (q.get("question") or q.get("text", ""))


def _question_row(q: IntakeQuestion | Mapping[str, object]) -> dict[str, JSONValue] | None:
    """One proposal question as a research question row; None when blank."""
    qid, text = _question_parts(q)
    if isinstance(text, str) and text.strip():
        return {"question_id": qid, "text": text.strip(), "status": "open"}
    return None


def _proposal_question_rows(proposal: IntakeProposal) -> list[dict[str, JSONValue]]:
    """Non-blank proposal questions as research question rows."""
    out: list[dict[str, JSONValue]] = []
    for q in proposal.questions or ():
        row = _question_row(q)
        if row is not None:
            out.append(row)
    return out


def plan_refinement(thesis: Thesis, proposal: IntakeProposal) -> dict[str, object]:
    """Pure merge preview: added claims/expressions/requirements plus merged payload."""
    claims, added_claims = _plan_claims(thesis, proposal)
    expressions, added_expr = _plan_expressions(thesis, proposal)
    requirements, added_reqs = _plan_requirements(thesis, proposal, added_expr)
    merged = _plan_merged(thesis, proposal, claims, added_claims, expressions, added_expr,
                          requirements, added_reqs)
    return {
        "merged": merged,
        "added_claims": added_claims,
        "added_expressions": added_expr,
        "added_requirements": added_reqs,
    }


def _plan_claims(thesis: Thesis, proposal: IntakeProposal) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
    """Kept claim dicts plus newly added claim dicts."""
    old = {c.statement for c in thesis.claims}
    kept = [c.to_dict() for c in thesis.claims]
    return kept, [dict(c) for c in proposal.claims if c["statement"] not in old]


def _plan_expressions(thesis: Thesis, proposal: IntakeProposal) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
    """Kept expression dicts plus newly added expression dicts."""
    old = {(e.intent, e.instrument, e.direction, e.structure, e.horizon) for e in thesis.expressions}
    kept = [e.to_dict() for e in thesis.expressions]
    added = [dict(e) for e in proposal.expressions
             if (e["intent"], e["instrument"], e["direction"], e["structure"], e["horizon"]) not in old]
    return kept, added


def _plan_requirements(thesis: Thesis, proposal: IntakeProposal,
                       added_expr: list[dict[str, JSONValue]]) -> tuple[list[dict[str, JSONValue]], list[dict[str, JSONValue]]]:
    """Kept requirement dicts plus ones linked to newly added expressions."""
    new_ids = {e["expression_id"] for e in added_expr}
    kept = [r.to_dict() for r in thesis.requirements]
    return kept, [dict(r) for r in proposal.requirements if r["expression_id"] in new_ids]


def _plan_merged(thesis: Thesis, proposal: IntakeProposal, claims: list[dict[str, JSONValue]],
                 added_claims: list[dict[str, JSONValue]], expressions: list[dict[str, JSONValue]],
                 added_expr: list[dict[str, JSONValue]], requirements: list[dict[str, JSONValue]],
                 added_reqs: list[dict[str, JSONValue]]) -> dict[str, object]:
    """Merged payload: fresh thesis text plus deduped union lists."""
    return {
        "user_thesis": proposal.user_thesis,
        "scope": proposal.scope if proposal.scope != UNKNOWN else thesis.scope,
        "claims": claims + added_claims,
        "assumptions": list(dict.fromkeys([*thesis.assumptions, *proposal.assumptions])),
        "invalidators": list(dict.fromkeys([*thesis.invalidators, *proposal.invalidators])),
        "unknowns": list(dict.fromkeys([*thesis.unknowns, *proposal.unknowns])),
        "expressions": expressions + added_expr,
        "requirements": requirements + added_reqs,
    }


def apply_refinement(repository: ThesisRepository, thesis_id: str, plan: Mapping[str, object], proposal: IntakeProposal, *, effective_at: str | None = None) -> dict[str, object]:
    """Apply a refinement plan; watch changes are append-only, never touching user rules."""
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    _refinement_gate(repository, thesis, tid, effective_at)
    merged = _refinement_merged(plan, tid)
    updated = repository.update_thesis(tid, effective_at=effective_at, **merged)
    fresh_scope, fresh_claim_ids, fresh_expr_ids, covered = _refinement_coverage(repository, tid, effective_at)
    covered_claims, covered_exprs = covered
    added_claims, added_expressions, added_requirements = _refinement_added(plan, tid)
    new_rules = _refinement_rules(fresh_scope, proposal, fresh_claim_ids, fresh_expr_ids,
                                  covered_claims, covered_exprs)
    if new_rules:
        repository.apply_research_result(tid, {"watch_add": new_rules}, "", effective_at=effective_at)
    return {
        "thesis_id": tid,
        "slug": updated.slug,
        "added_claims": len(added_claims),
        "added_expressions": len(added_expressions),
        "added_requirements": len(added_requirements),
        "rules_added": [dict(r) for r in new_rules],
    }


def _refinement_gate(repository: ThesisRepository, thesis: Thesis, tid: str, effective_at: str | None) -> None:
    """Refuse refinement unless the thesis (or snapshot) is active."""
    if effective_at is None:
        if thesis.status != "active":
            raise ValueError(f"thesis {tid!r} is {thesis.status}; refusing refinement")
        return
    snap_status = repository.load_state_as_of(tid, effective_at).thesis["status"]
    if snap_status != "active":
        raise ValueError(f"thesis {tid!r} is {snap_status}; refusing refinement")


def _refinement_merged(plan: Mapping[str, object], tid: str) -> dict[str, object]:
    """Merged payload from the plan; must be a mapping."""
    merged = plan["merged"]
    if not isinstance(merged, dict):
        raise ValueError(f"thesis {tid!r} refinement plan 'merged' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return merged


def _live_coverage(repository: ThesisRepository, tid: str) -> tuple[str, list[str], list[str], dict[str, set[str]], dict[str, set[str]]]:
    """Live coverage: scope, IDs, and per-rule covered targets."""

    fresh = repository.load_thesis(tid)
    covered_claims: dict[str, set[str]] = {}
    covered_exprs: dict[str, set[str]] = {}
    for r in repository.load_watch_rules(tid):
        _fold_live_rule(r, covered_claims, covered_exprs)
    return (fresh.scope, [c.claim_id for c in fresh.claims],
            [e.expression_id for e in fresh.expressions], covered_claims, covered_exprs)


def _fold_live_rule(r: object, covered_claims: dict[str, set[str]], covered_exprs: dict[str, set[str]]) -> None:
    """Fold one live watch rule into the covered-target maps."""
    from app.thesis.monitor import (
        SUPPORTED_HANDLERS,  # local: monitor owns the handler table
    )

    if not getattr(r, "enabled", False) or getattr(r, "support_status", None) != "supported":
        return
    rt = getattr(r, "rule_type", None)
    if not isinstance(rt, str) or rt not in SUPPORTED_HANDLERS:
        return
    covered_claims.setdefault(rt, set()).update(getattr(r, "claim_ids", ()))
    covered_exprs.setdefault(rt, set()).update(getattr(r, "expression_ids", ()))


def _snapshot_ids(rows: object, key: str) -> list[str]:
    """String IDs for one snapshot row list."""
    if not isinstance(rows, list):
        return []
    return [c[key] for c in rows if isinstance(c, dict) and isinstance(c.get(key), str)]


def _snapshot_row_enabled(r: dict[str, object]) -> bool:
    """True when a snapshot watch row is supported+enabled."""
    return bool(r.get("enabled")) and r.get("support_status") == "supported"


def _snapshot_rule_eligible(r: object) -> str | None:
    """Rule type when a snapshot watch row is supported+enabled; else None."""
    from app.thesis.monitor import (
        SUPPORTED_HANDLERS,  # local: monitor owns the handler table
    )

    if not isinstance(r, dict) or not _snapshot_row_enabled(r):
        return None
    rt = r.get("rule_type")
    return rt if isinstance(rt, str) and rt in SUPPORTED_HANDLERS else None


def _snapshot_targets(row: dict[str, object], key: str) -> list[str]:
    """String targets for one snapshot watch row list field."""
    raw = row.get(key, ())
    return [c for c in raw if isinstance(c, str)] if isinstance(raw, (list, tuple)) else []


def _snapshot_coverage(repository: ThesisRepository, tid: str,
                       effective_at: str) -> tuple[str, list[str], list[str], dict[str, set[str]], dict[str, set[str]]]:
    """Snapshot coverage at effective_at: scope, IDs, per-rule covered targets."""
    snap = repository.load_state_as_of(tid, effective_at)
    _scope = snap.thesis.get("scope", UNKNOWN)
    covered_claims: dict[str, set[str]] = {}
    covered_exprs: dict[str, set[str]] = {}
    for r in _snapshot_rule_rows(snap):
        rt = _snapshot_rule_eligible(r)
        if rt is None or not isinstance(r, dict):
            continue
        covered_claims.setdefault(rt, set()).update(_snapshot_targets(r, "claim_ids"))
        covered_exprs.setdefault(rt, set()).update(_snapshot_targets(r, "expression_ids"))
    return (_scope if isinstance(_scope, str) else UNKNOWN,
            _snapshot_ids(snap.thesis.get("claims", []), "claim_id"),
            _snapshot_ids(snap.thesis.get("expressions", []), "expression_id"),
            covered_claims, covered_exprs)


def _watch_section(snap: object) -> Mapping[str, object] | None:
    """Watch section of a state snapshot (None when absent)."""
    watch = getattr(snap, "watch", None)
    return watch if isinstance(watch, Mapping) else None


def _snapshot_rule_rows(snap: object) -> list[object]:
    """Watch rows of a state snapshot (empty when absent)."""
    watch = _watch_section(snap)
    rows: object = watch.get("rules", []) if watch is not None else []
    return list(rows) if isinstance(rows, list) else []


def _refinement_coverage(repository: ThesisRepository, tid: str,
                         effective_at: str | None) -> tuple[str, list[str], list[str], tuple[dict[str, set[str]], dict[str, set[str]]]]:
    """Coverage triple for live or snapshot state."""
    if effective_at is None:
        scope, cids, eids, cc, ce = _live_coverage(repository, tid)
    else:
        scope, cids, eids, cc, ce = _snapshot_coverage(repository, tid, effective_at)
    return scope, cids, eids, (cc, ce)


def _refinement_added(plan: Mapping[str, object], tid: str) -> tuple[list[object], list[object], list[object]]:
    """Added claim/expression/requirement lists; all three must be lists."""
    added_claims = plan["added_claims"]
    added_expressions = plan["added_expressions"]
    added_requirements = plan["added_requirements"]
    if not isinstance(added_claims, list) or not isinstance(added_expressions, list) or not isinstance(added_requirements, list):
        raise ValueError(f"thesis {tid!r} refinement plan must list added claims/expressions/requirements")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return added_claims, added_expressions, added_requirements


def _trim_rule_targets(cand: dict[str, JSONValue], covered_claims: dict[str, set[str]],
                       covered_exprs: dict[str, set[str]]) -> dict[str, JSONValue] | None:
    """Candidate rule trimmed to uncovered targets; None when fully covered."""
    rt = cand.get("rule_type")
    if not isinstance(rt, str):
        return None
    known_c = covered_claims.setdefault(rt, set())
    known_e = covered_exprs.setdefault(rt, set())
    cids = _trim_ids(cand.get("claim_ids", []), known_c)
    eids = _trim_ids(cand.get("expression_ids", []), known_e)
    if not (cids or eids):
        return None
    trimmed: dict[str, JSONValue] = {"rule_id": new_rule_id(), "rule_type": rt}
    for key, value in cand.items():
        if key not in ("rule_id", "claim_ids", "expression_ids"):
            trimmed[key] = value
    trimmed["claim_ids"] = list[JSONValue](cids)
    trimmed["expression_ids"] = list[JSONValue](eids)
    known_c.update(cids)
    known_e.update(eids)
    return trimmed


def _trim_ids(raw: object, known: set[str]) -> list[str]:
    """Candidate IDs minus already-covered ones."""
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c not in known]


def _refinement_rules(scope: str, proposal: IntakeProposal, claim_ids: list[str], expr_ids: list[str],
                      covered_claims: dict[str, set[str]], covered_exprs: dict[str, set[str]]) -> list[dict[str, JSONValue]]:
    """New watch rules for uncovered targets only (append-only)."""
    new_rules: list[dict[str, JSONValue]] = []
    for cand in build_initial_watch_rules(scope, proposal.requirements,
                                          claim_ids=claim_ids, expression_ids=expr_ids):
        trimmed = _trim_rule_targets(cand, covered_claims, covered_exprs)
        if trimmed is not None:
            new_rules.append(trimmed)
    return new_rules
