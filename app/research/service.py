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

__all__ = [
    "ResearchNotFound",
    "cancel_research",
    "complete_job",
    "create_research",
    "inspect_research",
    "list_research",
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
    store.save_session(updated)
    store.save_job(job)
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
            if job.status in ("queued", "running"):
                return job.to_dict()  # type: ignore[return-value]
        return existing[0].to_dict()  # type: ignore[return-value]
    updated, job = _jobs.create_job(found, [], job_type="source_agent", owner="service")
    store.save_session(updated)
    store.save_job(job)
    return job.to_dict()  # type: ignore[return-value]


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
    return {
        "session": found.to_dict(),
        "jobs": [j.to_dict() for j in job_list],
        "pending_next_action": pending_next_action(found, job_list),
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
    wave = wave_raw if isinstance(wave_raw, int) and not isinstance(wave_raw, bool) else job.wave_id
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
