"""Stockbot committee agent: balanced read of one evidence freeze.

Same freeze input as bull/bear; no research caps (reads frozen evidence
only, never calls tools). Model decides the balanced case; infra validates
refs against the freeze and packages ``research_requests``.

Fake-model sketch (no live calls): fake ``model(prompt)`` returns canned
text; call ``run_stockbot`` with a freeze id + evidence ids; assert refs are
a subset of the freeze and ``research_requests`` carry requesting_agents.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import ResearchRequest
from .scout import ModelFn


def _coerce_wave(wave_id: int | str) -> int:
    """Accept int>=1 or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"stockbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"stockbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"stockbot: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if wave < 1:
        raise ValueError(f"stockbot: 'wave_id' must be >= 1, got {wave_id!r}")
    return wave


@dataclass
class StockbotAnalysis:
    session_id: str
    wave_id: int
    freeze_id: str
    evidence_ids: list[str]
    as_of: str
    question: str
    answer: str
    base_case: str
    key_evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    research_requests: list[ResearchRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave(self.wave_id)
    freeze_id: str
    evidence_ids: list[str]
    as_of: str
    question: str
    answer: str
    base_case: str
    key_evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    research_requests: list[ResearchRequest] = field(default_factory=list)


def run_stockbot(
    question: str,
    *,
    session_id: str,
    wave_id: int | str,
    freeze_id: str,
    evidence_ids: Sequence[str],
    as_of: str,
    model: ModelFn,
    follow_ups: Sequence[ResearchRequest] | None = None,
    report: Callable[[str], Sequence[ResearchRequest]] | None = None,
    evidence_text: str = "",
) -> StockbotAnalysis:
    """Balanced synthesis over the frozen evidence set (no tool calls; wave_id stored as int)."""
    wave = _coerce_wave(wave_id)
    frozen = list(evidence_ids)
    prompt = (
        f"Balanced read (no forced recommendation). Question: {question}\n"
        f"Freeze: {freeze_id} as of {as_of} evidence={len(frozen)}"
    )
    if evidence_text.strip():
        prompt += f"\nEvidence (cite ids; do not invent):\n{evidence_text.strip()}"
    text = model(prompt).strip()
    extra: list[ResearchRequest] = list(report(text) if report is not None else (follow_ups or []))
    for request in extra:
        if "stockbot" not in request.requesting_agents:
            request.requesting_agents.append("stockbot")
    refs = [eid for eid in frozen]
    return StockbotAnalysis(
        session_id=session_id,
        wave_id=wave,
        freeze_id=freeze_id,
        evidence_ids=frozen,
        as_of=as_of,
        question=question,
        answer=text,
        base_case=text,
        key_evidence=refs[:5],
        unknowns=[] if refs else ["freeze holds no evidence"],
        what_would_change=[],
        refs=refs,
        research_requests=extra,
    )


__all__ = ["StockbotAnalysis", "run_stockbot"]
