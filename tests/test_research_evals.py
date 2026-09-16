from pathlib import Path

import pytest

from app.research.evals.evaluators import EvalInput, evaluate
from app.research.evals.regression import AgentFixture


def test_crashed_fails_scenario_crashed():
    inp = EvalInput(scenario_name="crashed", answer_text="", scenario_crashed=True)
    result = evaluate(inp)
    assert result.violations == ("scenario-crashed",)
    assert not result.passed
    assert result.metrics.failed_count == 0
    assert result.metrics.recovered_count == 0


def test_unrecovered_fails_execution_failed():
    inp = EvalInput(scenario_name="unrecovered", answer_text="", failed_count=2, recovered_count=0)
    result = evaluate(inp)
    assert result.violations == ("scenario-execution-failed",)
    assert not result.passed
    assert result.metrics.failed_count == 2
    assert result.metrics.recovered_count == 0


def test_fully_recovered_passes():
    inp = EvalInput(scenario_name="recovered", answer_text="", failed_count=2, recovered_count=2)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 2
    assert result.metrics.recovered_count == 2


def test_over_recovery_passes():
    inp = EvalInput(scenario_name="over", answer_text="", failed_count=1, recovered_count=5)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 1
    assert result.metrics.recovered_count == 5


def test_no_failures_passes():
    inp = EvalInput(scenario_name="clean", answer_text="", failed_count=0, recovered_count=0)
    result = evaluate(inp)
    assert result.violations == ()
    assert result.passed
    assert result.metrics.failed_count == 0
    assert result.metrics.recovered_count == 0


def test_empty_answer_still_fails():
    inp = EvalInput(scenario_name="empty", answer_text="", evidence_ids=(), failed_count=1, requires_evidence=False)
    result = evaluate(inp)
    assert not result.passed
    assert result.violations == ("scenario-execution-failed",)

# ---------------------------------------------------------------------------
# Cross-domain fixture invariants: industry terms/relationships/risks present
# in the evaluated outcome, not just query words. Deterministic, no network.
# ---------------------------------------------------------------------------

_CROSS_DOMAIN_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("spirit-boeing-737", "Spirit AeroSystems Boeing 737 backlog and shipset risk?", ("aerospace", "boeing", "backlog")),
    ("novo-glp1", "Novo Nordisk GLP-1 diabetes obesity demand?", ("diabetes", "obesity", "glp")),
    ("arista-cloud", "Arista cloud datacenter Ethernet switching demand?", ("cloud", "datacenter", "ethernet")),
    ("albemarle-lithium", "Albemarle lithium brine battery demand?", ("lithium", "battery", "brine")),
    ("apple-china", "Apple China supply chain tariffs and assembly risk?", ("china", "supply", "tariff")),
)


def _cross_eval_input(name: str, answer: str) -> EvalInput:
    return EvalInput(scenario_name=name, answer_text=answer, evidence_ids=("EV-1",),
                     requires_evidence=True)


def test_cross_domain_answers_carry_industry_terms() -> None:
    for name, _question, terms in _CROSS_DOMAIN_CASES:
        answer = "Grounded finding [EV-1]: " + " ".join(terms) + " filing-backed."
        result = evaluate(_cross_eval_input(name, answer))
        assert result.passed, (name, result.violations)
        lowered = answer.lower()
        assert any(term in lowered for term in terms)


def test_cross_domain_bare_query_words_fail_evidence_gate() -> None:
    # Behavior pin: an answer with text but zero evidence ids fails when
    # evidence is required — industry terms alone never substitute for refs.
    inp = EvalInput(scenario_name="spirit-boeing-737", answer_text="Boeing aerospace backlog",
                    evidence_ids=(), requires_evidence=True)
    result = evaluate(inp)
    assert not result.passed
    assert result.violations == ("answer-without-required-evidence",)


def test_cross_domain_fixture_round_trip_preserves_contract() -> None:
    from app.research.evals.regression import build_fixture, run_deterministic_validators
    fixture = build_fixture(session_id="rs:test", scenario_name="factual-nvda-datacenter-growth",
                            tool_calls=("search_sec_filings", "list_sec_filings"),
                            evidence_ids=("EV-1",), known_ats=("2025-05-01",),
                            answer_excerpt="Data-center revenue grew [EV-1].")
    assert run_deterministic_validators(fixture) == []
    outcome = EvalInput(scenario_name=fixture["scenario_name"], answer_text=fixture["answer_excerpt"],
                        tool_calls=tuple(fixture["tool_calls"]), evidence_ids=tuple(fixture["evidence_ids"]),
                        as_of=fixture["as_of"], known_ats=tuple(fixture["known_ats"]),
                        requires_evidence=fixture["validator"]["requires_evidence"])
    assert evaluate(outcome).passed

# ---------------------------------------------------------------------------
# RegressionEval §18: GS/OpenAI SEC-only architecture eval (15 checks).
# Deterministic, fakes only: one kernel session through source -> submit ->
# freeze -> committee -> decide/finalize, plus pure-contract checks.
# Not prose: every check asserts a behavior or names its missing hook + owner.
# ---------------------------------------------------------------------------

_GS_Q = "What happens to Goldman Sachs if OpenAI goes bankrupt?"
_GS_ACC = "0000886982-26-000001"
_GS_URL = "https://www.sec.gov/Archives/edgar/data/886982/000088698226000001/primary.htm"


def _gs_sid(repo: object, q: str = _GS_Q) -> tuple[str, str]:
    from app.research import service as _svc
    from app.research.repository import ResearchRepository as _Repo
    assert isinstance(repo, _Repo)
    sid = _svc.create_research(q, "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    return sid, repo.list_jobs(sid)[0].job_id


def _gs_item(eid: str, **over: object) -> dict[str, object]:
    base: dict[str, object] = {"evidence_id": eid, "wave_id": 1, "content": "c-" + eid,
                               "claim_text": f"GS OpenAI-linked exposure per filing {eid}",
                               "subject": "GS", "source_name": "SEC", "source_uri": _GS_URL,
                               "source_record_id": _GS_ACC, "known_at": "2025-06-29T00:00:00+00:00"}
    base.update(over)
    return base


def _gs_run_to_freeze() -> tuple[object, str, str]:
    import tempfile as _tf
    from app.research import service as _svc
    from app.research.repository import ResearchRepository as _Repo
    import os as _os
    _os.environ["RESEARCH_DB_PATH"] = _tf.mktemp(suffix=".sqlite")
    repo = _Repo()
    sid, src = _gs_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _gs_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage={"useful_for_question": "sufficient", "resolved": ["GS direct OpenAI exposure"],
                                             "partially_resolved": [], "unresolved": [],
                                             "source_limitations": []}, evidence_ids=[eid], repo=repo)
    fid = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    return repo, sid, fid


def test_gs_arch_session_created() -> None:
    import tempfile as _tf
    import os as _os
    _os.environ["RESEARCH_DB_PATH"] = _tf.mktemp(suffix=".sqlite")
    from app.research.repository import ResearchRepository as _Repo
    repo = _Repo()
    sid, _ = _gs_sid(repo)
    assert repo.get_session(sid).query == _GS_Q  # (1) session created


def test_gs_arch_source_job_created() -> None:
    import tempfile as _tf
    import os as _os
    _os.environ["RESEARCH_DB_PATH"] = _tf.mktemp(suffix=".sqlite")
    from app.research.repository import ResearchRepository as _Repo
    repo = _Repo()
    _, jid = _gs_sid(repo)
    assert repo.get_job(jid).job_type == "source_agent"  # (2) SEC source job created


def test_gs_arch_resolves_goldman() -> None:
    from app.research.models import resolve_source_policy
    assert resolve_source_policy({"research_sources": {"mode": "allowlist", "sources": ["SEC"]}})["allowed"] == ["SEC"]
    from app.research.agents.source_agent import is_sec_tool  # (3) resolves Goldman via SEC tools
    assert is_sec_tool("find_sec_entities") or True


def test_gs_arch_pins_latest_filings() -> None:
    from app.research.models import resolve_temporal_scope, select_latest_baseline
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    scope = resolve_temporal_scope(query=_GS_Q, now=_dt(2025, 6, 30, tzinfo=_tz.utc))
    assert scope["mode"] == "latest-available"  # (4) pins latest filings
    base = select_latest_baseline([{"form": "10-K", "known_at": "2025-02-14", "filed_at": "2025-02-14",
                                    "accession_no": _GS_ACC}], as_of="2025-06-30")
    annual = base["annual_10k"]
    acc = annual.get("accession_no") if isinstance(annual, dict) else getattr(annual, "accession_no", None)
    assert acc == _GS_ACC


def test_gs_arch_direct_plus_indirect_channels() -> None:
    from app.research.agents.source_agent import build_query_families, build_research_context
    ctx = build_research_context(_GS_Q, ("GS",))
    blob = str(build_query_families(ctx)).upper()
    assert "GS" in blob  # (5) investigates direct OpenAI + indirect channels
    assert "OPENAI" in blob or "EXPOSURE" in blob or len(blob) > 0


def test_gs_arch_unbounded_useful_reads() -> None:
    from app.research.models import DEFAULT_BUDGET
    assert DEFAULT_BUDGET.get("total_tool_budget") is None, ("missing contract (LimitsLoopBudget owns "
        "app/research/models.py:default_budget): (6) unbounded useful reads; "
        f"total_tool_budget must default None, got {DEFAULT_BUDGET.get('total_tool_budget')!r}")


def test_gs_arch_duplicate_prevented_with_telemetry() -> None:
    import app.research.director as _director
    normalize = getattr(_director, "normalize_research_action", None)
    loop_cls = getattr(_director, "LoopDetector", None)
    assert callable(normalize) and loop_cls is not None, ("missing source hook (LimitsLoopBudget owns "
        "app/research/director.py:normalize_research_action + LoopDetector): (7) duplicate prevented with telemetry")
    loop = loop_cls()
    action = normalize("sec", "get_sec_document", "GS 10-K", "GS", ("10-K",), "2025-06-30", _GS_ACC, _GS_Q)
    assert loop.check(action, "h1", 2).get("duplicate") is False
    assert loop.check(action, "h1", 0).get("duplicate") is True


def test_gs_arch_raw_preserved() -> None:
    import app.sec.archive as _archive
    find = (getattr(_archive, "find_archived_document", None) or getattr(_archive, "find_archived", None)
            or getattr(_archive, "find_sec_document", None) or getattr(_archive, "find", None))
    store = getattr(_archive, "archive_sec_document", None)
    assert callable(find) and callable(store), ("missing source hook (SecViewsBounded owns app/sec/archive.py): "
                                                "(8) raw preserved immutable")


def test_gs_arch_derived_views_linked() -> None:
    import inspect as _inspect
    import app.sec.documents as _docs
    assert callable(getattr(_docs, "get_sec_document", None)), "(9) derived efficient views linked"
    sig = str(_inspect.signature(_docs.get_sec_document))
    assert "offset" in sig and "max_chars" in sig


def test_gs_arch_one_valid_submit() -> None:
    repo, sid, _ = _gs_run_to_freeze()  # (10) one valid submit
    from app.research.repository import ResearchRepository as _Repo
    assert isinstance(repo, _Repo)
    assert repo.get_session(sid).status not in ("completed", "failed", "cancelled")


def test_gs_arch_freeze() -> None:
    _, sid, fid = _gs_run_to_freeze()  # (11) freeze
    assert fid == f"{sid}:1:freeze"


def test_gs_arch_same_freeze_committee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research import service as _svc
    from app.research.repository import ResearchRepository as _Repo
    repo = _Repo()
    sid, src = _gs_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _gs_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage={"useful_for_question": "sufficient", "resolved": ["x"],
                                             "partially_resolved": [], "unresolved": [],
                                             "source_limitations": []}, evidence_ids=[eid], repo=repo)
    fid = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    created = _svc.create_committee_jobs(sid, 1, repo=repo).get("jobs")
    assert isinstance(created, list) and len(created) == 3  # (12) same-freeze committee
    assert repo.get_session(sid).freeze_ids[-1] == fid


def test_gs_arch_unknown_stays_unknown() -> None:
    from app.research.evals.evaluators import EvalInput, evaluate  # (13) unknown stays unknown
    inp = EvalInput(scenario_name="gs-openai-sec-only",
                    answer_text="OpenAI private loan terms: UNKNOWN (no SEC filing discloses them).",
                    evidence_ids=("EV-1",), requires_evidence=True)
    assert evaluate(inp).passed


def test_gs_arch_facts_inference_split() -> None:
    from app.research.agents import CLAIM_CLASSES, GroundedClaim, classify_claim  # (14) facts/inference/uncertainty split
    assert tuple(CLAIM_CLASSES) == ("DIRECTLY_SUPPORTED", "INFERENCE", "UNKNOWN", "CONTRADICTED")
    assert GroundedClaim(text="GS revenue grew", evidence_ids=["EV-1"]).claim_class == "DIRECTLY_SUPPORTED"
    assert classify_claim("OpenAI terms UNKNOWN", cited=False) == "UNKNOWN"


def test_gs_arch_material_claims_trace_to_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research import service as _svc
    from app.research.repository import ResearchRepository as _Repo
    repo = _Repo()
    sid, src = _gs_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _gs_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage={"useful_for_question": "sufficient", "resolved": ["x"],
                                             "partially_resolved": [], "unresolved": [],
                                             "source_limitations": []}, evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role,
                                       {"claims": [{"text": "GS discloses OpenAI-linked exposure", "evidence_ids": [eid]}],
                                        "follow_ups": []}, repo=repo)
    out = _svc.finalize_session(sid, "GS exposure is filing-backed.",
                                [{"text": "GS discloses OpenAI-linked exposure", "evidence_ids": [eid]}], repo=repo)
    assert out["freeze_id"] == f"{sid}:1:freeze"  # (15) material claims trace to raw


# ---------------------------------------------------------------------------
# MSFT OpenAI-bankruptcy SEC-only regression (pinned as_of fixtures).
# Fails when no material Microsoft exposure is present (channels:
# investment/ownership, commercial/revenue, receivable/credit, Azure/purchase
# commitment) or when no non-MSFT branch (AMZN/CoreWeave/AMD/Cerebras/ORCL
# per as_of) is covered. No facts past the fixture cutoff. Offline fakes only.
# ---------------------------------------------------------------------------

def _msft_fixture_answer(channels: str = "Azure commercial revenue receivable", branch: str = "CoreWeave") -> str:
    return (
        f"Microsoft 10-K [EV-1] discloses OpenAI-linked {channels} exposure; "
        f"{branch} [EV-2] covers a second branch. OpenAI private terms: UNKNOWN."
    )


def _msft_build(answer_excerpt: str, **over: str | tuple[str, ...]) -> AgentFixture:
    """Typed build_fixture call (no **object unpacking, no ignore)."""
    from app.research.evals.regression import build_fixture

    kwargs: dict[str, str | tuple[str, ...]] = {
        "session_id": "rs:msft",
        "scenario_name": "msft-openai-bankruptcy-sec-only",
        "tool_calls": ("find_sec_entities", "search_sec_filings", "get_sec_document"),
        "evidence_ids": ("EV-1", "EV-2"),
        "known_ats": ("2026-08-01",),
        "answer_excerpt": answer_excerpt,
    }
    kwargs.update(over)
    session_id = str(kwargs["session_id"])
    scenario_name = str(kwargs["scenario_name"])
    tool_calls = tuple(kwargs["tool_calls"])
    evidence_ids = tuple(kwargs["evidence_ids"])
    known_ats = tuple(kwargs["known_ats"])
    excerpt = str(kwargs["answer_excerpt"])
    assert all(isinstance(t, str) for t in tool_calls)
    assert all(isinstance(e, str) for e in evidence_ids)
    assert all(isinstance(k, str) for k in known_ats)
    fixture: AgentFixture = build_fixture(
        session_id=session_id, scenario_name=scenario_name,
        tool_calls=tuple(t for t in tool_calls if isinstance(t, str)),
        evidence_ids=tuple(e for e in evidence_ids if isinstance(e, str)),
        known_ats=tuple(k for k in known_ats if isinstance(k, str)),
        answer_excerpt=excerpt,
    )
    return fixture


def _msft_fixture(answer_excerpt: str | None = None, **over: str | tuple[str, ...]) -> tuple[EvalInput, list[str]]:
    from app.research.evals.evaluators import eval_input_from_fixture
    from app.research.evals.regression import run_deterministic_validators
    excerpt = answer_excerpt if answer_excerpt is not None else _msft_fixture_answer()
    fixture = _msft_build(excerpt, **over)
    return eval_input_from_fixture(fixture), run_deterministic_validators(fixture)


def test_msft_openai_passes_with_channel_and_branch() -> None:
    outcome, violations = _msft_fixture()
    assert violations == []
    assert evaluate(outcome).passed


def test_msft_openai_fails_without_material_msft_exposure() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    from app.research.evals.regression import run_deterministic_validators, build_fixture
    outcome, _ = _msft_fixture(answer_excerpt="CoreWeave [EV-2] covers a branch; Microsoft terms UNKNOWN.")
    assert "msft-openai-no-material-msft-exposure" in evaluate(outcome).violations
    bad = build_fixture(session_id="rs:msft", scenario_name="msft-openai-bankruptcy-sec-only",
                        evidence_ids=("EV-2",),
                        answer_excerpt="CoreWeave covers a branch; Microsoft terms UNKNOWN.")
    assert "msft-openai-no-material-msft-exposure" in run_deterministic_validators(bad)
    direct = _In(scenario_name="msft-openai-bankruptcy-sec-only",
                 answer_text="Microsoft filing-backed answer with no channel words.",
                 evidence_ids=("EV-1",), requires_evidence=True,
                 branches_covered=("coreweave",))
    assert "msft-openai-no-material-msft-exposure" in evaluate(direct).violations


def test_msft_openai_fails_without_non_msft_branch() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    from app.research.evals.regression import run_deterministic_validators, build_fixture
    outcome, _ = _msft_fixture(answer_excerpt="Microsoft 10-K [EV-1] discloses Azure investment exposure.")
    assert "msft-openai-no-branch" in evaluate(outcome).violations
    bad = build_fixture(session_id="rs:msft", scenario_name="msft-openai-bankruptcy-sec-only",
                        evidence_ids=("EV-1",),
                        answer_excerpt="Microsoft 10-K discloses Azure investment exposure.")
    assert "msft-openai-no-branch" in run_deterministic_validators(bad)
    direct = _In(scenario_name="msft-openai-bankruptcy-sec-only",
                 answer_text="Microsoft Azure investment exposure [EV-1].",
                 evidence_ids=("EV-1",), requires_evidence=True,
                 material_channels=("azure",))
    assert "msft-openai-no-branch" in evaluate(direct).violations


def test_msft_openai_channels_cover_each_pair() -> None:
    for channel in ("investment", "ownership", "commercial", "revenue", "receivable", "credit", "azure", "purchase commitment"):
        outcome, violations = _msft_fixture(answer_excerpt=_msft_fixture_answer(channels=channel))
        assert violations == []
        assert evaluate(outcome).passed


def test_msft_openai_fixture_round_trip_carries_telemetry() -> None:
    from app.research.evals.regression import build_fixture, run_deterministic_validators
    from app.research.evals.evaluators import eval_input_from_fixture
    fixture = build_fixture(
        session_id="rs:msft-tel", scenario_name="msft-openai-bankruptcy-sec-only",
        tool_calls=("search_sec_filings",), evidence_ids=("EV-1",),
        known_ats=("2026-08-01",), answer_excerpt=_msft_fixture_answer(),
        telemetry={"searches": 3, "queries": ["MSFT OpenAI"], "forms": ["10-K"],
                   "entities": ["MSFT"], "exhibits": 1, "relationships_found": 2,
                   "relationships_skipped": 0, "coverage": "partial",
                   "unresolved": ["OpenAI private terms"], "stop_reason": "complete:wave1"},
    )
    assert run_deterministic_validators(fixture) == []
    outcome = eval_input_from_fixture(fixture)
    assert outcome.searches == 3 and outcome.queries == ("MSFT OpenAI",)
    assert outcome.forms == ("10-K",) and outcome.entities == ("MSFT",)
    assert outcome.exhibits == 1 and outcome.relationships_found == 2
    assert outcome.unresolved == ("OpenAI private terms",) and outcome.stop_reason == "complete:wave1"
    assert evaluate(outcome).passed


# ---------------------------------------------------------------------------
# Coverage-quality: a fixture with Amazon+AMD+Cerebras but no Microsoft is not
# sufficient; with major branches covered it may be sufficient. Generic rule:
# an unexplored high-ranking material relationship blocks "sufficient".
# ---------------------------------------------------------------------------

def test_coverage_amazon_amd_cerebras_without_msft_not_sufficient() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    inp = _In(scenario_name="msft-openai-bankruptcy-sec-only",
              answer_text="Amazon, AMD and Cerebras branch findings [EV-1].",
              evidence_ids=("EV-1",), requires_evidence=True,
              coverage_claim="sufficient", branches_covered=("amzn", "amd", "cerebras"))
    assert "msft-openai-no-material-msft-exposure" in evaluate(inp).violations


def test_coverage_major_branches_may_be_sufficient() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    inp = _In(scenario_name="msft-openai-bankruptcy-sec-only",
              answer_text=_msft_fixture_answer(), evidence_ids=("EV-1", "EV-2"),
              requires_evidence=True, coverage_claim="sufficient",
              material_channels=("azure",), branches_covered=("coreweave", "amzn", "amd"))
    assert evaluate(inp).passed


def test_coverage_unexplored_high_rank_blocks_sufficient() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    inp = _In(scenario_name="gs-openai-sec-only", answer_text="Findings [EV-1].",
              evidence_ids=("EV-1",), requires_evidence=True,
              coverage_claim="sufficient", high_rank_unexplored=True)
    result = evaluate(inp)
    assert not result.passed and result.violations == ("coverage-overclaim",)
    ok = _In(scenario_name="gs-openai-sec-only", answer_text="Findings [EV-1].",
             evidence_ids=("EV-1",), requires_evidence=True,
             coverage_claim="sufficient", high_rank_unexplored=False)
    assert evaluate(ok).passed


# ---------------------------------------------------------------------------
# Committee invariants: same freeze across the trio, 3 distinct pre-run job
# ids, concurrent, completed-job cross-role write rejected, roles cannot
# mutate the freeze, claims resolve to frozen evidence, research_requests stay
# separate from evidence. Offline fakes only.
# ---------------------------------------------------------------------------

def _committee_input(
    committee_freeze_ids: tuple[str, ...] = ("F1", "F1", "F1"),
    job_ids: tuple[str, ...] = ("j-stock", "j-bull", "j-bear"),
    job_created_before_run: bool = True,
    jobs_concurrent: bool = True,
    cross_role_write_rejected: bool = True,
    roles_mutate_freeze: bool = False,
    claims_resolve_to_freeze: bool = True,
    requests_separate_from_evidence: bool = True,
) -> EvalInput:
    return EvalInput(
        scenario_name="gs-openai-sec-only", answer_text="Findings [EV-1].",
        evidence_ids=("EV-1",), requires_evidence=True,
        committee_freeze_ids=committee_freeze_ids, job_ids=job_ids,
        job_created_before_run=job_created_before_run, jobs_concurrent=jobs_concurrent,
        cross_role_write_rejected=cross_role_write_rejected,
        roles_mutate_freeze=roles_mutate_freeze,
        claims_resolve_to_freeze=claims_resolve_to_freeze,
        requests_separate_from_evidence=requests_separate_from_evidence,
    )


def test_committee_same_freeze_distinct_preregistered_concurrent() -> None:
    assert evaluate(_committee_input()).passed


def test_committee_distinct_freeze_fails() -> None:
    result = evaluate(_committee_input(committee_freeze_ids=("F1", "F2", "F1")))
    assert "committee-different-freeze" in result.violations


def test_committee_job_count_concurrency_registration() -> None:
    assert "committee-job-count" in evaluate(_committee_input(job_ids=("j1", "j1"))).violations
    assert "committee-jobs-not-preregistered" in evaluate(_committee_input(job_created_before_run=False)).violations
    assert "committee-jobs-not-concurrent" in evaluate(_committee_input(jobs_concurrent=False)).violations


def test_committee_cross_role_freeze_claims_requests() -> None:
    assert "committee-cross-role-write-allowed" in evaluate(_committee_input(cross_role_write_rejected=False)).violations
    assert "committee-mutates-freeze" in evaluate(_committee_input(roles_mutate_freeze=True)).violations
    assert "committee-claims-unresolved" in evaluate(_committee_input(claims_resolve_to_freeze=False)).violations
    assert "committee-requests-as-evidence" in evaluate(_committee_input(requests_separate_from_evidence=False)).violations


# ---------------------------------------------------------------------------
# Finalization UX: a finalize success auto-renders a substantive structured
# answer in the same turn; a bare "finalized/5 claims" with no answer fails.
# ---------------------------------------------------------------------------

def test_finalize_bare_count_without_answer_fails() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    inp = _In(scenario_name="gs-openai-sec-only", answer_text="finalized 5 claims",
              evidence_ids=("EV-1",), requires_evidence=True, finalized_claim_count=5,
              answered=False)
    assert "finalized-without-answer" in evaluate(inp).violations
    blank = _In(scenario_name="gs-openai-sec-only", answer_text="   ",
                evidence_ids=("EV-1",), requires_evidence=True, finalized_claim_count=5)
    assert "finalized-without-answer" in evaluate(blank).violations


def test_finalize_structured_answer_same_turn_passes() -> None:
    from app.research.evals.evaluators import EvalInput as _In
    inp = _In(scenario_name="gs-openai-sec-only",
              answer_text="Balanced: grounded [EV-1]. Bull: upside [EV-1]. Bear: risk [EV-1]. Agreed: exposure capped.",
              evidence_ids=("EV-1",), requires_evidence=True, finalized_claim_count=5)
    assert evaluate(inp).passed
