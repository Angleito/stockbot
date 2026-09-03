"""Context typing and original-intent classification. stdlib only."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Sequence


class InstructionAuthority(StrEnum):
    SYSTEM = "system"
    USER = "user"
    NONE = "none"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"


class Integrity(StrEnum):
    CANONICAL = "canonical"
    AUTHENTICATED = "authenticated"
    EXTERNAL = "external"
    DERIVED = "derived"


class SecurityStatus(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    QUARANTINED = "quarantined"
    BLOCKED = "blocked"


class SourceType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"
    MCP = "mcp"
    FILING = "filing"
    WEB = "web"
    DATABASE = "database"
    CALCULATION = "calculation"


@dataclass(frozen=True)
class ContextEnvelope:
    """One item of context, typed and labeled before it may enter model context."""

    content: object
    source: str
    source_type: SourceType
    instruction_authority: InstructionAuthority
    sensitivity: Sensitivity
    integrity: Integrity
    external: bool
    retrieved_at: str | None
    security_status: SecurityStatus = SecurityStatus.PENDING


@dataclass(frozen=True)
class OriginalIntent:
    """The user's original research intent: the last user turn plus base domains.

    Authorization for `portfolio_read` comes only from explicit session
    approval (the `approve_portfolio` callback in `run_chat`), never from
    chat history.
    """

    request: str
    permitted_domains: frozenset[str]

def classify_intent(user_turns: Sequence[str]) -> OriginalIntent:
    """Deterministic classifier: the request is the last user turn; permitted
    domains are always financial/public-web research. `portfolio_read` is
    never granted here — only explicit session approval adds it.
    """
    turns = [t for t in user_turns if isinstance(t, str)]
    request = turns[-1] if turns else ""
    return OriginalIntent(
        request=request,
        permitted_domains=frozenset({"financial_research", "public_web_research"}),
    )


@dataclass
class RunSecurityContext:
    """Per-run security state: the original intent and what the gateway saw."""

    original_intent: OriginalIntent
    capabilities: frozenset[str]
    data_labels: set[str] = field(default_factory=set)
    quarantined_items: int = 0
    security_events: list[dict] = field(default_factory=list)
