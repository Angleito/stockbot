"""Job lifecycle: create/start/complete/fail/cancel/inspect/list_children.

All limits come from the session policy dict, never prompts. Pure
functions: the session + job list stay authoritative, no private copies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from .models import (
    FAILURE_CATEGORY_VALUES,
    JOB_TYPE_VALUES,
    SOURCE_RUNTIME_BUDGET_S,
    Failure,
    FailureCategory,
    Job,
    JobStatus,
    JobType,
    ResearchSession,
    new_job_id,
    utcnow,
    validate_json_mapping,
)

__all__ = [
    "COMMITTEE_TYPES",
    "active_jobs",
    "cancel_job",
    "children_allowed",
    "complete_job",
    "create_job",
    "default_tool_budget",
    "fail_job",
    "inspect_job",
    "list_children",
    "start_job",
]

ACTIVE_STATUSES: frozenset[str] = frozenset({JobStatus.QUEUED.value, JobStatus.RUNNING.value})
COMMITTEE_TYPES: frozenset[str] = frozenset(
    {JobType.STOCKBOT.value, JobType.BULLBOT.value, JobType.BEARBOT.value}
)
_JOBTYPE_SECTION: dict[str, str] = {
    JobType.SOURCE_AGENT.value: "source",
    JobType.SCOUT.value: "scout",
}


def _section(policy: Mapping[str, object], name: str, where: str) -> Mapping[str, object]:
    section = policy.get(name, {})
    if not isinstance(section, Mapping):
        raise ValueError(f"{where}: policy section {name!r} must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return section


def _limit(section: Mapping[str, object], key: str, where: str) -> int:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: policy {key!r} must be an int, got {value!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _research_limit(policy: Mapping[str, object], key: str) -> int:
    return _limit(_section(policy, "research", "<job>"), key, "<job>")


def children_allowed(policy: Mapping[str, object], job_type: str) -> int:
    """Max children for a parent of this type (0 when the type spawns nothing)."""
    section_name = _JOBTYPE_SECTION.get(job_type)
    if section_name is None:
        return 0
    return _limit(_section(policy, section_name, "<job>"), "max_children", "<job>")


def default_tool_budget(policy: Mapping[str, object], job_type: str) -> int | None:
    """Default tool budget for a type, or None when the type is unbilled."""
    section_name = _JOBTYPE_SECTION.get(job_type)
    if section_name is None:
        return None
    return _limit(_section(policy, section_name, "<job>"), "max_tool", "<job>")


def active_jobs(jobs: list[Job]) -> list[Job]:
    """Jobs still holding a parallel slot (queued or running)."""
    return [j for j in jobs if j.status in ACTIVE_STATUSES]


def inspect_job(jobs: list[Job], job_id: str) -> Job:
    """Return one job; raises KeyError when absent."""
    for job in jobs:
        if job.job_id == job_id:
            return job
    raise KeyError(f"unknown job_id: {job_id!r}")


def list_children(jobs: list[Job], parent_job_id: str) -> list[Job]:
    """Direct children of one parent, oldest first."""
    return [j for j in jobs if j.parent_job_id == parent_job_id]


def _check_committee(mine: list[Job], policy: Mapping[str, object], jtype: str) -> None:
    """Enforce the committee parallel cap (committee types only)."""
    if jtype not in COMMITTEE_TYPES:
        return
    committee_section = _section(policy, "committee", "<job>")
    running_committee = sum(1 for j in mine if j.job_type in COMMITTEE_TYPES and j.status in ACTIVE_STATUSES)
    if running_committee >= _limit(committee_section, "max_parallel", "<job>"):
        raise ValueError("<job>: parallelism_exceeded: committee max_parallel reached")


def _check_capacity(session: ResearchSession, jobs: list[Job], jtype: str, wave_id: int) -> list[Job]:
    """Enforce wave/total/parallel/committee limits; return session-scoped jobs."""
    policy = session.policy
    max_waves = _research_limit(policy, "max_waves")
    if wave_id < 1 or wave_id > max_waves:
        raise ValueError(f"<job>: wave_budget_exhausted: wave {wave_id} outside 1..{max_waves}")
    if len(session.job_ids) >= _research_limit(policy, "max_total_jobs"):
        raise ValueError("<job>: job_budget_exhausted: session max_total_jobs reached")
    mine = [j for j in jobs if j.session_id == session.session_id]
    if len(active_jobs(mine)) >= _research_limit(policy, "max_parallel"):
        raise ValueError("<job>: parallelism_exceeded: session max_parallel reached")
    _check_committee(mine, policy, jtype)
    return mine


def _check_parent(mine: list[Job], policy: Mapping[str, object], parent_job_id: str | None) -> None:
    """Enforce the depth/children limit for one parent."""
    if parent_job_id is None:
        return
    parent = inspect_job(mine, parent_job_id)
    allowed = children_allowed(policy, parent.job_type)
    if len(list_children(mine, parent_job_id)) >= allowed:
        raise ValueError(f"<job>: depth_exceeded: parent {parent_job_id!r} allows {allowed} children")


def _parse_deadline(deadline: datetime | str | None) -> datetime | None:
    """Normalize an ISO-8601/datetime deadline; None stays None."""
    if deadline is None:
        return None
    if isinstance(deadline, datetime):
        return deadline
    if isinstance(deadline, str) and deadline.strip():
        try:
            return datetime.fromisoformat(deadline.strip())
        except ValueError:
            raise ValueError(f"<job>: 'deadline' must be ISO-8601, got {deadline!r}") from None
    raise ValueError(f"<job>: 'deadline' must be ISO-8601, datetime, or null, got {deadline!r}")


def _build_job(session: ResearchSession, *, jtype: str, owner: str, wave_id: int, parent_job_id: str | None, source_domain: str | None, model: str | None, token_budget: int | None, tool_budget: int | None, child_budget: int | None, deadline: datetime | str | None, job_id: str | None) -> tuple[ResearchSession, Job]:
    """Assemble, validate, and attach one queued job."""
    policy = session.policy
    parsed_deadline = _parse_deadline(deadline)
    job = Job(
        job_id=job_id or new_job_id(),
        session_id=session.session_id,
        wave_id=wave_id,
        parent_job_id=parent_job_id,
        job_type=jtype,
        owner=owner,
        source_domain=source_domain,
        status=JobStatus.QUEUED.value,
        created_at=utcnow(),
        deadline=parsed_deadline if parsed_deadline is not None else (utcnow() + timedelta(seconds=SOURCE_RUNTIME_BUDGET_S)) if jtype == JobType.SOURCE_AGENT.value else None,
        model=model,
        token_budget=token_budget,
        tool_budget=tool_budget if tool_budget is not None else default_tool_budget(policy, jtype),
        child_budget=child_budget if child_budget is not None else children_allowed(policy, jtype),
    )
    job.validate("<job>")
    out = replace(session, job_ids=[*session.job_ids, job.job_id], updated_at=utcnow())
    out.validate("<session>")
    return out, job


def create_job(
    session: ResearchSession,
    jobs: list[Job],
    *,
    job_type: str | JobType,
    owner: str,
    wave_id: int = 1,
    parent_job_id: str | None = None,
    source_domain: str | None = None,
    model: str | None = None,
    token_budget: int | None = None,
    tool_budget: int | None = None,
    child_budget: int | None = None,
    deadline: datetime | str | None = None,
    job_id: str | None = None,
) -> tuple[ResearchSession, Job]:
    """Create a queued job, enforcing wave/total/parallel/depth limits from policy.

    Raises ValueError naming the FailureCategory when a limit bites.
    Returns the updated session (job_ids extended) alongside the new job.
    """
    jtype = job_type.value if isinstance(job_type, JobType) else job_type
    if jtype not in JOB_TYPE_VALUES:
        raise ValueError(f"<job>: unknown job_type {job_type!r}")
    if not owner:
        raise ValueError("<job>: 'owner' must be a non-empty string")
    if isinstance(wave_id, bool) or not isinstance(wave_id, int):
        raise ValueError(f"<job>: 'wave_id' must be an int, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    mine = _check_capacity(session, jobs, jtype, wave_id)
    _check_parent(mine, session.policy, parent_job_id)
    return _build_job(session, jtype=jtype, owner=owner, wave_id=wave_id, parent_job_id=parent_job_id, source_domain=source_domain, model=model, token_budget=token_budget, tool_budget=tool_budget, child_budget=child_budget, deadline=deadline, job_id=job_id)


def start_job(job: Job) -> Job:
    """Mark a queued job running; raises ValueError otherwise."""
    if job.status != JobStatus.QUEUED.value:
        raise ValueError(f"<job>: cannot start job in status {job.status!r}")
    out = replace(job, status=JobStatus.RUNNING.value, started_at=utcnow(), last_heartbeat_at=utcnow())
    out.validate("<job>")
    return out


def complete_job(job: Job, *, result: Mapping[str, object] | None = None) -> Job:
    """Mark a running job completed with an optional JSON result."""
    if job.status != JobStatus.RUNNING.value:
        raise ValueError(f"<job>: cannot complete job in status {job.status!r}")
    out = replace(
        job,
        status=JobStatus.COMPLETED.value,
        completed_at=utcnow(),
        result=validate_json_mapping(result or {}, "<job>: 'result'"),
    )
    out.validate("<job>")
    return out


def fail_job(job: Job, category: str | FailureCategory, message: str) -> Job:
    """Mark a queued/running job failed with a categorized failure."""
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        raise ValueError(f"<job>: cannot fail job in status {job.status!r}")
    failure = Failure(
        category=category.value if isinstance(category, FailureCategory) else category,
        message=message,
    )
    failure.validate("<job>: failure")
    if failure.category not in FAILURE_CATEGORY_VALUES:
        raise ValueError(f"<job>: unknown failure category {failure.category!r}")
    out = replace(job, status=JobStatus.FAILED.value, completed_at=utcnow(), failure=failure)
    out.validate("<job>")
    return out


def cancel_job(job: Job) -> Job:
    """Mark a queued/running job cancelled; raises ValueError otherwise."""
    if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
        raise ValueError(f"<job>: cannot cancel job in status {job.status!r}")
    out = replace(job, status=JobStatus.CANCELLED.value, completed_at=utcnow())
    out.validate("<job>")
    return out
