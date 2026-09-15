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


def _coerce_int_id(wave_id: int) -> int:
    if wave_id >= 1:
        return wave_id
    raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")


def _coerce_str_id(wave_id: str) -> int:
    text = wave_id.strip()
    if text.isdigit():
        value = int(text)
        if value >= 1:
            return value
    raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")


def _coerce_wave_id(wave_id: int | str) -> int:
    """Accept canonical int or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    if isinstance(wave_id, int):
        return _coerce_int_id(wave_id)
    if isinstance(wave_id, str):
        return _coerce_str_id(wave_id)
    raise ValueError(f"committee: 'wave_id' must be an int >= 1, got {wave_id!r}")


def _dedup(items: Sequence[str], cap: int = 20) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))[:cap]


def _shared_lines(stock_refs: list[str], bull_refs: list[str], bear_refs: list[str]) -> list[str]:
    return [f"all three cite {eid}" for eid in stock_refs if eid in bull_refs and eid in bear_refs]


def _solo_line(label: str, ids: list[str]) -> str | None:
    if not ids:
        return None
    return f"{label}-only evidence: {', '.join(ids[:5])}"


def _stance_lines(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis, stock_refs: list[str], bull_refs: list[str], bear_refs: list[str]) -> tuple[list[str], list[str]]:
    disagreement = [
        f"bull ({bull.stance}) vs bear ({bear.stance}) on: {stock.question}",
        f"base cites {len(stock_refs)} items; bull {len(bull_refs)}; bear {len(bear_refs)}",
    ]
    for line in (
        _solo_line("bull", [eid for eid in bull_refs if eid not in bear_refs]),
        _solo_line("bear", [eid for eid in bear_refs if eid not in bull_refs]),
    ):
        if line is not None:
            disagreement.append(line)
    return _shared_lines(stock_refs, bull_refs, bear_refs), disagreement


def _merge_requests(
    stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis
) -> list[ResearchRequest]:
    seen: dict[str, ResearchRequest] = {}
    for request in (*stock.research_requests, *bull.research_requests, *bear.research_requests):
        prior = seen.get(request.question)
        if prior is None:
            seen[request.question] = request
        else:
            for agent in request.requesting_agents:
                if agent not in prior.requesting_agents:
                    prior.requesting_agents.append(agent)
    return list(seen.values())


def compute_disagreement(
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
) -> CommitteeDisagreement:
    """Deterministic merge: shared evidence agrees, stance split disagrees."""
    from app.research.agents import claims_refs
    stock_refs = claims_refs(stock.claims)
    bull_refs = claims_refs(bull.claims)
    bear_refs = claims_refs(bear.claims)
    agreement, disagreement = _stance_lines(stock, bull, bear, stock_refs, bull_refs, bear_refs)
    return CommitteeDisagreement(
        session_id=stock.session_id,
        wave_id=_coerce_wave_id(stock.wave_id),
        freeze_id=stock.freeze_id,
        agreement=agreement,
        disagreement=disagreement,
        critical_uncertainties=_dedup([*stock.unknowns, *bull.unknowns, *bear.unknowns]),
        requested_research=_merge_requests(stock, bull, bear),
    )


__all__ = ["CommitteeDisagreement", "compute_disagreement"]
