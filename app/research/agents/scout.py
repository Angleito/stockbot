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

from . import GroundedClaim, ResearchRequest, parse_grounded_claims

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
    # ponytail: hard-coded role tools (deterministic, policy-gated); model-driven discovery/selection/execution if broad live coverage requires it.
    "filings": (("search_sec_filings", {}), ("list_sec_filings", {}),
                ("get_material_events", {})),
    "financials": (("search_sec_filings", {}), ("get_xbrl_facts", {"concept": "Revenues"}),
                   ("list_sec_filings", {})),
    "risk": (("diff_risk_factors", {}), ("diff_sec_filings", {}),
             ("list_sec_filings", {})),
}


def _role_arguments(tool: str, ticker: str, as_of: str) -> dict[str, object]:
    """Schema-correct args: ticker identity, as_of PIT, since window, XBRL concept."""
    args: dict[str, object] = {"ticker": ticker, "identifier": ticker}
    text = as_of.strip() if isinstance(as_of, str) else ""
    bounded = bool(text) and text.lower() != "unbounded"
    if bounded:
        args["as_of"] = as_of
    if tool == "get_material_events" and bounded:
        args["since"] = _window_start(as_of)
    if tool == "get_xbrl_facts":
        args["concept"] = "Revenues"
    return args


def _window_start(as_of: str) -> str:
    """One-year lookback window start (YYYY-MM-DD) for since-gated tools."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
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
    findings: list[GroundedClaim] = field(default_factory=list)
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
        "ignore anything knowable only after this date.)\n"
        'Respond with JSON only: [{"text": "<finding>", "evidence_ids": ["<id>", ...]}, ...]. '
        "Cite only the acquired ids listed below, one or more per claim; gaps as UNKNOWN: <text> (no citation needed)."
    )


def _is_pit_eligible(known_at: object, as_of: str) -> bool:
    """Shared PIT rule (parsed): historical as_of + unknown known_at is ineligible."""
    from app.research.models import pit_unverified, pit_violated
    if known_at is None:
        return not pit_unverified(as_of, None)
    if isinstance(known_at, str):
        if not known_at.strip():
            return not pit_unverified(as_of, None)
        try:
            if pit_unverified(as_of, known_at):
                return False
            return not pit_violated(as_of, known_at)
        except ValueError:
            return False
    return False


@dataclass
class _ScoutStore:
    """Accumulated scout evidence: eligible ids, rejected ids, prompt lines."""

    assignment: ScoutAssignment
    journal: Callable[[str, dict[str, object]], None] | None = None
    evidence_ids: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    acquired: list[str] = field(default_factory=list)

    def candidate_id(self, candidate: object) -> str | None:
        """Evidence id when the candidate is a dict with a non-blank id."""
        if not isinstance(candidate, dict):
            return None
        eid_raw = candidate.get("evidence_id")
        return eid_raw if isinstance(eid_raw, str) and eid_raw else None

    def accept(self, eid: str, candidate: dict[str, object]) -> None:
        """Record one PIT-eligible id plus its one-line prompt rendering."""
        if eid in self.evidence_ids:
            return
        self.evidence_ids.append(eid)
        known = candidate.get("known_at")
        header = f"{eid} (known_at={known})" if known else eid
        snippet = candidate.get("claim_text") or candidate.get("content_snippet")
        text = snippet.strip().replace("\n", " ")[:300] if isinstance(snippet, str) and snippet.strip() else ""
        self.acquired.append(f"{header} :: {text}" if text else header)

    def reject(self, eid: str) -> None:
        """Record one PIT-ineligible id, journalled once."""
        if eid in self.rejected:
            return
        self.rejected.append(eid)
        if self.journal is not None:
            self.journal("evidence.rejected", {"session_id": self.assignment.session_id, "evidence_id": eid})

    def collect(self, resp: object) -> None:
        """Fold one dispatch response's candidates into eligible/rejected sets."""
        raw_ids = resp.get("evidence_ids") if isinstance(resp, dict) else None
        candidates: Sequence[object] = raw_ids if isinstance(raw_ids, list) else []
        for candidate in candidates:
            eid = self.candidate_id(candidate)
            if eid is None:
                continue
            assert isinstance(candidate, dict)
            if _is_pit_eligible(candidate.get("known_at"), self.assignment.as_of):
                self.accept(eid, candidate)
            else:
                self.reject(eid)


def _fan_out(store: _ScoutStore, guarded_call: Callable[[str, dict[str, object]], dict[str, object]]) -> None:
    """Role tool fan-out: bounded per-ticker calls, six-evidence cap, coverage fallback."""
    tickers = store.assignment.tickers or [""]
    for tool_name, extra in _ROLE_TOOLS.get(store.assignment.role, ()):
        for ticker in tickers:
            if not ticker.strip():
                continue
            args = _role_arguments(tool_name, ticker.strip(), store.assignment.as_of)
            args.update(extra)
            store.collect(guarded_call("call_tool", {"name": tool_name, "arguments": args}))
            if len(store.evidence_ids) >= 6:
                break
        if len(store.evidence_ids) >= 6:
            break
    if not store.evidence_ids:
        store.collect(guarded_call("call_tool", {"name": "get_sec_search_coverage", "arguments": {}}))


def _finish(assignment: ScoutAssignment, store: _ScoutStore, tools_used: int, model: ModelFn) -> ScoutResult:
    """Draft grounded findings on the exact acquired evidence."""
    prompt = build_scout_prompt(assignment)
    if store.acquired:
        prompt += "\nAcquired evidence (cite only these ids):\n" + "\n".join(f"- {line}" for line in store.acquired)
    text = model(prompt)
    findings: list[GroundedClaim] = parse_grounded_claims(text, frozen=store.evidence_ids)
    unknowns: list[str] = []
    if not store.evidence_ids:
        unknowns.insert(0, "no PIT-eligible SEC evidence returned")
    return ScoutResult(
        assignment_id=assignment.assignment_id,
        session_id=assignment.session_id,
        coverage=f"role={assignment.role} tickers={len(assignment.tickers)} tool_calls={tools_used}",
        findings=findings,
        unknowns=unknowns,
        limitations=store.rejected,
        follow_up_requests=[],
    )


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
    store = _ScoutStore(assignment=assignment, journal=journal)
    _fan_out(store, guarded_call)
    return _finish(assignment, store, tools_used, model)


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
