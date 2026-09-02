"""Context typing and original-intent classification. stdlib only."""

from __future__ import annotations

import re
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
    """The user's original research intent, accumulated over all user turns."""

    request: str
    permitted_domains: frozenset[str]


# Portfolio-interest noun alternation (spec §44).
_PORTFOLIO_NOUN_RE = re.compile(
    r"\b(?:portfolio|position|positions|holding|holdings|stake|mandate|thesis"
    r"|balance|cash|account|invested|cost\s+basis)\b",
    re.IGNORECASE,
)
# First-person possessive/pronoun; "me" is included so personal-impact
# follow-ups ("how does that affect me?") classify as portfolio interest.
_PRONOUN_RE = re.compile(r"\b(?:my|mine|me)\b", re.IGNORECASE)
# Personal-impact verbs that make a first-person pronoun portfolio-relevant.
_IMPACT_VERB_RE = re.compile(r"\b(?:affect\w*|impact\w*)\b", re.IGNORECASE)

_PRONOUN_WINDOW = 5


def _portfolio_interest(text: str) -> bool:
    """True when the turn expresses personal portfolio/financial interest."""
    if _PORTFOLIO_NOUN_RE.search(text):
        return True
    # Possessive/pronoun within 5 whitespace-delimited tokens of a portfolio
    # noun or a personal-impact verb ("how does that affect me?").
    tokens = text.split()
    for i, token in enumerate(tokens):
        if not _PRONOUN_RE.search(token):
            continue
        window = tokens[max(0, i - _PRONOUN_WINDOW): i + _PRONOUN_WINDOW + 1]
        if any(_PORTFOLIO_NOUN_RE.search(w) or _IMPACT_VERB_RE.search(w) for w in window):
            return True
    return False


def classify_intent(user_turns: Sequence[str]) -> OriginalIntent:
    """Deterministic classifier: the request is the last user turn; permitted
    domains are financial/public-web research always, plus portfolio_read when
    ANY user turn expresses portfolio interest."""
    turns = [t for t in user_turns if isinstance(t, str)]
    request = turns[-1] if turns else ""
    domains = {"financial_research", "public_web_research"}
    if any(_portfolio_interest(t) for t in turns):
        domains.add("portfolio_read")
    return OriginalIntent(request=request, permitted_domains=frozenset(domains))


@dataclass
class RunSecurityContext:
    """Per-run security state: the original intent and what the gateway saw."""

    original_intent: OriginalIntent
    capabilities: frozenset[str]
    data_labels: set[str] = field(default_factory=set)
    quarantined_items: int = 0
    security_events: list[dict] = field(default_factory=list)
