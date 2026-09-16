"""Deterministic director: per-wave orchestration + next-wave gate.

Pipeline (models decide content; infra owns everything else)::

    create-session > SEC job > verify artifacts > freeze E1
      > launch stockbot/bullbot/bearbot on the same freeze (parallel)
      > collect > compute disagreement
      > decide_next_wave: budgets when explicitly configured, then coverage
        challenge (sufficient claim with missing branches / material open /
        unsearched routes -> targeted SEC follow-up), then material +
        actionable committee follow-up, then the novelty/loop stop
        (zero-novelty or exact-repeat convergence -> no_novelty/loop_detected).
        Waves are sequence numbers, not a two-wave architecture: the runner
        loops 1..N and each wave resolves one uncertainty, then a new freeze +
        rerun analysis; the loop never redefines the objective (original
        question + covered vs remaining branches ride Wave1Result across waves).

Stopping reasons (persisted via ``record_stop``): ``complete``,
``max_waves``/``runtime_exceeded``/``jobs_exceeded`` (only when that budget is
explicitly configured; never a completeness proof), ``no_questions``,
``not_actionable``, ``low_gain``, ``no_novelty``, ``loop_detected``.

Fake-model sketch (no live calls): inject ``DirectorDeps`` with lambdas
returning canned ids/evidence/analyses; call ``run_wave1`` then
``decide_next_wave``; assert the stop reason persists and the next wave fires
only when a material actionable SEC request exists within budget.
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
    "no_novelty",
    "loop_detected",
]

WAVE1_ID = 1

# Zero-novelty counts that make a wave unproductive (all four zero -> zero-novelty).
NOVELTY_ZERO_KEYS = ("new_raw_documents", "new_evidence_records", "new_relationships", "resolved_questions")
# Consecutive zero-novelty waves on the same branch before that branch stops.
ZERO_NOVELTY_LIMIT = 2


@dataclass
class DirectorBudgets:
    """Runaway-test budget guard; None means unlimited (never a completeness proof)."""
    max_waves: int | None = None
    max_jobs: int | None = None
    max_tool_calls: int | None = None
    runtime_budget_s: float | None = None


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
    novelty: dict[str, object] = field(default_factory=dict)

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
    def precheck(self, action: tuple[str, str, str, str, tuple[str, ...], str, str, str]) -> dict[str, object]:
        """Pre-dispatch gate: an exact repeat of a no-progress action is never re-executed.

        Materially different actions always run; only the same normalized action
        whose prior run produced no evidence is blocked (telemetry entry appended).
        """
        prior = self.seen.get(action)
        if prior is not None and prior[1] <= 0:
            entry: dict[str, object] = {"action": list(action), "reason": "research_loop_detected"}
            self.telemetry.append(entry)
            return {"duplicate": True, "reason": "research_loop_detected"}
        return {"duplicate": False, "reason": ""}
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

    Every budget is optional: a field left at None is unlimited and never fires.
    max_waves is a runaway-test budget guard only; hitting it never proves
    coverage complete (settle carries limitations). Tool calls are unlimited
    by default (max_tool_calls=None); explicit int limits, when configured,
    are enforced at dispatch (repository/runner), not here.
    """
    _ = tool_calls_used
    if budgets.max_waves is not None and waves_used >= budgets.max_waves:
        return WaveDecision(False, "max_waves", f"waves_used={waves_used} max={budgets.max_waves}")
    if budgets.runtime_budget_s is not None and elapsed_s >= budgets.runtime_budget_s:
        return WaveDecision(False, "runtime_exceeded", f"elapsed={elapsed_s}s budget={budgets.runtime_budget_s}s")
    if budgets.max_jobs is not None and jobs_used >= budgets.max_jobs:
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
    return WaveDecision(True, "continue", f"wave {wave1.wave_id + 1} authorized: {top.question}",
                        targeted_question=top.question, targeted_domain=top.requested_source_domain)


def _novelty_count(novelty: Mapping[str, object], key: str) -> int:
    """Non-negative int for one novelty key; malformed or absent values count as 0."""
    value = novelty.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _novelty_stop(wave1: Wave1Result, novelty: Mapping[str, object] | None) -> WaveDecision | None:
    """Convergence gate: an unproductive branch, or a blocked exact repeat, stops the run.

    Zero-novelty means the wave produced no new raw document, no new evidence
    record, no new relationship, and no resolved question. One such wave may
    retry the branch; ZERO_NOVELTY_LIMIT consecutive ones stop it. A blocked
    exact repeat inside a zero-novelty wave stops it immediately. Counts never
    stop research: only the absence of material progress does.
    """
    counts: object = novelty if isinstance(novelty, Mapping) and novelty else wave1.novelty
    if not isinstance(counts, Mapping) or not counts:
        return None
    if any(_novelty_count(counts, key) > 0 for key in NOVELTY_ZERO_KEYS):
        return None
    blocked = _novelty_count(counts, "duplicate_actions_blocked")
    streak = _novelty_count(counts, "zero_novelty_waves") or 1
    if blocked:
        return WaveDecision(
            False, "loop_detected",
            f"wave {wave1.wave_id}: {blocked} exact repeated action(s) with no evidence progress and a zero-novelty wave",
        )
    if streak >= ZERO_NOVELTY_LIMIT:
        return WaveDecision(
            False, "no_novelty",
            f"wave {wave1.wave_id}: {streak} consecutive zero-novelty wave(s) on this branch "
            "(no new raw documents, evidence, relationships, or resolved questions)",
        )
    return None


def decide_next_wave(
    wave1: Wave1Result,
    *,
    deps: DirectorDeps,
    budgets: DirectorBudgets | None = None,
    waves_used: int = 1,
    jobs_used: int = 4,
    tool_calls_used: int = 0,
    elapsed_s: float = 0.0,
    novelty: Mapping[str, object] | None = None,
) -> WaveDecision:
    """Gate the next wave (budgets -> coverage challenge -> committee -> novelty/loop).

    ``waves_used`` is the wave number the caller has completed; waves are
    sequence numbers, so there is no fixed wave ceiling: every gate is decided
    by coverage, the committee, and measured progress. Budgets fire only when
    explicitly configured. The novelty/loop gate runs last and vetoes an
    otherwise-authorized wave when the branch just finished produced zero
    novelty (``no_novelty``) or blocked an exact repeated action
    (``loop_detected``).
    """
    budgets = budgets if budgets is not None else DirectorBudgets()
    decision = _budget_stop(budgets, waves_used, jobs_used, tool_calls_used, elapsed_s)
    if decision is None:
        decision = _coverage_challenge(wave1)
    if decision is None:
        decision = _research_stop(wave1)
    if decision.authorized:
        decision = _novelty_stop(wave1, novelty) or decision
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
    "NOVELTY_ZERO_KEYS",
    "WAVE1_ID",
    "ZERO_NOVELTY_LIMIT",
    "DirectorBudgets",
    "DirectorDeps",
    "LoopDetector",
    "StopReason",
    "Wave1Result",
    "WaveDecision",
    "decide_next_wave",
    "normalize_research_action",
    "run_wave1",
    "synthesize_wave1",
]
