"""Core tool-calling loop. Model-agnostic: the model is always a parameter."""

import hashlib
import json
import logging
import subprocess
import time
from collections.abc import Mapping
from datetime import date, datetime, timezone

import requests

from . import __version__
from .config import (
    OPENROUTER_BASE_URL,
    get_openrouter_api_key,
)
from .policy import ChatInputError, ChatPolicy, PUBLIC_CHAT_ROLES, RequestContext
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT
from .redact import redact_json, redact_text, redact_value
from .runtime import (
    AgentState,
    BudgetRemaining,
    EventType,
    ResearchPlan,
    ResearchRequest,
    ResearchResult,
    ToolCall,
)
from .storage.ids import request_id, run_id
from .storage.runs import (
    RunRecorder,
    get_runs_db_path,
    reset_current_recorder,
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


def _update_budget(state: AgentState, limits, elapsed_seconds: float, recorder) -> None:
    """Recompute remaining budget from the typed state and recorder counters."""
    state.budget_remaining = BudgetRemaining(
        rounds=max(0, limits.max_rounds - state.round),
        tool_calls=max(0, limits.max_tool_calls - len(state.tool_calls)),
        model_calls=max(0, limits.max_model_calls - recorder.model_calls),
        runtime_seconds=max(0.0, limits.max_runtime - elapsed_seconds),
        evidence_tokens=max(0, limits.max_evidence_tokens - recorder.evidence_tokens),
    )


def _tool_source_meta(result) -> tuple[list[str], dict]:
    """Best-effort top-level source name + freshness extraction from a result."""
    if not isinstance(result, dict) or result.get("source") is None:
        return [], {}
    source = str(result["source"])
    source_names = [source]
    source_freshness: dict[str, str] = {}
    for key in ("as_of", "freshness", "retrieved_at"):
        if key in result:
            source_freshness[source] = str(result[key])
            break
    return source_names, source_freshness


def _tool_row_count(result) -> int:
    """Largest top-level list in a result, as a row-count estimate."""
    if not isinstance(result, dict):
        return 0
    return max((len(value) for value in result.values() if isinstance(value, list)), default=0)


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
        claims=[],
        evidence_refs=list(state.evidence),
        counterevidence_refs=[],
        confidence=1.0 if status == "completed" else 0.0,
        data_freshness=data_freshness,
        unresolved_questions=[],
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


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
    Returns:
        The assistant's final text response (or a tuple with the trace,
        or the ResearchResult when return_result is set).
    """
    if mode not in ("quick", "research", "demo"):
        raise ValueError(f"invalid mode: {mode!r}")
    if model not in policy.allowed_models:
        raise ChatInputError("requested model is not allowed")
    normalized_messages = _normalize_public_messages(messages, policy)

    request = ResearchRequest(
        request_id=request_id(),
        question=normalized_messages[-1]["content"],
        created_at=datetime.now(timezone.utc).isoformat(),
        as_of=context.as_of or date.today().isoformat(),
        principal=context.principal_id,
        capabilities=tuple(sorted(cap.name for cap in context.capabilities)),
        preferred_model=model,
        mode=mode,
    )
    plan = ResearchPlan(
        question=request.question,
        entities=[],
        as_of=request.as_of,
        hypotheses=[],
        required_data=[],
    )
    state = AgentState(
        run_id=run_id(),
        plan=plan,
        budget_remaining=BudgetRemaining(
            rounds=context.run_limits.max_rounds,
            tool_calls=context.run_limits.max_tool_calls,
            model_calls=context.run_limits.max_model_calls,
            runtime_seconds=context.run_limits.max_runtime,
            evidence_tokens=context.run_limits.max_evidence_tokens,
        ),
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

    # The trusted policy remains the only system message. Public history has
    # already been normalized to prevent tool metadata or privileged roles.
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + normalized_messages
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

    started = time.perf_counter()
    token = set_current_recorder(recorder)
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
                EventType.PLAN_CREATED, round=0, metadata={"plan": plan.to_dict()}
            )
            try:
                while True:
                    _update_budget(
                        state, context.run_limits, time.perf_counter() - started, recorder
                    )
                    if (
                        state.budget_remaining.model_calls <= 0
                        or state.budget_remaining.runtime_seconds <= 0
                    ):
                        return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")

                    recorder.current_round = state.round
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
                    data = _call_openrouter(
                        model, msgs, available_tools, policy.upstream_timeout_seconds
                    )
                    t1 = time.perf_counter()
                    choice = data["choices"][0]
                    message = choice["message"]
                    tool_calls = message.get("tool_calls") or []
                    estimated_cost = recorder.record_model_call(
                        round=state.round,
                        provider=context.model_policy.provider,
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

                    if not tool_calls:
                        text = message.get("content") or ""
                        return _finish(text, "completed")

                    if state.round >= context.run_limits.max_rounds:
                        # Cap reached; force a final answer without more tools.
                        msgs.append({"role": "user", "content": (
                            "Tool call limit reached. Answer with what you have now, "
                            "or say you don't have the data."
                        )})
                        recorder.current_round = state.round
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
                        data = _call_openrouter(
                            model, msgs, available_tools, policy.upstream_timeout_seconds
                        )
                        t1 = time.perf_counter()
                        choice = data["choices"][0]
                        estimated_cost = recorder.record_model_call(
                            round=state.round,
                            provider=context.model_policy.provider,
                            model=model,
                            started_at=t0_iso,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                            usage=data.get("usage"),
                            finish_reason=choice.get("finish_reason"),
                            tool_call_count=len(choice["message"].get("tool_calls") or []),
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
                        text = choice["message"].get("content") or ""
                        return _finish(text, "partial")

                    # Append the assistant message that requested the tools.
                    msgs.append(message)
                    failed_tools: list[tuple[str, dict]] = []
                    if state.budget_remaining.tool_calls <= 0:
                        return _finish(_BUDGET_EXHAUSTED_RESPONSE, "budget_exhausted")
                    for tc in tool_calls:
                        fn = tc["function"]
                        name = fn["name"]
                        try:
                            arguments = json.loads(fn["arguments"] or "{}")
                        except json.JSONDecodeError:
                            arguments = {}
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
                        else:
                            result = execute_tool(name, arguments, model, context=context)
                        t1 = time.perf_counter()
                        failed = _is_failed_result(result)
                        denied = failed and "not permitted" in str(result.get("error", ""))
                        status = "completed" if not failed else ("denied" if denied else "failed")
                        error_type = None
                        error_message = None
                        if failed:
                            error_type = "permission_denied" if denied else "tool_error"
                            error_message = redact_text(str(result.get("error")))[:2000]
                        tool_call_id = f"{state.run_id}:tc:{recorder.next_tool_seq()}"
                        source_names, source_freshness = _tool_source_meta(result)
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
                            result_row_count=_tool_row_count(result),
                            result_bytes=len(json.dumps(result)),
                            result_hash=hashlib.sha256(
                                json.dumps(result, sort_keys=True).encode()
                            ).hexdigest(),
                            source_names=json.dumps(source_names),
                            source_freshness=json.dumps(source_freshness),
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
                            result_bytes=call.result_bytes,
                            result_hash=call.result_hash,
                            source_names=call.source_names,
                            source_freshness=call.source_freshness,
                            error_type=error_type,
                            error_message=error_message,
                        )
                        result_summary = redact_json(json.dumps(result))
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
                            state.plan.required_data.append(
                                {"tool": name, "arguments": redact_value(arguments)}
                            )
                            evidence_id = (
                                f"{state.run_id}:evid:{recorder.next_evidence_seq():04d}"
                            )
                            state.evidence.append(evidence_id)
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
                            state.failures.append(
                                {"tool": name, "error": result.get("error")}
                            )
                            failed_tools.append((name, result))
                        msgs.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": render_tool_result(result),
                        })
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
        reset_current_recorder(token)

    # Unreachable, but keep a safe return.
    if return_result:
        return ResearchResult(
            run_id=state.run_id,
            answer="",
            claims=[],
            evidence_refs=[],
            counterevidence_refs=[],
            confidence=0.0,
            data_freshness={},
            unresolved_questions=[],
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
    if return_detailed_trace:
        return "", detailed_trace
    if return_trace:
        return "", tool_trace
    return ""
