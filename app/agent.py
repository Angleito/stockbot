"""Core tool-calling loop. Model-agnostic: the model is always a parameter."""

import json
import logging

import requests

from .config import OPENROUTER_BASE_URL, get_openrouter_api_key
from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, execute_tool

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8


def _call_openrouter(model: str, messages: list) -> dict:
    resp = requests.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {get_openrouter_api_key()}",
            "Content-Type": "application/json",
        },
        json={"model": model, "messages": messages, "tools": TOOLS},
        timeout=180,
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

    Returns:
        The assistant's final text response (or a tuple with the trace).
    """
    # Prepend the system prompt if not already present.
    msgs = list(messages)
    if not msgs or msgs[0].get("role") != "system":
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + msgs

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
            result = execute_tool(name, arguments, model)
            msgs.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result)[:100000],
            })

    # Unreachable, but keep a safe return.
    if return_detailed_trace:
        return "", detailed_trace
    if return_trace:
        return "", tool_trace
    return ""
