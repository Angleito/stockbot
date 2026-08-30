"""Core tool-calling loop. Model-agnostic: the model is always a parameter."""

import json
import logging
from contextvars import ContextVar

import requests

from .config import (
    OPENROUTER_BASE_URL,
    get_openrouter_api_key,
    get_openrouter_timeout_seconds,
)
from .prompts import SYSTEM_PROMPT
from .tool_render import render_tool_result
from .tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8

# Per-run context avoids mutating the module-level tool schema when concurrent
# HTTP requests have different authorization scopes.
_openrouter_tools: ContextVar[list] = ContextVar("openrouter_tools", default=TOOLS)

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


def _call_openrouter(model: str, messages: list) -> dict:
    resp = requests.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {get_openrouter_api_key()}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "tools": _openrouter_tools.get()},
        timeout=get_openrouter_timeout_seconds(),
    )
    if resp.status_code >= 400:
        logger.error("OpenRouter error (%s): %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def run_chat(
    messages: list,
    model: str,
    return_trace: bool = False,
    return_detailed_trace: bool = False,
    allowed_tool_names: frozenset[str] | None = None,
):
    """Run the tool-calling loop until the model returns plain text.

    Args:
        messages: conversation history (list of OpenAI-format messages).
        model: OpenRouter model string, e.g. "google/gemini-3.7-flash".
        return_trace: if True, returns (text, tool_trace) where tool_trace is
            the list of tool names called — used by the eval suite.
        return_detailed_trace: if True, returns (text, detailed_trace) where
            each entry is {"name": tool, "arguments": parsed args}. Takes
            precedence over return_trace.
        allowed_tool_names: optional per-request capability boundary. Tools not
            in this set are neither advertised to nor executed for the model.

    Returns:
        The assistant's final text response (or a tuple with the trace).
    """
    # The Stockbot policy is always the highest-priority message. HTTP callers
    # are additionally restricted to user/assistant history at the boundary.
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + list(messages)
    permitted_tool_names = (
        frozenset(tool["function"]["name"] for tool in TOOLS)
        if allowed_tool_names is None
        else frozenset(allowed_tool_names)
    )
    available_tools = [
        tool for tool in TOOLS if tool["function"]["name"] in permitted_tool_names
    ]
    _openrouter_tools.set(available_tools)

    want_trace = return_trace or return_detailed_trace
    tool_trace = []
    detailed_trace = []
    for _round in range(MAX_TOOL_ROUNDS + 1):
        data = _call_openrouter(model, msgs)
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
            data = _call_openrouter(model, msgs)
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
                result = execute_tool(name, arguments, model)
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
