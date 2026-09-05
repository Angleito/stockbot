"""Pi tool gateway: run_chat's security gates without the LLM loop.

Pi is the reasoning model; every tool call it makes still passes the same
gates as app/agent.py run_chat (permit filter, argument validation, intent
firewall, egress/private-args checks, ingress scan, LOCAL_CONTEXT-only
execution, DLP, budget + recorder). Rival pattern to avoid: calling
execute_tool directly from the bridge (drops all 7 gates).

One deliberate divergence: the ingress scan uses envelope_for_tool +
prepare_context directly instead of ContextBuilder.add_tool_result, because
the builder's search_web path runs a nested OpenRouter completion
(quarantine_reader) and there is no main model in the Pi harness — Pi
itself reads the rendered evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .agent import (
    _BUDGET_EXHAUSTED_RESPONSE,
    _is_failed_result,
    _tool_result_meta,
    _unavailable_data_response,
)
from .policy import LOCAL_CONTEXT, Capability
from .redact import redact_json, redact_text
from .security.action_policy import (
    TOOL_DOMAINS,
    authorize_egress,
    authorize_tool_call,
    private_pattern_hit,
)
from .security.context import (
    RunSecurityContext,
    Sensitivity,
    SessionAuthorization,
    SessionSecurityState,
    classify_intent,
)
from .security.context_gateway import (
    QuarantinedContext,
    envelope_for_tool,
    prepare_context,
)
from .security.response_guard import guard_response
from .runtime import ExecutionBudget
from .storage.runs import get_current_recorder
from .tool_render import render_tool_result
from .tools import (
    TOOL_REGISTRY_VERSION,
    _validate_tool_arguments,
    execute_tool,
    tool_is_permitted,
    tools_for_capabilities,
)

logger = logging.getLogger(__name__)

# Model label recorded for Pi-driven tool calls. Handlers ignore it
# (no nested completions remain on the Pi path); it exists for provenance.
PI_MODEL = "pi"

# Single source of truth stays in app/tools.py; this is just the RESEARCH
# projection of the canonical registry.
RESEARCH_TOOL_NAMES: frozenset[str] = frozenset(
    tool["function"]["name"]
    for tool in tools_for_capabilities(frozenset({Capability.RESEARCH}))
)


@dataclass
class PiSessionContext:
    """Per-Pi-session grant + labels + budget. Deny-by-default."""

    session_id: str
    authorization: SessionAuthorization = field(default_factory=SessionAuthorization)
    security_state: SessionSecurityState = field(default_factory=SessionSecurityState)
    run_security: RunSecurityContext = field(init=False)
    budget: ExecutionBudget = field(init=False)

    def __post_init__(self) -> None:
        self.security_state.authorization = self.authorization
        if self.security_state.private_context_seen:
            data_labels = {"private"}
        else:
            data_labels = set()
        self.run_security = RunSecurityContext(
            original_intent=classify_intent([]),
            capabilities=frozenset(cap.name for cap in LOCAL_CONTEXT.capabilities),
            authorization=self.authorization,
            data_labels=data_labels,
        )
        limits = LOCAL_CONTEXT.run_limits
        self.budget = ExecutionBudget(
            max_rounds=limits.max_rounds,
            max_tool_calls=limits.max_tool_calls,
            max_model_calls=limits.max_model_calls,
            max_runtime=limits.max_runtime,
            max_evidence_tokens=limits.max_evidence_tokens,
        )


def _record_security(
    session: PiSessionContext,
    source: str,
    payload: str,
    decision: str,
    reason: str | None,
    *,
    score: int | None = None,
    verdict: str | None = None,
    rule_ids: list[str] | None = None,
) -> None:
    session.run_security.security_events.append(
        {
            "source": source,
            "decision": decision,
            "reason": reason,
        }
    )
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_security_event(
            source=source,
            sha256=hashlib.sha256(payload.encode()).hexdigest(),
            score=score,
            verdict=verdict,
            rule_ids=rule_ids or [],
            decision=decision,
            reason=reason,
        )


def _args_json(arguments: dict) -> str:
    return json.dumps(arguments, sort_keys=True)


def execute_pi_tool(name: str, arguments: dict, session: PiSessionContext) -> dict:
    """Run one Pi-requested tool through all gates. Never raises."""
    try:
        return _execute_pi_tool(name, arguments, session)
    except Exception as exc:  # never break the bridge loop
        logger.exception("Pi tool gateway failed for '%s'", name)
        return {"error": f"Pi tool gateway failed for tool '{name}': {exc}"}


def _execute_pi_tool(name: str, arguments: dict, session: PiSessionContext) -> dict:
    recorder = get_current_recorder()
    run_id = recorder.run_id if recorder is not None else f"pi-{session.session_id}"
    args_for_hash = (
        _args_json(arguments) if isinstance(arguments, dict) else json.dumps(str(arguments))
    )

    # Gate 1: RESEARCH-only permit filter (same denial shape as run_chat).
    if name not in RESEARCH_TOOL_NAMES or not tool_is_permitted(name, LOCAL_CONTEXT):
        _record_security(
            session, name, args_for_hash, "action_blocked", f"tool not permitted: {name}"
        )
        return {"error": f"Tool is not permitted: {name}"}

    # Gate 2: schema validation + 8KB arg-bytes cap.
    invalid = _validate_tool_arguments(name, arguments)
    if invalid is not None:
        return {"error": invalid, "error_type": "invalid_tool_arguments"}
    if len(json.dumps(arguments)) > LOCAL_CONTEXT.tool_policy.max_arguments_bytes:
        return {
            "error": (
                "Tool arguments exceed the maximum size "
                f"({LOCAL_CONTEXT.tool_policy.max_arguments_bytes} bytes): {name}"
            ),
            "error_type": "invalid_tool_arguments",
        }

    # Gate 8 (reserve): one budget slot per call before any external work.
    # search_web is exempt from run budgets.
    if name != "search_web" and not session.budget.reserve_tool_call():
        return {"error": _BUDGET_EXHAUSTED_RESPONSE, "error_type": "budget_exhausted"}

    # Gate 3: intent firewall. No approval callback in this plan, so
    # portfolio-shaped calls are always denied (RESEARCH-only).
    intent_allowed, intent_reason = authorize_tool_call(name, arguments, session.run_security)
    if not intent_allowed:
        if TOOL_DOMAINS.get(name) == "portfolio_read":
            _record_security(
                session, name, args_for_hash, "action_blocked", intent_reason
            )
            return {
                "error": "Portfolio access is not authorized for this session",
                "error_type": "authorization_denied",
                "soft": True,
            }
        _record_security(session, name, args_for_hash, "action_blocked", intent_reason)
        return {
            "error": "Tool call exceeds original user intent",
            "error_type": "intent_denied",
            "soft": True,
        }

    # Gate 4: search_web egress; every other tool's private-pattern args check.
    if name == "search_web":
        decision = authorize_egress("exa", arguments, session.run_security)
        if not decision.allowed:
            _record_security(
                session, name, args_for_hash, "egress_blocked", decision.reason
            )
            return {
                "error": "Egress blocked: private data must not leave Stockbot",
                "error_type": "egress_denied",
                "soft": True,
            }
    else:
        hit = private_pattern_hit(args_for_hash)
        if hit:
            _record_security(session, name, args_for_hash, "action_blocked", hit)
            return {
                "error": "Tool arguments contain private data that must not be transmitted",
                "error_type": "private_args_denied",
                "soft": True,
            }

    # Gate 6: LOCAL_CONTEXT only, never a broker context, in this plan.
    t0 = time.perf_counter()
    t0_iso = datetime.now(timezone.utc).isoformat()
    result = execute_tool(name, arguments, PI_MODEL, context=LOCAL_CONTEXT)
    t1 = time.perf_counter()

    failed = _is_failed_result(result)
    soft = failed and result.get("soft") is True
    denied = failed and "not permitted" in str(result.get("error", ""))
    status = "completed" if not failed else ("denied" if denied else "failed")
    error_type = None
    error_message = None
    if failed:
        error_type = result.get("error_type") or (
            "permission_denied" if denied else "tool_error"
        )
        error_message = redact_text(str(result.get("error")))[:2000]
    meta = _tool_result_meta(result)
    if recorder is not None:
        tool_call_id = f"{run_id}:tc:{recorder.next_tool_seq()}"
        recorder.record_tool_call(
            tool_call_id=tool_call_id,
            round=0,
            tool_name=name,
            arguments_json=json.dumps(arguments),
            started_at=t0_iso,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status=status,
            result_row_count=meta.row_count,
            returned_count=meta.returned_count,
            truncated=meta.truncated,
            result_bytes=len(json.dumps(result)),
            result_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest(),
            source_names=json.dumps(meta.source_names),
            source_freshness=json.dumps(meta.source_freshness),
            as_of=meta.as_of,
            error_type=error_type,
            error_message=error_message,
        )
    else:
        tool_call_id = f"{run_id}:tc:0"

    if failed:
        if soft:
            return result
        # Gate 7 (hard failure): deterministic unavailable-data shape, no model call.
        return {
            "error": _unavailable_data_response([(name, result)]),
            "error_type": error_type or "tool_error",
        }

    # Gate 5: ingress scan on the rendered evidence (no quarantine_reader:
    # its nested OpenRouter completion has no main model in this harness).
    rendered = render_tool_result(
        result, max_bytes=LOCAL_CONTEXT.run_limits.max_tool_result_bytes
    )
    envelope = envelope_for_tool(name, result)
    outcome = prepare_context(envelope, rendered)
    if isinstance(outcome, QuarantinedContext):
        session.run_security.quarantined_items += 1
        _record_security(
            session,
            envelope.source,
            rendered,
            "quarantined" if outcome.verdict == "QUARANTINE" else "blocked",
            "; ".join(outcome.reasons) if outcome.reasons else None,
            score=outcome.score,
            verdict=outcome.verdict,
            rule_ids=list(outcome.rule_ids),
        )
        return {
            "error": (
                "Tool result withheld by Stockbot security gateway. "
                "No usable evidence was provided."
            ),
            "error_type": "ingress_blocked",
            "soft": True,
        }

    # Gate 7 (success path): DLP over what Pi receives, then record evidence.
    final_text = guard_response(outcome.text, session.run_security, run_id)
    if name == "search_web":
        session.run_security.data_labels.add("external")
    if envelope.sensitivity is Sensitivity.PRIVATE:
        session.run_security.data_labels.add("private")
    if name != "search_web" and not session.budget.add_evidence_tokens(len(final_text) // 4):
        return {"error": _BUDGET_EXHAUSTED_RESPONSE, "error_type": "budget_exhausted"}
    if recorder is not None:
        evidence_id = f"{run_id}:evid:{recorder.next_evidence_seq():04d}"
        recorder.record_evidence(
            evidence_id=evidence_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            round=0,
            tool_name=name,
            rendered_hash=hashlib.sha256(final_text.encode()).hexdigest(),
            rendered_bytes=len(final_text.encode("utf-8")),
            estimated_tokens=len(final_text) // 4,
            source_names=json.dumps(meta.source_names),
            source_freshness=json.dumps(meta.source_freshness),
            as_of=meta.as_of,
            rendered_text=redact_text(final_text),
        )
        recorder.record_security_event(
            source=envelope.source,
            sha256=hashlib.sha256(final_text.encode()).hexdigest(),
            score=None,
            verdict=None,
            rule_ids=[
                envelope.source,
                envelope.sensitivity.value,
                envelope.integrity.value,
            ],
            decision="allowed",
            reason=None,
        )
    return result
