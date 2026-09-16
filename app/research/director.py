"""Deterministic director: wave-1 orchestration + wave-2 gate.

Pipeline (models decide content; infra owns everything else)::

    create-session > SEC job > verify artifacts > freeze E1
      > launch stockbot/bullbot/bearbot on the same freeze (parallel)
      > collect > compute disagreement
      > decide_wave2: coverage challenge first (sufficient claim with missing
        branches / material open / unsearched routes -> targeted SEC follow-up),
        then material + actionable committee follow-up within budget,
        else synthesize final. Targeted wave resolves one uncertainty, then a
        new freeze + rerun analysis; it never redefines the objective (original
        question + covered vs remaining branches ride Wave1Result across waves).

Stopping reasons (persisted via ``record_stop``): ``complete``,
``max_waves`` (runaway-test budget guard, never completeness proof),
``runtime_exceeded``, ``jobs_exceeded``,
``no_questions``, ``not_actionable``, ``low_gain``.

Fake-model sketch (no live calls): inject ``DirectorDeps`` with lambdas
returning canned ids/evidence/analyses; call ``run_wave1`` then
``decide_wave2``; assert the stop reason persists and wave-2 fires only
when a material actionable SEC request exists within budget.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic
from typing import Literal

from app.research.agents import ResearchRequest
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis
from app.research.synthesis.committee import (
    CommitteeDisagreement,
    _coerce_wave_id,
    compute_disagreement,
)
from app.research.synthesis.final import FinalSynthesis, synthesize_final

StopReason = Literal[
    "continue",
    "complete",
    "max_waves",
    "runtime_exceeded",
    "jobs_exceeded",
    "no_questions",
    "not_actionable",
    "low_gain",
]

WAVE1_ID = 1
WAVE2_ID = 2


@dataclass
class DirectorBudgets:
    """Runaway-test budget guard (max_waves=2 caps waves, never proves completeness)."""
    max_waves: int = 2
    max_jobs: int = 20
    max_tool_calls: int | None = None
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
    coverage: dict[str, object] | None = None
    relationships: list[dict[str, object]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    question: str = ""

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)


@dataclass
class WaveDecision:
    authorized: bool
    stop_reason: StopReason
    reason_detail: str
    targeted_question: str = ""
    targeted_domain: str = ""


def normalize_research_action(source: str, tool: str, query: str, ticker: str, forms: Sequence[str] | str, as_of: str, accession: str, objective: str) -> tuple[str, str, str, str, tuple[str, ...], str, str, str]:
    """Semantic action key: exact-tuple equality only (no fuzzy/lexical similarity)."""
    form_tuple = tuple(forms) if isinstance(forms, Sequence) and not isinstance(forms, str) else ((forms,) if isinstance(forms, str) and forms else ())
    return (source.strip().lower(), tool.strip(), query.strip(), ticker.strip().upper(), tuple(f.strip().upper() for f in form_tuple if isinstance(f, str)), as_of.strip(), accession.strip(), objective.strip())


@dataclass
class LoopDetector:
    """Exact-repeat detector: same action + same result + no evidence progress."""
    seen: dict[tuple[str, str, str, str, tuple[str, ...], str, str, str], tuple[str, int]] = field(default_factory=dict)
    telemetry: list[dict[str, object]] = field(default_factory=list)
    def check(self, action: tuple[str, str, str, str, tuple[str, ...], str, str, str], result_hash: str, evidence_delta: int) -> dict[str, object]:
        """Record one action outcome; reject exact no-progress repeats."""
        prior = self.seen.get(action)
        if prior is not None and prior[0] == result_hash and evidence_delta <= 0:
            entry: dict[str, object] = {"action": list(action), "result_hash": result_hash, "reason": "research_loop_detected"}
            self.telemetry.append(entry)
            return {"duplicate": True, "reason": "research_loop_detected"}
        self.seen[action] = (result_hash, evidence_delta)
        return {"duplicate": False, "reason": ""}


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
    if interrupt_after == "source":
        deps.record_stop(session_id, "interrupted:source")
        return Wave1Result(session_id=session_id, wave_id=wid, freeze_id="", evidence_ids=list(evidence_ids), question=question)
    freeze_id = deps.create_freeze(session_id)
    if interrupt_after == "freeze":
        deps.record_stop(session_id, "interrupted:freeze")
        return Wave1Result(
            session_id=session_id, wave_id=wid, freeze_id=freeze_id, evidence_ids=list(evidence_ids), question=question
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
        question=question,
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


def _budget_stop(budgets: DirectorBudgets, waves_used: int, jobs_used: int, tool_calls_used: int, elapsed_s: float) -> WaveDecision | None:
    """First exhausted budget wins; None when all budgets hold.

    max_waves is a runaway-test budget guard only; hitting it never proves
    coverage complete (settle carries limitations). Tool calls are unlimited
    by default (max_tool_calls=None); explicit int limits, when configured,
    are enforced at dispatch (repository/runner), not here.
    """
    _ = tool_calls_used
    if waves_used >= budgets.max_waves:
        return WaveDecision(False, "max_waves", f"waves_used={waves_used} max={budgets.max_waves}")
    if elapsed_s >= budgets.runtime_budget_s:
        return WaveDecision(False, "runtime_exceeded", f"elapsed={elapsed_s}s budget={budgets.runtime_budget_s}s")
    if jobs_used >= budgets.max_jobs:
        return WaveDecision(False, "jobs_exceeded", f"jobs_used={jobs_used} max={budgets.max_jobs}")
    return None


def _coverage_str_list(coverage: Mapping[str, object] | None, key: str) -> list[str]:
    """String list from a coverage mapping (non-strings dropped); empty when absent."""
    if not isinstance(coverage, Mapping):
        return []
    raw = coverage.get(key)
    return [v for v in raw if isinstance(v, str) and v.strip()] if isinstance(raw, list) else []


def _coverage_questions(coverage: Mapping[str, object] | None, extra: Sequence[str] = ()) -> list[str]:
    """Material open questions: explicit material list + unresolved + dossier opens."""
    if not isinstance(coverage, Mapping):
        return [q for q in extra if isinstance(q, str) and q.strip()]
    out: list[str] = []
    for key in ("material_open_questions", "unresolved", "open_questions"):
        out.extend(_coverage_str_list(coverage, key))
    out.extend(q for q in extra if isinstance(q, str) and q.strip())
    return list(dict.fromkeys(out))


def _coverage_branches(coverage: Mapping[str, object] | None) -> tuple[list[str], list[str]]:
    """(covered branches, remaining branches) from the coverage envelope."""
    covered = _coverage_str_list(coverage, "major_entities_investigated")
    remaining: list[str] = []
    for key in ("major_entities_missing", "remaining_branches"):
        remaining.extend(_coverage_str_list(coverage, key))
    routes = _coverage_str_list(coverage, "routes_unsearched")
    for route in routes:
        if route not in covered:
            remaining.append(route)
    return covered, list(dict.fromkeys(remaining))


def _coverage_challenge(wave1: Wave1Result) -> WaveDecision | None:
    """Reject a sufficient claim with missing branches / material open questions / unsearched routes.

    Targeted wave resolves one uncertainty (first remaining/material item);
    the original question + covered-vs-remaining branches (+ relationship
    count as impact-channel proxy) ride the decision detail so the next wave
    keeps its objective and never redefines it.
    """
    coverage = wave1.coverage if isinstance(wave1.coverage, Mapping) and wave1.coverage.get("useful_for_question") == "sufficient" else None
    if coverage is None:
        return None
    covered, remaining = _coverage_branches(coverage)
    open_q = _coverage_questions(coverage, wave1.open_questions)
    missing = [r for r in remaining if r not in covered]
    target = (missing + open_q)[:1]
    if not target:
        return None
    question = wave1.question.strip() or target[0]
    detail = (f"coverage challenge: {len(missing)} branch(es) remaining, {len(open_q)} material open question(s), {len(wave1.relationships)} relationship(s); targeted follow-up on {target[0]!r} (original question: {question[:160]!r}; covered: {covered[:5]}; remaining: {missing[:5]})")
    return WaveDecision(True, "continue", detail, targeted_question=f"{question} :: targeted follow-up: {target[0]}", targeted_domain="SEC")


def _research_stop(wave1: Wave1Result) -> WaveDecision:
    """Material + actionable follow-up selection (committee gate; runs after the coverage challenge)."""
    if wave1.disagreement is None or not wave1.disagreement.requested_research:
        return WaveDecision(False, "no_questions", "committee requested no follow-up research")
    material = [r for r in wave1.disagreement.requested_research if _is_material(r)]
    if not material:
        return WaveDecision(False, "low_gain", "no material follow-up (all low-gain or unmotivated)")
    actionable = [r for r in material if _is_actionable(r)]
    if not actionable:
        return WaveDecision(False, "not_actionable", "material requests need non-SEC domains")
    actionable.sort(key=_decision_key, reverse=True)
    top = actionable[0]
    return WaveDecision(True, "continue", f"wave2 authorized: {top.question}",
                        targeted_question=top.question, targeted_domain=top.requested_source_domain)


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
    """Gate exactly one targeted SEC wave (coverage challenge first); persist the reason either way."""
    decision = _budget_stop(budgets, waves_used, jobs_used, tool_calls_used, elapsed_s)
    if decision is None:
        decision = _coverage_challenge(wave1)
    if decision is None:
        decision = _research_stop(wave1)
    deps.record_stop(wave1.session_id, f"{decision.stop_reason}:{decision.reason_detail}")
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
    "LoopDetector",
    "StopReason",
    "Wave1Result",
    "WaveDecision",
    "decide_wave2",
    "normalize_research_action",
    "run_wave1",
    "synthesize_wave1",
]
