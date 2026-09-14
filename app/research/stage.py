"""Stage-gated capabilities: derive research stage, enforce per-stage tool sets.

Kernel-authoritative gate (TS mirrors for UX only). Canonical tool names only.
"""

from __future__ import annotations

from typing import Literal

from app.policy import Capability
from app.tools import tools_for_capabilities

Stage = Literal["SOURCE_RESEARCH", "COMMITTEE", "FINAL"]


def _schema_name(tool: dict[str, object]) -> str | None:
    """OpenAI schema function name (TOOLS entries are untyped app-side JSON)."""
    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        return name if isinstance(name, str) else None
    return None


# Canonical agent-visible set: single source of truth stays in app/tools.py.
RESEARCH_TOOL_NAMES: frozenset[str] = frozenset(
    name
    for tool in tools_for_capabilities(frozenset({Capability.RESEARCH}))
    for name in [_schema_name(tool)]
    if name is not None
)

DISCOVERY_TOOLS: frozenset[str] = frozenset({
    "browse_tools", "search_tools", "describe_tool", "list_tool_domains", "call_tool",
})

CONTROL_TOOLS: frozenset[str] = frozenset({
    "research_start", "research_resume", "research_status", "research_cancel",
    "research_read", "research_add_evidence", "research_add_analysis",
    "research_finalize",
})

_THESIS_TOOLS: frozenset[str] = frozenset({
    "thesis_create", "thesis_show", "thesis_refine", "thesis_watch",
    "thesis_journal", "thesis_status",
})

# Data dispatches: everything research-capable except discovery, thesis
# actions, and local research controls.
DISPATCH_TOOLS: frozenset[str] = frozenset(
    RESEARCH_TOOL_NAMES - DISCOVERY_TOOLS - CONTROL_TOOLS - _THESIS_TOOLS
)

STAGE_ALLOW: dict[Stage, frozenset[str]] = {
    "SOURCE_RESEARCH": DISCOVERY_TOOLS | DISPATCH_TOOLS | frozenset({
        "research_resume", "research_status", "research_read", "research_cancel", "research_add_evidence",
    }),
    "COMMITTEE": DISCOVERY_TOOLS | frozenset({
        "research_resume", "research_status", "research_read", "research_cancel", "research_add_analysis",
    }),
    "FINAL": DISCOVERY_TOOLS | frozenset({
        "research_resume", "research_status", "research_read", "research_cancel", "research_finalize",
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
    if tool_name in DISCOVERY_TOOLS:
        return
    allowed = STAGE_ALLOW.get(stage)  # type: ignore[call-overload]
    if allowed is not None and tool_name in allowed:
        return
    raise ValueError(f"Stage {stage} forbids tool '{tool_name}'")


__all__ = ["STAGE_ALLOW", "Stage", "check_stage_tool", "stage_for_session", "RESEARCH_TOOL_NAMES", "DISCOVERY_TOOLS", "CONTROL_TOOLS", "DISPATCH_TOOLS"]
