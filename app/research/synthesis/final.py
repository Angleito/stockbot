"""Final synthesis: answer + base/bull/bear + disagreement, no recommendation.

The synthesizer never issues a forced buy/sell/hold call; ``answer``
summarizes what the frozen evidence supports and names what would change it.

Fake-model sketch (no live calls): canned trio of analyses -> canned
``CommitteeDisagreement`` -> ``synthesize_final``; assert ``refs`` stay
within the freeze and ``answer`` is non-empty with no invented evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis
from .committee import CommitteeDisagreement, _coerce_wave_id


@dataclass
class FinalSynthesis:
    session_id: str
    wave_id: int
    freeze_id: str
    as_of: str
    question: str
    answer: str
    base_case: str
    bull_case: str
    bear_case: str
    disagreement: CommitteeDisagreement
    key_evidence: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)


def synthesize_final(
    question: str,
    *,
    session_id: str,
    wave_id: int | str,
    freeze_id: str,
    as_of: str,
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
    disagreement: CommitteeDisagreement,
    model: object = None,
) -> FinalSynthesis:
    """Package the trio + disagreement into the final record (no live calls).

    ``model`` is accepted for API symmetry but unused: final text is the
    deterministic join below so synthesis stays reproducible. Pass a string
    to override the joined answer (e.g. a canned model draft in tests).
    """
    frozen: list[str] = []
    for eid in (*stock.refs, *bull.refs, *bear.refs):
        if eid not in frozen:
            frozen.append(eid)
    key_evidence: list[str] = []
    for eid in (*stock.key_evidence, *bull.key_evidence, *bear.key_evidence):
        if eid in frozen and eid not in key_evidence:
            key_evidence.append(eid)
    unknowns: list[str] = []
    for unknown in (*stock.unknowns, *bull.unknowns, *bear.unknowns):
        if unknown not in unknowns:
            unknowns.append(unknown)
    changes: list[str] = []
    for change in (*stock.what_would_change, *bull.what_would_change, *bear.what_would_change):
        if change not in changes:
            changes.append(change)
    joined = (
        f"Balanced: {stock.base_case} Bull: {bull.bull_case} Bear: {bear.bear_case} "
        f"Agreed: {'; '.join(disagreement.agreement) or 'none stated'}."
    )
    answer = joined if model is None else str(model)
    return FinalSynthesis(
        session_id=session_id,
        wave_id=_coerce_wave_id(wave_id),
        freeze_id=freeze_id,
        as_of=as_of,
        question=question,
        answer=answer,
        base_case=stock.base_case,
        bull_case=bull.bull_case,
        bear_case=bear.bear_case,
        disagreement=disagreement,
        key_evidence=key_evidence,
        unknowns=unknowns or list(disagreement.critical_uncertainties),
        what_would_change=changes,
        refs=frozen,
    )


__all__ = ["FinalSynthesis", "synthesize_final"]
