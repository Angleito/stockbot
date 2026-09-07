"""Application-level authorization policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .config import get_data_root


class Capability(StrEnum):
    RESEARCH = "research"
    BROKER_MARKET_READ = "broker_market_read"
    PORTFOLIO_READ = "portfolio_read"


@dataclass(frozen=True)
class RunLimits:
    """Run budget for a single research run.

    max_tool_result_bytes mirrors tool_render.MAX_TOOL_MESSAGE_BYTES.
    """

    max_tool_calls: int = 64
    max_runtime: float = 600.0     # seconds
    max_tool_result_bytes: int = 64 * 1024   # == tool_render.MAX_TOOL_MESSAGE_BYTES
    max_evidence_tokens: int = 48_000


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: frozenset[str] | None = None    # None -> capability-derived registry
    max_arguments_bytes: int = 8 * 1024
    deny_unpermitted: bool = True


@dataclass(frozen=True)
class RequestContext:
    principal_id: str
    capabilities: frozenset[Capability]
    tool_policy: ToolPolicy = ToolPolicy()
    data_root: Path = field(default_factory=get_data_root)
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

