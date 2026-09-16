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
    coverage_claim: str = ""
    high_rank_unexplored: bool = False
    answered: bool = True
    material_channels: tuple[str, ...] = ()
    branches_covered: tuple[str, ...] = ()
    finalized_claim_count: int = 0
    job_ids: tuple[str, ...] = ()
    job_created_before_run: bool = True
    jobs_concurrent: bool = True
    cross_role_write_rejected: bool = True
    roles_mutate_freeze: bool = False
    claims_resolve_to_freeze: bool = True
    requests_separate_from_evidence: bool = True
    searches: int = 0
    queries: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    exhibits: int = 0
    relationships_found: int = 0
    relationships_skipped: int = 0
    unresolved: tuple[str, ...] = ()
    stop_reason: str = ""


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


def _v_scenario_crashed(inp: EvalInput) -> str | None:
    return "scenario-crashed" if inp.scenario_crashed else None


def _v_unrecovered_failure(inp: EvalInput) -> str | None:
    if inp.failed_count > inp.recovered_count:
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


# OpenAI-bankruptcy MSFT regression gate (fixture cutoff 2026-08-10): the run fails when no material
# Microsoft exposure is present (channels: investment/ownership, commercial/revenue, receivable/credit,
# Azure/purchase commitment) or when no non-MSFT branch (AMZN/CoreWeave/AMD/Cerebras/ORCL per as_of) is covered.
_MSFT_CHANNELS: tuple[str, ...] = (
    "investment",
    "ownership",
    "commercial",
    "revenue",
    "receivable",
    "credit",
    "azure",
    "purchase commitment",
)

_MSFT_BRANCHES: tuple[str, ...] = ("amzn", "amazon", "coreweave", "amd", "cerebras", "orcl", "oracle")


def _msft_lower(inp: EvalInput) -> str:
    return " ".join((*inp.material_channels, inp.answer_text)).lower()


def _msft_branches(inp: EvalInput) -> str:
    return " ".join((*inp.branches_covered, inp.answer_text)).lower()


def _v_msft_exposure(inp: EvalInput) -> str | None:
    if inp.scenario_name != "msft-openai-bankruptcy-sec-only":
        return None
    if not any(channel in _msft_lower(inp) for channel in _MSFT_CHANNELS):
        return "msft-openai-no-material-msft-exposure"
    if not any(branch in _msft_branches(inp) for branch in _MSFT_BRANCHES):
        return "msft-openai-no-branch"
    return None


def _v_coverage_quality(inp: EvalInput) -> str | None:
    if inp.coverage_claim == "sufficient" and inp.high_rank_unexplored:
        return "coverage-overclaim"
    return None


_COMMITTEE_JOB_CHECKS: tuple[tuple[str, str], ...] = (
    ("job_created_before_run", "committee-jobs-not-preregistered"),
    ("jobs_concurrent", "committee-jobs-not-concurrent"),
    ("cross_role_write_rejected", "committee-cross-role-write-allowed"),
)


def _v_committee_invariants(inp: EvalInput) -> str | None:
    if len(set(inp.committee_freeze_ids)) > 1:
        return "committee-different-freeze"
    if inp.job_ids:
        if len(set(inp.job_ids)) < 3:
            return "committee-job-count"
        failed = next((code for attr, code in _COMMITTEE_JOB_CHECKS if not getattr(inp, attr)), None)
        if failed is not None:
            return failed
    return next((code for flag, code in ((inp.roles_mutate_freeze, "committee-mutates-freeze"), (not inp.claims_resolve_to_freeze, "committee-claims-unresolved"), (not inp.requests_separate_from_evidence, "committee-requests-as-evidence")) if flag), None)


def _v_finalize_answer(inp: EvalInput) -> str | None:
    if inp.scenario_name != "msft-openai-bankruptcy-sec-only" and inp.finalized_claim_count <= 0:
        return None
    if inp.finalized_claim_count > 0 and (not inp.answered or not inp.answer_text.strip()):
        return "finalized-without-answer"
    return None


_CHECKS: tuple[Callable[[EvalInput], str | None], ...] = (
    _v_scenario_crashed,
    _v_unrecovered_failure,
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
    _v_msft_exposure,
    _v_coverage_quality,
    _v_committee_invariants,
    _v_finalize_answer,
)

_PIT_PROVENANCE: frozenset[str] = frozenset(
    {"future-crossing-as_of", "fabricated-evidence-id", "fabricated-source", "prohibited-source"}
)


def _eval_completeness(inp: EvalInput) -> float:
    completeness = 1.0
    if inp.requires_evidence and not inp.evidence_ids:
        completeness -= 0.5
    if inp.claims_untraced > 0:
        completeness -= 0.5
    return max(0.0, completeness)


def _eval_metrics(inp: EvalInput, violations: tuple[str, ...]) -> EvalMetrics:
    return EvalMetrics(
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
        completeness=_eval_completeness(inp),
    )


def evaluate(inp: EvalInput) -> ScenarioResult:
    """Run every hard invariant over one outcome and compute its s31 metrics."""
    violations = tuple(v for v in (check(inp) for check in _CHECKS) if v is not None)
    return ScenarioResult(
        scenario_name=inp.scenario_name, passed=not violations, violations=violations, metrics=_eval_metrics(inp, violations)
    )


def _fixture_evidence(fixture: AgentFixture) -> tuple[tuple[str, ...], float, int]:
    evidence = tuple(fixture["evidence_ids"])
    requires = fixture["validator"]["requires_evidence"]
    coverage = 1.0 if evidence else 0.0
    untraced = 1 if (requires and not evidence) else 0
    return evidence, coverage, untraced


def _telemetry_int(telemetry: object, key: str) -> int:
    """Validated telemetry int (bools and mistyped values coerce to 0)."""
    value = telemetry.get(key) if isinstance(telemetry, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _telemetry_strs(telemetry: object, key: str) -> tuple[str, ...]:
    """Validated telemetry string tuple (mistyped lists coerce to ())."""
    value = telemetry.get(key) if isinstance(telemetry, dict) else None
    return tuple(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else ()


def eval_input_from_fixture(fixture: AgentFixture) -> EvalInput:
    """Convert a promoted fixture into an evaluable outcome."""
    evidence, coverage, untraced = _fixture_evidence(fixture)
    telemetry = fixture.get("telemetry") or {}
    stop_reason = telemetry.get("stop_reason") if isinstance(telemetry, dict) else None
    return EvalInput(scenario_name=fixture["scenario_name"], answer_text=fixture["answer_excerpt"], tool_calls=tuple(fixture["tool_calls"]), evidence_ids=evidence, evidence_coverage=coverage, as_of=fixture["as_of"], known_ats=tuple(fixture["known_ats"]), budget_used=fixture["budget_used"] or 0, budget_cap=fixture["budget_cap"] or 0, freeze_before=fixture["freeze_before"], freeze_after=fixture["freeze_after"], claims_untraced=untraced, requires_evidence=fixture["validator"]["requires_evidence"], searches=_telemetry_int(telemetry, "searches"), queries=_telemetry_strs(telemetry, "queries"), forms=_telemetry_strs(telemetry, "forms"), entities=_telemetry_strs(telemetry, "entities"), exhibits=_telemetry_int(telemetry, "exhibits"), relationships_found=_telemetry_int(telemetry, "relationships_found"), relationships_skipped=_telemetry_int(telemetry, "relationships_skipped"), unresolved=_telemetry_strs(telemetry, "unresolved"), stop_reason=stop_reason if isinstance(stop_reason, str) else "")


def _static_outcome(name: str) -> EvalInput:
    scenario = get_scenario(name)
    return EvalInput(
        scenario_name=name,
        answer_text="",
        tool_calls=scenario.expected_tools,
        as_of=scenario.as_of,
        requires_evidence=scenario.requires_evidence,
    )


def _fixture_outcome(name: str, fixtures_dir: Path | None) -> EvalInput | None:
    try:
        return eval_input_from_fixture(load_fixture(name, fixtures_dir))
    except (OSError, ValueError) as exc:
        logger.warning("fixture %s unreadable, using static outcome: %s", name, exc)
        return None


def outcomes_from_fixtures(
    scenario_names: Sequence[str] | None = None, fixtures_dir: Path | None = None
) -> list[EvalInput]:
    """Build outcomes for the suite: fixture-backed where promoted, static otherwise."""
    names = list(scenario_names) if scenario_names is not None else [s.name for s in list_scenarios()]
    saved = set(list_fixtures(fixtures_dir))
    outcomes: list[EvalInput] = []
    for name in names:
        if name in saved:
            loaded = _fixture_outcome(name, fixtures_dir)
            if loaded is not None:
                outcomes.append(loaded)
                continue
        outcomes.append(_static_outcome(name))
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
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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


def _persist_eval_run(
    conn: sqlite3.Connection, eval_run_id: str, model: str, provider: str, prompt_version: str, sha: str, started_at: str
) -> None:
    conn.execute(
        "INSERT INTO eval_runs (eval_run_id, model, provider, harness_version,"
        " prompt_version, git_sha, started_at, scenario_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (eval_run_id, model, provider, HARNESS_VERSION, prompt_version, sha, started_at, SCENARIO_VERSION),
    )


def _persist_result(conn: sqlite3.Connection, eval_run_id: str, result: ScenarioResult) -> None:
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


def _persist_failures(conn: sqlite3.Connection, eval_run_id: str, model: str, result: ScenarioResult, started_at: str) -> None:
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
        _persist_eval_run(conn, eval_run_id, model, provider, prompt_version, sha, started_at)
        for result in results:
            _persist_result(conn, eval_run_id, result)
            _persist_failures(conn, eval_run_id, model, result, started_at)
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


def _decode_violations(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _stored_row(cells: tuple[object, ...]) -> StoredScenarioResult:
    return StoredScenarioResult(
        scenario_name=_row_str(cells[0]),
        passed=cells[1] == 1,
        violations=_decode_violations(json.loads(_row_str(cells[2]))),
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
    return [_stored_row(tuple(row)) for row in rows]


@dataclass(frozen=True)
class ExperimentSummary:
    experiment_id: str
    before_run_id: str
    after_run_id: str
    delta_passed: int
    improved: tuple[str, ...]
    regressed: tuple[str, ...]


def _improved(before: dict[str, bool], after: dict[str, bool]) -> tuple[str, ...]:
    return tuple(sorted(name for name, ok in after.items() if ok and not before.get(name, True)))


def _regressed(before: dict[str, bool], after: dict[str, bool]) -> tuple[str, ...]:
    return tuple(sorted(name for name, ok in after.items() if not ok and before.get(name, False)))


def _passed_count(results: dict[str, bool]) -> int:
    return sum(1 for ok in results.values() if ok)


def _experiment_diff(
    before: dict[str, bool], after: dict[str, bool]
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    return _passed_count(after) - _passed_count(before), _improved(before, after), _regressed(before, after)


def _persist_experiment(summary: ExperimentSummary, payload: str, data_root: Path | None) -> None:
    with sqlite3.connect(_db_path(data_root)) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO experiments (experiment_id, before_run_id, after_run_id,"
            " created_at, summary_json) VALUES (?, ?, ?, ?, ?)",
            (
                summary.experiment_id,
                summary.before_run_id,
                summary.after_run_id,
                datetime.now(timezone.utc).isoformat(),
                payload,
            ),
        )


def compare_experiments(
    *, before_run_id: str, after_run_id: str, data_root: Path | None = None
) -> ExperimentSummary:
    """Diff two eval runs scenario by scenario and persist the experiment."""
    before = {r.scenario_name: r.passed for r in get_eval_results(before_run_id, data_root)}
    after = {r.scenario_name: r.passed for r in get_eval_results(after_run_id, data_root)}
    delta, improved, regressed = _experiment_diff(before, after)
    summary = ExperimentSummary(
        experiment_id=f"exp:{uuid.uuid4().hex[:12]}",
        before_run_id=before_run_id,
        after_run_id=after_run_id,
        delta_passed=delta,
        improved=improved,
        regressed=regressed,
    )
    payload = json.dumps(
        {"delta_passed": delta, "improved": list(improved), "regressed": list(regressed)}, sort_keys=True
    )
    _persist_experiment(summary, payload, data_root)
    return summary
