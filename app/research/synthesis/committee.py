"""Committee disagreement: deterministic merge of the three frozen reads.

Fake-model sketch (no live calls): build canned ``StockbotAnalysis`` /
``BullAnalysis`` / ``BearAnalysis`` sharing one freeze id, call
``compute_disagreement``, assert shared evidence lands in ``agreement`` and
every follow-up lands in ``requested_research`` with requesting agents kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.research.agents import ResearchRequest
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis


@dataclass
class CommitteeDisagreement:
    session_id: str
    wave_id: int
    freeze_id: str
    agreement: list[str] = field(default_factory=list)
    disagreement: list[str] = field(default_factory=list)
    critical_uncertainties: list[str] = field(default_factory=list)
    requested_research: list[ResearchRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)


def _coerce_wave_id(wave_id: int | str) -> int:
    """Accept canonical int or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        if wave_id >= 1:
            return wave_id
        raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, str):
        text = wave_id.strip()
        if text.isdigit():
            value = int(text)
            if value >= 1:
                return value
        raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")
    raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")


def _dedup(items: Sequence[str], cap: int = 20) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))[:cap]


def compute_disagreement(
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
) -> CommitteeDisagreement:
    """Deterministic merge: shared evidence agrees, stance split disagrees."""
    shared = [eid for eid in stock.key_evidence if eid in bull.key_evidence and eid in bear.key_evidence]
    agreement = [f"all three cite {eid}" for eid in shared]
    disagreement = [
        f"bull ({bull.stance}) vs bear ({bear.stance}) on: {stock.question}",
        f"base cites {len(stock.key_evidence)} items; bull {len(bull.key_evidence)}; bear {len(bear.key_evidence)}",
    ]
    only_bull = [eid for eid in bull.key_evidence if eid not in bear.key_evidence]
    only_bear = [eid for eid in bear.key_evidence if eid not in bull.key_evidence]
    if only_bull:
        disagreement.append(f"bull-only evidence: {', '.join(only_bull[:5])}")
    if only_bear:
        disagreement.append(f"bear-only evidence: {', '.join(only_bear[:5])}")
    uncertainties = _dedup([*stock.unknowns, *bull.unknowns, *bear.unknowns])
    seen: dict[str, ResearchRequest] = {}
    for request in (*stock.research_requests, *bull.research_requests, *bear.research_requests):
        prior = seen.get(request.question)
        if prior is None:
            seen[request.question] = request
        else:
            for agent in request.requesting_agents:
                if agent not in prior.requesting_agents:
                    prior.requesting_agents.append(agent)
    return CommitteeDisagreement(
        session_id=stock.session_id,
        wave_id=_coerce_wave_id(stock.wave_id),
        freeze_id=stock.freeze_id,
        agreement=agreement,
        disagreement=disagreement,
        critical_uncertainties=uncertainties,
        requested_research=list(seen.values()),
    )


__all__ = ["CommitteeDisagreement", "compute_disagreement"]
