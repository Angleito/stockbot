"""Committee disagreement: deterministic merge of the three frozen reads.

Fake-model sketch (no live calls): build canned ``StockbotAnalysis`` /
``BullAnalysis`` / ``BearAnalysis`` sharing one freeze id, call
``compute_disagreement``, assert shared evidence lands in ``agreement`` and
every follow-up lands in ``requested_research`` with requesting agents kept.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.research.agents import ResearchRequest, claims_refs
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis


@dataclass
class CriticalDisagreement:
    """One preserved bull-vs-bear split: question, both reads, frozen evidence links."""

    question: str
    bull: str
    bear: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class CommitteeDisagreement:
    session_id: str
    wave_id: int
    freeze_id: str
    agreement: list[str] = field(default_factory=list)
    disagreement: list[str] = field(default_factory=list)
    critical_uncertainties: list[str] = field(default_factory=list)
    requested_research: list[ResearchRequest] = field(default_factory=list)
    consensus: list[str] = field(default_factory=list)
    critical_disagreements: list[CriticalDisagreement] = field(default_factory=list)

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


def _stance_lines(
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
    stock_refs: list[str],
    bull_refs: list[str],
    bear_refs: list[str],
) -> tuple[list[str], list[str]]:
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


def _merge_requests(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[ResearchRequest]:
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


def _member_view(analysis: object, keys: tuple[str, ...]) -> str:
    """First non-blank prose view across candidate attrs; blank when absent."""
    for key in keys:
        value = getattr(analysis, key, "")
        if isinstance(value, str) and value.strip():
            return value.strip()[:2000]
    return ""


_ROLE_ORDER: tuple[tuple[str, str], ...] = (("stockbot", "stock"), ("bullbot", "bull"), ("bearbot", "bear"))


def _declared_by_text(analyses: Mapping[str, object], attr: str, field: str) -> dict[str, dict[str, str]]:
    """One member mapping per text: {role: declared value} for claims/channels that carry it."""
    by_text: dict[str, dict[str, str]] = {}
    for role, name in _ROLE_ORDER:
        for item in getattr(analyses[name], attr, None) or []:
            text = getattr(item, "text", None)
            declared = getattr(item, field, None)
            if isinstance(text, str) and text.strip() and isinstance(declared, str) and declared.strip():
                by_text.setdefault(text.strip(), {})[role] = declared.strip()
    return by_text


def _conflict_lines(by_text: Mapping[str, Mapping[str, str]], label: str) -> list[str]:
    """One line per text whose declared value differs across committee roles."""
    out: list[str] = []
    for text, reads in by_text.items():
        if len(set(reads.values())) > 1:
            detail = ", ".join(f"{role} {read}" for role, read in reads.items())
            out.append(f"{label} conflict on {text[:160]!r}: {detail}")
    return out


def _claim_conflicts(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[str]:
    """Same text declared with different claim_type across roles (contradicted vs observed_fact above all)."""
    return _conflict_lines(
        _declared_by_text({"stock": stock, "bull": bull, "bear": bear}, "claims", "claim_type"),
        "claim type",
    )


def _channel_conflicts(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[str]:
    """Same impact channel declared with different directions across roles."""
    return _conflict_lines(
        _declared_by_text({"stock": stock, "bull": bull, "bear": bear}, "impact_channels", "direction"),
        "impact channel",
    )


_SPLIT_SIDES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    (
        "bullish",
        ("executive_view", "bull_case"),
        "bullish read stands on bull-only evidence",
        "bearish read disputes the bullish read",
    ),
    (
        "bearish",
        ("executive_view", "bear_case"),
        "bullish read disputes the bearish read",
        "bearish read stands on bear-only evidence",
    ),
)


def _critical_split(
    stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis, bull_refs: list[str], bear_refs: list[str]
) -> list[CriticalDisagreement]:
    """One preserved split per bull/bear-only evidence branch (empty when stances fully overlap)."""
    only = {
        "bullish": [eid for eid in bull_refs if eid not in bear_refs],
        "bearish": [eid for eid in bear_refs if eid not in bull_refs],
    }
    views = {"bullish": _member_view(bull, _SPLIT_SIDES[0][1]), "bearish": _member_view(bear, _SPLIT_SIDES[1][1])}
    out: list[CriticalDisagreement] = []
    for stance, keys, bull_fallback, bear_fallback in _SPLIT_SIDES:
        ids = only[stance]
        if ids:
            view = views[stance]
            out.append(
                CriticalDisagreement(
                    question=f"How far does the {stance} read of {stock.question} hold?",
                    bull=view or bull_fallback,
                    bear=view or bear_fallback,
                    evidence_ids=ids[:10],
                )
            )
    return out


def compute_disagreement(
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
) -> CommitteeDisagreement:
    """Deterministic merge: shared evidence agrees; stance, claim-type, and channel splits disagree.

    Claim-type conflicts (a text declared ``observed_fact`` by one role and
    ``contradicted`` or ``inference`` by another) and opposite channel
    directions are preserved as disagreement lines; every merged research
    request stays in ``requested_research`` for the Director to route.
    """
    stock_refs = claims_refs(stock.claims)
    bull_refs = claims_refs(bull.claims)
    bear_refs = claims_refs(bear.claims)
    agreement, disagreement = _stance_lines(stock, bull, bear, stock_refs, bull_refs, bear_refs)
    disagreement.extend(_claim_conflicts(stock, bull, bear))
    disagreement.extend(_channel_conflicts(stock, bull, bear))
    return CommitteeDisagreement(
        session_id=stock.session_id,
        wave_id=_coerce_wave_id(stock.wave_id),
        freeze_id=stock.freeze_id,
        agreement=agreement,
        disagreement=disagreement,
        critical_uncertainties=_dedup([*stock.unknowns, *bull.unknowns, *bear.unknowns]),
        requested_research=_merge_requests(stock, bull, bear),
        consensus=list(agreement),
        critical_disagreements=_critical_split(stock, bull, bear, bull_refs, bear_refs),
    )


__all__ = ["CommitteeDisagreement", "CriticalDisagreement", "compute_disagreement"]
