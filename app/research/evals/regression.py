"""Regression fixtures: promote sessions into human-readable deterministic fixtures (s35).

Deterministic validators run first; a live regression run is only warranted
when a failure needs model reasoning to reproduce. Fixtures live under
evals/fixtures/agent_scenarios/<scenario-name>.json (indent=2 JSON).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from app.research.evals.scenarios import get_scenario

FIXTURE_FORMAT = "agent-scenario-fixture/v1"

# s35 deterministic-first mapping: each validator replaces a live reproduction.
# future-accepted -> PIT validator, dangling-dossier -> dossier validator,
# over-budget -> scheduler check, bull-new-research -> capability policy,
# freeze-mutate -> immutable-freeze constraint.
VALIDATORS: tuple[str, ...] = (
    "pit",
    "dossier",
    "scheduler-budget",
    "capability-policy",
    "immutable-freeze",
    "timeout_resume_requires_failed",
)

# Timeout closure (failure-closure invariant timeout-requires-failed-job): a Pi
# TimeoutExpired must persist session FAILED + job FAILED(TIMEOUT) +
# model.failed/wave.stopped with zero RUNNING jobs before LiveModelError
# surfaces. The promoted fixture carries that closure in answer_excerpt, so
# this stays deterministic over fixture JSON alone; a pre-fix "Pi timed out"
# excerpt with no closure markers fails.
_TIMEOUT_CLOSURE_MARKERS: tuple[str, ...] = (
    "LiveModelError",
    "session=failed",
    "job=failed",
    "model.failed",
    "wave.stopped",
    "running_jobs=0",
)


class FixtureValidator(TypedDict):
    pit_as_of: str | None
    expected_tools: list[str]
    requires_evidence: bool
    validators: list[str]


class AgentFixture(TypedDict):
    format: str
    scenario_name: str
    family: str
    session_id: str
    question: str
    as_of: str | None
    tool_calls: list[str]
    evidence_ids: list[str]
    known_ats: list[str]
    answer_excerpt: str
    budget_used: int | None
    budget_cap: int | None
    freeze_before: str | None
    freeze_after: str | None
    validator: FixtureValidator


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def default_fixtures_dir() -> Path:
    """Repo fixtures dir (overridable per call for tests)."""
    return _repo_root() / "evals" / "fixtures" / "agent_scenarios"


def build_fixture(
    *,
    session_id: str,
    scenario_name: str,
    question: str | None = None,
    as_of: str | None = None,
    tool_calls: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (),
    known_ats: tuple[str, ...] = (),
    answer_excerpt: str = "",
    budget_used: int | None = None,
    budget_cap: int | None = None,
    freeze_before: str | None = None,
    freeze_after: str | None = None,
) -> AgentFixture:
    """Assemble a fixture; question/as_of default to the scenario definition."""
    scenario = get_scenario(scenario_name)
    resolved_q = question if question is not None else scenario.question
    resolved_as_of = as_of if as_of is not None else scenario.as_of
    return {
        "format": FIXTURE_FORMAT,
        "scenario_name": scenario.name,
        "family": scenario.family.value,
        "session_id": session_id,
        "question": resolved_q,
        "as_of": resolved_as_of,
        "tool_calls": list(tool_calls),
        "evidence_ids": list(evidence_ids),
        "known_ats": list(known_ats),
        "answer_excerpt": answer_excerpt,
        "budget_used": budget_used,
        "budget_cap": budget_cap,
        "freeze_before": freeze_before,
        "freeze_after": freeze_after,
        "validator": {
            "pit_as_of": resolved_as_of,
            "expected_tools": list(scenario.expected_tools),
            "requires_evidence": scenario.requires_evidence,
            "validators": list(VALIDATORS),
        },
    }


def save_fixture(fixture: AgentFixture, fixtures_dir: Path | None = None) -> Path:
    """Write a human-readable fixture; returns its path."""
    dest_dir = fixtures_dir if fixtures_dir is not None else default_fixtures_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{fixture['scenario_name']}.json"
    path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def promote_to_fixture(
    *,
    session_id: str,
    scenario_name: str,
    question: str | None = None,
    as_of: str | None = None,
    tool_calls: tuple[str, ...] = (),
    evidence_ids: tuple[str, ...] = (),
    known_ats: tuple[str, ...] = (),
    answer_excerpt: str = "",
    fixtures_dir: Path | None = None,
) -> Path:
    """Validate the scenario name, build the fixture, and save it."""
    fixture = build_fixture(
        session_id=session_id,
        scenario_name=scenario_name,
        question=question,
        as_of=as_of,
        tool_calls=tool_calls,
        evidence_ids=evidence_ids,
        known_ats=known_ats,
        answer_excerpt=answer_excerpt,
    )
    return save_fixture(fixture, fixtures_dir)


def _req_str(raw: dict[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"fixture: {key!r} must be a string")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _opt_str(raw: dict[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"fixture: {key!r} must be a string or null")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _opt_int(raw: dict[str, object], key: str) -> int | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"fixture: {key!r} must be an int or null")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return value


def _req_str_list(raw: dict[str, object], key: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"fixture: {key!r} must be a list of strings")
    return [item for item in value if isinstance(item, str)]


def _fixture_raw(path: Path) -> tuple[dict[str, object], dict[str, object], bool]:
    decoded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"fixture {path}: top level must be an object")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    raw: dict[str, object] = {k: v for k, v in decoded.items() if isinstance(k, str)}
    validator_raw = raw.get("validator")
    if not isinstance(validator_raw, dict):
        raise ValueError(f"fixture {path}: validator must be an object")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    vraw: dict[str, object] = {k: v for k, v in validator_raw.items() if isinstance(k, str)}
    flag = vraw.get("requires_evidence")
    if not isinstance(flag, bool):
        raise ValueError(f"fixture {path}: requires_evidence must be bool")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    return raw, vraw, flag


def _fixture_body(raw: dict[str, object], vraw: dict[str, object], flag: bool) -> AgentFixture:
    return {
        "format": _req_str(raw, "format"),
        "scenario_name": _req_str(raw, "scenario_name"),
        "family": _req_str(raw, "family"),
        "session_id": _req_str(raw, "session_id"),
        "question": _req_str(raw, "question"),
        "as_of": _opt_str(raw, "as_of"),
        "tool_calls": _req_str_list(raw, "tool_calls"),
        "evidence_ids": _req_str_list(raw, "evidence_ids"),
        "known_ats": _req_str_list(raw, "known_ats"),
        "answer_excerpt": _req_str(raw, "answer_excerpt"),
        "budget_used": _opt_int(raw, "budget_used"),
        "budget_cap": _opt_int(raw, "budget_cap"),
        "freeze_before": _opt_str(raw, "freeze_before"),
        "freeze_after": _opt_str(raw, "freeze_after"),
        "validator": {
            "pit_as_of": _opt_str(vraw, "pit_as_of"),
            "expected_tools": _req_str_list(vraw, "expected_tools"),
            "requires_evidence": flag,
            "validators": _req_str_list(vraw, "validators"),
        },
    }


def load_fixture(scenario_name: str, fixtures_dir: Path | None = None) -> AgentFixture:
    """Load and validate one fixture; ValueError on malformed content."""
    dest_dir = fixtures_dir if fixtures_dir is not None else default_fixtures_dir()
    path = dest_dir / f"{scenario_name}.json"
    raw, vraw, flag = _fixture_raw(path)
    return _fixture_body(raw, vraw, flag)


def list_fixtures(fixtures_dir: Path | None = None) -> list[str]:
    """Sorted scenario names with a saved fixture."""
    dest_dir = fixtures_dir if fixtures_dir is not None else default_fixtures_dir()
    if not dest_dir.is_dir():
        return []
    return sorted(p.stem for p in dest_dir.glob("*.json") if p.is_file())


def _v_fixture_pit(fixture: AgentFixture) -> str | None:
    """PIT gate: any known_at after pit_as_of is a future crossing."""
    as_of = fixture["validator"]["pit_as_of"]
    if as_of is None:
        return None
    for known_at in fixture["known_ats"]:
        if known_at > as_of:
            return "future-crossing-as_of"
    return None


def _v_fixture_evidence(fixture: AgentFixture) -> str | None:
    """Evidence-required fixtures must cite at least one id."""
    if fixture["validator"]["requires_evidence"] and not fixture["evidence_ids"]:
        return "untraceable-dossier-claim"
    return None


def _v_fixture_budget(fixture: AgentFixture) -> str | None:
    """Over-budget fixtures fail the scheduler check."""
    used = fixture["budget_used"]
    cap = fixture["budget_cap"]
    if used is not None and cap is not None and used > cap:
        return "budget-violation"
    return None


def _v_fixture_capability(fixture: AgentFixture) -> str | None:
    """Unsupported families must not drive tools (capability policy)."""
    if fixture["family"] == "unsupported" and fixture["tool_calls"]:
        return "capability-policy-violation"
    return None


def _v_fixture_freeze(fixture: AgentFixture) -> str | None:
    """Freeze ids are immutable across the fixture."""
    before = fixture["freeze_before"]
    after = fixture["freeze_after"]
    if before is not None and after is not None and before != after:
        return "frozen-mutation"
    return None


def _v_fixture_timeout(fixture: AgentFixture) -> str | None:
    """Timeout fixtures must carry the failed-closure markers."""
    if fixture["scenario_name"] != "timeout-model-call-failed-resume":
        return None
    excerpt = fixture["answer_excerpt"]
    if any(marker not in excerpt for marker in _TIMEOUT_CLOSURE_MARKERS):
        return "timeout-without-failed-closure"
    return None


_FIXTURE_CHECKS = (
    _v_fixture_pit,
    _v_fixture_evidence,
    _v_fixture_budget,
    _v_fixture_capability,
    _v_fixture_freeze,
    _v_fixture_timeout,
)


def run_deterministic_validators(fixture: AgentFixture) -> list[str]:
    """s35 validators over one fixture; returns violation codes (empty = pass)."""
    return [code for code in (check(fixture) for check in _FIXTURE_CHECKS) if code is not None]


def _trace_line(header: object, kinds: dict[str, int], count: int) -> str:
    trace_id = getattr(header, "trace_id", "?")
    wave_id = getattr(header, "wave_id", "?")
    model = getattr(header, "model", "?")
    status = getattr(header, "status", "?")
    breakdown = ", ".join(f"{kind}={n}" for kind, n in sorted(kinds.items()))
    return (
        f"  trace {trace_id} wave={wave_id} model={model}"
        f" status={status} events={count}"
        + (f" [{breakdown}]" if breakdown else "")
    )


def _trace_lines(session_id: str, data_root: Path | None) -> list[str]:
    from app.research.evals.traces import get_trace_events, list_traces

    headers = list_traces(session_id, data_root)
    if not headers:
        return [f"session {session_id}: no traces recorded yet"]
    lines = [f"session {session_id}: {len(headers)} trace(s)"]
    for header in headers:
        kinds: dict[str, int] = {}
        for event in get_trace_events(header.trace_id, data_root):
            kinds[event.event_type] = kinds.get(event.event_type, 0) + 1
        lines.append(_trace_line(header, kinds, sum(kinds.values())))
    return lines


def inspect_session(session_id: str, data_root: Path | None = None) -> str:
    """Human-readable trace summary for one session; never raises."""
    try:
        return "\n".join(_trace_lines(session_id, data_root))
    except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return f"session {session_id}: trace inspect unavailable ({exc})"
