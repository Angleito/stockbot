"""Application-level authorization and chat policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    RESEARCH = "research"
    BROKER_MARKET_READ = "broker_market_read"
    PORTFOLIO_READ = "portfolio_read"


@dataclass(frozen=True)
class RunLimits:
    """Run budget for a single research run.

    max_tool_result_bytes mirrors tool_render.MAX_TOOL_MESSAGE_BYTES.
    """

    max_rounds: int = 8            # replaces MAX_TOOL_ROUNDS
    max_tool_calls: int = 64
    max_model_calls: int = 32
    max_runtime: float = 600.0     # seconds
    max_tool_result_bytes: int = 64 * 1024   # == tool_render.MAX_TOOL_MESSAGE_BYTES
    max_evidence_tokens: int = 48_000


@dataclass(frozen=True)
class ModelPolicy:
    provider: str = "openrouter"
    allowed_models: frozenset[str] = frozenset()   # runtime view; HTTP layer enforces via ChatPolicy
    default_model: str | None = None
    timeout_seconds: float = 60.0
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: frozenset[str] | None = None    # None -> capability-derived registry
    max_arguments_bytes: int = 8 * 1024
    deny_unpermitted: bool = True


@dataclass(frozen=True)
class RequestContext:
    principal_id: str
    capabilities: frozenset[Capability]
    model_policy: ModelPolicy = ModelPolicy()
    tool_policy: ToolPolicy = ToolPolicy()
    data_root: Path = Path(__file__).resolve().parent.parent / "data"
    as_of: str | None = None
    run_limits: RunLimits = RunLimits()


LOCAL_CONTEXT = RequestContext(
    principal_id="local",
    capabilities=frozenset({Capability.RESEARCH}),
)

LOCAL_BROKER_CONTEXT = RequestContext(
    principal_id="local-broker",
    capabilities=frozenset({Capability.RESEARCH, Capability.BROKER_MARKET_READ, Capability.PORTFOLIO_READ}),
)

PUBLIC_CHAT_ROLES = frozenset({"user", "assistant"})


class ChatInputError(ValueError):
    """An untrusted message or model violates the server chat policy."""


@dataclass(frozen=True)
class ChatPolicy:
    allowed_models: frozenset[str]
    max_messages: int
    max_message_chars: int
    upstream_timeout_seconds: float
