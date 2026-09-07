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
from app.thesis.models import new_claim_id, new_expression_id, new_question_id, new_requirement_id

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
        "Normalize the user's investment idea into a single JSON object with keys:",
        "user_thesis, scope, claims, assumptions, invalidators, unknowns,",
        "expressions, requirements, questions.",
        "Rules: preserve the user's belief as unvalidated claims (status 'unvalidated');",
        "do not strengthen assertions; keep unavailable values the literal 'unknown';",
        "keep zero or more trade expressions separate from the thesis itself;",
        "attach timing/magnitude/portfolio-only requirements to expressions, never the thesis;",
        "never select or recommend a trade; ask at most 3 material questions whose",
        "answers would change scope, horizon, invalidation research, or expression research.",
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
