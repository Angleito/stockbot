"""Bullbot committee agent: bullish read of one evidence freeze.

Same freeze input as stockbot/bearbot; no research caps (frozen evidence
only, never calls tools). Model argues the bull case; infra validates refs
against the freeze and packages ``research_requests``.

Fake-model sketch (no live calls): fake ``model(prompt)`` returns canned
bull text with per-claim citations like ``CLAIM: <text> [EV-1]``; call
``run_bullbot``; assert ``stance`` is bullish and refs stay
within the freeze.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import GroundedClaim, ResearchRequest, parse_committee_output
from .scout import ModelFn


def _coerce_wave(wave_id: int | str) -> int:
    """Accept int>=1 or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"bullbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"bullbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"bullbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if wave < 1:
        raise ValueError(f"bullbot: 'wave_id' must be >= 1, got {wave_id!r}")
    return wave


@dataclass
class BullAnalysis:
    session_id: str
    wave_id: int
    freeze_id: str
    evidence_ids: list[str]
    as_of: str
    question: str
    stance: str
    bull_case: str
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    claims: list[GroundedClaim] = field(default_factory=list)
    research_requests: list[ResearchRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave(self.wave_id)


def run_bullbot(
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
) -> BullAnalysis:
    """Bullish synthesis over the frozen evidence set (no tool calls; wave_id stored as int)."""
    wave = _coerce_wave(wave_id)
    frozen = list(evidence_ids)
    prompt = (
        f"Bull case only (no forced recommendation). Question: {question}\n"
        f"Freeze: {freeze_id} as of {as_of} evidence={len(frozen)}\n"
        'Respond with one JSON object only: {"claims": [{"text": "<finding>", "evidence_ids": ["<freeze-id>", ...]}], "follow_ups": ["<question>?", ...]}. '
        "Cite only freeze ids for each factual claim; follow_ups are SEC follow-up questions (may be [])."
    )
    if evidence_text.strip():
        prompt += f"\nEvidence (cite ids; do not invent):\n{evidence_text.strip()}"
    text = model(prompt).strip()
    extra: list[ResearchRequest] = list(follow_ups or [])
    for request in extra:
        if "bullbot" not in request.requesting_agents:
            request.requesting_agents.append("bullbot")
    claims, envelope_follow = parse_committee_output(text, frozen=frozen, agent="bullbot")
    unknowns: list[str] = [] if claims or frozen else ["freeze holds no evidence"]
    prose = "\n".join(c.text for c in claims).strip()
    if not prose:
        prose = "No grounded claims in freeze." if frozen else "Freeze holds no evidence."
    text = prose
    extra = list(envelope_follow) + list(extra)
    return BullAnalysis(
        session_id=session_id,
        wave_id=wave,
        freeze_id=freeze_id,
        evidence_ids=frozen,
        as_of=as_of,
        question=question,
        stance="bullish",
        bull_case=text,
        unknowns=unknowns,
        what_would_change=[],
        claims=claims,
        research_requests=extra,
    )


__all__ = ["BullAnalysis", "run_bullbot"]
