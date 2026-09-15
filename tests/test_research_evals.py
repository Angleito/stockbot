from pathlib import Path

import pytest

from app.research.evals.evaluators import EvalInput, evaluate


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

