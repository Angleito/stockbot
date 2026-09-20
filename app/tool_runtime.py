"""Runtime-neutral tool execution surface for Stockbot agents.

Stable boundary for any agent runtime that uses Stockbot tools. Sole
execution authority for canonical tools: schedulers pass exact JEV-selected
tool(s) in; this module runs them through the hardened gates (permit+schema,
staged session/stage check, company resolve, call_tool unwrap,
authorization/egress/private-args, budget, LOCAL_CONTEXT execution,
ingress/DLP, evidence-token accounting, staged persistence, recorder).
Decision-agnostic: no registry, no tool-selection logic, no JEV/Needle
knowledge. Never raises.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .pi_gateway import (
    PiSessionContext,
    _check_intent_and_egress,
    _execute_and_record,
    _extract_source_refs,
    _failed_dict,
    _failed_result_response,
    _failure_outcome,
    _finalize_success,
    _persist_staged_tool_result,
    _recorder_run_id,
    _resolve_company_arguments,
    _run_pre_gates,
    _tool_result_meta,
    _unavailable_data_response,
    _unwrap_call_tool,
)
from .runtime import ToolResultMeta
from .tool_render import render_tool_result

logger = logging.getLogger(__name__)


class RuntimeToolSession(PiSessionContext):
    """Per-runtime-session grant + labels + budget. Deny-by-default.

    Subclasses the hardened gateway session so fields, labels, and budget
    init stay identical by construction; the gate helpers accept it wherever
    the gateway session is accepted.
    """


# ponytail: alias shims, remove in cutover wave
AgentToolSession = RuntimeToolSession


@dataclass(frozen=True)
class ToolOutcome:
    tool_name: str
    content: str
    source_handle: dict[str, object] | None
    source_refs: dict[str, object] | None
    error: str | None
    error_type: str | None
    retryable: bool
    meta: ToolResultMeta


# ponytail: only handler-level tool_error retries; denials/budgets/validation are deterministic.
_RETRYABLE_ERROR_TYPES = frozenset({"tool_error"})


def outcome_from_result(tool_name: str, result: dict[str, object]) -> ToolOutcome:
    """Convert a gateway result dict into a frozen scheduler-facing outcome."""
    content = render_tool_result(result)
    refs = _extract_source_refs(result)
    source_refs = dict(refs) if refs else None
    raw_handle = result.get("source_handle")
    source_handle = dict(raw_handle) if isinstance(raw_handle, dict) and raw_handle else None
    raw_error = result.get("error")
    error = str(raw_error) if raw_error is not None else None
    raw_etype = result.get("error_type")
    error_type = raw_etype if isinstance(raw_etype, str) else None
    retryable = error is not None and error_type in _RETRYABLE_ERROR_TYPES
    return ToolOutcome(
        tool_name=tool_name,
        content=content,
        source_handle=source_handle,
        source_refs=source_refs,
        error=error,
        error_type=error_type,
        retryable=retryable,
        meta=_tool_result_meta(result),
    )


def _dispatch_inner_call(
    name: str,
    arguments: dict[str, object],
    session: RuntimeToolSession,
    *,
    tool_call_id: str | None,
    protocol_id: str | None,
    bridge_queue_ms: float,
    data_root: str | Path | None,
    as_of: str | None,
    active_research_session_id: str | None,
    active_research_job_id: str | None,
) -> dict[str, object] | None:
    """Generic dispatch: call_tool validates then tail-calls the inner tool once."""
    # The outer wrapper consumes no budget slot and writes no recorder row.
    if name != "call_tool":
        return None
    unwrap = _unwrap_call_tool(name, arguments)
    if unwrap.error is not None:
        return unwrap.error
    inner_name = unwrap.inner_name or ""
    inner_args = unwrap.inner_args or {}
    return _execute_agent_tool(
        inner_name,
        inner_args,
        session,
        tool_call_id=tool_call_id,
        protocol_id=protocol_id,
        bridge_queue_ms=bridge_queue_ms,
        data_root=data_root,
        as_of=as_of,
        active_research_session_id=active_research_session_id,
        active_research_job_id=active_research_job_id,
    )


def _execute_agent_tool(
    name: str,
    arguments: dict[str, object],
    session: RuntimeToolSession,
    *,
    tool_call_id: str | None = None,
    protocol_id: str | None = None,
    bridge_queue_ms: float = 0.0,
    data_root: str | Path | None = None,
    as_of: str | None = None,
    active_research_session_id: str | None = None,
    active_research_job_id: str | None = None,
) -> dict[str, object]:
    # Generic dispatch: call_tool validates then tail-calls the inner tool once.
    # The outer wrapper consumes no budget slot and writes no recorder row.
    tail = _dispatch_inner_call(
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
    if tail is not None:
        return tail
    # Single-dispatch company-name support, schema-driven: any tool whose
    # schema declares company_name alongside a ticker/entity identifier
    # fills the missing identifier before validation. Cards keep the
    # identifier required (visible signal) while name-only dispatches
    # still execute; tools without company_name are untouched.
    if isinstance(arguments, dict):
        arguments = _resolve_company_arguments(name, arguments)
    pre = _run_pre_gates(
        name,
        arguments,
        session,
        data_root=data_root,
        as_of=as_of,
        active_research_session_id=active_research_session_id,
        active_research_job_id=active_research_job_id,
    )
    if isinstance(pre, dict):
        return pre
    staged, args_for_hash, _dispatch_consumed = pre
    recorder, run_id = _recorder_run_id(session)

    # Gate 3: intent firewall. No approval callback in this plan, so
    # portfolio-shaped calls are always denied (RESEARCH-only).
    # Gate 4: search_web egress; every other tool's private-pattern args check.
    gate_error = _check_intent_and_egress(name, arguments, session, args_for_hash)
    if gate_error is not None:
        return gate_error

    # Gate 6: LOCAL_CONTEXT only, never a broker context, in this plan.
    # Handler + rendering run OUTSIDE the session lock so calls overlap.
    # Gates above stay LOCAL_CONTEXT-based; only the final execute_tool
    # context carries the validated data_root override.
    (
        result,
        resolved_tc_id,
        status,
        meta,
        _error_type,
        _error_message,
        _cache_hit,
        _cache_type,
        _handler_ms,
    ) = _execute_and_record(
        name,
        arguments,
        session,
        run_id=run_id,
        recorder=recorder,
        tool_call_id=tool_call_id,
        protocol_id=protocol_id,
        bridge_queue_ms=bridge_queue_ms,
        data_root=data_root,
        as_of=as_of,
        research_session_id=staged.session_id,
    )

    failed_map = _failed_dict(result)
    if failed_map is not None:
        _failed, soft, _denied, _status2, failed_error_type, _msg2 = _failure_outcome(failed_map)
        return _failed_result_response(name, failed_map, failed_error_type, soft)
    if not isinstance(result, dict):
        return {
            "error": _unavailable_data_response([(name, {"error": "empty tool result"})]),
            "error_type": "tool_error",
        }
    _persist_staged_tool_result(name, result, staged, resolved_tc_id)

    # Gate 5: ingress scan on the rendered evidence; quarantined or blocked
    # results are withheld from the agent with a fixed placeholder.
    # Gate 7 (success path): DLP over what the agent receives, then record evidence.
    # Session lock covers guard_response + label/budget mutations only.
    return _finalize_success(
        name,
        result,
        session,
        run_id=run_id,
        recorder=recorder,
        resolved_tc_id=resolved_tc_id,
        status=status,
        meta=meta,
    )


def execute_agent_tool(
    name: str,
    arguments: dict[str, object],
    session: RuntimeToolSession,
    *,
    tool_call_id: str | None = None,
    protocol_id: str | None = None,
    bridge_queue_ms: float = 0.0,
    data_root: str | Path | None = None,
    as_of: str | None = None,
    active_research_session_id: str | None = None,
    active_research_job_id: str | None = None,
) -> dict[str, object]:
    """Run one agent-requested tool through all gates. Never raises."""
    try:
        return _execute_agent_tool(
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
    except Exception as exc:  # never break the bridge loop
        logger.exception("Runtime tool gateway failed for '%s'", name)
        return {"error": f"Runtime tool gateway failed for tool '{name}': {exc}"}


__all__ = [
    "AgentToolSession",
    "RuntimeToolSession",
    "ToolOutcome",
    "execute_agent_tool",
    "outcome_from_result",
]
