"""Deterministic evaluators: hard invariants (s30) + metrics (s31) + multi-model runner.

Pure checks over EvalInput: no model calls, no network. The multi-model
runner only labels a run (`--model`) and persists it with model,
harness version, prompt version, git SHA, timestamp, and scenario version;
`compare_experiments` diffs a before/after pair.

Never-fail catalogue (s30): searched-twice, browsed-first, multi-tool use,
two-valid-tools, and recovered-bad-candidate are explicitly NOT checks —
no invariant below references discovery counts, tool counts, or retries
punitively.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_data_root
from app.research.evals.regression import AgentFixture, list_fixtures, load_fixture
from app.research.evals.scenarios import SCENARIO_VERSION, get_scenario, list_scenarios
from app.research.evals.traces import HARNESS_VERSION

logger = logging.getLogger(__name__)

EVAL_DB_NAME = "eval_runs.sqlite"

# Sources outside the evidence policy; exact-match only.
PROHIBITED_SOURCES: frozenset[str] = frozenset({"unvetted-social-rumor"})

# Behaviours that must never fail a scenario (s30); kept as a named constant
# so reviewers can verify no check below penalises them.
NEVER_FAIL_PATTERNS: tuple[str, ...] = (
    "searched-twice",
    "browsed-first",
    "multi-tool-valid",
    "two-valid-tools",
    "recovered-bad-candidate",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_runs (
  eval_run_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  provider TEXT NOT NULL,
  harness_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  started_at TEXT NOT NULL,
  scenario_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_scenario_results (
  eval_run_id TEXT NOT NULL,
  scenario_name TEXT NOT NULL,
  passed INTEGER NOT NULL,
  violations_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  PRIMARY KEY (eval_run_id, scenario_name)
);
CREATE TABLE IF NOT EXISTS experiments (
  experiment_id TEXT PRIMARY KEY,
  before_run_id TEXT NOT NULL,
  after_run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failure_records (
  failure_id TEXT PRIMARY KEY,
  eval_run_id TEXT NOT NULL,
  scenario_name TEXT NOT NULL,
  violation TEXT NOT NULL,
  created_at TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_run ON eval_scenario_results(eval_run_id);
CREATE INDEX IF NOT EXISTS idx_failures_run ON failure_records(eval_run_id);
"""


@dataclass(frozen=True)
class EvalInput:
    """One evaluated outcome. Missing timestamps stay None; never invented."""

    scenario_name: str
    answer_text: str
    tool_calls: tuple[str, ...] = ()
    discovery_calls: int = 0
    job_count: int = 0
    failed_count: int = 0
    recovered_count: int = 0
    evidence_ids: tuple[str, ...] = ()
    evidence_coverage: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    wall_clock_ms: float = 0.0
    as_of: str | None = None
    known_ats: tuple[str, ...] = ()
    budget_used: int = 0
    budget_cap: int = 0
    freeze_before: str | None = None
    freeze_after: str | None = None
    sources_used: tuple[str, ...] = ()
    claims_untraced: int = 0
    committee_freeze_ids: tuple[str, ...] = ()
    requires_evidence: bool = True
    has_private_leak: bool = False
    has_fabricated_id: bool = False
    has_fabricated_source: bool = False
    scenario_crashed: bool = False


@dataclass(frozen=True)
class EvalMetrics:
    """s31 metrics for one scenario outcome."""

    success: bool
    wall_clock_ms: float
    job_count: int
    tool_call_count: int
    discovery_calls: int
    failed_count: int
    recovered_count: int
    evidence_count: int
    evidence_coverage: float
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    pit_provenance_violations: int
    disagreement: bool
    completeness: float

    def as_dict(self) -> dict[str, str | int | float | bool]:
        return {
            "success": self.success,
            "wall_clock_ms": self.wall_clock_ms,
            "job_count": self.job_count,
            "tool_call_count": self.tool_call_count,
            "discovery_calls": self.discovery_calls,
            "failed_count": self.failed_count,
            "recovered_count": self.recovered_count,
            "evidence_count": self.evidence_count,
            "evidence_coverage": self.evidence_coverage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost": self.estimated_cost,
            "pit_provenance_violations": self.pit_provenance_violations,
            "disagreement": self.disagreement,
            "completeness": self.completeness,
        }


@dataclass(frozen=True)
class ScenarioResult:
    scenario_name: str
    passed: bool
    violations: tuple[str, ...]
    metrics: EvalMetrics


def _v_execution_failure(inp: EvalInput) -> str | None:
    if inp.scenario_crashed or inp.failed_count > 0:
        return "scenario-execution-failed"
    return None


def _v_future_crossing(inp: EvalInput) -> str | None:
    if inp.as_of is None:
        return None
    for known_at in inp.known_ats:
        if known_at and known_at > inp.as_of:
            return "future-crossing-as_of"
    return None


def _v_fabricated_id(inp: EvalInput) -> str | None:
    return "fabricated-evidence-id" if inp.has_fabricated_id else None


def _v_fabricated_source(inp: EvalInput) -> str | None:
    return "fabricated-source" if inp.has_fabricated_source else None


def _v_private_leak(inp: EvalInput) -> str | None:
    return "private-data-leak" if inp.has_private_leak else None


def _v_budget(inp: EvalInput) -> str | None:
    if inp.budget_cap > 0 and inp.budget_used > inp.budget_cap:
        return "budget-violation"
    return None


def _v_freeze(inp: EvalInput) -> str | None:
    if inp.freeze_before is not None and inp.freeze_after is not None and inp.freeze_before != inp.freeze_after:
        return "frozen-mutation"
    return None


def _v_prohibited(inp: EvalInput) -> str | None:
    for source in inp.sources_used:
        if source in PROHIBITED_SOURCES:
            return "prohibited-source"
    return None


def _v_untraced(inp: EvalInput) -> str | None:
    return "untraceable-dossier-claim" if inp.claims_untraced > 0 else None


def _v_committee(inp: EvalInput) -> str | None:
    if len(set(inp.committee_freeze_ids)) > 1:
        return "committee-different-freeze"
    return None


def _v_no_evidence(inp: EvalInput) -> str | None:
    if inp.requires_evidence and inp.answer_text.strip() and not inp.evidence_ids:
        return "answer-without-required-evidence"
    return None


_CHECKS: tuple[Callable[[EvalInput], str | None], ...] = (
    _v_execution_failure,
    _v_future_crossing,
    _v_fabricated_id,
    _v_fabricated_source,
    _v_private_leak,
    _v_budget,
    _v_freeze,
    _v_prohibited,
    _v_untraced,
    _v_committee,
    _v_no_evidence,
)

_PIT_PROVENANCE: frozenset[str] = frozenset(
    {"future-crossing-as_of", "fabricated-evidence-id", "fabricated-source", "prohibited-source"}
)


def evaluate(inp: EvalInput) -> ScenarioResult:
    """Run every hard invariant over one outcome and compute its s31 metrics."""
    violations = tuple(v for v in (check(inp) for check in _CHECKS) if v is not None)
    completeness = 1.0
    if inp.requires_evidence and not inp.evidence_ids:
        completeness -= 0.5
    if inp.claims_untraced > 0:
        completeness -= 0.5
    metrics = EvalMetrics(
        success=not violations,
        wall_clock_ms=inp.wall_clock_ms,
        job_count=inp.job_count,
        tool_call_count=len(inp.tool_calls),
        discovery_calls=inp.discovery_calls,
        failed_count=inp.failed_count,
        recovered_count=inp.recovered_count,
        evidence_count=len(inp.evidence_ids),
        evidence_coverage=inp.evidence_coverage,
        input_tokens=inp.input_tokens,
        output_tokens=inp.output_tokens,
        estimated_cost=inp.estimated_cost,
        pit_provenance_violations=sum(1 for v in violations if v in _PIT_PROVENANCE),
        disagreement=len(set(inp.committee_freeze_ids)) > 1,
        completeness=max(0.0, completeness),
    )
    return ScenarioResult(
        scenario_name=inp.scenario_name, passed=not violations, violations=violations, metrics=metrics
    )


def eval_input_from_fixture(fixture: AgentFixture) -> EvalInput:
    """Convert a promoted fixture into an evaluable outcome."""
    requires = fixture["validator"]["requires_evidence"]
    evidence = tuple(fixture["evidence_ids"])
    return EvalInput(
        scenario_name=fixture["scenario_name"],
        answer_text=fixture["answer_excerpt"],
        tool_calls=tuple(fixture["tool_calls"]),
        evidence_ids=evidence,
        evidence_coverage=1.0 if evidence else 0.0,
        as_of=fixture["as_of"],
        known_ats=tuple(fixture["known_ats"]),
        budget_used=fixture["budget_used"] or 0,
        budget_cap=fixture["budget_cap"] or 0,
        freeze_before=fixture["freeze_before"],
        freeze_after=fixture["freeze_after"],
        claims_untraced=1 if (requires and not evidence) else 0,
        requires_evidence=requires,
    )


def outcomes_from_fixtures(
    scenario_names: Sequence[str] | None = None, fixtures_dir: Path | None = None
) -> list[EvalInput]:
    """Build outcomes for the suite: fixture-backed where promoted, static otherwise."""
    names = list(scenario_names) if scenario_names is not None else [s.name for s in list_scenarios()]
    saved = set(list_fixtures(fixtures_dir))
    outcomes: list[EvalInput] = []
    for name in names:
        scenario = get_scenario(name)
        if name in saved:
            try:
                outcomes.append(eval_input_from_fixture(load_fixture(name, fixtures_dir)))
                continue
            except (OSError, ValueError) as exc:
                logger.warning("fixture %s unreadable, using static outcome: %s", name, exc)
        outcomes.append(
            EvalInput(
                scenario_name=name,
                answer_text="",
                tool_calls=scenario.expected_tools,
                as_of=scenario.as_of,
                requires_evidence=scenario.requires_evidence,
            )
        )
    return outcomes


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = proc.stdout.strip()
        return sha if sha else "unknown"
    except Exception:
        return "unknown"


def _db_path(data_root: Path | None) -> Path:
    root = data_root if data_root is not None else get_data_root()
    return root / EVAL_DB_NAME


@dataclass(frozen=True)
class EvalRunSummary:
    eval_run_id: str
    model: str
    provider: str
    passed_count: int
    failed_count: int
    scenario_count: int


def run_eval_suite(
    *,
    model: str,
    provider: str = "unknown",
    prompt_version: str = "v1",
    git_sha: str | None = None,
    outcomes: Sequence[EvalInput] = (),
    data_root: Path | None = None,
) -> EvalRunSummary:
    """Evaluate outcomes and persist the stamped run; returns its summary."""
    eval_run_id = f"eval:{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(timezone.utc).isoformat()
    sha = git_sha if git_sha is not None else _git_sha()
    results = [evaluate(inp) for inp in outcomes]
    db = _db_path(data_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO eval_runs (eval_run_id, model, provider, harness_version,"
            " prompt_version, git_sha, started_at, scenario_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (eval_run_id, model, provider, HARNESS_VERSION, prompt_version, sha, started_at, SCENARIO_VERSION),
        )
        for result in results:
            conn.execute(
                "INSERT INTO eval_scenario_results (eval_run_id, scenario_name, passed,"
                " violations_json, metrics_json) VALUES (?, ?, ?, ?, ?)",
                (
                    eval_run_id,
                    result.scenario_name,
                    1 if result.passed else 0,
                    json.dumps(list(result.violations), sort_keys=True),
                    json.dumps(result.metrics.as_dict(), sort_keys=True),
                ),
            )
            for violation in result.violations:
                conn.execute(
                    "INSERT INTO failure_records (failure_id, eval_run_id, scenario_name,"
                    " violation, created_at, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"fail:{uuid.uuid4().hex[:12]}",
                        eval_run_id,
                        result.scenario_name,
                        violation,
                        started_at,
                        json.dumps({"model": model, "scenario_version": SCENARIO_VERSION}, sort_keys=True),
                    ),
                )
    passed = sum(1 for r in results if r.passed)
    return EvalRunSummary(
        eval_run_id=eval_run_id,
        model=model,
        provider=provider,
        passed_count=passed,
        failed_count=len(results) - passed,
        scenario_count=len(results),
    )


@dataclass(frozen=True)
class StoredScenarioResult:
    scenario_name: str
    passed: bool
    violations: tuple[str, ...]


@dataclass(frozen=True)
class EvalRunRow:
    eval_run_id: str
    model: str
    provider: str
    harness_version: str
    prompt_version: str
    git_sha: str
    started_at: str
    scenario_version: str


def _row_str(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def get_eval_run(eval_run_id: str, data_root: Path | None = None) -> EvalRunRow | None:
    """Fetch one eval run header; None when absent."""
    with sqlite3.connect(_db_path(data_root)) as conn:
        conn.executescript(_SCHEMA)
        row = conn.execute(
            "SELECT eval_run_id, model, provider, harness_version, prompt_version,"
            " git_sha, started_at, scenario_version FROM eval_runs WHERE eval_run_id = ?",
            (eval_run_id,),
        ).fetchone()
    if row is None:
        return None
    cells = tuple(row)
    return EvalRunRow(
        eval_run_id=_row_str(cells[0]),
        model=_row_str(cells[1]),
        provider=_row_str(cells[2]),
        harness_version=_row_str(cells[3]),
        prompt_version=_row_str(cells[4]),
        git_sha=_row_str(cells[5]),
        started_at=_row_str(cells[6]),
        scenario_version=_row_str(cells[7]),
    )


def get_eval_results(eval_run_id: str, data_root: Path | None = None) -> list[StoredScenarioResult]:
    """Per-scenario results for one eval run."""
    with sqlite3.connect(_db_path(data_root)) as conn:
        conn.executescript(_SCHEMA)
        rows = conn.execute(
            "SELECT scenario_name, passed, violations_json FROM eval_scenario_results"
            " WHERE eval_run_id = ? ORDER BY scenario_name ASC",
            (eval_run_id,),
        ).fetchall()
    out: list[StoredScenarioResult] = []
    for row in rows:
        cells = tuple(row)
        decoded: object = json.loads(_row_str(cells[2]))
        violations = tuple(item for item in decoded if isinstance(item, str)) if isinstance(decoded, list) else ()
        out.append(
            StoredScenarioResult(
                scenario_name=_row_str(cells[0]),
                passed=cells[1] == 1,
                violations=violations,
            )
        )
    return out


@dataclass(frozen=True)
class ExperimentSummary:
    experiment_id: str
    before_run_id: str
    after_run_id: str
    delta_passed: int
    improved: tuple[str, ...]
    regressed: tuple[str, ...]


def compare_experiments(
    *, before_run_id: str, after_run_id: str, data_root: Path | None = None
) -> ExperimentSummary:
    """Diff two eval runs scenario by scenario and persist the experiment."""
    before = {r.scenario_name: r.passed for r in get_eval_results(before_run_id, data_root)}
    after = {r.scenario_name: r.passed for r in get_eval_results(after_run_id, data_root)}
    improved = sorted(name for name, ok in after.items() if ok and not before.get(name, True))
    regressed = sorted(name for name, ok in after.items() if not ok and before.get(name, False))
    delta = sum(1 for ok in after.values() if ok) - sum(1 for ok in before.values() if ok)
    summary = ExperimentSummary(
        experiment_id=f"exp:{uuid.uuid4().hex[:12]}",
        before_run_id=before_run_id,
        after_run_id=after_run_id,
        delta_passed=delta,
        improved=tuple(improved),
        regressed=tuple(regressed),
    )
    payload = json.dumps(
        {"delta_passed": delta, "improved": improved, "regressed": regressed}, sort_keys=True
    )
    with sqlite3.connect(_db_path(data_root)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO experiments (experiment_id, before_run_id, after_run_id,"
            " created_at, summary_json) VALUES (?, ?, ?, ?, ?)",
            (
                summary.experiment_id,
                before_run_id,
                after_run_id,
                datetime.now(timezone.utc).isoformat(),
                payload,
            ),
        )
    return summary
