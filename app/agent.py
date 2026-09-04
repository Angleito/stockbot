"""Shared pure helpers for Pi-gateway tool dispatch (no LLM loop)."""

import subprocess
from collections.abc import Mapping

from .policy import ChatInputError, ChatPolicy, PUBLIC_CHAT_ROLES
from .runtime import ToolResultMeta
from .tool_render import render_tool_result

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
