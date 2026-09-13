"""Deterministic director: wave-1 orchestration + wave-2 gate.

Pipeline (models decide content; infra owns everything else)::

    create-session > SEC job > verify artifacts > freeze E1
      > launch stockbot/bullbot/bearbot on the same freeze (parallel)
      > collect > compute disagreement
      > decide_wave2: iff material + wave-budget + actionable, authorize one
        targeted SEC wave (E2), else synthesize final.

Stopping reasons (persisted via ``record_stop``): ``complete``,
``max_waves``, ``runtime_exceeded``, ``jobs_exceeded``,
``budget_exhausted``, ``no_questions``, ``not_actionable``, ``low_gain``.

Fake-model sketch (no live calls): inject ``DirectorDeps`` with lambdas
returning canned ids/evidence/analyses; call ``run_wave1`` then
``decide_wave2``; assert the stop reason persists and wave-2 fires only
when a material actionable SEC request exists within budget.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from app.research.agents import ResearchRequest
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis
from app.research.synthesis.committee import CommitteeDisagreement, _coerce_wave_id, compute_disagreement
from app.research.synthesis.final import FinalSynthesis, synthesize_final

StopReason = Literal[
    "continue",
    "complete",
    "max_waves",
    "runtime_exceeded",
    "jobs_exceeded",
    "budget_exhausted",
    "no_questions",
    "not_actionable",
    "low_gain",
]

WAVE1_ID = 1
WAVE2_ID = 2


@dataclass
class DirectorBudgets:
    max_waves: int = 2
    max_jobs: int = 20
    max_tool_calls: int = 60
    runtime_budget_s: float = 900.0


@dataclass
class DirectorDeps:
    """Session-keyed steps; callers bind question/wave/tickers/models in closures."""

    create_session: Callable[[str, str], str]
    fetch_wave_evidence: Callable[[str], list[str]]
    create_freeze: Callable[[str], str]
    run_committee: Callable[[str], tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]]
    record_stop: Callable[[str, str], None]
    clock: Callable[[], float] = monotonic


@dataclass
class Wave1Result:
    session_id: str
    wave_id: int
    freeze_id: str
    evidence_ids: list[str] = field(default_factory=list)
    stock: StockbotAnalysis | None = None
    bull: BullAnalysis | None = None
    bear: BearAnalysis | None = None
    disagreement: CommitteeDisagreement | None = None

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)


@dataclass
class WaveDecision:
    authorized: bool
    stop_reason: StopReason
    reason_detail: str
    targeted_question: str = ""
    targeted_domain: str = ""


def _decision_key(request: ResearchRequest) -> tuple[int, int]:
    return (_gain_rank(request), len(request.requesting_agents))


def run_wave1(
    question: str,
    as_of: str,
    *,
    deps: DirectorDeps,
    tickers: Sequence[str] = (),
    wave_id: int | str = WAVE1_ID,
    interrupt_after: str | None = None,
) -> Wave1Result:
    """Create session, run SEC wave, verify, freeze, run committee, disagree.

    ``interrupt_after`` honors session.policy["interrupt_after"] (wiring reads
    the policy and passes it through): "source" returns after evidence with
    no freeze; "freeze" returns after freezing with no committee. Absent/None
    runs to completion. "one-committee" is owned by the run_committee closure
    (it launches agents and can stop after one); the trio here is atomic.
    """
    _ = tickers  # tickers scope the SEC wave inside fetch_wave_evidence.
    wid = _coerce_wave_id(wave_id)
    session_id = deps.create_session(question, as_of)
    evidence_ids = deps.fetch_wave_evidence(session_id)
    if not evidence_ids:
        deps.record_stop(session_id, "no_questions:empty-wave1")
        return Wave1Result(session_id=session_id, wave_id=wid, freeze_id="", evidence_ids=[])
    if interrupt_after == "source":
        deps.record_stop(session_id, "interrupted:source")
        return Wave1Result(session_id=session_id, wave_id=wid, freeze_id="", evidence_ids=list(evidence_ids))
    freeze_id = deps.create_freeze(session_id)
    if interrupt_after == "freeze":
        deps.record_stop(session_id, "interrupted:freeze")
        return Wave1Result(
            session_id=session_id, wave_id=wid, freeze_id=freeze_id, evidence_ids=list(evidence_ids)
        )
    stock, bull, bear = deps.run_committee(session_id)
    disagreement = compute_disagreement(stock, bull, bear)
    return Wave1Result(
        session_id=session_id,
        wave_id=wid,
        freeze_id=freeze_id,
        evidence_ids=list(evidence_ids),
        stock=stock,
        bull=bull,
        bear=bear,
        disagreement=disagreement,
    )


def _gain_rank(request: ResearchRequest) -> int:
    gain = request.expected_gain.strip().lower()
    if gain == "high":
        return 2
    if gain == "medium":
        return 1
    return 0


def _is_material(request: ResearchRequest) -> bool:
    return bool(request.why_material.strip()) and _gain_rank(request) > 0


def _is_actionable(request: ResearchRequest) -> bool:
    return request.requested_source_domain.strip().upper() == "SEC"


def decide_wave2(
    wave1: Wave1Result,
    *,
    deps: DirectorDeps,
    budgets: DirectorBudgets = DirectorBudgets(),
    waves_used: int = 1,
    jobs_used: int = 4,
    tool_calls_used: int = 0,
    elapsed_s: float = 0.0,
) -> WaveDecision:
    """Gate exactly one targeted SEC wave; persist the reason either way."""
    session_id = wave1.session_id
    if waves_used >= budgets.max_waves:
        decision = WaveDecision(False, "max_waves", f"waves_used={waves_used} max={budgets.max_waves}")
    elif elapsed_s >= budgets.runtime_budget_s:
        decision = WaveDecision(False, "runtime_exceeded", f"elapsed={elapsed_s}s budget={budgets.runtime_budget_s}s")
    elif jobs_used >= budgets.max_jobs:
        decision = WaveDecision(False, "jobs_exceeded", f"jobs_used={jobs_used} max={budgets.max_jobs}")
    elif tool_calls_used >= budgets.max_tool_calls:
        decision = WaveDecision(False, "budget_exhausted", f"tool_calls={tool_calls_used} max={budgets.max_tool_calls}")
    elif wave1.disagreement is None or not wave1.disagreement.requested_research:
        decision = WaveDecision(False, "no_questions", "committee requested no follow-up research")
    else:
        requests = wave1.disagreement.requested_research
        material = [request for request in requests if _is_material(request)]
        if not material:
            decision = WaveDecision(False, "low_gain", "no material follow-up (all low-gain or unmotivated)")
        else:
            actionable = [request for request in material if _is_actionable(request)]
            if not actionable:
                decision = WaveDecision(False, "not_actionable", "material requests need non-SEC domains")
            else:
                actionable.sort(key=_decision_key, reverse=True)
                top = actionable[0]
                decision = WaveDecision(
                    True,
                    "continue",
                    f"wave2 authorized: {top.question}",
                    targeted_question=top.question,
                    targeted_domain=top.requested_source_domain,
                )
    deps.record_stop(session_id, f"{decision.stop_reason}:{decision.reason_detail}")
    return decision


def synthesize_wave1(
    question: str,
    as_of: str,
    wave1: Wave1Result,
    model_override: object = None,
) -> FinalSynthesis | None:
    """Deterministic final packaging of a completed wave (no live calls)."""
    if wave1.stock is None or wave1.bull is None or wave1.bear is None or wave1.disagreement is None:
        return None
    return synthesize_final(
        question,
        session_id=wave1.session_id,
        wave_id=wave1.wave_id,
        freeze_id=wave1.freeze_id,
        as_of=as_of,
        stock=wave1.stock,
        bull=wave1.bull,
        bear=wave1.bear,
        disagreement=wave1.disagreement,
        model=model_override,
    )


__all__ = [
    "WAVE1_ID",
    "WAVE2_ID",
    "DirectorBudgets",
    "DirectorDeps",
    "StopReason",
    "Wave1Result",
    "WaveDecision",
    "decide_wave2",
    "run_wave1",
    "synthesize_wave1",
]
