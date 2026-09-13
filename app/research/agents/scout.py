"""Bounded SEC scouts: temporary assignments, not personas.

Three roles (assignment templates, never identities): filings/material-event,
financial/XBRL trend, risk-factor/language-diff. Scouts take an assignment +
session/as_of + SEC-only domain + tool/time budgets, run with ``max_children
= 0`` enforced, and return a ``ScoutResult``.

Fake-model sketch (no live calls): fake ``dispatch(name, args)`` returns
``{"evidence_id": ..., "known_at": ...}`` dicts; fake ``model(prompt)``
returns canned text; call ``run_scout`` and assert
``finding_ids``/``evidence_ids``/``follow_up_requests``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from . import ResearchRequest

ScoutRole = Literal["filings", "financials", "risk"]

ROLE_PROMPTS: dict[str, str] = {
    "filings": (
        "Temporary assignment: filings/material-event scout. List material "
        "events (8-K/6-K, offerings, insider transactions) for the tickers in "
        "scope. Cite evidence ids only; record gaps as unknowns."
    ),
    "financials": (
        "Temporary assignment: financial/XBRL-trend scout. Summarize reported "
        "XBRL/financial-statement trends for the tickers in scope. Never "
        "recalculate tool-computed metrics; cite evidence ids only."
    ),
    "risk": (
        "Temporary assignment: risk-factor/language-diff scout. Compare risk "
        "factor language across filings and flag new/removed/softened "
        "language. Quote briefly with evidence ids; gaps go to unknowns."
    ),
}

MAX_CHILDREN = 0
ALLOWED_DOMAIN = "SEC"

_ROLE_TOOLS: dict[str, tuple[tuple[str, dict[str, str]], ...]] = {
    # Role -> bounded (tool, extra-args); ticker/as_of/since bound at call time.
    "filings": (("search_sec_filings", {}), ("list_sec_filings", {}),
                ("get_material_events", {})),
    "financials": (("search_sec_filings", {}), ("get_xbrl_facts", {"concept": "Revenues"}),
                   ("list_sec_filings", {})),
    "risk": (("diff_risk_factors", {}), ("diff_sec_filings", {}),
             ("list_sec_filings", {})),
}


def _role_arguments(tool: str, ticker: str, as_of: str) -> dict[str, object]:
    """Schema-correct args: ticker identity, as_of PIT, since window, XBRL concept."""
    args: dict[str, object] = {"ticker": ticker, "identifier": ticker, "as_of": as_of}
    if tool == "get_material_events":
        args["since"] = _window_start(as_of)
    if tool == "get_xbrl_facts":
        args["concept"] = "Revenues"
    return args


def _window_start(as_of: str) -> str:
    """One-year lookback window start (YYYY-MM-DD) for since-gated tools."""
    from datetime import datetime as _dt, timedelta as _td
    try:
        end = _dt.fromisoformat(as_of.replace("Z", "+00:00"))
        start = (end - _td(days=365)).date().isoformat()
        if len(start) == 10:
            return start
    except ValueError:
        pass
    return "2024-01-01"

DispatchFn = Callable[[str, dict[str, object]], dict[str, object]]
ModelFn = Callable[[str], str]


@dataclass
class ScoutAssignment:
    assignment_id: str
    session_id: str
    as_of: str
    role: ScoutRole
    question: str
    tickers: list[str] = field(default_factory=list)
    max_tool_calls: int = 8
    time_budget_s: float = 120.0
    allowed_domain: str = ALLOWED_DOMAIN


@dataclass
class ScoutResult:
    assignment_id: str
    session_id: str
    coverage: str
    finding_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    follow_up_requests: list[ResearchRequest] = field(default_factory=list)


def build_scout_prompt(assignment: ScoutAssignment) -> str:
    """Deterministic prompt for one assignment: role template + scope."""
    template = ROLE_PROMPTS[assignment.role]
    tickers = ", ".join(assignment.tickers) if assignment.tickers else "scope tickers TBD"
    return (
        f"{template}\nQuestion: {assignment.question}\n"
        f"Tickers: {tickers}\nAs of: {assignment.as_of} (PIT cutoff; "
        "ignore anything knowable only after this date.)"
    )


def _is_pit_eligible(known_at: object, as_of: str) -> bool:
    """known_at > as_of rejects; None stays eligible-but-unverified (never invent)."""
    if known_at is None:
        return True
    if isinstance(known_at, str):
        return known_at <= as_of
    return True


def run_scout(
    assignment: ScoutAssignment,
    *,
    dispatch: DispatchFn,
    model: ModelFn,
    journal: Callable[[str, dict[str, object]], None] | None = None,
) -> ScoutResult:
    """Run one bounded scout: role tool calls first, model drafts on exact evidence.

    ``max_children = 0``: this function never spawns child jobs. PIT: evidence
    with ``known_at > as_of`` is rejected and journalled as ``evidence.rejected``.
    """
    tools_used = 0

    def guarded_call(name: str, args: dict[str, object]) -> dict[str, object]:
        nonlocal tools_used
        # ponytail: hard ceiling, per-scout fan-out if throughput matters.
        if tools_used >= assignment.max_tool_calls:
            return {"error": "scout tool budget exhausted", "soft": True}
        tools_used += 1
        return dispatch(name, args)

    catalog = guarded_call("browse_tools", {})
    _ = catalog  # discovery hint only; the role plan below decides calls.
    evidence_ids: list[str] = []
    rejected: list[str] = []
    acquired: list[str] = []

    def _collect(resp: object) -> None:
        raw_ids = resp.get("evidence_ids") if isinstance(resp, dict) else None
        candidates: Sequence[object] = raw_ids if isinstance(raw_ids, list) else []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            eid_raw = candidate.get("evidence_id")
            if not isinstance(eid_raw, str) or not eid_raw:
                continue
            if _is_pit_eligible(candidate.get("known_at"), assignment.as_of):
                if eid_raw not in evidence_ids:
                    evidence_ids.append(eid_raw)
                    known = candidate.get("known_at")
                    acquired.append(f"{eid_raw} (known_at={known})" if known else eid_raw)
            else:
                if eid_raw not in rejected:
                    rejected.append(eid_raw)
                    if journal is not None:
                        journal(
                            "evidence.rejected",
                            {"session_id": assignment.session_id, "evidence_id": eid_raw},
                        )
    tickers = assignment.tickers or [""]
    for tool_name, extra in _ROLE_TOOLS.get(assignment.role, ()):
        for ticker in tickers:
            if not ticker.strip():
                continue
            args = _role_arguments(tool_name, ticker.strip(), assignment.as_of)
            args.update(extra)
            _collect(guarded_call("call_tool", {"name": tool_name, "arguments": args}))
            if len(evidence_ids) >= 6:
                break
        if len(evidence_ids) >= 6:
            break
    if not evidence_ids:
        _collect(guarded_call("call_tool", {"name": "get_sec_search_coverage", "arguments": {}}))
    prompt = build_scout_prompt(assignment)
    if acquired:
        prompt += "\nAcquired evidence (cite only these ids):\n" + "\n".join(f"- {line}" for line in acquired)
    text = model(prompt)
    unknowns: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.lower()
        if lowered.startswith(("unknown:", "gap:", "limitation:", "follow-up:", "follow up:")):
            unknowns.append(line[:300])
            if len(unknowns) >= 5:
                break
    if not evidence_ids:
        unknowns.insert(0, "no PIT-eligible SEC evidence returned")
    return ScoutResult(
        assignment_id=assignment.assignment_id,
        session_id=assignment.session_id,
        coverage=f"role={assignment.role} tickers={len(assignment.tickers)} tool_calls={tools_used}",
        finding_ids=[f"{assignment.assignment_id}:f{i}" for i in range(len(evidence_ids))],
        evidence_ids=evidence_ids,
        unknowns=unknowns,
        limitations=rejected,
        follow_up_requests=[],
    )


__all__ = [
    "ALLOWED_DOMAIN",
    "MAX_CHILDREN",
    "ROLE_PROMPTS",
    "DispatchFn",
    "ModelFn",
    "ScoutAssignment",
    "ScoutResult",
    "ScoutRole",
    "build_scout_prompt",
    "run_scout",
]
