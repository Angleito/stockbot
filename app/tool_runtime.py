"""Runtime-neutral tool execution surface for Stockbot agents.

This module is the stable boundary for any agent runtime that wants to use
Stockbot tools. Needle, OMP/Pi, and future runtimes should depend on this
surface rather than on a model-specific gateway.

The hardened implementation still lives in app.pi_gateway today. Keeping that
behind this compatibility seam lets us migrate names/internal ownership
incrementally without duplicating policy, security, budgets, evidence handling,
or tool execution.
"""

from __future__ import annotations

from pathlib import Path

from .pi_gateway import PiSessionContext, execute_pi_tool


class AgentToolSession(PiSessionContext):
    """Runtime-neutral per-agent tool session.

    Inherits the existing hardened session state while callers migrate away
    from Pi-specific names. The session owns authorization/security labels and
    execution/evidence budgets across a sequence of tool calls.
    """


def execute_agent_tool(
    name: str,
    arguments: dict[str, object],
    session: AgentToolSession,
    *,
    tool_call_id: str | None = None,
    protocol_id: str | None = None,
    bridge_queue_ms: float = 0.0,
    data_root: str | Path | None = None,
    as_of: str | None = None,
    active_research_session_id: str | None = None,
    active_research_job_id: str | None = None,
) -> dict[str, object]:
    """Execute one canonical Stockbot tool through the hardened agent gateway.

    This is intentionally a thin compatibility seam for now. Do not bypass it
    with direct app.tools.execute_tool calls from agent runtimes.
    """
    return execute_pi_tool(
        name,
        arguments,
        session,
        tool_call_id=tool_call_id,
        protocol_id=protocol_id,
        bridge_queue_ms=bridge_queue_ms,
        data_root=data_root,
        as_of=as_of,
        active_research_session_id=active_research_session_id,
        active_research_job_id=active_research_job_id,
    )


__all__ = ["AgentToolSession", "execute_agent_tool"]
