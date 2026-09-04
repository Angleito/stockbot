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
class SessionAuthorization:
    """Explicit session grant for private domains. Never derived from chat text;
    only first-use approval creates it."""
    portfolio_read: bool = False

@dataclass
class SessionSecurityState:
    """Caller-held session state: the explicit grant plus whether PRIVATE
    content has entered session model context. Taint clears only when the
    process ends; there is no mid-process reset."""
    authorization: SessionAuthorization = field(default_factory=SessionAuthorization)
    private_context_seen: bool = False


@dataclass(frozen=True)
class OriginalIntent:
    """The user's original research intent: the last user turn plus base domains.

    `permitted_domains` never carries `portfolio_read`; portfolio authorization
    lives only in `SessionAuthorization`.
    """

    request: str
    permitted_domains: frozenset[str]

def classify_intent(user_turns: Sequence[str]) -> OriginalIntent:
    """Deterministic classifier: the request is the last user turn; permitted
    domains are always financial/public-web research. `portfolio_read` is
    never granted here — only explicit session approval creates a
    `SessionAuthorization`.
    """
    turns = [t for t in user_turns if isinstance(t, str)]
    request = turns[-1] if turns else ""
    return OriginalIntent(
        request=request,
        permitted_domains=frozenset({"financial_research", "public_web_research"}),
    )


@dataclass
class RunSecurityContext:
    """Per-run security state: the original intent and what the gateway saw.

    `original_intent` is chat-derived and never carries `portfolio_read`;
    `authorization` is the explicit session grant for private domains.
    """

    original_intent: OriginalIntent
    capabilities: frozenset[str]
    authorization: SessionAuthorization = field(default_factory=SessionAuthorization)
    data_labels: set[str] = field(default_factory=set)
    # Set when a successful private tool result actually enters model context;
    # gates the portfolio-data usage notice (approval alone is not enough).
    private_ingress: bool = False
    quarantined_items: int = 0
    security_events: list[dict] = field(default_factory=list)
