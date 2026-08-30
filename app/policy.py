"""Application-level authorization and chat policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Capability(StrEnum):
    RESEARCH = "research"
    PORTFOLIO_READ = "portfolio_read"


@dataclass(frozen=True)
class RequestContext:
    principal: str
    capabilities: frozenset[Capability]


LOCAL_CONTEXT = RequestContext(
    principal="local",
    capabilities=frozenset({Capability.RESEARCH, Capability.PORTFOLIO_READ}),
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
