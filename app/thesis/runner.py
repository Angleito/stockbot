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
    require_watch_targets,
)
from app.security.action_policy import TOOL_DOMAINS
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
# Model-authored URLs and publication/retrieval metadata are never durable provenance.
_PROVENANCE_KEYS = frozenset({
    "url", "urls", "link", "links", "source_url",
    "provenance", "publication", "published_at", "retrieved_at", "retrieval", "origin",
})


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
        from app.thesis.monitor import SUPPORTED_HANDLERS  # local: avoid monitor -> runner cycle
        watches: list[dict] = []
        for i, r in enumerate(_str_list(d, "watch_add", path)):
            where = f"{path}: watch_add[{i}]"
            if not isinstance(r, dict):
                raise ValueError(f"{where}: must be a mapping")
            rd = dict(r)
            rd.setdefault("rule_id", new_rule_id())
            rule = WatchRule.from_dict(rd, where)  # unknown types must stay disabled/unsupported
            if rule.rule_type not in SUPPORTED_HANDLERS:
                reason = rule.support_reason or (
                    "no production source for 'new_external_evidence'; never queried"
                    if rule.rule_type == "new_external_evidence"
                    else f"no deterministic monitor backing for {rule.rule_type!r}; never queried"
                )
                rule = WatchRule(
                    rule_id=rule.rule_id,
                    rule_type=rule.rule_type,
                    enabled=False,
                    support_status="unsupported",
                    support_reason=reason,
                    claim_ids=rule.claim_ids,
                    expression_ids=rule.expression_ids,
                )
            for cid in rule.claim_ids:
                if cid not in claim_ids:
                    raise ValueError(f"{where}: rule references absent claim {cid!r}")
            for eid in rule.expression_ids:
                if eid not in expression_ids:
                    raise ValueError(f"{where}: rule references absent expression {eid!r}")
            require_watch_targets(rule, where)
            watches.append(rule.to_dict())
        evidence: list[dict] = []
        for i, ref in enumerate(_str_list(d, "evidence_refs", path)):
            where = f"{path}: evidence_refs[{i}]"
            if not isinstance(ref, dict):
                raise ValueError(f"{where}: must be a mapping")
            smuggled = [k for k in _BODY_KEYS | _PROVENANCE_KEYS if ref.get(k)]
            if smuggled:
                raise ValueError(f"{where}: evidence refs must not carry bodies/provenance (got {smuggled})")
            ed = dict(ref)
            ed.setdefault("evidence_id", new_evidence_id())
            ed["thesis_id"] = thesis_id
            ev = EvidenceRef.from_dict(ed, where)  # requires canonical_ref
            from app.thesis.monitor import _as_dt  # local: monitor -> runner -> context
            dt_ev, dt_cut = _as_dt(ev.known_at), _as_dt(known_at)
            if dt_ev is None or dt_cut is None or dt_ev > dt_cut:
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
            "Exact shapes (IDs for new questions/memories/rules/evidence are assigned",
            "automatically; every list item MUST be an object, never a bare string):",
            "claim_updates: [{claim_id (a thesis claim ID from context),",
            "  status: supported|challenged|invalidated|unresolved}] (empty when none change).",
            "expression_updates: [{expression_id (a thesis expression ID from context),",
            "  status: undecided|active|flagged|closed}] (empty when none change).",
            "questions_answered: [{question_id (an open question ID from context),",
            "  answer: string}] (empty when none can be answered).",
            "questions_add: [{text: string}] (at most 3 still-unknown questions).",
            "memories_add: [{text: string}] (at most 1 durable fact).",
            "watch_add: [{rule_type (a semantic monitor name like new_filing, never a tool",
            "  name), claim_ids: [IDs], expression_ids: [IDs]}] (empty when none needed).",
            "evidence_refs: [{canonical_ref, summary, known_at}] (refs already in context;",
            "  known_at at or before the run known_at; never invent a ref).",
            "alert: true/false; alert_reason/counterevidence/alternative/journal_summary:",
            "strings; state: object only to change thesis status, else null."
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


def _check_evidence_provenance(evidence_refs: tuple, allowed: set[str], trigger_id: str) -> None:
    """Pure membership: every ref must name an allowed canonical ref."""
    for ref in evidence_refs:
        if ref.get("canonical_ref") not in allowed:
            raise ValueError(
                f"<research/gateway>: foreign canonical_ref {ref.get('canonical_ref')!r}"
                f" (trigger {trigger_id!r}); refusing writeback")


def _payload_from_result(
    result: ThesisResearchResult, *, run_id: str, trigger: Any, known_at: str,
    tool_names: list, started_at: str,
) -> dict:
    title, body = _build_journal(
        run_id=run_id,
        thesis_id=trigger.thesis_id,
        trigger=trigger.to_dict(),
        result=result,
        tool_names=tool_names,
        started_at=started_at,
        completed_at=_utcnow(),
        known_at=known_at,
    )
    return {
        "claim_updates": list(result.claim_updates),
        "expression_updates": list(result.expression_updates),
        "questions_answered": list(result.questions_answered),
        "evidence_refs": list(result.evidence_refs),
        "state": result.state,
        "questions_add": list(result.questions_add),
        "memories_add": list(result.memories_add),
        "watch_add": list(result.watch_add),
        "journal_entry": {
            "entry_id": f"journal:{run_id}",
            "title": title,
            "body": body,
            "trigger_id": trigger.trigger_id,
            "known_at": known_at,
        },
        "trigger_id": trigger.trigger_id,
    }


def _outcome_from_applied(
    repository: Any, thesis_id: str, trigger_id: str, run_id: str,
    payload: dict, tool_names: list, allowed_refs: set[str] | None = None,
) -> RunOutcome:
    # Re-read the recorded intent's payload for IDs (replay-safe: same result twice).
    outcome = repository.apply_research_result(thesis_id, payload, run_id, allowed_refs=allowed_refs)
    repository.clear_pending_result(thesis_id, trigger_id)
    return RunOutcome(
        run_id=run_id,
        thesis_id=thesis_id,
        trigger_id=trigger_id,
        journal_path=str(outcome.get("journal", "")),
        evidence_ids=tuple(e["evidence_id"] for e in payload.get("evidence_refs", [])),
        processed=True,
        tools_used=tuple(tool_names),
    )


def run_trigger(
    repository: Any,
    thesis_id: str,
    trigger_id: str,
    gateway: ResearchGateway,
    request_context: RequestContext,
    *,
    known_at: str,
) -> RunOutcome:
    """Run one pending trigger through the gateway and persist via the repository.

    The validated result is recorded as a durable pending intent before any
    mutation; a retry replays the recorded result without a second model call
    (existing by-ID dedup keeps journals/questions singular). The trigger is
    marked processed only after all result files install, then the intent is
    removed. Invalid/foreign results raise before the intent is written, so
    they change nothing and leave the trigger pending.
    """
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    trigger = next(
        (t for t in repository.load_triggers(tid) if t.trigger_id == trigger_id), None
    )
    if trigger is None:
        raise KeyError(f"unknown trigger: {trigger_id!r}")

    # Crash-recovery replay comes first: the recorded result wins over trigger
    # status (a crash can land after processing but before intent removal).
    pending = repository.read_pending_result(tid, trigger_id)
    if pending is not None:
        return _outcome_from_applied(
            repository, tid, trigger_id, pending["run_id"],
            pending["payload"], list(pending.get("tool_names", [])),
            set(pending["allowed_refs"]) if pending.get("allowed_refs") is not None else None)
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
    # Trigger sub-runs research only: thesis tools stay interactive-only.
    visible = [t for t in visible if TOOL_DOMAINS.get(t["function"]["name"]) not in ("thesis_read", "thesis_write")]
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
        allowed = set(trigger.canonical_refs) | {
            e["canonical_ref"] for e in context.evidence_refs if e.get("canonical_ref")}
        _check_evidence_provenance(result.evidence_refs, allowed, trigger.trigger_id)
        payload = _payload_from_result(
            result, run_id=rid, trigger=trigger, known_at=known_at,
            tool_names=tool_names, started_at=started_at)
        repository.write_pending_result(
            tid, trigger_id,
            {"run_id": rid, "known_at": known_at,
             "tool_names": tool_names, "payload": payload, "allowed_refs": sorted(allowed)})
        return _outcome_from_applied(repository, tid, trigger_id, rid, payload, tool_names, allowed)
    except Exception as exc:
        _fail(rid, exc)
        raise
