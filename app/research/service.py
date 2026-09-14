"""Authoritative kernel service API: deterministic persistence/policy/PIT/evidence/jobs.

Pi/CLI/IPC call these functions; nothing here invokes a model, spawns a
subprocess, or synthesizes outcomes. stdlib + kernel modules only.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from . import jobs as _jobs
from . import session as _session
from .evidence import (
    Evidence,
    evidence_content_hash,
    evidence_to_dict,
    ingest_evidence,
)
from .models import JSONValue, default_policy, utcnow
from .repository import ResearchRepository, pending_next_action

if TYPE_CHECKING:
    from .agents.bearbot import BearAnalysis
    from .agents.bullbot import BullAnalysis
    from .agents.stockbot import StockbotAnalysis
    from .director import Wave1Result
    from .models import Job, ResearchSession

__all__ = [
    "ResearchNotFound",
    "authorize_and_consume_dispatch",
    "cancel_research",
    "complete_job",
    "create_research",
    "decide_wave2",
    "finalize_session",
    "freeze_session",
    "inspect_research",
    "list_research",
    "record_committee_analysis",
    "record_evidence",
    "resume_research",
    "retry_job",
    "run_research",
    "start_job",
]


class ResearchNotFound(KeyError):
    """Unknown session/job/evidence id. Subclasses KeyError for existing handlers."""


_TERMINAL_JOBS = frozenset({"completed", "failed", "cancelled", "timed_out"})


def _repo(repo: ResearchRepository | Path | str | None = None) -> ResearchRepository:
    if isinstance(repo, ResearchRepository):
        return repo
    if repo is None:
        return ResearchRepository()
    return ResearchRepository(path=repo)


def _require_session(repo: ResearchRepository, session_id: str):
    try:
        return repo.get_session(session_id)
    except KeyError:
        raise ResearchNotFound(f"unknown session_id: {session_id!r}") from None


def _require_job(repo: ResearchRepository, job_id: str):
    try:
        return repo.get_job(job_id)
    except KeyError:
        raise ResearchNotFound(f"unknown job_id: {job_id!r}") from None


def create_research(
    question: str,
    objective: str | None = None,
    *,
    as_of: str | None = None,
    policy: dict[str, JSONValue] | None = None,
    repo: ResearchRepository | Path | str | None = None,
) -> str:
    """Create a session plus its first source_agent job; returns session_id."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("create_research: 'question' must be a non-empty string")
    store = _repo(repo)
    new_session = _session.create_session(
        question.strip(),
        (objective or question).strip(),
        as_of=as_of,
        policy=policy if policy is not None else default_policy(),
    )
    updated, job = _jobs.create_job(new_session, [], job_type="source_agent", owner="service")
    running = _jobs.start_job(job)
    store.save_session_and_job(updated, running)
    return updated.session_id


def run_research(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Idempotent first-job ensure: returns the open/first job for a session."""
    store = _repo(repo)
    found = _require_session(store, session_id)
    existing = store.list_jobs(session_id)
    if existing:
        for job in existing:
            if job.status == "running":
                return job.to_dict()  # type: ignore[return-value]
        for job in existing:
            if job.status == "queued":
                running = _jobs.start_job(job)
                store.save_job(running)
                return running.to_dict()  # type: ignore[return-value]
        return existing[0].to_dict()  # type: ignore[return-value]
    updated, job = _jobs.create_job(found, [], job_type="source_agent", owner="service")
    running = _jobs.start_job(job)
    store.save_session_and_job(updated, running)
    return running.to_dict()  # type: ignore[return-value]


def resume_research(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Read-only resume snapshot; never duplicates jobs/evidence."""
    store = _repo(repo)
    try:
        state = store.resume(session_id)
    except KeyError:
        raise ResearchNotFound(f"unknown session_id: {session_id!r}") from None
    return {
        "session": state.session.to_dict(),
        "wave": state.wave,
        "budgets": dict(state.budgets),
        "open_job_ids": list(state.open_job_ids),
        "pending_next_action": state.pending_next_action,
    }


def inspect_research(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Session + jobs + deterministic next action (read-only)."""
    store = _repo(repo)
    found = _require_session(store, session_id)
    job_list = store.list_jobs(session_id)
    latest_freeze: dict[str, JSONValue] | None = None
    if found.freeze_ids:
        fid = found.freeze_ids[-1]
        try:
            latest_freeze = store.get_freeze(fid)
        except KeyError:
            raise ValueError(f"inspect: unknown freeze_id: {fid!r}") from None
    return {
        "session": found.to_dict(),
        "jobs": [j.to_dict() for j in job_list],
        "pending_next_action": pending_next_action(found, job_list),
        "latest_freeze": latest_freeze,
    }


def cancel_research(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Cancel a session; terminal sessions return current state (no-op)."""
    from .models import SessionStatus

    store = _repo(repo)
    found = _require_session(store, session_id)
    if found.status in _session.TERMINAL_STATUSES:
        return found.to_dict()  # type: ignore[return-value]
    try:
        out = _session.transition_session(found, SessionStatus.CANCELLED)
    except ValueError:
        return found.to_dict()  # type: ignore[return-value]
    store.save_session(out)
    return out.to_dict()  # type: ignore[return-value]


def start_job(
    session_id: str,
    type: str = "source_agent",
    source: str | None = None,
    parent: str | None = None,
    budget: dict[str, object] | None = None,
    *,
    repo: ResearchRepository | Path | str | None = None,
    owner: str = "pi",
    wave_id: int = 1,
    model: str | None = None,
) -> dict[str, JSONValue]:
    """Create a queued job for Pi to run, then mark it running. Returns the job."""
    store = _repo(repo)
    found = _require_session(store, session_id)
    existing = store.list_jobs(session_id)
    details: dict[str, object] = dict(budget or {})
    token_budget = details.get("token_budget")
    tool_budget = details.get("tool_budget")
    child_budget = details.get("child_budget")
    updated, job = _jobs.create_job(
        found,
        existing,
        job_type=type,
        owner=str(details.get("owner", owner)),
        wave_id=int(details.get("wave_id", wave_id)),  # type: ignore[arg-type]
        parent_job_id=parent,
        source_domain=source,
        model=model if model is not None else details.get("model"),  # type: ignore[arg-type]
        token_budget=token_budget if isinstance(token_budget, int) else None,
        tool_budget=tool_budget if isinstance(tool_budget, int) else None,
        child_budget=child_budget if isinstance(child_budget, int) else None,
    )
    store.save_session(updated)
    store.save_job(job)
    running = _jobs.start_job(job)
    store.save_job(running)
    return running.to_dict()  # type: ignore[return-value]


def complete_job(
    job_id: str,
    outcome: Mapping[str, object] | None = None,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Mark a Pi-run job completed; terminal jobs return current state (no-op)."""
    store = _repo(repo)
    job = _require_job(store, job_id)
    if job.status in _TERMINAL_JOBS:
        return job.to_dict()  # type: ignore[return-value]
    if job.status == "queued":
        job = _jobs.start_job(job)
        store.save_job(job)
    result = dict(outcome) if isinstance(outcome, Mapping) else {}
    try:
        done = _jobs.complete_job(job, result=result)
    except ValueError:
        return job.to_dict()  # type: ignore[return-value]
    store.save_job(done)
    return done.to_dict()  # type: ignore[return-value]


def _coerce_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        return datetime.fromisoformat(value.strip())
    return None


def record_evidence(
    session_id: str,
    job_id: str,
    item: Mapping[str, object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Validate (PIT/provenance/IDs) + persist one finding. Returns the record."""
    from .evidence import EvidenceLedger

    if not isinstance(item, Mapping):
        raise ValueError("record_evidence: 'item' must be a mapping")
    store = _repo(repo)
    found = _require_session(store, session_id)
    job = _require_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"record_evidence: job {job_id!r} belongs to {job.session_id!r}")
    if job.status != "running":
        raise ValueError(f"record_evidence: job {job_id!r} status is {job.status!r} (running required)")
    if job.job_type not in ("source_agent", "scout"):
        raise ValueError(f"record_evidence: job {job_id!r} job_type {job.job_type!r} (source_agent|scout required)")
    if found.status in _session.TERMINAL_STATUSES:
        raise ValueError(f"record_evidence: session {session_id!r} status is {found.status!r} (terminal)")
    data = dict(item)
    content_raw = data.get("content")
    claim = data.get("claim_text", data.get("claim", ""))
    if not isinstance(content_raw, str) or not content_raw.strip():
        if isinstance(claim, str) and claim.strip():
            content = claim.strip()
        else:
            content = json.dumps(dict(data), sort_keys=True, default=str)
    else:
        content = content_raw.strip()
    if not content:
        content = "{}"
    wave_raw = data.get("wave_id", job.wave_id)
    if isinstance(wave_raw, bool) or not isinstance(wave_raw, int):
        wave_raw = job.wave_id
    if wave_raw != job.wave_id:
        raise ValueError(f"record_evidence: item wave {wave_raw!r} != job wave {job.wave_id!r}")
    wave = job.wave_id
    # ponytail: current_wave stays 0 on the Pi path until wave 2; floor at 1 via freeze count.
    live_wave = max(found.current_wave, len(found.freeze_ids) + 1, 1)
    if job.wave_id != live_wave:
        raise ValueError(f"record_evidence: job wave {job.wave_id!r} != current wave {live_wave!r}")
    if f"{session_id}:{job.wave_id}:freeze" in found.freeze_ids:
        raise ValueError(f"record_evidence: wave {job.wave_id} already frozen for {session_id!r}")
    evidence_id_raw = data.get("evidence_id")
    evidence_id = (
        evidence_id_raw
        if isinstance(evidence_id_raw, str) and evidence_id_raw.strip()
        else f"{session_id}:ev:{uuid.uuid4().hex[:8]}"
    )
    source_name_raw = data.get("source_name", data.get("source", job.source_domain or "pi"))
    source_name = source_name_raw if isinstance(source_name_raw, str) and source_name_raw.strip() else "pi"
    record = Evidence(
        evidence_id=evidence_id,
        session_id=session_id,
        wave_id=wave,
        source_type=str(data.get("source_type", "pi")),
        source_name=source_name,
        subject=str(data.get("subject", found.query[:120])),
        claim_text=claim[:2000] if isinstance(claim, str) else content[:500],
        content=content,
        content_hash=evidence_content_hash(content),
        retrieved_at=_coerce_dt(data.get("retrieved_at")) or utcnow(),
        source_uri=data.get("source_uri") if isinstance(data.get("source_uri"), str) else None,
        source_record_id=data.get("source_record_id")
        if isinstance(data.get("source_record_id"), str)
        else None,
        published_at=_coerce_dt(data.get("published_at")),
        known_at=_coerce_dt(data.get("known_at")),
        effective_at=_coerce_dt(data.get("effective_at")),
        job_id=job_id,
        agent_id=str(data.get("agent_id", "pi")),
        supports=tuple(s for s in data.get("supports", ()) if isinstance(s, str))  # type: ignore[union-attr]
        if isinstance(data.get("supports", ()), (list, tuple))
        else (),
        contradicts=tuple(s for s in data.get("contradicts", ()) if isinstance(s, str))  # type: ignore[union-attr]
        if isinstance(data.get("contradicts", ()), (list, tuple))
        else (),
        confidence=float(data["confidence"]) if isinstance(data.get("confidence"), (int, float)) else None,
        quality=data.get("quality") if isinstance(data.get("quality"), str) else None,
        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},  # type: ignore[arg-type]
        superseded_by=data.get("superseded_by") if isinstance(data.get("superseded_by"), str) else None,
    )
    ledger = EvidenceLedger()
    for existing in store.list_evidence(session_id):
        try:
            from .evidence import evidence_from_dict

            ledger.append(evidence_from_dict(existing))
        except Exception:
            continue
    ingest_evidence(ledger, record, as_of=found.as_of)
    stored = evidence_to_dict(record)
    store.save_evidence(stored)
    if record.evidence_id not in found.evidence_ids:
        store.save_session(
            replace(
                found,
                evidence_ids=[*found.evidence_ids, record.evidence_id],
                updated_at=utcnow(),
            )
        )
    return stored

def list_research(
    limit: int = 20,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> list[dict[str, object]]:
    """Newest-first session summaries (read-only; empty when no DB yet)."""
    return _repo(repo).list_sessions(limit=limit)


def retry_job(
    job_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
    owner: str = "cli-retry",
) -> dict[str, JSONValue]:
    """Enqueue a replacement for a failed/cancelled/timed-out job; else no-op."""
    store = _repo(repo)
    job = _require_job(store, job_id)
    if job.status not in ("failed", "cancelled", "timed_out"):
        return job.to_dict()
    found = _require_session(store, job.session_id)
    existing = store.list_jobs(job.session_id)
    updated, replacement = _jobs.create_job(
        found,
        existing,
        job_type=job.job_type,
        owner=owner,
        wave_id=job.wave_id,
        parent_job_id=job.parent_job_id,
        source_domain=job.source_domain,
        model=job.model,
        token_budget=job.token_budget,
        tool_budget=job.tool_budget,
        child_budget=job.child_budget,
    )
    store.save_session(updated)
    store.save_job(replacement)
    return replacement.to_dict()

def _wave1_state(
    store: ResearchRepository,
    found: ResearchSession,
) -> tuple[Wave1Result, dict[str, object]]:
    """Rebuild the trio Wave1Result from the latest freeze's committee-run jobs."""
    from .agents import parse_committee_output
    from .agents.bearbot import BearAnalysis
    from .agents.bullbot import BullAnalysis
    from .agents.stockbot import StockbotAnalysis
    from .director import Wave1Result
    from .synthesis.committee import compute_disagreement

    sid = found.session_id
    if not found.freeze_ids:
        raise ValueError(f"committee: session {sid!r} has no freeze")
    fid = found.freeze_ids[-1]
    try:
        frozen = store.get_freeze(fid)
    except KeyError:
        raise ValueError(f"committee: unknown freeze_id: {fid!r}") from None
    raw_ids = frozen.get("evidence_ids")
    ids = [e for e in raw_ids if isinstance(e, str)] if isinstance(raw_ids, list) else []
    wave_raw = frozen.get("wave_id")
    wave = wave_raw if isinstance(wave_raw, int) and not isinstance(wave_raw, bool) and wave_raw >= 1 else 1
    as_of = found.as_of.isoformat() if isinstance(found.as_of, datetime) else "unbounded"
    run_jobs: list[str] = []
    for entry in found.committee_runs:
        if isinstance(entry, dict) and entry.get("freeze_id") == fid:
            got = entry.get("jobs", [])
            if isinstance(got, list):
                run_jobs.extend(j for j in got if isinstance(j, str))
    by_role: dict[str, tuple[Job, dict[str, JSONValue]]] = {}
    for jid in run_jobs:
        try:
            job = store.get_job(jid)
        except KeyError:
            continue
        res = job.result
        if job.status != "completed" or not isinstance(res, dict):
            continue
        if not isinstance(res.get("claims"), list):
            continue
        by_role.setdefault(job.job_type, (job, res))
    stock: StockbotAnalysis | None = None
    bull: BullAnalysis | None = None
    bear: BearAnalysis | None = None
    for role in ("stockbot", "bullbot", "bearbot"):
        hit = by_role.get(role)
        if hit is None:
            continue
        _, res = hit
        claims, follow_ups = parse_committee_output(json.dumps(dict(res)), frozen=ids, agent=role)
        prose = "\n".join(c.text for c in claims).strip() or "No grounded claims in freeze."
        raw_unks = res.get("unknowns", [])
        unknowns = [u for u in raw_unks if isinstance(u, str)] if isinstance(raw_unks, list) else []
        raw_chg = res.get("what_would_change", [])
        changes = [u for u in raw_chg if isinstance(u, str)] if isinstance(raw_chg, list) else []
        if role == "stockbot":
            stock = StockbotAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=found.query, answer=prose, base_case=prose, unknowns=unknowns, what_would_change=changes, claims=claims, research_requests=follow_ups)
        elif role == "bullbot":
            bull = BullAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=found.query, stance="bullish", bull_case=prose, unknowns=unknowns, what_would_change=changes, claims=claims, research_requests=follow_ups)
        else:
            bear = BearAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=found.query, stance="bearish", bear_case=prose, unknowns=unknowns, what_would_change=changes, claims=claims, research_requests=follow_ups)
    disagreement = compute_disagreement(stock, bull, bear) if stock is not None and bull is not None and bear is not None else None
    wave1 = Wave1Result(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), stock=stock, bull=bull, bear=bear, disagreement=disagreement)
    return wave1, {"freeze_id": fid, "evidence_ids": ids, "wave_id": wave, "as_of": as_of}


def freeze_session(
    session_id: str,
    wave_id: int = 1,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Freeze wave evidence (PIT-checked) and move RESEARCHING -> FREEZING."""
    from . import freeze as _freeze
    from .evidence import evidence_from_dict
    from .models import SessionStatus
    if isinstance(wave_id, bool) or not isinstance(wave_id, int) or wave_id < 1:
        raise ValueError(f"freeze_session: 'wave_id' must be an int >= 1, got {wave_id!r}")
    store = _repo(repo)
    found = _require_session(store, session_id)
    raw_section: object = found.policy.get("research", {})
    research: dict[str, object] = raw_section if isinstance(raw_section, dict) else {}
    max_waves = research.get("max_waves", 2)
    if isinstance(max_waves, int) and not isinstance(max_waves, bool) and wave_id > max_waves:
        raise ValueError(f"freeze_session: wave {wave_id} exceeds max_waves {max_waves}")
    # Pi bootstrap mirrors runner C->P->R; service.create_research leaves CREATED.
    if found.status == SessionStatus.CREATED.value:
        found = _session.transition_session(found, SessionStatus.PLANNING)
        store.save_session(found)
    if found.status == SessionStatus.PLANNING.value:
        found = _session.transition_session(found, SessionStatus.RESEARCHING)
        store.save_session(found)
    if wave_id >= 2 and found.status == SessionStatus.TARGETED_RESEARCH.value:
        pass  # T->FREEZING via the transition below (runner R|T->F mirror).
    elif found.status != SessionStatus.RESEARCHING.value:
        raise ValueError(f"freeze_session: session {session_id!r} status is {found.status!r} (RESEARCHING required)")
    recs = [evidence_from_dict(record) for record in store.list_evidence(session_id)]
    wave_recs = [e for e in recs if e.wave_id <= wave_id]
    if not wave_recs:
        raise ValueError(f"freeze_session: session {session_id!r} wave {wave_id} has no evidence")
    open_src = [j.job_id for j in store.list_jobs(session_id)
                if j.wave_id == wave_id and j.job_type in ("source_agent", "scout")
                and j.status in ("queued", "running")]
    if open_src:
        raise ValueError(f"freeze_session: {len(open_src)} source jobs still open for wave {wave_id}: {open_src}")
    fid = f"{session_id}:{wave_id}:freeze"
    frozen = _freeze.create_freeze(freeze_id=fid, session_id=session_id, wave_id=wave_id, records=wave_recs, as_of=found.as_of)
    _freeze.verify_freeze(frozen, wave_recs)
    try:
        store.save_freeze(_freeze.freeze_to_dict(frozen))
    except ValueError:
        pass
    out = _session.transition_session(found, SessionStatus.FREEZING)
    if fid not in out.freeze_ids:
        out = replace(out, freeze_ids=[*out.freeze_ids, fid], updated_at=utcnow())
    store.save_session(out)
    return _freeze.freeze_to_dict(frozen)


def record_committee_analysis(
    session_id: str,
    job_id: str,
    role: str,
    analysis: dict[str, object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Validate one trio analysis against the freeze, persist it, complete the job."""
    from .agents import parse_committee_output
    from .models import SessionStatus, validate_json_value

    if role not in ("stockbot", "bullbot", "bearbot"):
        raise ValueError(f"record_committee_analysis: role must be stockbot|bullbot|bearbot, got {role!r}")
    if not isinstance(analysis, Mapping):
        raise ValueError(f"record_committee_analysis: 'analysis' must be a mapping, got {type(analysis).__name__}")
    store = _repo(repo)
    found = _require_session(store, session_id)
    job = _require_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"record_committee_analysis: job {job_id!r} belongs to {job.session_id!r}")
    if job.status != "running":
        raise ValueError(f"record_committee_analysis: job {job_id!r} status is {job.status!r} (running required)")
    if job.job_type != role:
        raise ValueError(f"record_committee_analysis: job {job_id!r} job_type {job.job_type!r} != role {role!r}")
    if not found.freeze_ids:
        raise ValueError(f"record_committee_analysis: session {session_id!r} has no freeze")
    fid = found.freeze_ids[-1]
    try:
        frozen = store.get_freeze(fid)
    except KeyError:
        raise ValueError(f"record_committee_analysis: unknown freeze_id: {fid!r}") from None
    raw_ids = frozen.get("evidence_ids")
    freeze_ids = [e for e in raw_ids if isinstance(e, str)] if isinstance(raw_ids, list) else []
    frozen_wave = frozen.get("wave_id")
    if isinstance(frozen_wave, int) and not isinstance(frozen_wave, bool) and job.wave_id != frozen_wave:
        raise ValueError(f"record_committee_analysis: job wave {job.wave_id!r} != freeze wave {frozen_wave!r}")
    try:
        envelope = json.dumps(dict(analysis))
    except TypeError as exc:
        raise ValueError(f"record_committee_analysis: 'analysis' must be JSON-able: {exc}") from exc
    parse_committee_output(envelope, frozen=freeze_ids, agent=role)
    done = _jobs.complete_job(job, result=dict(analysis))
    store.save_job(done)
    cur = found
    if cur.status == SessionStatus.FREEZING.value:
        cur = _session.transition_session(cur, SessionStatus.ANALYZING)
    runs: list[JSONValue] = list(cur.committee_runs)
    for i, entry in enumerate(runs):
        if isinstance(entry, dict) and entry.get("freeze_id") == fid:
            got = entry.get("jobs", [])
            known: list[str] = [j for j in got if isinstance(j, str)] if isinstance(got, list) else []
            if job_id not in known:
                known.append(job_id)
            merged: dict[str, JSONValue] = {
                "freeze_id": fid,
                "wave_id": entry.get("wave_id", job.wave_id),
                "jobs": validate_json_value(known, "<service>"),
            }
            runs[i] = merged
            break
    else:
        fresh: dict[str, JSONValue] = {
            "freeze_id": fid,
            "wave_id": job.wave_id,
            "jobs": validate_json_value([job_id], "<service>"),
        }
        runs.append(fresh)
    cur = replace(cur, committee_runs=runs, updated_at=utcnow())
    store.save_session(cur)
    return done.to_dict()


def decide_wave2(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Gate one targeted wave; persists the stop reason either way."""
    from .director import DirectorBudgets, DirectorDeps, Wave1Result, decide_wave2 as _decide

    store = _repo(repo)
    found = _require_session(store, session_id)
    jobs = store.list_jobs(session_id)
    wave1: Wave1Result | None = None
    try:
        wave1, _ = _wave1_state(store, found)
    except ValueError:
        pass
    if wave1 is None:
        wave1 = Wave1Result(session_id=session_id, wave_id=1, freeze_id="", evidence_ids=[], stock=None, bull=None, bear=None, disagreement=None)
        waves_used = 1
    else:
        waves_used = max(1, len(found.freeze_ids))
    raw_used = found.budget.get("tool_calls_used")
    tool_used = raw_used if isinstance(raw_used, int) and not isinstance(raw_used, bool) and raw_used >= 0 else 0
    elapsed = (utcnow() - found.created_at).total_seconds() if isinstance(found.created_at, datetime) else 0.0

    def _record_stop(sid: str, reason: str) -> None:
        from .journal import append_event, hydrate

        hydrate(sid, store.list_events(sid))
        store.save_event(append_event(sid, "wave.stopped", "service", "service", {"reason": reason}))

    def _no_create_session(_question: str, _as_of: str) -> str:
        raise AssertionError("decide_wave2: create_session is not sequenced in service")

    def _no_fetch(_sid: str) -> list[str]:
        raise AssertionError("decide_wave2: fetch_wave_evidence is not sequenced in service")

    def _no_freeze(_sid: str) -> str:
        raise AssertionError("decide_wave2: create_freeze is not sequenced in service")

    def _no_committee(_sid: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        raise AssertionError("decide_wave2: run_committee is not sequenced in service")

    deps = DirectorDeps(
        create_session=_no_create_session,
        fetch_wave_evidence=_no_fetch,
        create_freeze=_no_freeze,
        run_committee=_no_committee,
        record_stop=_record_stop,
    )
    decision = _decide(wave1, deps=deps, budgets=DirectorBudgets(), waves_used=waves_used, jobs_used=len(jobs), tool_calls_used=tool_used, elapsed_s=elapsed)
    if decision.authorized:
        from .models import SessionStatus

        cur = store.get_session(session_id)
        if cur.status == SessionStatus.ANALYZING.value:
            cur = _session.transition_session(cur, SessionStatus.TARGETED_RESEARCH)
            cur = replace(
                cur,
                current_wave=2,
                targeted_question=decision.targeted_question or None,
                targeted_domain=decision.targeted_domain or None,
                updated_at=utcnow(),
            )
            store.save_session(cur)
        from .journal import append_event, hydrate as _hydrate

        _hydrate(session_id, store.list_events(session_id))
        store.save_event(append_event(session_id, "wave.authorized", "service", "service", {
            "question": decision.targeted_question, "domain": decision.targeted_domain,
        }))
    else:
        from .models import SessionStatus as _SS
        cur = store.get_session(session_id)
        if cur.status == _SS.ANALYZING.value:
            store.save_session(_session.transition_session(cur, _SS.SYNTHESIZING))
    return {
        "authorized": decision.authorized,
        "stop_reason": decision.stop_reason,
        "reason_detail": decision.reason_detail,
        "targeted_question": decision.targeted_question,
        "targeted_domain": decision.targeted_domain,
    }


def finalize_session(
    session_id: str,
    answer: str,
    claims: list[object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Persist the trio-joined synthesis and move to COMPLETED."""
    from .agents import parse_grounded_claims
    from .models import SessionStatus, validate_json_value
    from .synthesis.final import synthesize_final

    store = _repo(repo)
    found = _require_session(store, session_id)
    if found.status in _session.TERMINAL_STATUSES:
        raise ValueError(f"finalize_session: session {session_id!r} status is {found.status!r} (terminal)")
    wave1, meta = _wave1_state(store, found)
    fid = meta["freeze_id"]
    assert isinstance(fid, str)
    missing = [name for name, present in (("stockbot", wave1.stock), ("bullbot", wave1.bull), ("bearbot", wave1.bear)) if present is None]
    if missing:
        raise ValueError(f"finalize_session: missing committee analyses for {missing} on freeze {fid!r}")
    assert wave1.stock is not None and wave1.bull is not None and wave1.bear is not None
    disagreement = wave1.disagreement
    if disagreement is None:
        raise ValueError(f"finalize_session: missing disagreement on freeze {fid!r}")
    if not isinstance(claims, list):
        raise ValueError(f"finalize_session: 'claims' must be a list, got {type(claims).__name__}")
    if not claims:
        raise ValueError("finalize_session: 'claims' must be a non-empty grounded list")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("finalize_session: 'answer' must be a non-empty string")
    open_all = [j.job_id for j in store.list_jobs(session_id) if j.status in ("queued", "running")]
    if open_all:
        raise ValueError(f"finalize_session: {len(open_all)} jobs still open: {open_all}")
    narrowed: list[dict[str, object]] = []
    for item in claims:
        if not isinstance(item, Mapping):
            raise ValueError(f"finalize_session: each claim must be a mapping, got {type(item).__name__}")
        refs = item.get("evidence_ids", [])
        narrowed.append({"text": item.get("text", item.get("claim_text", item.get("claim", ""))), "evidence_ids": [e for e in refs] if isinstance(refs, (list, tuple)) else []})
    try:
        envelope = json.dumps(narrowed)
    except TypeError as exc:
        raise ValueError(f"finalize_session: 'claims' must be JSON-able: {exc}") from exc
    ids_raw = meta["evidence_ids"]
    assert isinstance(ids_raw, list)
    grounded = parse_grounded_claims(envelope, frozen=[e for e in ids_raw if isinstance(e, str)])
    wave_raw = meta["wave_id"]
    as_of_raw = meta["as_of"]
    assert isinstance(wave_raw, int) and isinstance(as_of_raw, str)
    synth = synthesize_final(found.query, session_id=session_id, wave_id=wave_raw, freeze_id=fid, as_of=as_of_raw, stock=wave1.stock, bull=wave1.bull, bear=wave1.bear, disagreement=disagreement, model=answer)
    if not synth.answer.strip():
        raise ValueError("finalize_session: synthesis produced an empty answer")
    claims_json: list[JSONValue] = []
    for claim in grounded:
        row: dict[str, JSONValue] = {"text": claim.text, "evidence_ids": validate_json_value(list(claim.evidence_ids), "<service>")}
        claims_json.append(row)
    final: dict[str, JSONValue] = {"answer": synth.answer, "freeze_id": fid, "claims": claims_json}
    cur = store.get_session(session_id)
    if cur.status == SessionStatus.ANALYZING.value:
        cur = _session.transition_session(cur, SessionStatus.SYNTHESIZING)
        store.save_session(cur)
    cur = replace(store.get_session(session_id), final_result=final, updated_at=utcnow())
    store.save_session(cur)
    if cur.status == SessionStatus.SYNTHESIZING.value:
        cur = _session.transition_session(cur, SessionStatus.COMPLETED)
        store.save_session(cur)
    return {"session_id": session_id, "freeze_id": fid, "status": cur.status}


def authorize_and_consume_dispatch(
    session_id: str,
    job_id: str,
    tool_name: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Authorize one tool dispatch, then atomically consume job + global budget slots."""
    from .stage import check_stage_tool, stage_for_session

    store = _repo(repo)
    found = _require_session(store, session_id)
    job = _require_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"dispatch: job {job_id!r} belongs to {job.session_id!r}")
    jobs = store.list_jobs(session_id)
    check_stage_tool(stage_for_session(found, jobs), tool_name)
    if job.source_domain is not None and not tool_name.startswith("research"):
        if job.source_domain.upper() == "SEC":
            from .agents.source_agent import is_sec_tool

            if not is_sec_tool(tool_name):
                raise ValueError(f"dispatch: tool {tool_name!r} outside SEC domain for job {job_id!r}")
    try:
        billed, spent = store.consume_dispatch_budget(session_id, job_id)
    except KeyError as exc:
        raise ResearchNotFound(exc.args[0] if exc.args else str(exc)) from None
    raw_used: object = billed.budget.get("tool_calls_used", 0)
    used = raw_used if isinstance(raw_used, int) and not isinstance(raw_used, bool) else 0
    return {"session_id": session_id, "job_id": job_id, "tool_name": tool_name,
            "tool_budget": spent.tool_budget, "tool_calls_used": used}
