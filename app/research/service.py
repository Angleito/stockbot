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
from .models import (
    JSONValue,
    default_policy,
    utcnow,
    validate_json_mapping,
    validate_json_value,
)
from .repository import ResearchRepository, pending_next_action

if TYPE_CHECKING:
    from .agents import GroundedClaim
    from .agents.bearbot import BearAnalysis
    from .agents.bullbot import BullAnalysis
    from .agents.stockbot import StockbotAnalysis
    from .director import Wave1Result, WaveDecision
    from .models import Job, ResearchSession
    from .synthesis.committee import CommitteeDisagreement

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
    "submit_source_result",
    "create_committee_jobs",
    "transition_job_completed",
    "research_events",
    "heartbeat_job",
    "job_diagnostics",
    "resume_research",
    "retry_job",
    "run_research",
    "start_job",
]


class ResearchNotFound(KeyError):
    """Unknown session/job/evidence id. Subclasses KeyError for existing handlers."""


def transition_job_completed(session_id: str, job_id: str, *, repo: ResearchRepository | Path | str | None = None) -> dict[str, object]:
    """Single authoritative wave/counter transition: job wave + session current_wave together."""
    store = _repo(repo)
    job = _require_job(store, job_id)
    found = _require_session(store, session_id)
    if job.session_id != found.session_id:
        raise ValueError("transition_job_completed: session/job mismatch")
    wave = job.wave_id if isinstance(job.wave_id, int) and job.wave_id >= 1 else 1
    cur = store.get_session(session_id)
    if wave > cur.current_wave:
        from dataclasses import replace as _replace
        store.save_session(_replace(cur, current_wave=wave, updated_at=utcnow()))
        cur = store.get_session(session_id)
    return {"session_id": session_id, "job_id": job_id, "current_wave": cur.current_wave, "job_wave": wave}


def research_events(session_id: str, job_id: str | None = None, limit: int = 100, cursor: int = 0,
                    *, repo: ResearchRepository | Path | str | None = None) -> dict[str, object]:
    """Bounded structured events from the journal (read-only)."""
    store = _repo(repo)
    _require_session(store, session_id)
    limit = max(1, min(200, limit if isinstance(limit, int) else 100))
    cursor = max(0, cursor if isinstance(cursor, int) else 0)
    events = store.list_events(session_id)
    if job_id:
        events = [e for e in events if isinstance(e.payload, dict) and e.payload.get("job_id") == job_id]
    page = events[cursor:cursor + limit]
    return {"session_id": session_id, "job_id": job_id, "cursor": cursor, "limit": limit,
            "events": [{"ts": e.timestamp.isoformat(), "kind": e.event_type, "job_id": (e.payload.get("job_id") if isinstance(e.payload, dict) else None),
                        "detail": dict(e.payload)} for e in page]}


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


def _emit(store: ResearchRepository, session_id: str, event_type: str, payload: dict[str, object]) -> None:
    """Append one journal event (best-effort; never breaks the mutation)."""
    try:
        from .journal import append_event, hydrate
        hydrate(session_id, store.list_events(session_id))
        store.save_event(append_event(session_id, event_type, "service", "service", dict(payload)))
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
        pass


def _enforce_live_job(store: ResearchRepository, job_id: str):
    """Fail-closed deadline guard shared by every governed mutation."""
    from dataclasses import replace as _replace

    from .models import Failure, normalize_time

    job = _require_job(store, job_id)
    if job.deadline is not None:
        deadline = normalize_time(job.deadline)
        if utcnow() > deadline:
            timed = _replace(job, status="timed_out", completed_at=utcnow(),
                             failure=Failure(category="timeout", message="deadline expired"))
            store.save_job(timed)
            raise ValueError(f"ERR_JOB_TIMED_OUT: job {job_id!r} deadline expired")
    if job.status == "timed_out":
        raise ValueError(f"ERR_JOB_TIMED_OUT: job {job_id!r} timed out")
    return job


def heartbeat_job(job_id: str, *, repo: ResearchRepository | Path | str | None = None) -> dict[str, JSONValue]:
    """Refresh last_heartbeat_at; read path surfaces staleness + remaining."""
    from dataclasses import replace as _replace
    store = _repo(repo)
    job = _require_job(store, job_id)
    beat = _replace(job, last_heartbeat_at=utcnow())
    store.save_job(beat)
    return beat.to_dict()


def job_diagnostics(session_id: str, job_id: str, *, repo: ResearchRepository | Path | str | None = None) -> dict[str, JSONValue]:
    """Non-empty diagnostics: staleness, deadline remaining, evidence count."""
    from .models import HEARTBEAT_STALE_S
    store = _repo(repo)
    job = _require_job(store, job_id)
    now = utcnow()
    hb = job.last_heartbeat_at or job.started_at or job.created_at
    staleness = max(0.0, (now - hb).total_seconds()) if hb is not None else 0.0
    remaining = (job.deadline - now).total_seconds() if job.deadline is not None else None
    count = sum(1 for r in store.list_evidence(session_id) if r.get("job_id") == job_id)
    return {"job_id": job_id, "stale": staleness > HEARTBEAT_STALE_S, "staleness_s": staleness,
            "deadline_remaining_s": remaining, "evidence_count": count,
            "last_event": job.status}


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
    updated, job = _jobs.create_job(new_session, [], job_type="source_agent", owner="pi", source_domain=None)
    running = _jobs.start_job(job)
    store.save_session_and_job(updated, running)
    _emit(store, updated.session_id, "job.started", {"job_id": running.job_id})
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
                return job.to_dict()
        for job in existing:
            if job.status == "queued":
                running = _jobs.start_job(job)
                store.save_job(running)
                return running.to_dict()
        return existing[0].to_dict()
    updated, job = _jobs.create_job(found, [], job_type="source_agent", owner="pi", source_domain=None)
    running = _jobs.start_job(job)
    store.save_session_and_job(updated, running)
    return running.to_dict()


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
        return found.to_dict()
    try:
        out = _session.transition_session(found, SessionStatus.CANCELLED)
    except ValueError:
        return found.to_dict()
    store.save_session(out)
    return out.to_dict()


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
    wave_raw: object = details.get("wave_id", wave_id)
    if isinstance(wave_raw, bool) or not isinstance(wave_raw, int):
        raise ValueError(f"start_job: 'wave_id' must be an int, got {wave_raw!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    wave_id_arg: int = wave_raw
    model_raw: object = details.get("model")
    model_arg: str | None = model if model is not None else (model_raw if isinstance(model_raw, str) else None)
    updated, job = _jobs.create_job(
        found,
        existing,
        job_type=type,
        owner=str(details.get("owner", owner)),
        wave_id=wave_id_arg,
        parent_job_id=parent,
        source_domain=source,
        model=model_arg,
        token_budget=token_budget if isinstance(token_budget, int) else None,
        tool_budget=tool_budget if isinstance(tool_budget, int) else None,
        child_budget=child_budget if isinstance(child_budget, int) else None,
    )
    store.save_session(updated)
    store.save_job(job)
    running = _jobs.start_job(job)
    store.save_job(running)
    return running.to_dict()


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
        return job.to_dict()
    if job.status == "queued":
        job = _jobs.start_job(job)
        store.save_job(job)
    result = dict(outcome) if isinstance(outcome, Mapping) else {}
    try:
        done = _jobs.complete_job(job, result=result)
    except ValueError:
        return job.to_dict()
    store.save_job(done)
    transition_job_completed(job.session_id, job.job_id, repo=store)
    return store.get_job(job.job_id).to_dict()


def _coerce_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _evidence_content(data: Mapping[str, object]) -> tuple[str, object]:
    """Resolve content with claim/JSON fallbacks. Returns (content, claim)."""
    content_raw = data.get("content")
    claim = data.get("claim_text", data.get("claim", ""))
    if isinstance(content_raw, str) and content_raw.strip():
        return content_raw.strip(), claim
    if isinstance(claim, str) and claim.strip():
        return claim.strip(), claim
    dumped = json.dumps(dict(data), sort_keys=True, default=str)
    return dumped if dumped else "{}", claim


def _evidence_wave(data: Mapping[str, object], job_wave: int) -> int:
    """Coerce item wave to job wave; non-int waves fall back to the job wave."""
    wave_raw = data.get("wave_id", job_wave)
    if isinstance(wave_raw, bool) or not isinstance(wave_raw, int):
        return job_wave
    if wave_raw != job_wave:
        raise ValueError(f"record_evidence: item wave {wave_raw!r} != job wave {job_wave!r}")
    return job_wave


def _norm_lower(value: object) -> str:
    """Lowercase-normalize one identity-key component."""
    return str(value or "").strip().lower()


def _evidence_identity(data: Mapping[str, object], ev_type: str, claim: object,
                       subject: str) -> str:
    """Identity key for dedupe: search ids vs filing refs."""
    tail: object = data.get("observation_type", claim[:80] if isinstance(claim, str) else "")
    if ev_type == "search_coverage":
        return "|".join(("sec", _norm_lower(data.get("operation", "search")), _norm_lower(data.get("search_id")),
                         _norm_lower(data.get("query")), _norm_lower(subject)))
    return "|".join(("sec", _norm_lower(data.get("operation", "filing")), _norm_lower(data.get("source_record_id")),
                     _norm_lower(subject), _norm_lower(tail)))


def _require_positive_provenance(ev_type: str, data: Mapping[str, object]) -> None:
    """Positives need a document hit: accession + passage/section/fact + known_at."""
    if ev_type != "filing_observation":
        return
    claim = data.get("claim_text", data.get("claim", ""))
    text = claim if isinstance(claim, str) else ""
    if _is_negative_claim(text):
        return
    accession = data.get("source_record_id") or data.get("accession_no") or data.get("accession")
    explicit = (data.get("matching_passage") or data.get("passage") or data.get("fact")
                or data.get("section") or data.get("document"))
    if isinstance(explicit, str) and explicit.strip():
        passage: object = explicit
    elif data.get("search_id") is not None or data.get("query") is not None:
        raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (search hit without passage is not evidence; cite document/section/passage)")
    else:
        content = data.get("content")
        claim_text = data.get("claim_text", data.get("claim", ""))
        passage = ((content if isinstance(content, str) and content.strip() else None)
                   or (claim_text if isinstance(claim_text, str) and claim_text.strip() else None))
        if isinstance(accession, str) and accession.strip() and passage is None:
            passage = f"accession {accession}"
    if not isinstance(accession, str) or not accession.strip():
        raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (positive claim needs accession)")


def _require_search_coverage(data: Mapping[str, object]) -> None:
    """search_coverage items must name the query and search id."""
    for req in ("query", "search_id"):
        raw = data.get(req)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"record_evidence: search_coverage requires '{req}'")


def _is_negative_claim(text: str) -> bool:
    """Negative phrasing: explicit absence framing, never a bare mention."""
    lowered = text.strip().lower()
    return lowered.startswith(("no ", "not found", "none ", "absent", "does not exist", "no evidence of", "no such "))
    # ponytail: prefix list covers absence framing; bare "unknown ticker" style text stays positive.


NEGATIVE_COVERAGE_KEYS = ("forms", "dates", "partitions", "docs", "gaps", "complete")


def _negative_coverage(data: Mapping[str, object]) -> dict[str, object]:
    """Negative-evidence coverage envelope ({forms,dates,partitions,docs,gaps,complete})."""
    raw = data.get("coverage", data.get("search_coverage"))
    if not isinstance(raw, Mapping):
        raise ValueError("record_evidence: ERR_COVERAGE_REQUIRED (negative claim needs coverage {forms,dates,partitions,docs,gaps,complete})")
    cov = dict(raw)
    missing = [k for k in NEGATIVE_COVERAGE_KEYS if k not in cov]
    if missing:
        raise ValueError(f"record_evidence: ERR_COVERAGE_REQUIRED (negative coverage missing {missing})")
    for key in ("forms", "dates", "partitions", "docs", "gaps"):
        if not isinstance(cov.get(key), list):
            raise ValueError(f"record_evidence: ERR_COVERAGE_REQUIRED (negative coverage[{key!r}] must be a list)")
    if not isinstance(cov.get("complete"), bool):
        raise ValueError("record_evidence: ERR_COVERAGE_REQUIRED (negative coverage['complete'] must be a bool)")
    return cov


def _require_negative_scope(text: str, cov: dict[str, object]) -> None:
    """Non-exhaustive no-hit stays scoped: universal absence needs complete:true."""
    if cov.get("complete") is True:
        return
    lowered = text.strip().lower()
    universal = lowered.startswith(("no ", "none ", "absent", "does not exist", "no evidence of", "no such "))
    if universal:
        raise ValueError("record_evidence: ERR_COVERAGE_REQUIRED (universal negative needs coverage complete:true; else scope the claim)")
def _require_negative_provenance(ev_type: str, data: Mapping[str, object]) -> None:
    """Negatives cite the executed SearchRun; an arbitrary filing never proves absence."""
    if ev_type != "filing_observation":
        return
    claim = data.get("claim_text", data.get("claim", ""))
    text = claim if isinstance(claim, str) else ""
    if not _is_negative_claim(text):
        return
    search_id = data.get("search_id")
    query = data.get("query")
    if not isinstance(search_id, str) or not search_id.strip():
        raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (negative claim needs search_id of the executed SearchRun)")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (negative claim needs query of the executed SearchRun)")
    for key in ("accession_no", "accession"):
        ref = data.get(key)
        if isinstance(ref, str) and ref.strip():
            raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (negative cites SearchRun ID only; arbitrary filing proves no absence)")
    cov = _negative_coverage(data)
    _require_negative_scope(text, cov)


def _require_filing_provenance(ev_type: str, data: Mapping[str, object], subject: str) -> None:
    """Reject an OpenAI claim filed against an unrelated source record."""
    if ev_type != "filing_observation" or subject.strip().lower() != "openai":
        _require_negative_provenance(ev_type, data)
        return
    blob = f"{data.get('source_record_id') or ''} {data.get('source_uri') or ''}".lower()
    if "openai" not in blob:
        raise ValueError("record_evidence: ERR_PROVENANCE_MISMATCH (OpenAI claim off unrelated filing)")


def _live_evidence_job(store: ResearchRepository, session_id: str, job_id: str):
    """Load session + live running source/scout job, or raise the owning guard."""
    found = _require_session(store, session_id)
    job = _enforce_live_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"record_evidence: job {job_id!r} belongs to {job.session_id!r}")
    if job.status != "running":
        raise ValueError(f"record_evidence: job {job_id!r} status is {job.status!r} (running required)")
    if job.job_type not in ("source_agent", "scout"):
        raise ValueError(f"record_evidence: job {job_id!r} job_type {job.job_type!r} (source_agent|scout required)")
    if found.status in _session.TERMINAL_STATUSES:
        raise ValueError(f"record_evidence: session {session_id!r} status is {found.status!r} (terminal)")
    return found, job


def _evidence_id_name(data: Mapping[str, object], session_id: str, job: Job) -> tuple[str, str]:
    """Coerce evidence_id + source_name with job fallbacks."""
    evidence_id_raw = data.get("evidence_id")
    evidence_id = (
        evidence_id_raw
        if isinstance(evidence_id_raw, str) and evidence_id_raw.strip()
        else f"{session_id}:ev:{uuid.uuid4().hex[:8]}"
    )
    source_name_raw = data.get("source_name", data.get("source", job.source_domain or "pi"))
    source_name = source_name_raw if isinstance(source_name_raw, str) and source_name_raw.strip() else "pi"
    return evidence_id, source_name


def _str_tuple(raw: object) -> tuple[str, ...]:
    """Filter one supports/contradicts payload down to plain strings."""
    return tuple(s for s in raw if isinstance(s, str)) if isinstance(raw, (list, tuple)) else ()


def _evidence_ids_names(data: Mapping[str, object], session_id: str, job: Job) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
    """Coerce evidence_id/source_name/supports/contradicts with job fallbacks."""
    evidence_id, source_name = _evidence_id_name(data, session_id, job)
    return evidence_id, source_name, _str_tuple(data.get("supports", ())), _str_tuple(data.get("contradicts", ()))


def _build_evidence_record(data: Mapping[str, object], *, session_id: str, job_id: str, wave: int,
                           evidence_id: str, source_name: str, subject: str, claim: object,
                           content: str, supports: tuple[str, ...], contradicts: tuple[str, ...],
                           metadata: dict[str, JSONValue], identity_key: str, ev_type: str) -> Evidence:
    """Assemble the Evidence row from coerced fields (no I/O)."""
    source_uri_raw = data.get("source_uri")
    source_record_id_raw = data.get("source_record_id")
    confidence_raw = data.get("confidence")
    quality_raw = data.get("quality")
    superseded_by_raw = data.get("superseded_by")
    kind_raw: object = data.get("record_kind")
    meta: object = data.get("metadata")
    if kind_raw is None and isinstance(meta, dict):
        kind_raw = meta.get("record_kind")
    kind = kind_raw if isinstance(kind_raw, str) and kind_raw in ("discovery", "evidence") else ("discovery" if ev_type == "search_coverage" else "evidence")
    claim_text = claim[:2000] if isinstance(claim, str) and claim.strip() else (f"{ev_type} {evidence_id}" if kind == "discovery" else content[:500])
    return Evidence(
        evidence_id=evidence_id,
        session_id=session_id,
        wave_id=wave,
        source_type=str(data.get("source_type", "pi")),
        source_name=source_name,
        subject=subject,
        claim_text=claim_text,
        content=content,
        content_hash=evidence_content_hash(content),
        retrieved_at=_coerce_dt(data.get("retrieved_at")) or utcnow(),
        source_uri=source_uri_raw if isinstance(source_uri_raw, str) else None,
        source_record_id=source_record_id_raw
        if isinstance(source_record_id_raw, str)
        else None,
        published_at=_coerce_dt(data.get("published_at")),
        known_at=_coerce_dt(data.get("known_at")),
        effective_at=_coerce_dt(data.get("effective_at")),
        job_id=job_id,
        agent_id=str(data.get("agent_id", "pi")),
        supports=supports,
        contradicts=contradicts,
        confidence=float(confidence_raw) if isinstance(confidence_raw, (int, float)) else None,
        quality=quality_raw if isinstance(quality_raw, str) else None,
        metadata={**metadata, "identity_key": identity_key, "evidence_type": ev_type},
        superseded_by=superseded_by_raw if isinstance(superseded_by_raw, str) else None,
        record_kind=kind,
    )

def _persist_evidence_record(store: ResearchRepository, session_id: str, job_id: str,
                             found: ResearchSession, record: Evidence,
                             metadata: dict[str, JSONValue]) -> dict[str, JSONValue]:
    """PIT-gate + append the record, link it to the session, return the stored dict."""
    from .evidence import EvidenceLedger

    ledger = EvidenceLedger()
    for existing in store.list_evidence(session_id):
        try:
            from .evidence import evidence_from_dict

            ledger.append(evidence_from_dict(existing))
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            continue
    ingest_evidence(ledger, record, as_of=found.as_of)
    stored = evidence_to_dict(record)
    store.save_evidence(stored)
    _emit(store, session_id, "evidence.accepted", {"job_id": job_id, "evidence_id": record.evidence_id})
    stored = dict(stored)
    stored["metadata"] = {k: v for k, v in metadata.items()}
    if record.evidence_id not in found.evidence_ids:
        store.save_session(
            replace(
                found,
                evidence_ids=[*found.evidence_ids, record.evidence_id],
                updated_at=utcnow(),
            )
        )
    return stored
def _evidence_live_wave(found: ResearchSession, job: Job, session_id: str) -> None:
    """Enforce the job wave is the live wave and not already frozen."""
    # ponytail: current_wave stays 0 on the Pi path until wave 2; floor at 1 via freeze count.
    live_wave = max(found.current_wave, len(found.freeze_ids) + 1, 1)
    if job.wave_id != live_wave:
        raise ValueError(f"record_evidence: job wave {job.wave_id!r} != current wave {live_wave!r}")
    if f"{session_id}:{job.wave_id}:freeze" in found.freeze_ids:
        raise ValueError(f"record_evidence: wave {job.wave_id} already frozen for {session_id!r}")


def _evidence_typed(data: dict[str, object], found: ResearchSession) -> tuple[dict[str, JSONValue], str, str]:
    """Validate metadata + route the type gate; return (metadata, ev_type, subject)."""
    metadata: dict[str, JSONValue] = validate_json_mapping(data.get("metadata", {}), "record_evidence: 'metadata'")
    ev_type = str(data.get("type", "filing_observation"))
    if ev_type == "search_coverage":
        _require_search_coverage(data)
    subject = str(data.get("subject", found.query[:120]))
    _require_filing_provenance(ev_type, data, subject)
    _require_positive_provenance(ev_type, data)
    return metadata, ev_type, subject


def _evidence_dedupe_cap(prior: list[dict[str, JSONValue]], job_id: str, identity_key: str):
    """Return the duplicate row when the identity key repeats, else enforce the per-job cap."""
    for row in prior:
        if isinstance(row.get("metadata"), dict) and row["metadata"].get("identity_key") == identity_key:
            return {"evidence_id": row.get("evidence_id"), "accepted": False, "duplicate_of": row.get("evidence_id")}
    if sum(1 for r in prior if r.get("job_id") == job_id) >= 8:
        raise ValueError("record_evidence: ERR_EVIDENCE_CAP (max 8 per source job; submit result)")
    return None


def record_evidence(
    session_id: str,
    job_id: str,
    item: Mapping[str, object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Validate (PIT/provenance/IDs) + persist one finding. Returns the record."""
    if not isinstance(item, Mapping):
        raise ValueError("record_evidence: 'item' must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    store = _repo(repo)
    found, job = _live_evidence_job(store, session_id, job_id)
    data = dict(item)
    content, claim = _evidence_content(data)
    wave = _evidence_wave(data, job.wave_id)
    _evidence_live_wave(found, job, session_id)
    evidence_id, source_name, supports, contradicts = _evidence_ids_names(data, session_id, job)
    metadata, ev_type, subject = _evidence_typed(data, found)
    identity_key = _evidence_identity(data, ev_type, claim, subject)
    prior = store.list_evidence(session_id)
    dup = _evidence_dedupe_cap(prior, job_id, identity_key)
    if dup is not None:
        return dup
    record = _build_evidence_record(data, session_id=session_id, job_id=job_id, wave=wave,
                                    evidence_id=evidence_id, source_name=source_name, subject=subject,
                                    claim=claim, content=content, supports=supports,
                                    contradicts=contradicts, metadata=metadata,
                                    identity_key=identity_key, ev_type=ev_type)
    return _persist_evidence_record(store, session_id, job_id, found, record, metadata)


def _submit_coverage_ids(coverage: dict[str, object] | None,
                         evidence_ids: list[str] | None) -> tuple[dict[str, object], object, list[str]]:
    """Validate the coverage envelope + evidence list; return (cov, useful, ids)."""
    cov = dict(coverage or {})
    if "useful_for_question" not in cov:
        raise ValueError("submit_source_result: ERR_COVERAGE_REQUIRED (coverage.useful_for_question required: sufficient|insufficient)")
    useful = cov.get("useful_for_question")
    if useful not in ("sufficient", "insufficient"):
        raise ValueError("submit_source_result: coverage useful_for_question must be sufficient|insufficient")
    ids = list(evidence_ids or [])
    if not ids and useful != "insufficient":
        raise ValueError("submit_source_result: ERR_EMPTY_RESULT (no evidence with sufficient coverage)")
    return cov, useful, ids


def _submit_live_job(store: ResearchRepository, job_id: str):
    """Load the open running source job + session, or raise the owning guard."""
    job = _enforce_live_job(store, job_id)
    if job.status != "running":
        raise ValueError(f"submit_source_result: ERR_JOB_CLOSED (job {job_id!r} status is {job.status!r})")
    if job.job_type not in ("source_agent", "scout"):
        raise ValueError(f"submit_source_result: job {job_id!r} job_type {job.job_type!r} (source_agent|scout required)")
    found = _require_session(store, job.session_id)
    if found.status in _session.TERMINAL_STATUSES:
        raise ValueError(f"submit_source_result: session {found.session_id!r} status is {found.status!r} (terminal)")
    return job, found


def _submit_dossier(store: ResearchRepository, job: Job, found: ResearchSession, useful: object,
                    ids: list[str], unresolved: list[str] | None, cov: dict[str, object] | None = None) -> tuple[str, set[str]]:
    """Validate refs + persist the SEC dossier (idempotent); return (dossier_id, ledger_ids)."""
    from .dossiers import create_dossier, default_coverage
    from .dossiers.sec import dossier_to_dict

    ledger_ids = {str(r.get("evidence_id")) for r in store.list_evidence(job.session_id)}
    missing = [e for e in ids if e not in ledger_ids]
    if missing:
        raise ValueError(f"submit_source_result: ERR_EVIDENCE_NOT_FOUND {missing[:3]}")
    dossier_id = f"{job.session_id}:{job.wave_id}:sec"
    coverage = {**default_coverage(), "complete": useful == "sufficient"}
    for key in ("resolved", "partially_resolved", "unresolved", "source_limitations",
                "dates", "partitions", "docs", "gaps"):
        values = (cov or {}).get(key)
        if isinstance(values, list) and all(isinstance(v, str) for v in values):
            coverage[key] = list(values)
    dossier = create_dossier(
        dossier_id=dossier_id, session_id=job.session_id, wave_id=job.wave_id,
        subject=found.query[:120], coverage=coverage,
        findings=[{"text": "source result", "evidence_ids": ids}] if ids else [],
        open_questions=list(unresolved or []),
    )
    try:
        store.save_dossier(dossier_to_dict(dossier))
    except ValueError:
        pass
    cur = store.get_session(job.session_id)
    if dossier_id not in cur.dossier_ids:
        store.save_session(replace(cur, dossier_ids=[*cur.dossier_ids, dossier_id], updated_at=utcnow()))
    return dossier_id, ledger_ids


def submit_source_result(
    job_id: str,
    coverage: dict[str, object] | None = None,
    evidence_ids: list[str] | None = None,
    unresolved_questions: list[str] | None = None,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Complete one running source job with validated coverage; evidence stays mutation-only.

    Validates job open + deadline live, every evidence id exists in this session,
    then completes the job with a coverage/result payload. Returns
    {job_status: completed, dossier-ish refs}. Terminal reuse fails closed.
    """
    store = _repo(repo)
    job, found = _submit_live_job(store, job_id)
    cov, _, ids = _submit_coverage_ids(coverage, evidence_ids)
    dossier_id, _ = _submit_dossier(store, job, found,
                                    cov.get("useful_for_question"), ids, unresolved_questions, cov)
    done = _jobs.complete_job(job, result={"coverage": cov, "evidence_ids": ids})
    store.save_job(done)
    _emit(store, job.session_id, "job.completed", {"job_id": job.job_id})
    transition_job_completed(job.session_id, job.job_id, repo=store)
    out: dict[str, JSONValue] = {"job_status": done.status, "job_id": done.job_id, "dossier_id": dossier_id,
            "evidence_ids": validate_json_value(ids, "<submit>")}
    return out


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

def _freeze_context(store: ResearchRepository, found: ResearchSession) -> tuple[str, str, list[str], int, str]:
    """Resolve (sid, fid, evidence ids, wave, as_of) for the latest freeze."""
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
    return sid, fid, ids, wave, as_of


def _str_list(raw: object) -> list[str]:
    """Filter one unknowns/changes payload down to plain strings."""
    return [u for u in raw if isinstance(u, str)] if isinstance(raw, list) else []


def _freeze_run_jobs(found: ResearchSession, fid: str) -> list[str]:
    """Collect committee-run job ids recorded for this freeze."""
    run_jobs: list[str] = []
    for entry in found.committee_runs:
        if isinstance(entry, dict) and entry.get("freeze_id") == fid:
            got = entry.get("jobs", [])
            if isinstance(got, list):
                run_jobs.extend(j for j in got if isinstance(j, str))
    return run_jobs


def _committee_results_by_role(store: ResearchRepository, found: ResearchSession, fid: str) -> dict[str, dict[str, JSONValue]]:
    """Latest-freeze completed trio results keyed by role; skips dangling/open/uncited."""
    by_role: dict[str, dict[str, JSONValue]] = {}
    for jid in _freeze_run_jobs(found, fid):
        try:
            job = store.get_job(jid)
        except KeyError:
            continue
        res = job.result
        if job.status != "completed" or not isinstance(res, dict):
            continue
        if not isinstance(res.get("claims"), list):
            continue
        by_role.setdefault(job.job_type, res)
    return by_role


def _build_stock(role_res: dict[str, JSONValue], *, sid: str, wave: int,
                 fid: str, ids: list[str], as_of: str, question: str) -> StockbotAnalysis:
    """Parse the stockbot envelope into its typed analysis."""
    from .agents import parse_committee_output
    from .agents.stockbot import StockbotAnalysis

    claims, follow_ups = parse_committee_output(json.dumps(dict(role_res)), frozen=ids, agent="stockbot")
    prose = "\n".join(c.text for c in claims).strip() or "No grounded claims in freeze."
    return StockbotAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=question, answer=prose, base_case=prose, unknowns=_str_list(role_res.get("unknowns")), what_would_change=_str_list(role_res.get("what_would_change")), claims=claims, research_requests=follow_ups)


def _build_bull(role_res: dict[str, JSONValue], *, sid: str, wave: int,
                fid: str, ids: list[str], as_of: str, question: str) -> BullAnalysis:
    """Parse the bullbot envelope into its typed analysis."""
    from .agents import parse_committee_output
    from .agents.bullbot import BullAnalysis

    claims, follow_ups = parse_committee_output(json.dumps(dict(role_res)), frozen=ids, agent="bullbot")
    prose = "\n".join(c.text for c in claims).strip() or "No grounded claims in freeze."
    return BullAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=question, stance="bullish", bull_case=prose, unknowns=_str_list(role_res.get("unknowns")), what_would_change=_str_list(role_res.get("what_would_change")), claims=claims, research_requests=follow_ups)


def _build_bear(role_res: dict[str, JSONValue], *, sid: str, wave: int,
                fid: str, ids: list[str], as_of: str, question: str) -> BearAnalysis:
    """Parse the bearbot envelope into its typed analysis."""
    from .agents import parse_committee_output
    from .agents.bearbot import BearAnalysis

    claims, follow_ups = parse_committee_output(json.dumps(dict(role_res)), frozen=ids, agent="bearbot")
    prose = "\n".join(c.text for c in claims).strip() or "No grounded claims in freeze."
    return BearAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), as_of=as_of, question=question, stance="bearish", bear_case=prose, unknowns=_str_list(role_res.get("unknowns")), what_would_change=_str_list(role_res.get("what_would_change")), claims=claims, research_requests=follow_ups)


def _wave1_state(
    store: ResearchRepository,
    found: ResearchSession,
) -> tuple[Wave1Result, dict[str, object]]:
    """Rebuild the trio Wave1Result from the latest freeze's committee-run jobs."""
    from .director import Wave1Result
    from .synthesis.committee import compute_disagreement

    sid, fid, ids, wave, as_of = _freeze_context(store, found)
    by_role = _committee_results_by_role(store, found, fid)
    kw = {"sid": sid, "wave": wave, "fid": fid, "ids": ids, "as_of": as_of, "question": found.query}
    stock = _build_stock(by_role["stockbot"], **kw) if "stockbot" in by_role else None
    bull = _build_bull(by_role["bullbot"], **kw) if "bullbot" in by_role else None
    bear = _build_bear(by_role["bearbot"], **kw) if "bearbot" in by_role else None
    disagreement = compute_disagreement(stock, bull, bear) if stock is not None and bull is not None and bear is not None else None
    wave1 = Wave1Result(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=list(ids), stock=stock, bull=bull, bear=bear, disagreement=disagreement)
    return wave1, {"freeze_id": fid, "evidence_ids": ids, "wave_id": wave, "as_of": as_of}


def _freeze_wave_cap(found: ResearchSession, session_id: str, wave_id: int) -> None:
    """Enforce the int>=1 wave id and the policy max_waves cap."""
    if isinstance(wave_id, bool) or not isinstance(wave_id, int) or wave_id < 1:
        raise ValueError(f"freeze_session: 'wave_id' must be an int >= 1, got {wave_id!r}")
    raw_section: object = found.policy.get("research", {})
    research: dict[str, object] = raw_section if isinstance(raw_section, dict) else {}
    max_waves = research.get("max_waves", 2)
    if isinstance(max_waves, int) and not isinstance(max_waves, bool) and wave_id > max_waves:
        raise ValueError(f"freeze_session: wave {wave_id} exceeds max_waves {max_waves}")


def _freeze_ready_session(store: ResearchRepository, session_id: str, wave_id: int):
    """Validate the wave cap, then walk CREATED->PLANNING->RESEARCHING (+T for wave 2+)."""
    from .models import SessionStatus

    found = _require_session(store, session_id)
    _freeze_wave_cap(found, session_id, wave_id)
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
    return found


def _freeze_wave_records(store: ResearchRepository, session_id: str, wave_id: int):
    from .evidence import evidence_from_dict

    recs = [evidence_from_dict(record) for record in store.list_evidence(session_id) if str(record.get("record_kind", (record.get("metadata") or {}).get("record_kind") if isinstance(record.get("metadata"), dict) else "evidence")) != "discovery"]
    wave_recs = [e for e in recs if e.wave_id <= wave_id]
    open_src = [j.job_id for j in store.list_jobs(session_id)
                if j.wave_id == wave_id and j.job_type in ("source_agent", "scout")
                and j.status in ("queued", "running")]
    if open_src:
        raise ValueError(f"freeze_session: {len(open_src)} source jobs still open for wave {wave_id}: {open_src}")
    return wave_recs


def _freeze_coverage_note(store: ResearchRepository, session_id: str) -> str:
    """Insufficiency travels as a limitation note; it never blocks the committee."""
    dossiers = store.list_dossiers(session_id)
    if not dossiers:
        return ""
    completes = [d.get("coverage", {}) for d in dossiers if isinstance(d.get("coverage"), dict)]
    if completes and all(isinstance(c, dict) and c.get("complete") is False for c in completes):
        return "no sufficient source coverage; proceeding with limitations"
    return ""


def freeze_session(
    session_id: str,
    wave_id: int = 1,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Freeze wave evidence (PIT-checked) and move RESEARCHING -> FREEZING."""
    from . import freeze as _freeze
    from .models import SessionStatus

    store = _repo(repo)
    found = _freeze_ready_session(store, session_id, wave_id)
    wave_recs = _freeze_wave_records(store, session_id, wave_id)
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
    if wave_id > out.current_wave:
        out = replace(out, current_wave=wave_id, updated_at=utcnow())
        store.save_session(out)
    _emit(store, session_id, "freeze.created", {"freeze_id": fid, "wave_id": wave_id})
    note = _freeze_coverage_note(store, session_id)
    if not wave_recs and not note:
        note = "no PIT-eligible SEC evidence for this wave; proceeding with limitations"
    # Committee creation stays caller-driven: freeze returns the verb + roles and
    # reuses pre-existing committee job ids when present, never auto-creating.
    jobs = store.list_jobs(session_id)
    existing = [j.job_id for j in jobs if j.wave_id == wave_id and j.job_type in ("stockbot", "bullbot", "bearbot")]
    out_dict = _freeze.freeze_to_dict(frozen)
    if note:
        out_dict["coverage_gate"] = "insufficient"
    out_dict["pending_next_action"] = {"verb": "EXECUTE_COMMITTEE",
        "jobs": existing or ["stockbot", "bullbot", "bearbot"], "wave_id": wave_id, "freeze_id": fid}
    if note:
        out_dict["limitations"] = note
    return out_dict


def create_committee_jobs(session_id: str, wave_id: int = 1, *, repo: ResearchRepository | Path | str | None = None) -> dict[str, object]:
    """Opt-in atomic trio creation: 3 distinct pi-owned committee jobs, ids returned together."""
    store = _repo(repo)
    found = _require_session(store, session_id)
    fid = f"{session_id}:{wave_id}:freeze"
    jobs = store.list_jobs(session_id)
    by_role: dict[str, str] = {}
    for j in jobs:
        if j.wave_id == wave_id and j.job_type in ("stockbot", "bullbot", "bearbot"):
            by_role.setdefault(j.job_type, j.job_id)
    created: list[str] = []
    cur = store.get_session(session_id)
    for role in ("stockbot", "bullbot", "bearbot"):
        if role in by_role:
            created.append(by_role[role])
            continue
        cur, job = _jobs.create_job(cur, store.list_jobs(session_id), job_type=role, owner="pi", wave_id=wave_id)
        store.save_session(cur)
        started = _jobs.start_job(job)
        store.save_job(started)
        created.append(started.job_id)
        _emit(store, session_id, "committee.created", {"job_id": started.job_id, "role": role})
    pending: dict[str, object] = {"verb": "EXECUTE_COMMITTEE", "jobs": created, "wave_id": wave_id, "freeze_id": fid}
    if fid not in found.freeze_ids:
        pending["freeze_pending"] = fid
    return {"session_id": session_id, "wave_id": wave_id, "jobs": created, "freeze_id": fid,
            "pending_next_action": pending}


def _committee_live_job(store: ResearchRepository, session_id: str, job_id: str, role: str,
                        analysis: object):
    """Validate role/shape, then load the running job owned by this session."""
    if role not in ("stockbot", "bullbot", "bearbot"):
        raise ValueError(f"record_committee_analysis: role must be stockbot|bullbot|bearbot, got {role!r}")
    if not isinstance(analysis, Mapping):
        raise ValueError(f"record_committee_analysis: 'analysis' must be a mapping, got {type(analysis).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    found = _require_session(store, session_id)
    job = _require_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"record_committee_analysis: job {job_id!r} belongs to {job.session_id!r}")
    if job.status != "running":
        raise ValueError(f"record_committee_analysis: job {job_id!r} status is {job.status!r} (running required)")
    if job.job_type != role:
        raise ValueError(f"record_committee_analysis: job {job_id!r} job_type {job.job_type!r} != role {role!r}")
    return found, job
def _committee_freeze_ids(store: ResearchRepository, session_id: str, found: ResearchSession, job_wave: int) -> tuple[str, list[str]]:
    """Resolve the latest freeze id + evidence ids; enforce the freeze-wave match."""
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
    if isinstance(frozen_wave, int) and not isinstance(frozen_wave, bool) and job_wave != frozen_wave:
        raise ValueError(f"record_committee_analysis: job wave {job_wave!r} != freeze wave {frozen_wave!r}")
    return fid, freeze_ids


def _committee_envelope(analysis: Mapping[str, object], role: str, freeze_ids: list[str]) -> dict[str, object]:
    """JSON-shape the analysis and ground it against the freeze; return the dict form."""
    from .agents import parse_committee_output

    try:
        envelope = json.dumps(dict(analysis))
    except TypeError as exc:
        raise ValueError(f"record_committee_analysis: 'analysis' must be JSON-able: {exc}") from exc
    parse_committee_output(envelope, frozen=freeze_ids, agent=role)
    return dict(analysis)


def _committee_track_run(cur: ResearchSession, fid: str, job_id: str, job_wave: int):
    """Merge one completed job into the session's committee_runs for this freeze."""
    from .models import validate_json_value

    runs: list[JSONValue] = list(cur.committee_runs)
    for i, entry in enumerate(runs):
        if isinstance(entry, dict) and entry.get("freeze_id") == fid:
            got = entry.get("jobs", [])
            known: list[str] = [j for j in got if isinstance(j, str)] if isinstance(got, list) else []
            if job_id not in known:
                known.append(job_id)
            merged: dict[str, JSONValue] = {
                "freeze_id": fid,
                "wave_id": entry.get("wave_id", job_wave),
                "jobs": validate_json_value(known, "<service>"),
            }
            runs[i] = merged
            break
    else:
        fresh: dict[str, JSONValue] = {
            "freeze_id": fid,
            "wave_id": job_wave,
            "jobs": validate_json_value([job_id], "<service>"),
        }
        runs.append(fresh)
    return replace(cur, committee_runs=runs, updated_at=utcnow())


def record_committee_analysis(
    session_id: str,
    job_id: str,
    role: str,
    analysis: dict[str, object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, JSONValue]:
    """Validate one trio analysis against the freeze, persist it, complete the job."""
    from .models import SessionStatus

    store = _repo(repo)
    found, job = _committee_live_job(store, session_id, job_id, role, analysis)
    fid, freeze_ids = _committee_freeze_ids(store, session_id, found, job.wave_id)
    result = _committee_envelope(analysis, role, freeze_ids)
    done = _jobs.complete_job(job, result=result)
    store.save_job(done)
    cur = found
    if cur.status == SessionStatus.FREEZING.value:
        cur = _session.transition_session(cur, SessionStatus.ANALYZING)
    cur = _committee_track_run(cur, fid, job_id, job.wave_id)
    store.save_session(cur)
    _emit(store, session_id, "committee.created", {"job_id": job_id, "role": role, "freeze_id": fid})
    return done.to_dict()


def _no_create_session(_question: str, _as_of: str) -> str:
    raise AssertionError("decide_wave2: create_session is not sequenced in service")


def _no_fetch(_sid: str) -> list[str]:
    raise AssertionError("decide_wave2: fetch_wave_evidence is not sequenced in service")


def _no_freeze(_sid: str) -> str:
    raise AssertionError("decide_wave2: create_freeze is not sequenced in service")


def _no_committee(_sid: str):
    raise AssertionError("decide_wave2: run_committee is not sequenced in service")


def _decide_inputs(store: ResearchRepository, session_id: str, found: ResearchSession):
    """Build the director inputs: wave1 (empty when no freeze), budgets, counters."""
    from .director import DirectorBudgets, DirectorDeps, Wave1Result

    jobs = store.list_jobs(session_id)
    try:
        wave1, _ = _wave1_state(store, found)
    except ValueError:
        wave1 = None
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

    deps = DirectorDeps(
        create_session=_no_create_session,
        fetch_wave_evidence=_no_fetch,
        create_freeze=_no_freeze,
        run_committee=_no_committee,
        record_stop=_record_stop,
    )
    return wave1, deps, DirectorBudgets(), waves_used, len(jobs), tool_used, elapsed


def _decide_settle(store: ResearchRepository, session_id: str, decision: WaveDecision) -> None:
    """Persist the gate outcome: authorized->TARGETED_RESEARCH(+next wave), else->SYNTHESIZING."""
    from .journal import append_event
    from .journal import hydrate as _hydrate
    from .models import SessionStatus

    if decision.authorized:
        cur = store.get_session(session_id)
        if cur.status == SessionStatus.ANALYZING.value:
            cur = _session.transition_session(cur, SessionStatus.TARGETED_RESEARCH)
            nxt = max(cur.current_wave, len(cur.freeze_ids)) + 1
            cur = replace(
                cur,
                current_wave=nxt,
                targeted_question=decision.targeted_question or None,
                targeted_domain=decision.targeted_domain or None,
                updated_at=utcnow(),
            )
            store.save_session(cur)
        _hydrate(session_id, store.list_events(session_id))
        store.save_event(append_event(session_id, "wave.authorized", "service", "service", {
            "question": decision.targeted_question, "domain": decision.targeted_domain,
        }))
    else:
        cur = store.get_session(session_id)
        if cur.status == SessionStatus.ANALYZING.value:
            store.save_session(_session.transition_session(cur, SessionStatus.SYNTHESIZING))


def decide_wave2(
    session_id: str,
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Gate one targeted wave; persists the stop reason either way."""
    from .director import decide_wave2 as _decide

    store = _repo(repo)
    found = _require_session(store, session_id)
    wave1, deps, budgets, waves_used, jobs_used, tool_used, elapsed = _decide_inputs(store, session_id, found)
    decision = _decide(wave1, deps=deps, budgets=budgets, waves_used=waves_used, jobs_used=jobs_used, tool_calls_used=tool_used, elapsed_s=elapsed)
    _decide_settle(store, session_id, decision)
    return {
        "authorized": decision.authorized,
        "stop_reason": decision.stop_reason,
        "reason_detail": decision.reason_detail,
        "targeted_question": decision.targeted_question,
        "targeted_domain": decision.targeted_domain,
    }


def _finalize_trio(store: ResearchRepository, session_id: str, found: ResearchSession):
    """Load wave1 + require the full trio and disagreement for this freeze."""
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
    return wave1, meta, fid, disagreement


def _finalize_claims(session_id: str, claims: list[object], freeze_ids: list[str]):
    """Shape caller claims and ground every citation against the freeze."""
    from .agents import parse_grounded_claims

    if not isinstance(claims, list):
        raise ValueError(f"finalize_session: 'claims' must be a list, got {type(claims).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    if not claims:
        raise ValueError("finalize_session: 'claims' must be a non-empty grounded list")
    narrowed: list[dict[str, object]] = []
    for item in claims:
        if not isinstance(item, Mapping):
            raise ValueError(f"finalize_session: each claim must be a mapping, got {type(item).__name__}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        refs = item.get("evidence_ids", [])
        narrowed.append({"text": item.get("text", item.get("claim_text", item.get("claim", ""))), "evidence_ids": [e for e in refs] if isinstance(refs, (list, tuple)) else []})
    try:
        envelope = json.dumps(narrowed)
    except TypeError as exc:
        raise ValueError(f"finalize_session: 'claims' must be JSON-able: {exc}") from exc
    return parse_grounded_claims(envelope, frozen=[e for e in freeze_ids if isinstance(e, str)])


def _finalize_answer(answer: object) -> str:
    """Require a non-blank synthesis answer."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("finalize_session: 'answer' must be a non-empty string")
    return answer


def _finalize_persist(store: ResearchRepository, session_id: str, fid: str,
                      answer: str, grounded: list[GroundedClaim]) -> dict[str, object]:
    """Persist the synthesis result and walk ANALYZING->SYNTHESIZING->COMPLETED."""
    from .models import SessionStatus, validate_json_value

    claims_json: list[JSONValue] = []
    for claim in grounded:
        row: dict[str, JSONValue] = {"text": claim.text, "evidence_ids": validate_json_value(list(claim.evidence_ids), "<service>")}
        claims_json.append(row)
    final: dict[str, JSONValue] = {"answer": answer, "freeze_id": fid, "claims": claims_json}
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


def _finalize_synth(found: ResearchSession, wave1: Wave1Result, meta: dict[str, object], fid: str, disagreement: CommitteeDisagreement,
                    model: str, claims: list[object], session_id: str):
    """Ground claims + run the trio synthesis; return (answer, grounded)."""
    from .synthesis.final import synthesize_final

    ids_raw = meta["evidence_ids"]
    assert isinstance(ids_raw, list)
    grounded = _finalize_claims(session_id, claims, [e for e in ids_raw if isinstance(e, str)])
    assert wave1.stock is not None and wave1.bull is not None and wave1.bear is not None
    wave_raw = meta["wave_id"]
    as_of_raw = meta["as_of"]
    assert isinstance(wave_raw, int) and isinstance(as_of_raw, str)
    synth = synthesize_final(found.query, session_id=session_id, wave_id=wave_raw, freeze_id=fid, as_of=as_of_raw, stock=wave1.stock, bull=wave1.bull, bear=wave1.bear, disagreement=disagreement, model=model)
    if not synth.answer.strip():
        raise ValueError("finalize_session: synthesis produced an empty answer")
    return synth.answer, grounded


def finalize_session(
    session_id: str,
    answer: str,
    claims: list[object],
    *,
    repo: ResearchRepository | Path | str | None = None,
) -> dict[str, object]:
    """Persist the trio-joined synthesis and move to COMPLETED."""
    store = _repo(repo)
    found = _require_session(store, session_id)
    if found.status in _session.TERMINAL_STATUSES:
        raise ValueError(f"finalize_session: session {session_id!r} status is {found.status!r} (terminal)")
    model = _finalize_answer(answer)
    wave1, meta, fid, disagreement = _finalize_trio(store, session_id, found)
    open_all = [j.job_id for j in store.list_jobs(session_id) if j.status in ("queued", "running")]
    if open_all:
        raise ValueError(f"finalize_session: {len(open_all)} jobs still open: {open_all}")
    synth_answer, grounded = _finalize_synth(found, wave1, meta, fid, disagreement, model, claims, session_id)
    return _finalize_persist(store, session_id, fid, synth_answer, grounded)


def _dispatch_live_job(store: ResearchRepository, session_id: str, job_id: str):
    """Load session + job with cross-session ownership enforced."""
    found = _require_session(store, session_id)
    job = _require_job(store, job_id)
    if job.session_id != found.session_id:
        raise ValueError(f"dispatch: job {job_id!r} belongs to {job.session_id!r}")
    return found, job


def _dispatch_check_domain(job: Job, job_id: str, tool_name: str) -> None:
    """Committee jobs never fetch: SEC-scoped source jobs accept SEC tools only."""
    if job.job_type in ("stockbot", "bullbot", "bearbot"):
        raise ValueError(f"dispatch: committee job {job_id!r} ({job.job_type}) cannot dispatch tools (frozen evidence only)")
    if job.source_domain is None or tool_name.startswith("research"):
        return
    if job.source_domain.upper() != "SEC":
        return
    from .agents.source_agent import is_sec_tool

    if not is_sec_tool(tool_name):
        raise ValueError(f"dispatch: tool {tool_name!r} outside SEC domain for job {job_id!r}")


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
    found, job = _dispatch_live_job(store, session_id, job_id)
    check_stage_tool(stage_for_session(found, store.list_jobs(session_id)), tool_name)
    _dispatch_check_domain(job, job_id, tool_name)
    try:
        billed, spent = store.consume_dispatch_budget(session_id, job_id)
    except KeyError as exc:
        raise ResearchNotFound(exc.args[0] if exc.args else str(exc)) from None
    raw_used: object = billed.budget.get("tool_calls_used", 0)
    used = raw_used if isinstance(raw_used, int) and not isinstance(raw_used, bool) else 0
    return {"session_id": session_id, "job_id": job_id, "tool_name": tool_name,
            "tool_budget": spent.tool_budget, "tool_calls_used": used}
