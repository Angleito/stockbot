"""Stage-gated capabilities: derive research stage, enforce per-stage tool sets.

Kernel-authoritative gate (TS mirrors for UX only). Stdlib only.
"""

from __future__ import annotations

from typing import Literal

try:
    from app.pi_gateway import RESEARCH_TOOL_NAMES as _RESEARCH_TOOLS
except Exception:  # pragma: no cover - gateway import fallback
    _RESEARCH_TOOLS = frozenset()
try:
    from app.research.agents.source_agent import SEC_TOOLS as _SEC_TOOLS
except Exception:  # pragma: no cover - agent import fallback
    _SEC_TOOLS = frozenset()

Stage = Literal["SOURCE_RESEARCH", "COMMITTEE", "FINAL"]

_DISCOVERY: frozenset[str] = frozenset({
    "browse_tools", "search_tools", "describe_tool", "list_tool_domains", "call_tool",
})

STAGE_ALLOW: dict[Stage, frozenset[str]] = {
    "SOURCE_RESEARCH": frozenset({
        "search_web", "get_fundamentals", "get_short_interest",
        "research_add_evidence",
        "research.session.inspect", "research.session.resume",
        "research.job.start", "research.job.complete", "research.freeze.create",
    } | set(_RESEARCH_TOOLS) | set(_SEC_TOOLS)),
    "COMMITTEE": frozenset({
        "research.session.inspect", "research.session.resume",
        "research.job.start", "research_add_analysis",
    }),
    "FINAL": frozenset({
        "research.session.inspect", "research.session.resume", "research.session.finalize",
    }),
}


def _field(obj: object, key: str, default: object = None) -> object:
    if isinstance(obj, dict):
        out: object = obj.get(key, default)
        return out
    fell: object = getattr(obj, key, default)
    return fell


def _trio_complete(session: object, jobs: object) -> bool:
    freeze_ids = _field(session, "freeze_ids", [])
    if not isinstance(freeze_ids, list) or not freeze_ids:
        return False
    fid = freeze_ids[-1]
    wanted: set[str] = set()
    runs = _field(session, "committee_runs", [])
    if isinstance(runs, list):
        for entry in runs:
            if isinstance(entry, dict) and entry.get("freeze_id") == fid:
                for jid in (entry.get("jobs") or []):
                    if isinstance(jid, str):
                        wanted.add(jid)
    by_id: dict[str, object] = {}
    if isinstance(jobs, list):
        for job in jobs:
            jid = _field(job, "job_id")
            if isinstance(jid, str):
                by_id[jid] = job
    roles: set[str] = set()
    for jid in wanted:
        job = by_id.get(jid)
        if job is not None and _field(job, "status") == "completed":
            roles.add(str(_field(job, "job_type")))
    return {"stockbot", "bullbot", "bearbot"} <= roles


def stage_for_session(session: object, jobs: object = ()) -> Stage:
    """Derive stage: terminal -> FINAL; wave-2 research -> SOURCE; trio-complete -> FINAL."""
    status = str(_field(session, "status", "") or "").upper()
    if status in ("SYNTHESIZING", "COMPLETED"):
        return "FINAL"
    # ponytail: wave-2 research re-opens source tools even though the wave-1 trio is done.
    if status == "TARGETED_RESEARCH":
        return "SOURCE_RESEARCH"
    freeze_ids = _field(session, "freeze_ids", [])
    if isinstance(freeze_ids, list) and freeze_ids:
        if _trio_complete(session, jobs):
            return "FINAL"
        return "COMMITTEE"
    if status in ("FREEZING", "ANALYZING"):
        if _trio_complete(session, jobs):
            return "FINAL"
        return "COMMITTEE"
    return "SOURCE_RESEARCH"


def check_stage_tool(stage: str, tool_name: str) -> None:
    """Raise ValueError when tool_name is forbidden in stage. Discovery always passes."""
    if tool_name in _DISCOVERY:
        return
    allowed = STAGE_ALLOW.get(stage)  # type: ignore[call-overload]
    if allowed is not None and tool_name in allowed:
        return
    raise ValueError(f"Stage {stage} forbids tool '{tool_name}'")


__all__ = ["STAGE_ALLOW", "Stage", "check_stage_tool", "stage_for_session"]
