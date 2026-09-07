"""Bounded research runs over a thesis trigger (stdlib + PyYAML only).

The gateway is injectable (same pattern as intake.IntakeGateway); production Pi
plugs in later and never receives a repository or filesystem tool. Model-visible
tools are derived per invocation from the canonical registry; never cached here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from app.config import get_data_root
from app.policy import Capability, RequestContext
from app.storage.ids import run_id as new_run_id
from app.thesis.context import build_context
from app.thesis.models import (
    ClaimStatus,
    EvidenceRef,
    ExpressionStatus,
    QuestionStatus,
    ThesisQuestion,
    ThesisState,
    WatchRule,
    new_evidence_id,
    new_memory_id,
    new_question_id,
    new_rule_id,
)
from app.tools import tools_for_capabilities

_GRANTS: dict[str, Capability] = {
    "broker-market-read": Capability.BROKER_MARKET_READ,
    "portfolio-read": Capability.PORTFOLIO_READ,
}
_READ_CAPS = frozenset({Capability.BROKER_MARKET_READ, Capability.PORTFOLIO_READ})
_CLAIM_VOCAB = {e.value for e in ClaimStatus}
_EXPRESSION_VOCAB = {e.value for e in ExpressionStatus}
_QUESTION_VOCAB = {e.value for e in QuestionStatus}
# Evidence refs carry canonical refs + summaries only; reject smuggled bodies.
_BODY_KEYS = frozenset({"body", "content", "filing_body", "full_text", "document_text"})


class ResearchGateway(Protocol):
    """Injectable bounded-research gateway (Pi plugs in later)."""

    def complete_research(self, prompt: str, *, request_context: Any, tools: list) -> dict | str:
        ...


def capabilities_for_grants(grants: list[str]) -> frozenset[Capability]:
    """Map explicit CLI grant strings to capabilities; reject anything else."""
    caps: set[Capability] = set()
    for g in grants or []:
        if g not in _GRANTS:
            raise ValueError(f"<runner>: unknown grant {g!r}; expected one of {sorted(_GRANTS)}")
        caps.add(_GRANTS[g])
    return frozenset(caps)


def visible_tools_for(request_context: RequestContext) -> list[dict]:
    """Derive model-visible tools from this invocation's capabilities (no cache)."""
    return tools_for_capabilities(frozenset(request_context.capabilities))


def _req_str(d: dict, key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise ValueError(f"{where}: '{key}' must be a non-empty string")
    return v


def _str_list(d: dict, key: str, where: str) -> list:
    vals = d.get(key, [])
    if not isinstance(vals, list):
        raise ValueError(f"{where}: '{key}' must be a list")
    return vals


@dataclass(frozen=True)
class ThesisResearchResult:
    claim_updates: tuple = ()
    expression_updates: tuple = ()
    questions_answered: tuple = ()
    questions_add: tuple = ()
    memories_add: tuple = ()
    watch_add: tuple = ()
    evidence_refs: tuple = ()
    state: dict | None = None
    alert: bool = False
    alert_reason: str = ""
    counterevidence: str = ""
    alternative: str = ""
    journal_summary: str = ""

    @classmethod
    def from_dict(
        cls,
        d: dict,
        *,
        thesis_id: str,
        claim_ids: set[str],
        expression_ids: set[str],
        question_ids: set[str],
        known_at: str,
        path: str = "<research/gateway>",
    ) -> ThesisResearchResult:
        if not isinstance(d, dict):
            raise ValueError(f"{path}: research result must be a mapping, got {type(d).__name__}")
        claims: list[dict] = []
        for i, c in enumerate(_str_list(d, "claim_updates", path)):
            where = f"{path}: claim_updates[{i}]"
            if not isinstance(c, dict):
                raise ValueError(f"{where}: must be a mapping")
            cid = _req_str(c, "claim_id", where)
            if cid not in claim_ids:
                raise ValueError(f"{where}: claim {cid!r} does not belong to thesis {thesis_id!r}")
            status = c.get("status")
            if status is not None and status not in _CLAIM_VOCAB:
                raise ValueError(f"{where}: bad claim status {status!r}")
            claims.append({"claim_id": cid, **({"status": status} if status else {})})
        expressions: list[dict] = []
        for i, e in enumerate(_str_list(d, "expression_updates", path)):
            where = f"{path}: expression_updates[{i}]"
            if not isinstance(e, dict):
                raise ValueError(f"{where}: must be a mapping")
            eid = _req_str(e, "expression_id", where)
            if eid not in expression_ids:
                raise ValueError(f"{where}: expression {eid!r} does not belong to thesis {thesis_id!r}")
            status = e.get("status")
            if status is not None and status not in _EXPRESSION_VOCAB:
                raise ValueError(f"{where}: bad expression status {status!r}")
            expressions.append({"expression_id": eid, **({"status": status} if status else {})})
        answered: list[dict] = []
        for i, q in enumerate(_str_list(d, "questions_answered", path)):
            where = f"{path}: questions_answered[{i}]"
            if not isinstance(q, dict):
                raise ValueError(f"{where}: must be a mapping")
            qid = _req_str(q, "question_id", where)
            if qid not in question_ids:
                raise ValueError(f"{where}: question {qid!r} does not belong to thesis {thesis_id!r}")
            answered.append({"question_id": qid, "answer": _req_str(q, "answer", where)})
        new_questions: list[dict] = []
        for i, q in enumerate(_str_list(d, "questions_add", path)):
            where = f"{path}: questions_add[{i}]"
            if not isinstance(q, dict):
                raise ValueError(f"{where}: must be a mapping")
            qd = dict(q)
            qd.setdefault("question_id", new_question_id())
            if qd.get("status", "open") not in _QUESTION_VOCAB:
                raise ValueError(f"{where}: bad question status {qd.get('status')!r}")
            new_questions.append(ThesisQuestion.from_dict(qd, where).to_dict())
        memories: list[dict] = []
        for i, m in enumerate(_str_list(d, "memories_add", path)):
            where = f"{path}: memories_add[{i}]"
            if not isinstance(m, dict):
                raise ValueError(f"{where}: must be a mapping")
            md = dict(m)
            md.setdefault("memory_id", new_memory_id())
            md.setdefault("created_at", known_at)
            if not isinstance(md.get("text"), str) or not md["text"].strip():
                raise ValueError(f"{where}: 'text' must be a non-empty string")
            memories.append(md)
        watches: list[dict] = []
        for i, r in enumerate(_str_list(d, "watch_add", path)):
            where = f"{path}: watch_add[{i}]"
            if not isinstance(r, dict):
                raise ValueError(f"{where}: must be a mapping")
            rd = dict(r)
            rd.setdefault("rule_id", new_rule_id())
            rule = WatchRule.from_dict(rd, where)  # unknown types must stay disabled/unsupported
            for cid in rule.claim_ids:
                if cid not in claim_ids:
                    raise ValueError(f"{where}: rule references absent claim {cid!r}")
            for eid in rule.expression_ids:
                if eid not in expression_ids:
                    raise ValueError(f"{where}: rule references absent expression {eid!r}")
            watches.append(rule.to_dict())
        evidence: list[dict] = []
        for i, ref in enumerate(_str_list(d, "evidence_refs", path)):
            where = f"{path}: evidence_refs[{i}]"
            if not isinstance(ref, dict):
                raise ValueError(f"{where}: must be a mapping")
            smuggled = [k for k in _BODY_KEYS if ref.get(k)]
            if smuggled:
                raise ValueError(f"{where}: evidence refs must not carry bodies (got {smuggled})")
            ed = dict(ref)
            ed.setdefault("evidence_id", new_evidence_id())
            ed["thesis_id"] = thesis_id
            ev = EvidenceRef.from_dict(ed, where)  # requires canonical_ref
            if not ev.known_at or ev.known_at > known_at:
                raise ValueError(f"{where}: future or missing known_at {ev.known_at!r} (run known_at {known_at!r})")
            evidence.append(ev.to_dict())
        state = None
        if d.get("state") is not None:
            if not isinstance(d["state"], dict):
                raise ValueError(f"{path}: 'state' must be a mapping")
            sd = dict(d["state"])
            sd["thesis_id"] = thesis_id
            state = ThesisState.from_dict(sd, f"{path}: state").to_dict()
        alert = d.get("alert", False)
        if not isinstance(alert, bool):
            raise ValueError(f"{path}: 'alert' must be a bool")
        return cls(
            claim_updates=tuple(claims),
            expression_updates=tuple(expressions),
            questions_answered=tuple(answered),
            questions_add=tuple(new_questions),
            memories_add=tuple(memories),
            watch_add=tuple(watches),
            evidence_refs=tuple(evidence),
            state=state,
            alert=alert,
            alert_reason=str(d.get("alert_reason", "")),
            counterevidence=str(d.get("counterevidence", "")),
            alternative=str(d.get("alternative", "")),
            journal_summary=str(d.get("journal_summary", "")),
        )


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    thesis_id: str
    trigger_id: str
    journal_path: str = ""
    evidence_ids: tuple = ()
    processed: bool = False
    tools_used: tuple = field(default_factory=tuple)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_prompt(context: Any, trigger: dict, known_at: str) -> str:
    return "\n".join(
        [
            "You are researching one thesis trigger. Answer these ten questions with structured JSON:",
            "1. claim validity: which thesis claims hold, weaken, or break?",
            "2. counterevidence: what contradicts the thesis?",
            "3. invalidators: did any explicit invalidation condition fire?",
            "4. expression viability and timing: is each trade expression still viable on its horizon?",
            "5. newly-answerable questions: which open questions can now be answered, and how?",
            "6. watch changes: which semantic monitors should be added (never tool names)?",
            "7. durable memory: what single durable fact is worth remembering?",
            "8. alert: should the user be alerted now, and why?",
            "9. alternative interpretation: what else could this evidence mean?",
            "10. remaining questions: what is still unknown?",
            "You MUST NOT: assume the thesis is true, choose or recommend a trade,",
            "invent unavailable market data (prices, Greeks, chains), modify canonical",
            "records, or write any files. Unknown values stay 'unknown'.",
            "Return ONLY JSON with keys: claim_updates, expression_updates,",
            "questions_answered, questions_add, memories_add, watch_add, evidence_refs",
            "(canonical_ref + summary + known_at, no bodies), state, alert,",
            "alert_reason, counterevidence, alternative, journal_summary.",
            f"known_at: {known_at}",
            f"trigger: {json.dumps(trigger, sort_keys=True)}",
            f"context: {json.dumps(context.thesis_packet, sort_keys=True)}",
            f"evidence: {json.dumps(context.evidence_refs, sort_keys=True)}",
            f"journals: {json.dumps(context.journal_excerpts, sort_keys=True)}",
        ]
    )


def _build_journal(
    *,
    run_id: str,
    thesis_id: str,
    trigger: dict,
    result: ThesisResearchResult,
    tool_names: list[str],
    started_at: str,
    completed_at: str,
    known_at: str,
) -> tuple[str, str]:
    lines = [
        f"run_id: {run_id}",
        f"thesis_id: {thesis_id}",
        f"trigger_id: {trigger.get('trigger_id', '')}",
        f"started_at: {started_at}",
        f"completed_at: {completed_at}",
        f"known_at: {known_at}",
        "",
        "## Trigger",
        json.dumps(trigger, sort_keys=True),
        "",
        "## Research Performed",
        result.journal_summary or "(no summary)",
        "",
        "## Tools Used",
        ", ".join(tool_names) or "(none)",
        "",
        "## Supporting Evidence",
        *(
            [f"- {e['canonical_ref']} ({e['known_at']}): {e.get('summary', '')}" for e in result.evidence_refs]
            or ["(none)"]
        ),
        "",
        "## Counterevidence",
        result.counterevidence or "(none)",
        "",
        "## Thesis Impact",
        *(
            [f"- {c['claim_id']}: {c.get('status', 'reviewed')}" for c in result.claim_updates]
            or ["(none)"]
        ),
        "",
        "## Expression Impact",
        *(
            [f"- {e['expression_id']}: {e.get('status', 'reviewed')}" for e in result.expression_updates]
            or ["(none)"]
        ),
        "## Conclusions",
        result.alternative or "(none)",
        *(
            [f"ALERT: {result.alert_reason or 'user attention warranted'}"] if result.alert else []
        ),
        "",
        "## Remaining Questions",
        *([f"- {q['text']}" for q in result.questions_add] or ["(none)"]),
    ]
    return f"Research run {run_id}", "\n".join(lines)


def _apply_answered(repository: Any, thesis_id: str, answered: tuple) -> None:
    repository.answer_questions(thesis_id, list(answered or ()))


def _fail(run_id: str, exc: Exception) -> None:
    try:
        from app.storage.runs import finalize_failed_run

        finalize_failed_run(run_id, error_type=type(exc).__name__, error_message=str(exc))
    except Exception:
        pass


def run_trigger(
    repository: Any,
    thesis_id: str,
    trigger_id: str,
    gateway: ResearchGateway,
    request_context: RequestContext,
    *,
    known_at: str,
) -> RunOutcome:
    """Run one pending trigger through the gateway and persist via the repository."""
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    trigger = next(
        (t for t in repository.load_triggers(tid) if t.trigger_id == trigger_id), None
    )
    if trigger is None:
        raise KeyError(f"unknown trigger: {trigger_id!r}")
    if trigger.status != "pending":
        raise ValueError(f"<runner>: trigger {trigger_id!r} is {trigger.status}, not pending")

    fresh = RequestContext(
        principal_id=request_context.principal_id,
        capabilities=frozenset({Capability.RESEARCH})
        | (frozenset(request_context.capabilities) & _READ_CAPS),
        data_root=getattr(request_context, "data_root", None) or get_data_root(),
        as_of=getattr(request_context, "as_of", None),
    )
    visible = visible_tools_for(fresh)  # derived each call, never cached
    tool_names = [t["function"]["name"] for t in visible]

    started_at = _utcnow()
    rid = new_run_id()
    try:
        context = build_context(repository, tid, trigger, known_at=known_at)
        raw = gateway.complete_research(
            _build_prompt(context, trigger.to_dict(), known_at),
            request_context=fresh,
            tools=visible,
        )
        data = json.loads(raw) if isinstance(raw, str) else raw
        result = ThesisResearchResult.from_dict(
            data,
            thesis_id=tid,
            claim_ids={c.claim_id for c in thesis.claims},
            expression_ids={e.expression_id for e in thesis.expressions},
            question_ids={q.question_id for q in repository.load_questions(tid)},
            known_at=known_at,
            path="<research/gateway>",
        )
        if result.claim_updates or result.expression_updates:
            patched = thesis.to_dict()
            claim_patch = {c["claim_id"]: c for c in result.claim_updates}
            patched["claims"] = [
                {**c, **claim_patch[c["claim_id"]]} if c["claim_id"] in claim_patch else c
                for c in patched["claims"]
            ]
            expr_patch = {e["expression_id"]: e for e in result.expression_updates}
            patched["expressions"] = [
                {**e, **expr_patch[e["expression_id"]]} if e["expression_id"] in expr_patch else e
                for e in patched["expressions"]
            ]
            repository.update_thesis(tid, claims=patched["claims"], expressions=patched["expressions"])
        title, body = _build_journal(
            run_id=rid,
            thesis_id=tid,
            trigger=trigger.to_dict(),
            result=result,
            tool_names=tool_names,
            started_at=started_at,
            completed_at=_utcnow(),
            known_at=known_at,
        )
        outcome = repository.apply_research_result(
            tid,
            {
                "evidence_refs": list(result.evidence_refs),
                "state": result.state,
                "questions_add": list(result.questions_add),
                "memories_add": list(result.memories_add),
                "journal_entry": {
                    "entry_id": f"journal:{rid}",
                    "title": title,
                    "body": body,
                    "trigger_id": trigger_id,
                    "known_at": known_at,
                },
                "trigger_id": trigger_id,
            },
            rid,
        )
        _apply_answered(repository, tid, result.questions_answered)
        return RunOutcome(
            run_id=rid,
            thesis_id=tid,
            trigger_id=trigger_id,
            journal_path=str(outcome.get("journal", "")),
            evidence_ids=tuple(e["evidence_id"] for e in result.evidence_refs),
            processed=True,
            tools_used=tuple(tool_names),
        )
    except Exception as exc:
        _fail(rid, exc)
        raise
