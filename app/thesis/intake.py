"""User-idea intake: normalize a natural-language thesis into a validated proposal.

# ponytail: the CLI default is a deterministic local read covering the plan's
# showcase + intake-table phrases; a Pi-backed JSON completion can plug the same
# IntakeGateway Protocol later with no migration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.policy import Capability
from app.thesis.models import (
    new_claim_id, new_expression_id, new_question_id, new_requirement_id, new_rule_id,
)

UNKNOWN = "unknown"

# StrEnum-free local copies of the small intake-relevant vocabularies.
_INSTRUMENTS = frozenset({"equity", "option", "future", "bond", "cash", UNKNOWN})
_DIRECTIONS = frozenset({"long", "short", "neutral", UNKNOWN})
_EXPRESSION_STATUSES = frozenset({"undecided", "active", "flagged", "closed"})
_REQUIREMENT_STATUSES = frozenset({"open", "answered"})
_MAX_QUESTIONS = 3


class IntakeGateway(Protocol):
    """Injectable structured-completion gateway (Pi plugs in later)."""

    def complete_json(self, prompt: str, *, request_context: Any) -> dict | str:
        ...


def _req_str(d: dict, key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _unknown_str(v: Any, key: str, where: str) -> str:
    if v is None or (isinstance(v, str) and not v.strip()):
        return UNKNOWN
    if isinstance(v, str):
        return v
    raise ValueError(f"{where}: '{key}' must be a string, got {type(v).__name__}")


def _str_list(d: dict, key: str, where: str) -> list:
    vals = d.get(key, [])
    if not isinstance(vals, list) or not all(isinstance(i, str) for i in vals):
        raise ValueError(f"{where}: '{key}' must be a list of strings")
    return list(vals)


def _as_list(d: dict, key: str, where: str) -> list:
    vals = d.get(key, [])
    if not isinstance(vals, list):
        raise ValueError(f"{where}: '{key}' must be a list, got {type(vals).__name__}")
    return vals


@dataclass(frozen=True)
class IntakeQuestion:
    question_id: str
    question: str
    question_type: str = UNKNOWN

    def to_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "question_type": self.question_type,
        }

    @classmethod
    def from_dict(cls, d: dict, path: str = "<intake>") -> IntakeQuestion:
        if not isinstance(d, dict):
            raise ValueError(f"{path}: question must be a mapping, got {type(d).__name__}")
        qid = d.get("question_id") or new_question_id()
        where = f"{path}: question {qid}"
        return cls(
            question_id=qid,
            question=_req_str(d, "question", where),
            question_type=_unknown_str(d.get("question_type", UNKNOWN), "question_type", where),
        )

def _validate_claim(c: Any, path: str) -> dict:
    if not isinstance(c, dict):
        raise ValueError(f"{path}: claim must be a mapping, got {type(c).__name__}")
    cid = c.get("claim_id") or new_claim_id()
    where = f"{path}: claim {cid}"
    if not isinstance(cid, str) or not cid:
        raise ValueError(f"{where}: 'claim_id' must be a non-empty string")
    return {
        "claim_id": cid,
        "statement": _req_str(c, "statement", where),
        "status": _unknown_str(c.get("status", "unvalidated"), "status", where),
    }


def _validate_expression(e: Any, path: str) -> dict:
    if not isinstance(e, dict):
        raise ValueError(f"{path}: expression must be a mapping, got {type(e).__name__}")
    eid = e.get("expression_id") or new_expression_id()
    where = f"{path}: expression {eid}"
    if not isinstance(eid, str) or not eid:
        raise ValueError(f"{where}: 'expression_id' must be a non-empty string")
    instrument = _unknown_str(e.get("instrument", UNKNOWN), "instrument", where)
    if instrument not in _INSTRUMENTS:
        raise ValueError(f"{where}: 'instrument' must be a known primitive or 'unknown', got {instrument!r}")
    direction = _unknown_str(e.get("direction", UNKNOWN), "direction", where)
    if direction not in _DIRECTIONS:
        raise ValueError(f"{where}: 'direction' must be a known primitive or 'unknown', got {direction!r}")
    structure = e.get("structure", UNKNOWN)  # open vocabulary, passed through unchanged
    if not isinstance(structure, str) or not structure.strip():
        raise ValueError(f"{where}: 'structure' must be a non-empty string (open vocabulary)")
    status = _unknown_str(e.get("status", "undecided"), "status", where)
    if status not in _EXPRESSION_STATUSES:
        raise ValueError(f"{where}: 'status' must be one of {sorted(_EXPRESSION_STATUSES)}, got {status!r}")
    out = {
        "expression_id": eid,
        "intent": _unknown_str(e.get("intent", UNKNOWN), "intent", where),
        "instrument": instrument,
        "direction": direction,
        "structure": structure,
        "horizon": _unknown_str(e.get("horizon", UNKNOWN), "horizon", where),
        "deterministic_support": _unknown_str(
            e.get("deterministic_support", UNKNOWN), "deterministic_support", where
        ),
        "status": status,
    }
    for key in ("leverage", "parameters"):
        v = e.get(key, {})
        if v is None:
            v = {}
        if not isinstance(v, dict):
            raise ValueError(f"{where}: '{key}' must be a mapping, got {type(v).__name__}")
        out[key] = dict(v)
    return out


def _validate_requirement(r: Any, path: str) -> dict:
    if not isinstance(r, dict):
        raise ValueError(f"{path}: requirement must be a mapping, got {type(r).__name__}")
    rid = r.get("requirement_id") or new_requirement_id()
    where = f"{path}: requirement {rid}"
    if not isinstance(rid, str) or not rid:
        raise ValueError(f"{where}: 'requirement_id' must be a non-empty string")
    status = _unknown_str(r.get("status", "open"), "status", where)
    if status not in _REQUIREMENT_STATUSES:
        raise ValueError(f"{where}: 'status' must be one of {sorted(_REQUIREMENT_STATUSES)}, got {status!r}")
    return {
        "requirement_id": rid,
        "expression_id": _req_str(r, "expression_id", where),
        "requirement_type": _req_str(r, "requirement_type", where),
        "statement": _req_str(r, "statement", where),
        "status": status,
    }


@dataclass(frozen=True)
class IntakeProposal:
    user_thesis: str
    scope: str = UNKNOWN
    claims: tuple = ()
    assumptions: tuple = ()
    invalidators: tuple = ()
    unknowns: tuple = ()
    expressions: tuple = ()
    requirements: tuple = ()
    questions: tuple = ()

    def __post_init__(self) -> None:
        self.validate("<intake>")

    def validate(self, path: str = "<intake>") -> None:
        where = f"{path}: proposal"
        if not isinstance(self.user_thesis, str) or not self.user_thesis.strip():
            raise ValueError(f"{where}: 'user_thesis' must be a non-empty string")
        if len(self.questions) > _MAX_QUESTIONS:
            raise ValueError(f"{where}: at most {_MAX_QUESTIONS} questions, got {len(self.questions)}")
        seen: set[str] = set()
        for c in self.claims:
            if c["status"] != "unvalidated":
                raise ValueError(
                    f"{where}: claim {c['claim_id']!r} status must stay 'unvalidated', got {c['status']!r}"
                )
            if c["claim_id"] in seen:
                raise ValueError(f"{where}: duplicate ID {c['claim_id']!r}")
            seen.add(c["claim_id"])
        for e in self.expressions:
            if e["expression_id"] in seen:
                raise ValueError(f"{where}: duplicate ID {e['expression_id']!r}")
            seen.add(e["expression_id"])
        expr_ids = {e["expression_id"] for e in self.expressions}
        for r in self.requirements:
            if r["requirement_id"] in seen:
                raise ValueError(f"{where}: duplicate ID {r['requirement_id']!r}")
            seen.add(r["requirement_id"])
            if r["expression_id"] not in expr_ids:
                raise ValueError(
                    f"{where}: requirement {r['requirement_id']!r} references absent expression "
                    f"{r['expression_id']!r}"
                )

    def to_dict(self) -> dict:
        return {
            "user_thesis": self.user_thesis,
            "scope": self.scope,
            "claims": [dict(c) for c in self.claims],
            "assumptions": list(self.assumptions),
            "invalidators": list(self.invalidators),
            "unknowns": list(self.unknowns),
            "expressions": [dict(e) for e in self.expressions],
            "requirements": [dict(r) for r in self.requirements],
            "questions": [q.to_dict() for q in self.questions],
        }

    @classmethod
    def from_dict(cls, d: dict, path: str = "<intake>") -> IntakeProposal:
        if not isinstance(d, dict):
            raise ValueError(f"{path}: proposal must be a mapping, got {type(d).__name__}")
        where = f"{path}: proposal"
        scope = d.get("scope", UNKNOWN)
        if scope is None or (isinstance(scope, str) and not scope.strip()):
            scope = UNKNOWN
        if not isinstance(scope, str):
            raise ValueError(f"{where}: 'scope' must be a string, got {type(scope).__name__}")
        return cls(
            user_thesis=_req_str(d, "user_thesis", where),
            scope=scope,
            claims=tuple(_validate_claim(c, path) for c in _as_list(d, "claims", where)),
            assumptions=tuple(_str_list(d, "assumptions", where)),
            invalidators=tuple(_str_list(d, "invalidators", where)),
            unknowns=tuple(_str_list(d, "unknowns", where)),
            expressions=tuple(_validate_expression(e, path) for e in _as_list(d, "expressions", where)),
            requirements=tuple(_validate_requirement(r, path) for r in _as_list(d, "requirements", where)),
            questions=tuple(IntakeQuestion.from_dict(q, path) for q in _as_list(d, "questions", where)),
        )


def _research_only_context(request_context: Any) -> Any:
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


def _build_prompt(text: str, answers: dict) -> str:
    lines = [
        "Normalize the user's investment idea into a single JSON object.",
        "No tools are needed; use only the idea text below.",
        "Exact shapes (omit IDs; they are assigned automatically):",
        "user_thesis: string (the idea, verbatim-ish).",
        "scope: short string, a ticker like NVDA or the literal unknown; never an object.",
        "assumptions/invalidators/unknowns: lists of strings (empty when none).",
        "expressions: [{intent: string, instrument: equity|option|future|bond|cash|unknown,",
        "  direction: long|short|neutral|unknown, structure: string, horizon: string,",
        "  status: undecided|active|flagged|closed}] (zero or more; never recommend a trade).",
        "requirements: [{expression_id: expr:N matching an expression above,",
        "  requirement_type: string, statement: string}] (timing/magnitude conditions only).",
        "questions: [{question: string, question_type: string}] (at most 3 material",
        "  questions whose answers would change scope, horizon, or expression research).",
        "Keep unavailable values the literal unknown; do not strengthen assertions.",
        f"Idea: {text}",
    ]
    if answers:
        lines.append(f"Answers: {json.dumps(answers)}")
    lines.append("Return JSON only.")
    return "\n".join(lines)


def _expr(*, intent: str, instrument: str, direction: str, structure: str, horizon: str = UNKNOWN) -> dict:
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


def _strategy_expression(combined: str) -> dict | None:
    if re.search(r"\bputs?\b", combined):
        return _expr(intent="bearish", instrument="option", direction="long", structure="long puts")
    if re.search(r"\bcalls?\b", combined):
        return _expr(intent="bullish", instrument="option", direction="long", structure="long calls")
    if re.search(r"\bshort\b|\binverse\b", combined):
        return _expr(intent="bearish", instrument="equity", direction="short", structure="short equity")
    if re.search(r"\baccumulat\w*|\bbuy\w*|\bown\b|\bbullish\b|\blong-term\b", combined):
        return _expr(intent="bullish", instrument="equity", direction="long", structure="equity")
    return None


def _local_interpret(text: str, answers: dict) -> IntakeProposal:
    blob = " ".join(str(v) for v in answers.values() if isinstance(v, (str, int, float)))
    blob = blob.lower()
    t = text.lower()
    combined = f"{t}\n{blob}"
    claim = {"claim_id": new_claim_id(), "statement": text, "status": "unvalidated"}
    base: dict[str, Any] = {
        "user_thesis": text,
        "scope": UNKNOWN,
        "claims": (claim,),
        "assumptions": (),
        "invalidators": (),
        "unknowns": (UNKNOWN,),
        "expressions": (),
        "requirements": (),
        "questions": (),
    }
    # Showcase: AI-infra repricing + answers wanting puts and post-selloff accumulation.
    if (
        "ai infrastructure" in t
        and "too high" in t
        and re.search(r"\bputs?\b", blob)
        and ("accumulat" in combined or "selloff" in combined or "sell-off" in combined)
    ):
        return IntakeProposal(
            **{
                **base,
                "expressions": (
                    _expr(intent="bearish", instrument="option", direction="long", structure="long puts"),
                    _expr(
                        intent="bullish",
                        instrument="equity",
                        direction="long",
                        structure="post-selloff equity accumulation",
                    ),
                ),
            }
        )
    # "own it for ten years" maps to obvious long-equity intent, no options questions.
    if re.search(r"\bten years\b|\b10 years\b|\bten-year\b", combined):
        return IntakeProposal(
            **{
                **base,
                "expressions": (
                    _expr(
                        intent="bullish",
                        instrument="equity",
                        direction="long",
                        structure="equity",
                        horizon="long-term",
                    ),
                ),
            }
        )
    # "still deciding" persists zero expressions rather than choosing one.
    if "still deciding" in combined:
        return IntakeProposal(**base)
    strat = _strategy_expression(combined)
    if strat is not None:
        return IntakeProposal(**{**base, "expressions": (strat,)})
    # No strategy stated: one expression-choice question, never a recommendation.
    return IntakeProposal(**{**base, "questions": (_expression_choice_question(),)})


def interpret_idea(
    text: str,
    answers: dict | None = None,
    gateway: IntakeGateway | None = None,
    request_context: Any = None,
) -> IntakeProposal:
    """Normalize ``text`` (+ optional ``answers``) into a validated IntakeProposal."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("<intake>: 'text' must be a non-empty string")
    ctx = _research_only_context(request_context)
    clean = text.strip()
    given = dict(answers or {})
    if gateway is None:
        # ponytail: deterministic local default (see module docstring).
        return _local_interpret(clean, given)
    # The gateway gets a prompt string plus the RESEARCH-only context only:
    # no filesystem/repo tools and no financial-research calls happen during intake.
    raw = gateway.complete_json(_build_prompt(clean, given), request_context=ctx)
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"<intake>: gateway returned invalid JSON: {exc}") from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise ValueError(
            f"<intake>: gateway must return a mapping or JSON string, got {type(raw).__name__}"
        )
    return IntakeProposal.from_dict(data, "<intake/gateway>")

_SCOPE_RE = re.compile(r"^[A-Za-z]{1,5}$")
_SETUP_QUESTION_ID = "setup:scope"


def build_initial_watch_rules(
    scope: str, requirements: Any, *, claim_ids: list = (), expression_ids: list = ()
) -> list[dict]:
    """Supported semantic rules only: explicit ticker scope -> ``new_filing`` plus
    any requirement whose type already names a supported monitor. No thresholds."""
    from app.thesis.monitor import SUPPORTED_HANDLERS  # local: monitor owns the table

    cids = [c for c in (claim_ids or []) if isinstance(c, str) and c]
    eids = [e for e in (expression_ids or []) if isinstance(e, str) and e]
    rules: list[dict] = []
    if _SCOPE_RE.fullmatch((scope or "").strip()) and "new_filing" in SUPPORTED_HANDLERS and (cids or eids):
        rules.append({
            "rule_id": new_rule_id(),
            "rule_type": "new_filing",
            "enabled": True,
            "support_status": "supported",
            "support_reason": "",
            "claim_ids": list(cids),
            "expression_ids": list(eids),
        })
    seen = {r["rule_type"] for r in rules}
    for r in requirements or []:
        rt = r.get("requirement_type") if isinstance(r, dict) else getattr(r, "requirement_type", None)
        eid = r.get("expression_id") if isinstance(r, dict) else getattr(r, "expression_id", None)
        if not isinstance(rt, str) or rt not in SUPPORTED_HANDLERS or rt in seen or eid not in eids:
            continue
        rules.append({
            "rule_id": new_rule_id(),
            "rule_type": rt,
            "enabled": True,
            "support_status": "supported",
            "support_reason": "",
            "claim_ids": [],
            "expression_ids": [eid],
        })
        seen.add(rt)
    return rules


def setup_needed_question() -> dict:
    return {
        "question_id": _SETUP_QUESTION_ID,
        "text": "Which ticker should this thesis monitor? Reply with the single ticker symbol (e.g. NVDA).",
        "status": "open",
    }


def create_thesis_from_proposal(repository: Any, proposal: IntakeProposal) -> dict:
    """Shared CLI + tool creation path: thesis, explicit scope, supported rules.

    Unresolvable scope persists a setup question instead of a fake active
    monitor. Returns thesis_id/slug/scope/rules/setup_needed/missing_questions.
    """
    rules = build_initial_watch_rules(
        proposal.scope, proposal.requirements,
        claim_ids=[c["claim_id"] for c in proposal.claims],
        expression_ids=[e["expression_id"] for e in proposal.expressions])
    thesis = repository.create_thesis(
        user_thesis=proposal.user_thesis,
        scope=proposal.scope,
        claims=[dict(c) for c in proposal.claims],
        assumptions=list(proposal.assumptions),
        invalidators=list(proposal.invalidators),
        unknowns=list(proposal.unknowns),
        expressions=[dict(e) for e in proposal.expressions],
        requirements=[dict(r) for r in proposal.requirements],
        watch_rules=rules,
    )
    missing: list[dict] = []
    questions_add: list[dict] = []
    for q in proposal.questions or ():
        if isinstance(q, dict):
            qid, text = q.get("question_id") or new_question_id(), q.get("question") or q.get("text", "")
        else:
            qid, text = q.question_id, q.question
        if isinstance(text, str) and text.strip():
            questions_add.append({"question_id": qid, "text": text.strip(), "status": "open"})
    if not rules:
        missing.append(setup_needed_question())
        questions_add.append(dict(missing[0]))
    if questions_add:
        repository.apply_research_result(thesis.thesis_id, {"questions_add": questions_add}, "")
    return {
        "thesis_id": thesis.thesis_id,
        "slug": thesis.slug,
        "scope": thesis.scope,
        "setup_needed": not rules,
        "missing_questions": [dict(m) for m in missing],
        "rules": [dict(r) for r in rules],
    }


def plan_refinement(thesis: Any, proposal: IntakeProposal) -> dict:
    """Pure merge preview: added claims/expressions/requirements plus merged payload."""
    old_claims = {c.statement for c in thesis.claims}
    claims = [c.to_dict() for c in thesis.claims]
    added_claims = [dict(c) for c in proposal.claims if c["statement"] not in old_claims]
    old_expr = {(e.intent, e.instrument, e.direction, e.structure, e.horizon)
                for e in thesis.expressions}
    expressions = [e.to_dict() for e in thesis.expressions]
    added_expr = [dict(e) for e in proposal.expressions
                  if (e["intent"], e["instrument"], e["direction"],
                      e["structure"], e["horizon"]) not in old_expr]
    new_ids = {e["expression_id"] for e in added_expr}
    requirements = [r.to_dict() for r in thesis.requirements]
    added_reqs = [dict(r) for r in proposal.requirements if r["expression_id"] in new_ids]
    merged = {
        "user_thesis": proposal.user_thesis,
        "scope": proposal.scope if proposal.scope != UNKNOWN else thesis.scope,
        "claims": claims + added_claims,
        "assumptions": list(dict.fromkeys([*thesis.assumptions, *proposal.assumptions])),
        "invalidators": list(dict.fromkeys([*thesis.invalidators, *proposal.invalidators])),
        "unknowns": list(dict.fromkeys([*thesis.unknowns, *proposal.unknowns])),
        "expressions": expressions + added_expr,
        "requirements": requirements + added_reqs,
    }
    return {
        "merged": merged,
        "added_claims": added_claims,
        "added_expressions": added_expr,
        "added_requirements": added_reqs,
    }


def apply_refinement(repository: Any, thesis_id: str, plan: dict, proposal: IntakeProposal) -> dict:
    """Apply a refinement plan; watch changes are append-only, never touching user rules."""
    from app.thesis.monitor import SUPPORTED_HANDLERS  # local: monitor owns the handler table

    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    if thesis.status != "active":
        raise ValueError(f"thesis {tid!r} is {thesis.status}; refusing refinement")
    updated = repository.update_thesis(tid, **plan["merged"])
    fresh = repository.load_thesis(tid)
    covered_claims: dict[str, set[str]] = {}
    covered_exprs: dict[str, set[str]] = {}
    for r in repository.load_watch_rules(tid):
        if not r.enabled or r.support_status != "supported" or r.rule_type not in SUPPORTED_HANDLERS:
            continue
        covered_claims.setdefault(r.rule_type, set()).update(r.claim_ids)
        covered_exprs.setdefault(r.rule_type, set()).update(r.expression_ids)
    new_rules = []
    for cand in build_initial_watch_rules(
            fresh.scope, proposal.requirements,
            claim_ids=[c.claim_id for c in fresh.claims],
            expression_ids=[e.expression_id for e in fresh.expressions]):
        rt = cand["rule_type"]
        known_c = covered_claims.setdefault(rt, set())
        known_e = covered_exprs.setdefault(rt, set())
        cids = [c for c in cand.get("claim_ids", []) if c not in known_c]
        eids = [e for e in cand.get("expression_ids", []) if e not in known_e]
        if not (cids or eids):
            continue
        cand = dict(cand, rule_id=new_rule_id(), claim_ids=cids, expression_ids=eids)
        new_rules.append(cand)
        known_c.update(cids)
        known_e.update(eids)
    if new_rules:
        repository.apply_research_result(tid, {"watch_add": new_rules}, "")
    return {
        "thesis_id": tid,
        "slug": updated.slug,
        "added_claims": len(plan["added_claims"]),
        "added_expressions": len(plan["added_expressions"]),
        "added_requirements": len(plan["added_requirements"]),
        "rules_added": [dict(r) for r in new_rules],
    }
