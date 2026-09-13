"""Agent package: shared request type + stable re-exports.

Fake-model sketch (no live calls): build a ``ScoutAssignment``, pass a
``dispatch`` callable wrapping ``app.tools.execute_tool`` (or a dict-returning
fake), pass a ``model`` callable returning canned text, assert on
``ScoutResult`` / ``StockbotAnalysis`` fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ResearchRequest:
    """One actionable follow-up question from a committee agent."""

    question: str
    why_material: str
    requested_source_domain: str
    expected_gain: str
    requesting_agents: list[str] = field(default_factory=list)


__all__ = ["ResearchRequest"]
