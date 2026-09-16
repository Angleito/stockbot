"""Bearbot committee agent: bearish read of one evidence freeze.

Same freeze input as stockbot/bullbot; no research caps (frozen evidence
only, never calls tools). Model argues the bear case; infra validates refs
against the freeze and packages ``research_requests``.

Fake-model sketch (no live calls): fake ``model(prompt)`` returns canned
bear text with per-claim citations like ``CLAIM: <text> [EV-1]``; call
``run_bearbot``; assert ``stance`` is bearish and refs stay
within the freeze.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import (
    CommitteeMateriality,
    GroundedClaim,
    ImpactChannel,
    ResearchRequest,
    parse_committee_envelope,
)
from .scout import ModelFn


def _coerce_wave(wave_id: int | str) -> int:
    """Accept int>=1 or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"bearbot: 'wave_id' must be an int >= 1, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"bearbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"bearbot: 'wave_id' must be an int >= 1, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    if wave < 1:
        raise ValueError(f"bearbot: 'wave_id' must be >= 1, got {wave_id!r}")
    return wave


@dataclass
class BearAnalysis:
    session_id: str
    wave_id: int
    freeze_id: str
    evidence_ids: list[str]
    as_of: str
    question: str
    stance: str
    bear_case: str
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    claims: list[GroundedClaim] = field(default_factory=list)
    research_requests: list[ResearchRequest] = field(default_factory=list)
    executive_view: str = ""
    impact_channels: list[ImpactChannel] = field(default_factory=list)
    materiality: CommitteeMateriality = field(default_factory=CommitteeMateriality)
    uncertainties: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave(self.wave_id)


def _tag_requests(extra: list[ResearchRequest]) -> list[ResearchRequest]:
    """Tag follow-ups with the bearbot agent."""
    for request in extra:
        if "bearbot" not in request.requesting_agents:
            request.requesting_agents.append("bearbot")
    return extra


def _prose_or_placeholder(claims: list[GroundedClaim], frozen: list[str]) -> str:
    """Joined claim text, or the empty-freeze placeholder."""
    prose = "\n".join(c.text for c in claims).strip()
    if prose:
        return prose
    return "No grounded claims in freeze." if frozen else "Freeze holds no evidence."


def run_bearbot(
    question: str,
    *,
    session_id: str,
    wave_id: int | str,
    freeze_id: str,
    evidence_ids: Sequence[str],
    as_of: str,
    model: ModelFn,
    follow_ups: Sequence[ResearchRequest] | None = None,
    evidence_text: str = "",
) -> BearAnalysis:
    """Bearish synthesis over the frozen evidence set (no tool calls; wave_id stored as int)."""
    wave = _coerce_wave(wave_id)
    frozen = list(evidence_ids)
    prompt = (
        f"Bear case only (strongest defensible downside/contagion read). Question: {question}\n"
        f"Freeze: {freeze_id} as of {as_of} evidence={len(frozen)}\n"
        'Respond with one JSON object only: {"role": "bearbot", "executive_view": "<bearish read>", "claims": [{"statement": "<finding>", "evidence_ids": ["<freeze-id>", ...]}], "impact_channels": [{"name": "<channel>", "assessment": "<read>", "evidence_ids": ["<freeze-id>"]}], "materiality": {"overall": "critical|high|medium|low", "reasoning": "<why>"}, "uncertainties": ["<open question>"], "research_requests": [{"question": "<follow-up>?", "why_it_matters": "<why>", "suggested_source": "SEC"}]}. '
        "Cite only freeze ids for each factual claim; legacy keys claims[].text and follow_ups[] are also accepted."
    )
    if evidence_text.strip():
        prompt += f"\nEvidence (cite ids; do not invent):\n{evidence_text.strip()}"
    text = model(prompt).strip()
    extra = _tag_requests(list(follow_ups or []))
    env = parse_committee_envelope(text, frozen=frozen, agent="bearbot")
    unknowns: list[str] = (
        list(env.uncertainties)
        if env.uncertainties
        else ([] if env.claims or frozen else ["freeze holds no evidence"])
    )
    prose = _prose_or_placeholder(env.claims, frozen)
    extra = list(env.follow_ups) + list(extra)
    view = env.executive_view or prose
    return BearAnalysis(
        session_id=session_id,
        wave_id=wave,
        freeze_id=freeze_id,
        evidence_ids=frozen,
        as_of=as_of,
        question=question,
        stance="bearish",
        bear_case=view,
        unknowns=unknowns,
        what_would_change=[],
        claims=env.claims,
        research_requests=extra,
        executive_view=view,
        impact_channels=list(env.impact_channels),
        materiality=env.materiality,
        uncertainties=list(unknowns),
    )


__all__ = ["BearAnalysis", "run_bearbot"]
