"""Core tool-calling loop. Model-agnostic: the model is always a parameter."""

import hashlib
import json
import logging
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone

import requests

from . import __version__
from .config import OPENROUTER_BASE_URL, get_openrouter_api_key
from .policy import ChatInputError, ChatPolicy, PUBLIC_CHAT_ROLES, RequestContext
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .redact import redact_json, redact_text, redact_value
from .security import prompt_injection
from .security.action_policy import (
    TOOL_DOMAINS,
    authorize_egress,
    authorize_tool_call,
    private_pattern_hit,
)
from .security.context import RunSecurityContext, SessionAuthorization, classify_intent
from .security.context_builder import ContextBuilder
from .security.response_guard import guard_response
from .runtime import (
    AgentState,
    BudgetRemaining,
    EventType,
    ExecutionBudget,
    ResearchPlan,
    ResearchRequest,
    ResearchResult,
    ToolCall,
    ToolResultMeta,
)
from .storage.ids import request_id, run_id
from .storage.runs import (
    RunRecorder,
    get_runs_db_path,
    model_error_category,
    reset_current_budget,
    reset_current_recorder,
    set_current_budget,
    set_current_recorder,
)
from .tool_render import render_tool_result
from .tools import TOOL_REGISTRY_VERSION, execute_tool, tools_for_capabilities

logger = logging.getLogger(__name__)

_UNAVAILABLE_HEADER = (
    "The requested data is unavailable: one or more tool calls failed or "
    "returned no data, so the exact values cannot be provided. No values "
    "are estimated, derived, or substituted."
)

_UNAVAILABLE_NEXT_STEP = (
    "Next step: correct the request (dataset, fields, filters, or "
    "credentials) and retry, or use a different dataset/source. The error "
    "above states exactly what failed."
)

_BUDGET_EXHAUSTED_RESPONSE = (
    "The research budget was exhausted before a final answer could be "
    "produced. Retry with a narrower question or fewer tool calls."
)

_git_sha_cache: str | None = None


def _is_failed_result(result) -> bool:
    """A tool result is a failure when it carries an explicit error."""
    return isinstance(result, dict) and bool(result.get("error"))


def _unavailable_data_response(failed: list[tuple[str, dict]]) -> str:
    """Deterministic user-facing response when any tool call failed.

    Built from the rendered error context of each failed tool (name,
    dataset/source, HTTP status, sanitized FINRA response, environment)
    plus a next step. The model is never consulted, so nothing can be
    invented, derived, or substituted to fill the gap.
    """
    lines = [_UNAVAILABLE_HEADER, ""]
    for name, result in failed:
        lines.append(f"Tool: {name}")
        for line in render_tool_result(result).splitlines():
            lines.append("  " + line)
        lines.append("")
    lines.append(_UNAVAILABLE_NEXT_STEP)
    return "\n".join(lines)


def _call_openrouter(
    model: str,
    messages: list,
    tools: list[dict],
    timeout_seconds: float,
) -> dict:
    resp = requests.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {get_openrouter_api_key()}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "tools": tools},
        timeout=timeout_seconds,
    )
    if resp.status_code >= 400:
        logger.error(
            "OpenRouter error (%s): %s", resp.status_code, redact_text(resp.text)
        )
    resp.raise_for_status()
    return resp.json()


def _normalize_public_messages(messages: list, policy: ChatPolicy) -> list[dict[str, str]]:
    """Reject privileged roles and metadata before they reach the agent loop."""
    if not isinstance(messages, list) or not messages:
        raise ChatInputError("messages must be a non-empty list")
    if len(messages) > policy.max_messages:
        raise ChatInputError("too many messages")

    normalized = []
    for message in messages:
        if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
            raise ChatInputError("messages may contain only role and content")
        role = message["role"]
        content = message["content"]
        if not isinstance(role, str) or role not in PUBLIC_CHAT_ROLES:
            raise ChatInputError(f"message role is not permitted: {role!r}")
        if not isinstance(content, str):
            raise ChatInputError("message content must be a string")
        content = content.strip()
        if not content:
            raise ChatInputError("message content must not be empty")
        if len(content) > policy.max_message_chars:
            raise ChatInputError("message content is too long")
        normalized.append({"role": role, "content": content})
    return normalized


def _git_sha() -> str:
    """Short HEAD sha for observability; 'unknown' on any failure (cached)."""
    global _git_sha_cache
    if _git_sha_cache is None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            _git_sha_cache = result.stdout.strip() or "unknown"
        except Exception:
            _git_sha_cache = "unknown"
    return _git_sha_cache


def _update_budget(state: AgentState, budget: ExecutionBudget) -> None:
    """Refresh the typed BudgetRemaining view from the execution budget."""
    state.budget_remaining = budget.remaining(state.round)


_NON_DATA_LIST_KEYS = frozenset({"source_records", "warnings", "metrics", "trends"})


def _tool_result_meta(result) -> ToolResultMeta:
    """Best-effort telemetry envelope for a tool result: row counts,
    truncation, source name, and freshness."""
    if not isinstance(result, dict):
        return ToolResultMeta(0, None, False, None, [], {})
    source = result.get("source") or result.get("dataset_id") or result.get("dataset")
    source_names = [str(source)] if source is not None else []
    freshness_value = next(
        (result[k] for k in ("data_freshness", "freshness", "as_of_date", "as_of", "retrieved_at")
         if result.get(k) is not None),
        None)
    source_freshness = (
        {str(source): str(freshness_value)} if source is not None and freshness_value is not None else {})
    as_of = next((result[k] for k in ("as_of_date", "as_of", "retrieved_at")
                  if result.get(k) is not None), None)
    returned_count = result.get("returned_count") if isinstance(result.get("returned_count"), int) else None
    row_count = result.get("row_count") if isinstance(result.get("row_count"), int) else max(
        (len(v) for k, v in result.items() if isinstance(v, list) and k not in _NON_DATA_LIST_KEYS),
        default=0)
    total = result.get("total_records")
    truncated = (
        bool(result.get("truncated"))
        or result.get("may_have_more") is True
        or (returned_count is not None and isinstance(total, int) and returned_count < total))
    return ToolResultMeta(row_count, returned_count, truncated,
                          str(as_of) if as_of is not None else None, source_names, source_freshness)


def _finish_run(
    recorder: RunRecorder,
    state: AgentState,
    text: str,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
) -> ResearchResult:
    """Record the finalization events, close the run row, build the result."""
    recorder.record_event(EventType.FINALIZATION_STARTED, round=state.round)
    recorder.record_event(
        EventType.FINAL_ANSWER_CREATED,
        round=state.round,
        result_summary=text[:2000],
        metadata={
            "answer": text,
            "answer_hash": hashlib.sha256(text.encode()).hexdigest(),
        },
    )
    recorder.complete(
        status=status,
        answer=text,
        error_type=error_type,
        error_message=error_message,
    )
    data_freshness: dict[str, str] = {}
    for call in state.tool_calls:
        try:
            freshness = json.loads(call.source_freshness or "{}")
        except (TypeError, ValueError):
            freshness = {}
        if isinstance(freshness, dict):
            data_freshness.update(freshness)
    return ResearchResult(
        run_id=state.run_id,
        answer=text,
        evidence_refs=list(state.evidence),
        groundedness=(
            "grounded"
            if (status == "completed" and state.evidence)
            else ("unverified" if status == "completed" else "partial")
        ),
        data_freshness=data_freshness,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


def _request_and_record(
    recorder, state, model, msgs, available_tools, policy, budget
) -> tuple[dict, float, str, float, list] | None:
    """Reserve, call, and record one model round; None when the budget is
    exhausted before the call."""
    if not budget.reserve_model_call():
        return None
    t0 = time.perf_counter()
    t0_iso = datetime.now(timezone.utc).isoformat()
    recorder.record_event(
        EventType.MODEL_REQUESTED,
        round=state.round,
        model=model,
        metadata={
            "message_count": len(msgs),
            "tool_count": len(available_tools),
        },
    )
    try:
        data = _call_openrouter(
            model, msgs, available_tools, policy.upstream_timeout_seconds
        )
    except Exception as exc:
        t1 = time.perf_counter()
        recorder.record_event(
            EventType.MODEL_FAILED,
            round=state.round,
            model=model,
            started_at=t0_iso,
            completed_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=(t1 - t0) * 1000.0,
            metadata={
                "provider": recorder.provider,
                "error_type": type(exc).__name__,
                "error_category": model_error_category(exc),
            },
        )
        recorder.record_model_call(
            round=state.round,
            provider=recorder.provider,
            model=model,
            started_at=t0_iso,
            completed_at=datetime.now(timezone.utc).isoformat(),
            usage=None,
            status="failed",
            error_type=type(exc).__name__,
            error_category=model_error_category(exc),
        )
        raise
    t1 = time.perf_counter()
    choice = data["choices"][0]
    tool_calls = choice["message"].get("tool_calls") or []
    estimated_cost = recorder.record_model_call(
        round=state.round,
        provider=recorder.provider,
        model=model,
        started_at=t0_iso,
        completed_at=datetime.now(timezone.utc).isoformat(),
        usage=data.get("usage"),
        finish_reason=choice.get("finish_reason"),
        tool_call_count=len(tool_calls),
        provider_request_id=data.get("id"),
    )
    recorder.record_event(
        EventType.MODEL_RESPONDED,
        round=state.round,
        model=model,
        started_at=t0_iso,
        completed_at=datetime.now(timezone.utc).isoformat(),
        duration_ms=(t1 - t0) * 1000.0,
        metadata={
            "finish_reason": choice.get("finish_reason"),
            "estimated_cost": estimated_cost,
        },
    )
    return (data, t0, t0_iso, estimated_cost, tool_calls)


def run_chat(
    messages: list,
    model: str,
    *,
    context: RequestContext,
    policy: ChatPolicy,
    return_trace: bool = False,
    return_detailed_trace: bool = False,
    return_result: bool = False,
    mode: str = "quick",
    approve_portfolio: Callable[[str, dict], bool] | None = None,
    session_authorization: SessionAuthorization | None = None,
):
    """Run the tool-calling loop until the model returns plain text.

    Args:
        messages: untrusted public conversation history (user/assistant only).
        model: OpenRouter model string, e.g. "google/gemini-3.7-flash".
        context: principal and application capabilities for this run.
        policy: server-controlled bounds, model allowlist, and upstream timeout.
        return_trace: if True, returns (text, tool_trace) where tool_trace is
            the list of tool names called — used by the eval suite.
        return_detailed_trace: if True, returns (text, detailed_trace) where
            each entry is {"name": tool, "arguments": parsed args}. Takes
            precedence over return_trace.
        return_result: if True, returns the typed ResearchResult. Takes
            precedence over both trace flags.
        mode: "quick" | "research" | "demo" — recorded on the run.
        approve_portfolio: first-use portfolio approval `(tool_name, args) -> bool`;
            True grants `portfolio_read` for the rest of the run and, when the
            caller holds the grant, for later runs seeded with it. None/False or
            a raised exception denies that call softly.
        session_authorization: explicit session grant seeding this run;
            deny-by-default when None.
    Returns:
        The assistant's final text response (or a tuple with the trace,
        or the ResearchResult when return_result is set).
    """
    if mode not in ("quick", "research", "demo"):
        raise ValueError(f"invalid mode: {mode!r}")
    if model not in policy.allowed_models:
        raise ChatInputError("requested model is not allowed")
    normalized_messages = _normalize_public_messages(messages, policy)

    # Original intent is the last user turn plus base research domains; it
    # gates every tool call (action firewall) together with the explicit
    # session grant. `portfolio_read` lives only in `authorization`, never in
    # `permitted_domains`.
    run_security = RunSecurityContext(
        original_intent=classify_intent(
            [m["content"] for m in normalized_messages if m["role"] == "user"]
        ),
        capabilities=frozenset(cap.name for cap in context.capabilities),
        authorization=session_authorization
        if session_authorization is not None
        else SessionAuthorization(),
    )

    # The question is redacted before it enters the observability path
    # (agent_runs.question column and RUN_STARTED/RESEARCH_CONTEXT_CREATED
    # metadata); the raw user message stays in the conversation sent to the
    # model.
    request = ResearchRequest(
        request_id=request_id(),
        question=redact_json(normalized_messages[-1]["content"]),
        created_at=datetime.now(timezone.utc).isoformat(),
        as_of=context.as_of or date.today().isoformat(),
        principal=context.principal_id,
        capabilities=tuple(sorted(cap.name for cap in context.capabilities)),
        preferred_model=model,
        mode=mode,
    )
    plan = ResearchPlan(
        question=request.question,
        as_of=request.as_of,
    )
    budget = ExecutionBudget(
        max_rounds=context.run_limits.max_rounds,
        max_tool_calls=context.run_limits.max_tool_calls,
        max_model_calls=context.run_limits.max_model_calls,
        max_runtime=context.run_limits.max_runtime,
        max_evidence_tokens=context.run_limits.max_evidence_tokens,
        max_exa_searches=context.run_limits.max_exa_searches,
    )
    state = AgentState(
        run_id=run_id(),
        plan=plan,
        budget_remaining=budget.remaining(0),
    )
    recorder = RunRecorder(
        run_id=state.run_id,
        request_id=request.request_id,
        question=request.question,
        as_of=request.as_of,
        model=model,
        provider=context.model_policy.provider,
        model_parameters={"timeout_seconds": policy.upstream_timeout_seconds},
        agent_version=__version__,
        prompt_version=PROMPT_VERSION,
        tool_registry_version=TOOL_REGISTRY_VERSION,
        git_sha=_git_sha(),
        data_root=context.data_root,
        max_result_bytes=context.run_limits.max_tool_result_bytes,
    )

    # The trusted policy remains the only system message; every tool result
    # passes the security gateway before it may enter model context. Public
    # history has already been normalized to prevent tool metadata or
    # privileged roles. Full history is staged in order INSIDE the recorder
    # block below (assistant turns need the recorder for security events).
    context_builder = ContextBuilder(run_security=run_security, model=model)
    context_builder.add_system(SYSTEM_PROMPT)
    available_tools = tools_for_capabilities(context.capabilities)
    if context.tool_policy.allowed_tools is not None:
        available_tools = [
            tool
            for tool in available_tools
            if tool["function"]["name"] in context.tool_policy.allowed_tools
        ]
    permitted_tool_names = frozenset(
        tool["function"]["name"] for tool in available_tools
    )

    want_trace = return_trace or return_detailed_trace
    tool_trace = []
    detailed_trace = []

    def _finish(text: str, status: str, *, error_type=None, error_message=None):
        # Response DLP runs on every final answer path (completed, partial,
        # budget_exhausted, unavailable-data). The recorder is active here.
        text = guard_response(text, run_security, state.run_id)
        if text and run_security.private_ingress:
            text += "\n\nNote: this answer used portfolio data you approved for this session."
        result = _finish_run(
            recorder, state, text, status,
            error_type=error_type, error_message=error_message,
        )
        if return_result:
            return result
        if return_detailed_trace:
            return result.answer, detailed_trace
        if return_trace:
            return result.answer, tool_trace
        return result.answer

    recorder_token = set_current_recorder(recorder)
    budget_token = set_current_budget(budget)
    try:
        with recorder:
            recorder.record_event(
                EventType.RUN_STARTED,
                round=0,
                model=model,
                metadata={
                    "question": request.question,
                    "mode": mode,
                    "principal_id": request.principal,
                    "as_of": request.as_of,
                    "request_id": request.request_id,
                },
            )
            recorder.record_event(
                EventType.RESEARCH_CONTEXT_CREATED,
                round=0,
                metadata={"question": request.question, "as_of": request.as_of},
            )
            # Conversation history keeps its original interleaving; assistant
            # turns are untrusted history scanned for prompt injection.
            for message in normalized_messages:
                if message["role"] == "user":
                    context_builder.add_user(message["content"])
                elif message["role"] == "assistant":
                    assessment = prompt_injection.assess(message["content"])
                    if assessment.verdict != "ALLOW":
                        context_builder.add_assistant(
                            "[Assistant message withheld by Stockbot security gateway.]"
                        )
                        recorder.record_security_event(
                            source="assistant_history",
                            sha256=hashlib.sha256(
                                message["content"].encode()
                            ).hexdigest(),
                            score=assessment.score,
                            verdict=assessment.verdict,
                            rule_ids=list(assessment.matched_rules),
                            decision=(
                                "blocked" if assessment.verdict == "BLOCK" else "quarantined"
                            ),
                            reason="; ".join(assessment.reasons),
                        )
                    else:
                        context_builder.add_assistant(message["content"])
            msgs = context_builder.render_for_model()
            try:
                while True:
                    _update_budget(state, budget)
                    if (
                        budget.model_calls_remaining() <= 0
                        or budget.runtime_remaining() <= 0
                        or budget.evidence_remaining() <= 0
                    ):
                        return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")

                    recorder.current_round = state.round
                    result = _request_and_record(
                        recorder, state, model, msgs, available_tools, policy, budget
                    )
                    if result is None:
                        return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")
                    data, t0, t0_iso, estimated_cost, tool_calls = result
                    message = data["choices"][0]["message"]

                    if not tool_calls:
                        text = message.get("content") or ""
                        return _finish(text, "completed")

                    if state.round >= context.run_limits.max_rounds:
                        # Cap reached; force a final answer without more tools.
                        context_builder.add_user(
                            "Tool call limit reached. Answer with what you have now, "
                            "or say you don't have the data."
                        )
                        recorder.current_round = state.round
                        result = _request_and_record(
                            recorder, state, model, msgs, available_tools, policy, budget
                        )
                        if result is None:
                            return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")
                        data, t0, t0_iso, estimated_cost, tool_calls = result
                        text = data["choices"][0]["message"].get("content") or ""
                        return _finish(text, "partial")

                    # Append the assistant message that requested the tools.
                    context_builder.add_assistant(message)
                    failed_tools: list[tuple[str, dict]] = []
                    if budget.tool_calls_remaining() <= 0:
                        return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")
                    for tc in tool_calls:
                        fn = tc["function"]
                        name = fn["name"]
                        if not budget.reserve_tool_call():
                            return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")
                        try:
                            arguments = json.loads(fn["arguments"] or "{}")
                            malformed = False
                        except json.JSONDecodeError:
                            arguments = fn["arguments"] or ""
                            malformed = True
                        if malformed:
                            result = {
                                "error": f"Tool arguments are not valid JSON for tool '{name}'",
                                "error_type": "invalid_tool_arguments",
                            }
                        else:
                            result = None
                        logger.info("Tool call: %s(%s)", name, redact_value(arguments))
                        if want_trace:
                            tool_trace.append(name)
                            detailed_trace.append({"name": name, "arguments": arguments})
                        recorder.record_event(
                            EventType.TOOL_REQUESTED,
                            round=state.round,
                            tool_name=name,
                            arguments=arguments,
                        )
                        if not malformed:
                            recorder.record_event(
                                EventType.TOOL_STARTED,
                                round=state.round,
                                tool_name=name,
                                arguments=arguments,
                            )
                            t0 = time.perf_counter()
                            t0_iso = datetime.now(timezone.utc).isoformat()
                            if name not in permitted_tool_names:
                                # A model must not be able to invoke a tool that was
                                # omitted from its schema (for example, a portfolio
                                # tool for a user without that authorization).
                                result = {"error": f"Tool is not permitted: {name}"}
                            elif (
                                context.tool_policy.max_arguments_bytes
                                and len(json.dumps(arguments))
                                > context.tool_policy.max_arguments_bytes
                            ):
                                result = {
                                    "error": (
                                        f"Tool arguments exceed the maximum size "
                                        f"({context.tool_policy.max_arguments_bytes} bytes): {name}"
                                    )
                                }
                            elif name == "search_web" and not budget.reserve_exa_search():
                                result = {
                                    "error": (
                                        f"Exa search budget exhausted "
                                        f"(max {context.run_limits.max_exa_searches} per run)"
                                    ),
                                    "source": "exa",
                                    "soft": True,
                                }
                            else:
                                intent_allowed, intent_reason = authorize_tool_call(
                                    name, arguments, run_security
                                )
                                if not intent_allowed and TOOL_DOMAINS.get(name) == "portfolio_read":
                                    approved = False
                                    if approve_portfolio is not None:
                                        try:
                                            approved = bool(approve_portfolio(name, arguments))
                                        except Exception:
                                            approved = False
                                    if approved:
                                        run_security.authorization = SessionAuthorization(
                                            portfolio_read=True
                                        )
                                        intent_allowed, intent_reason = authorize_tool_call(
                                            name, arguments, run_security
                                        )
                                if not intent_allowed:
                                    result = {
                                        "error": "Tool call exceeds original user intent",
                                        "error_type": "intent_denied",
                                        "soft": True,
                                    }
                                    recorder.record_security_event(
                                        source=name,
                                        sha256=hashlib.sha256(
                                            json.dumps(arguments, sort_keys=True).encode()
                                        ).hexdigest(),
                                        score=None,
                                        verdict=None,
                                        rule_ids=[],
                                        decision="action_blocked",
                                        reason=intent_reason,
                                    )
                                elif name == "search_web":
                                    decision = authorize_egress(
                                        "exa", arguments, run_security
                                    )
                                    if not decision.allowed:
                                        result = {
                                            "error": (
                                                "Egress blocked: private data must not "
                                                "leave Stockbot"
                                            ),
                                            "error_type": "egress_denied",
                                            "soft": True,
                                        }
                                        recorder.record_security_event(
                                            source=name,
                                            sha256=hashlib.sha256(
                                                json.dumps(arguments, sort_keys=True).encode()
                                            ).hexdigest(),
                                            score=None,
                                            verdict=None,
                                            rule_ids=[],
                                            decision="egress_blocked",
                                            reason=decision.reason,
                                        )
                                    else:
                                        result = execute_tool(
                                            name, arguments, model, context=context
                                        )
                                else:
                                    hit = private_pattern_hit(
                                        json.dumps(arguments, sort_keys=True)
                                    )
                                    if hit:
                                        result = {
                                            "error": (
                                                "Tool arguments contain private data "
                                                "that must not be transmitted"
                                            ),
                                            "error_type": "private_args_denied",
                                            "soft": True,
                                        }
                                        recorder.record_security_event(
                                            source=name,
                                            sha256=hashlib.sha256(
                                                json.dumps(arguments, sort_keys=True).encode()
                                            ).hexdigest(),
                                            score=None,
                                            verdict=None,
                                            rule_ids=[],
                                            decision="action_blocked",
                                            reason=hit,
                                        )
                                    else:
                                        result = execute_tool(
                                            name, arguments, model, context=context
                                        )
                            t1 = time.perf_counter()
                        else:
                            t0 = time.perf_counter()
                            t0_iso = datetime.now(timezone.utc).isoformat()
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
                        tool_call_id = f"{state.run_id}:tc:{recorder.next_tool_seq()}"
                        meta = _tool_result_meta(result)
                        call = ToolCall(
                            tool_call_id=tool_call_id,
                            run_id=state.run_id,
                            round=state.round,
                            tool_name=name,
                            tool_version=TOOL_REGISTRY_VERSION,
                            arguments_json=json.dumps(arguments),
                            started_at=t0_iso,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            duration_ms=(t1 - t0) * 1000.0,
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
                        state.tool_calls.append(call)
                        recorder.record_tool_call(
                            tool_call_id=tool_call_id,
                            round=state.round,
                            tool_name=name,
                            arguments_json=json.dumps(arguments),
                            started_at=t0_iso,
                            completed_at=call.completed_at,
                            status=status,
                            result_row_count=call.result_row_count,
                            returned_count=call.returned_count,
                            truncated=call.truncated,
                            result_bytes=call.result_bytes,
                            result_hash=call.result_hash,
                            source_names=call.source_names,
                            source_freshness=call.source_freshness,
                            as_of=call.as_of,
                            error_type=error_type,
                            error_message=error_message,
                        )
                        result_summary = redact_json(json.dumps(result))
                        rendered = render_tool_result(
                            result,
                            max_bytes=context.run_limits.max_tool_result_bytes,
                        )
                        recorder.record_event(
                            EventType.TOOL_COMPLETED if not failed else EventType.TOOL_FAILED,
                            round=state.round,
                            tool_name=name,
                            result_summary=result_summary,
                            success=not failed,
                            error_type=error_type,
                            metadata={"duration_ms": (t1 - t0) * 1000.0},
                        )
                        if not failed:
                            # Evidence is what the model actually receives: a
                            # result that would cross the evidence budget is a
                            # hard stop — no evidence record, and the tool
                            # message is never appended to the transcript.
                            allowed = context_builder.add_tool_result(
                                name, result, rendered, tc.get("id", "")
                            )
                            if allowed:
                                if TOOL_DOMAINS.get(name) == "portfolio_read":
                                    run_security.private_ingress = True
                                final_text = (
                                    context_builder.last_appended_text or rendered
                                )
                                if not budget.add_evidence_tokens(len(final_text) // 4):
                                    return _finish(
                                        _BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted"
                                    )
                                evidence_id = (
                                    f"{state.run_id}:evid:{recorder.next_evidence_seq():04d}"
                                )
                                state.evidence.append(evidence_id)
                                recorder.record_evidence(
                                    evidence_id=evidence_id,
                                    run_id=state.run_id,
                                    tool_call_id=tool_call_id,
                                    round=state.round,
                                    tool_name=name,
                                    rendered_hash=hashlib.sha256(
                                        final_text.encode()
                                    ).hexdigest(),
                                    rendered_bytes=len(final_text.encode("utf-8")),
                                    estimated_tokens=len(final_text) // 4,
                                    source_names=call.source_names,
                                    source_freshness=call.source_freshness,
                                    as_of=meta.as_of,
                                    rendered_text=redact_text(final_text),
                                )
                                recorder.record_event(
                                    EventType.EVIDENCE_ADDED,
                                    round=state.round,
                                    tool_name=name,
                                    result_summary=result_summary,
                                    success=True,
                                    evidence_ids=[evidence_id],
                                    metadata={"duration_ms": (t1 - t0) * 1000.0},
                                )
                        else:
                            if not soft:
                                state.failures.append(
                                    {"tool": name, "error": result.get("error")}
                                )
                                failed_tools.append((name, result))
                            # Error messages still reach the model, through the
                            # same gateway as successful results.
                            context_builder.add_tool_result(
                                name, result, rendered, tc.get("id", "")
                            )
                    if failed_tools:
                        # Strict failed-tool rule: stop the tool loop before another
                        # model completion can invent, derive, or substitute values.
                        # The deterministic unavailable-data response is returned
                        # as-is; successful sibling results stay in the transcript
                        # but never fill the failed request's gap.
                        return _finish(
                            _unavailable_data_response(failed_tools), "partial"
                        )
                    state.round += 1
            except Exception as exc:
                error_type = type(exc).__name__
                error_message = redact_text(str(exc))[:2000]
                recorder.record_event(
                    EventType.RUN_FAILED,
                    round=state.round,
                    metadata={
                        "error_type": error_type,
                        "error_message": error_message,
                    },
                )
                recorder.complete(
                    status="failed",
                    answer="",
                    error_type=error_type,
                    error_message=error_message,
                )
                raise
    finally:
        reset_current_recorder(recorder_token)
        reset_current_budget(budget_token)
