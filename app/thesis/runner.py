"""Bounded Pi runs over a thesis trigger (stdlib + PyYAML only).

Automated monitoring launches one normal-Pi subprocess per pending trigger
(see app.thesis.pi_runner); Pi persists its own findings through the
canonical thesis tools. The runner only selects the trigger, builds a small
bounded prompt, launches Pi without holding any lock across the subprocess,
and acknowledges the stable trigger ID only once a durable trigger-linked
journal entry exists. A failed launch (or a run with no such journal) leaves
the trigger pending with valid partial tool writes intact (at-least-once
retry; no rollback).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import get_data_root
from app.policy import Capability
from app.storage.ids import run_id as new_run_id
from app.thesis.context import ResearchContext, build_live_context
from app.thesis.models import Trigger
from app.thesis.pi_runner import run_thesis_pi
from app.thesis.repository import ThesisRepository

_GRANTS: dict[str, Capability] = {
    "broker-market-read": Capability.BROKER_MARKET_READ,
    "portfolio-read": Capability.PORTFOLIO_READ,
}


def capabilities_for_grants(grants: list[str]) -> frozenset[Capability]:
    """Map explicit CLI grant strings to capabilities; reject anything else."""
    caps: set[Capability] = set()
    for g in grants or []:
        if g not in _GRANTS:
            raise ValueError(f"<runner>: unknown grant {g!r}; expected one of {sorted(_GRANTS)}")
        caps.add(_GRANTS[g])
    return frozenset(caps)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    thesis_id: str
    trigger_id: str
    journal_path: str = ""
    evidence_ids: tuple[str, ...] = ()
    processed: bool = False
    tools_used: tuple[str, ...] = field(default_factory=tuple)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_prompt_text(text: str, ref: str) -> str:
    """Gate stored free text via the shared injection scanner; provenance labels never bypass it."""
    if text is None or text == "":
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return f"[recycled content withheld ref={ref}]"
        if text == "":
            return ""
    try:
        from app.security.prompt_injection import assess  # local: keep thesis import graph acyclic
        found = assess(text)
    except Exception:
        return f"[recycled content withheld ref={ref}]"
    if found.verdict in ("BLOCK", "QUARANTINE"):
        return f"[recycled content withheld ref={ref}]"
    return text


def _build_prompt(*, thesis_id: str, trigger: Trigger, data_cutoff: str, ctx: ResearchContext, run_id: str) -> str:
    refs = ", ".join(trigger.canonical_refs) or "(none)"
    packet = dict(ctx.thesis_packet)
    trig = packet.pop("trigger", {})
    tid_ref = trigger.trigger_id or "unknown"
    summary_line = _safe_prompt_text(trigger.summary or "", ref=f"trigger:{tid_ref}")[:500]
    if isinstance(trig, dict):
        trig = dict(trig)
        if "summary" in trig:
            raw = trig.get("summary", "")
            if raw is None or raw == "":
                trig["summary"] = ""
            else:
                s = raw if isinstance(raw, str) else str(raw)
                trig["summary"] = _safe_prompt_text(s, ref=f"trigger:{tid_ref}")
    trig_out = trig
    safe_evidence: list[object] = []
    for e in ctx.evidence_refs:
        if not isinstance(e, dict):
            safe_evidence.append(e)
            continue
        ed = dict(e)
        raw = ed.get("summary", "")
        if raw is None or raw == "":
            ed["summary"] = ""
        else:
            s = raw if isinstance(raw, str) else str(raw)
            eid = ed.get("evidence_id")
            eid_str = eid if isinstance(eid, str) and eid else "unknown"
            ed["summary"] = _safe_prompt_text(s, ref=f"evidence:{eid_str}")
        safe_evidence.append(ed)
    evidence_out = safe_evidence
    safe_journals: list[object] = []
    for j in ctx.journal_excerpts:
        if not isinstance(j, dict):
            safe_journals.append(j)
            continue
        jd = dict(j)
        raw = jd.get("excerpt", "")
        if raw is None or raw == "":
            jd["excerpt"] = ""
        else:
            s = raw if isinstance(raw, str) else str(raw)
            jid = jd.get("journal")
            jid_str = jid if isinstance(jid, str) and jid else "unknown"
            jd["excerpt"] = _safe_prompt_text(s, ref=f"journal:{jid_str}")
        safe_journals.append(jd)
    journals_out = safe_journals
    return "\n".join(
        [
            f"thesis_id: {thesis_id}",
            f"trigger_id: {trigger.trigger_id}",
            f"run_id: {run_id}",
            f"TRIGGER DATA CUTOFF: {data_cutoff}",
            f"trigger summary: {summary_line}",
            f"trigger canonical refs: {refs[:500]}",
            "The supplied trigger and stored-evidence packet is bounded by the trigger data cutoff. You may perform additional live research using currently available tools. Preserve the real timing/provenance of anything newly found.",
            "CURRENT THESIS STATE:",
            json.dumps(packet, sort_keys=True),
            "TRIGGER/EVIDENCE AVAILABLE TO THIS MONITOR TICK:",
            json.dumps({"trigger": trig_out, "evidence": evidence_out}, sort_keys=True),
            "CURRENT PRIOR JOURNAL CONTEXT:",
            json.dumps(journals_out, sort_keys=True),
            "Only trigger and evidence inputs are bounded by the trigger data cutoff; thesis, state, watch, questions, memory, and prior journals are current.",
            "Record material findings, supporting and counterevidence, with the thesis_journal tool.",
            "Before completing, write a material thesis_journal entry using"
            f" trigger_id '{trigger.trigger_id}' and run_id '{run_id}'.",
            "Do not copy the trigger data cutoff into journal known_at; omit known_at unless it independently represents when the journal information became known.",
        ]
    )


def _fail(run_id: str, exc: Exception) -> None:
    try:
        from app.storage.runs import finalize_failed_run

        finalize_failed_run(run_id, error_type=type(exc).__name__, error_message=str(exc))
    except Exception:
        pass


def run_trigger(
    repository: ThesisRepository,
    thesis_id: str,
    trigger_id: str,
    *,
    known_at: str | None = None,
) -> RunOutcome:
    """Launch normal Pi for one pending trigger; ack that trigger ID only.

    Failure (bad state, over-budget context, Pi launch/timeout/nonzero, or no
    durable trigger-linked journal) raises before acknowledgement, so the
    trigger stays pending and valid partial tool writes are retained for the
    retry.
    """
    known_at = known_at or _utcnow()
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    trigger = next(
        (t for t in repository.load_triggers(tid) if t.trigger_id == trigger_id), None
    )
    if trigger is None:
        raise KeyError(f"unknown trigger: {trigger_id!r}")
    if trigger.status != "pending":
        raise ValueError(f"<runner>: trigger {trigger_id!r} is {trigger.status}, not pending")
    # PIT/budget gate: raises before any Pi call when context is over budget.
    ctx = build_live_context(repository, tid, trigger, data_cutoff=known_at)
    root = getattr(repository, "root", None)
    data_root = root.parent if root is not None else get_data_root()
    rid = new_run_id()
    prompt = _build_prompt(thesis_id=tid, trigger=trigger, data_cutoff=known_at, ctx=ctx, run_id=rid)
    try:
        run_thesis_pi(thesis_id=tid, trigger_id=trigger.trigger_id,
                       prompt=prompt, data_root=data_root, run_id=rid)
        repository.load_triggers(tid)  # re-read: surface corrupt YAML instead of acking blind
        if not repository.has_journal_for_trigger(tid, trigger.trigger_id, run_id=rid):
            raise RuntimeError(
                f"<runner>: no durable journal for trigger {trigger.trigger_id!r} (thesis {tid!r});"
                f" Pi must write a material thesis_journal entry with trigger_id {trigger.trigger_id!r}"
                f" and run_id {rid!r} before the trigger can be acknowledged;"
                " leaving pending for retry"
            )
        repository.mark_trigger_processed(tid, trigger.trigger_id, rid)
    except Exception as exc:
        _fail(rid, exc)
        raise
    return RunOutcome(run_id=rid, thesis_id=tid, trigger_id=trigger.trigger_id, processed=True)
