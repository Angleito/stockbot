"""Core tool-calling loop. Model-agnostic: the model is always a parameter."""

import json
import logging
from collections.abc import Mapping

import requests

from .config import (
    OPENROUTER_BASE_URL,
    get_openrouter_api_key,
)
from .policy import ChatInputError, ChatPolicy, PUBLIC_CHAT_ROLES, RequestContext
from .prompts import SYSTEM_PROMPT
from .tool_render import render_tool_result
from .tools import execute_tool, tools_for_capabilities

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8

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
        logger.error("OpenRouter error (%s): %s", resp.status_code, resp.text)
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


def run_chat(
    messages: list,
    model: str,
    *,
    context: RequestContext,
    policy: ChatPolicy,
    return_trace: bool = False,
    return_detailed_trace: bool = False,
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
    Returns:
        The assistant's final text response (or a tuple with the trace).
    """
    if model not in policy.allowed_models:
        raise ChatInputError("requested model is not allowed")
    normalized_messages = _normalize_public_messages(messages, policy)
    # The trusted policy remains the only system message. Public history has
    # already been normalized to prevent tool metadata or privileged roles.
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + normalized_messages
    available_tools = tools_for_capabilities(context.capabilities)
    permitted_tool_names = frozenset(
        tool["function"]["name"] for tool in available_tools
    )

    want_trace = return_trace or return_detailed_trace
    tool_trace = []
    detailed_trace = []
    for _round in range(MAX_TOOL_ROUNDS + 1):
        data = _call_openrouter(
            model, msgs, available_tools, policy.upstream_timeout_seconds
        )
        choice = data["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            text = message.get("content") or ""
            if return_detailed_trace:
                return text, detailed_trace
            if return_trace:
                return text, tool_trace
            return text

        if _round == MAX_TOOL_ROUNDS:
            # Cap reached; force a final answer without more tools.
            msgs.append({"role": "user", "content": (
                "Tool call limit reached. Answer with what you have now, "
                "or say you don't have the data."
            )})
            data = _call_openrouter(
                model, msgs, available_tools, policy.upstream_timeout_seconds
            )
            text = data["choices"][0]["message"].get("content") or ""
            if return_detailed_trace:
                return text, detailed_trace
            if return_trace:
                return text, tool_trace
            return text

        # Append the assistant message that requested the tools, then results.
        msgs.append(message)
        failed_tools: list[tuple[str, dict]] = []
        for tc in tool_calls:
            fn = tc["function"]
            name = fn["name"]
            try:
                arguments = json.loads(fn["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            logger.info("Tool call: %s(%s)", name, arguments)
            if want_trace:
                tool_trace.append(name)
                detailed_trace.append({"name": name, "arguments": arguments})
            if name not in permitted_tool_names:
                # A model must not be able to invoke a tool that was omitted
                # from its schema (for example, a portfolio tool for a user
                # without that authorization).
                result = {"error": f"Tool is not permitted: {name}"}
            else:
                result = execute_tool(name, arguments, model, context=context)
            if _is_failed_result(result):
                failed_tools.append((name, result))
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": render_tool_result(result),
            })
        if failed_tools:
            # Strict failed-tool rule: stop the tool loop before another
            # model completion can invent, derive, or substitute values.
            # The deterministic unavailable-data response is returned as-is;
            # successful sibling results stay in the transcript but never
            # fill the failed request's gap.
            response = _unavailable_data_response(failed_tools)
            if return_detailed_trace:
                return response, detailed_trace
            if return_trace:
                return response, tool_trace
            return response

    # Unreachable, but keep a safe return.
    if return_detailed_trace:
        return "", detailed_trace
    if return_trace:
        return "", tool_trace
    return ""
