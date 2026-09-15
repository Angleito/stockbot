"""RunnerResid paths: branch tests for the six over-10 fns in app/research/runner.py.

_merge_disagreement, _LiveRun._reused_scout_result, _find_completed_scout,
_LiveRun._start_reused_scout, _LiveRun._run_wave2, _LiveRun._complete_wave2_tail,
_run_one_committee. Plain pytest, tmp_path DBs, fakes for models/dispatch.
"""
from __future__ import annotations

import json
import re as _re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from app.research.repository import ResearchRepository

if TYPE_CHECKING:
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    from app.research.director import Wave1Result
    from app.research.runner import _LiveRun


def _grounded(prompt: str) -> str:
    seen: list[str] = []
    for line in prompt.splitlines():
        stripped = line.strip()
        m = _re.match(r"^\[([^\[\]]+)\]", stripped)
        if m is not None:
            tok = str(m.group(1)).strip()
            if tok and tok not in seen:
                seen.append(tok)
            continue
        m2 = _re.match(r"^-\s+(\S+)", stripped)
        if m2 is not None:
            tok = str(m2.group(1)).strip()
            if (tok.startswith("EV-") or ":sec:" in tok) and tok not in seen:
                seen.append(tok)
    claims: list[dict[str, object]] = [] if not seen else [
        {"text": f"grounded finding {i}", "evidence_ids": [eid]} for i, eid in enumerate(seen[:6])
    ]
    if "Temporary assignment" in prompt:
        return json.dumps(claims)
    return json.dumps({"claims": claims, "follow_ups": []})


def _fake_dispatch(evidence_ids: tuple[str, ...] = ("EV-1", "EV-2")) -> Callable[[str, dict[str, object]], dict[str, object]]:
    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return {"matches": ["sec-10q", "sec-10k"]}
        if name == "call_tool":
            return {
                "record": {"id": str(args.get("record_id", "r")), "known_at": "2025-05-01"},
                "evidence_ids": list(evidence_ids),
            }
        return {}
    return _dispatch

def _make_run(repo: ResearchRepository) -> _LiveRun:
    from app.research.director import DirectorBudgets
    from app.research.runner import _LiveRun
    return _LiveRun(
        repo, "NVDA demand?", "o", "2025-06-30T00:00:00+00:00",
        "2025-06-30T00:00:00+00:00", ["NVDA"],
        _fake_dispatch(), _grounded, DirectorBudgets(),
    )

def _trio(sid: str, wave: int, fid: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
    from app.research.agents import GroundedClaim
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    claim = [GroundedClaim(text="holds share", evidence_ids=["E1"])]
    stock = StockbotAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=["E1"],
                             as_of="x", question="q?", answer="a", base_case="base", claims=list(claim))
    bull = BullAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=["E1"],
                        as_of="x", question="q?", stance="bullish", bull_case="up", claims=list(claim))
    bear = BearAnalysis(session_id=sid, wave_id=wave, freeze_id=fid, evidence_ids=["E1"],
                        as_of="x", question="q?", stance="bearish", bear_case="down", claims=list(claim))
    return stock, bull, bear


def test_merge_disagreement_dedup_and_agent_merge() -> None:
    from app.research.agents import ResearchRequest
    from app.research.runner import _merge_disagreement
    from app.research.synthesis.committee import CommitteeDisagreement
    r1 = ResearchRequest(question="Q1?", why_material="m", requested_source_domain="SEC",
                         expected_gain="high", requesting_agents=["stockbot"])
    r1b = ResearchRequest(question="Q1?", why_material="m", requested_source_domain="SEC",
                          expected_gain="high", requesting_agents=["bullbot"])
    r2 = ResearchRequest(question="Q2?", why_material="m", requested_source_domain="SEC",
                         expected_gain="high", requesting_agents=["bearbot"])
    d1 = CommitteeDisagreement(session_id="s", wave_id=1, freeze_id="f1",
                               agreement=["a1", "a2", "a1"], disagreement=["x"],
                               critical_uncertainties=["u1"], requested_research=[r1])
    d2 = CommitteeDisagreement(session_id="s", wave_id=2, freeze_id="f2",
                               agreement=["a2", "a3"], disagreement=["x", "y"],
                               critical_uncertainties=["u1", "u2"], requested_research=[r1b, r2])
    merged = _merge_disagreement(d1, d2)
    assert (merged.session_id, merged.wave_id, merged.freeze_id) == ("s", 2, "f2")
    assert merged.agreement == ["a1", "a2", "a3"]
    assert merged.disagreement == ["x", "y"]
    assert merged.critical_uncertainties == ["u1", "u2"]
    by_q = {r.question: r.requesting_agents for r in merged.requested_research}
    assert sorted(by_q["Q1?"]) == ["bullbot", "stockbot"]
    assert by_q["Q2?"] == ["bearbot"]


def test_reused_scout_result_coercion_and_default_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research import jobs as _jobs
    from app.research.agents.scout import ScoutAssignment
    from app.research.models import JobType
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("q?", "", None)
    src = run._open_source_job(sid, 1, "q?")
    asg = ScoutAssignment(assignment_id="scout-filings", session_id=sid, as_of="unbounded",
                          role="filings", question="q?", tickers=["NVDA"])
    sess, child = _jobs.create_job(repo.get_session(sid), repo.list_jobs(sid),
                                   job_type=JobType.SCOUT, owner="t", wave_id=1, parent_job_id=src)
    repo.save_session(sess)
    repo.save_job(child)
    res: dict[str, object] = {
        "assignment_id": "scout-filings", "coverage": "full",
        "findings": [{"text": "good finding", "evidence_ids": ["E1"]},
                     {"text": "  ", "evidence_ids": ["E2"]}, "junk"],
        "unknowns": ["u", 42], "limitations": [1, "lim"],
        "follow_up_requests": [
            {"question": "FQ?", "why_material": "m", "requested_source_domain": "SEC",
             "expected_gain": "high", "requesting_agents": ["a", 7]}],
    }
    out = run._reused_scout_result(sid, asg, child, res)
    assert out.coverage == "full" and out.assignment_id == "scout-filings"
    assert [f.text for f in out.findings] == ["good finding"]
    assert out.unknowns == ["u"] and out.limitations == ["lim"]
    assert len(out.follow_up_requests) == 1
    assert [e.event_type for e in repo.list_events(sid)].count("scout.reused") == 1
    out2 = run._reused_scout_result(sid, asg, child, {k: v for k, v in res.items() if k != "coverage"})
    assert out2.coverage == "reused"


def test_find_completed_scout_hit_and_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research import jobs as _jobs
    from app.research.agents.scout import ScoutAssignment
    from app.research.models import JobType
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("q?", "", None)
    src = run._open_source_job(sid, 1, "q?")
    asg = ScoutAssignment(assignment_id="scout-filings", session_id=sid, as_of="unbounded",
                          role="filings", question="q?", tickers=["NVDA"])
    assert run._find_completed_scout(sid, src, asg, repo.list_jobs(sid)) is None
    sess, child = _jobs.create_job(repo.get_session(sid), repo.list_jobs(sid),
                                   job_type=JobType.SCOUT, owner="t", wave_id=1, parent_job_id=src)
    repo.save_session(sess)
    repo.save_job(_jobs.start_job(child))
    repo.save_job(_jobs.complete_job(repo.get_job(child.job_id), result={
        "assignment_id": "scout-filings",
        "findings": [{"text": "t", "evidence_ids": ["E1"]}],
    }))
    got = run._find_completed_scout(sid, src, asg, repo.list_jobs(sid))
    assert got is not None and got.assignment_id == "scout-filings"
    assert run._find_completed_scout(sid, "no-such-parent", asg, repo.list_jobs(sid)) is None


def test_start_reused_scout_queued_and_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    import dataclasses

    from app.research import jobs as _jobs
    from app.research.models import JobType
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("q?", "", None)
    src = run._open_source_job(sid, 1, "q?")
    sess, queued = _jobs.create_job(repo.get_session(sid), repo.list_jobs(sid),
                                    job_type=JobType.SCOUT, owner="t", wave_id=1, parent_job_id=src)
    queued = dataclasses.replace(queued, diagnostics={"assignment_id": "scout-filings"})
    repo.save_session(sess)
    repo.save_job(queued)
    run._start_reused_scout(sid, queued, "scout-filings")
    assert repo.get_job(queued.job_id).status == "running"
    kinds = [e.event_type for e in repo.list_events(sid)]
    assert "job.started" in kinds and "scout.retried" in kinds
    running = repo.get_job(queued.job_id)
    run._start_reused_scout(sid, running, "")
    assert repo.get_job(queued.job_id).status == "running"


def test_run_one_committee_empty_and_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research.runner import _run_one_committee
    repo = ResearchRepository()
    run = _make_run(repo)
    run_empty = _make_run(repo)
    def _empty_fetch(session_id: str) -> list[str]:
        return []
    run_empty._fetch = _empty_fetch
    out0 = _run_one_committee(run_empty, repo, "q?", "", 1)
    assert out0["stop_reason"] == "no_questions:empty-wave1"
    assert out0["stock"] is None and out0["freeze_id"] == ""
    out = _run_one_committee(run, repo, "NVDA demand?", "2025-06-30T00:00:00+00:00", 1)
    assert out["stop_reason"] == "interrupted:one-committee"
    assert out["stock"] is not None and out["bull"] is None and out["bear"] is None
    assert out["freeze_id"] and out["evidence_ids"]
    kinds = [e.event_type for e in repo.list_events(str(out["session_id"]))]
    assert "committee.completed" in kinds and "wave.stopped" in kinds


def _real_wave1(run: _LiveRun, sid: str) -> Wave1Result:
    from app.research.director import Wave1Result
    from app.research.synthesis.committee import compute_disagreement
    src = run._open_source_job(sid, 1, "NVDA demand?")
    e1 = run._fetch_wave(sid, 1, "NVDA demand?", src, "")
    assert e1
    f1 = run._freeze_wave(sid, 1)
    s1, b1, r1 = run._committee_wave(sid, 1, "")
    d1 = compute_disagreement(s1, b1, r1)
    return Wave1Result(session_id=sid, wave_id=1, freeze_id=f1, evidence_ids=e1,
                       stock=s1, bull=b1, bear=r1, disagreement=d1)


def test_run_wave2_none_empty_and_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("NVDA demand?", "2025-06-30T00:00:00+00:00", None)
    w1 = _real_wave1(run, sid)
    def _no_wave2(sid: str, wave: int, q: str, src_job_id: str, prefix: str) -> list[str]:
        return []
    run._fetch_wave = _no_wave2
    assert run._run_wave2(w1, "TQ?") is None
    kinds = [e.event_type for e in repo.list_events(sid)]
    assert "wave.stopped" in kinds


def test_run_wave2_full_merges_both_waves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research.director import Wave1Result
    from app.research.synthesis.committee import CommitteeDisagreement
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("NVDA demand?", "2025-06-30T00:00:00+00:00", None)
    w1 = _real_wave1(run, sid)
    out = run._run_wave2(w1, "TQ follow-up?")
    assert out is not None
    assert out["targeted"] == "TQ follow-up?" and out["evidence_ids"]
    assert out["dossier_id"] == run.dossier_ids[-1]
    w2result = out["result"]
    assert isinstance(w2result, Wave1Result) and w2result.wave_id == 2
    w2dis = out["disagreement"]
    assert isinstance(w2dis, CommitteeDisagreement) and w2dis.wave_id == 2

def test_run_wave2_empty_dossier_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research.director import Wave1Result
    from app.research.synthesis.committee import CommitteeDisagreement
    repo = ResearchRepository()
    run = _make_run(repo)
    assert run.dossier_ids == []
    sid = run._create_session("q?", "", None)
    stock, bull, bear = _trio(sid, 2, "f2x")
    d1 = CommitteeDisagreement(session_id=sid, wave_id=1, freeze_id="f1",
                               agreement=["a1"], disagreement=["x"], critical_uncertainties=[])
    w1 = Wave1Result(session_id=sid, wave_id=1, freeze_id="f1", evidence_ids=["E1"],
                     stock=stock, bull=bull, bear=bear, disagreement=d1)
    def _fetch(sid: str, wave: int, q: str, src_job_id: str, prefix: str) -> list[str]:
        return ["E1"]
    def _freeze(session_id: str, wave: int) -> str:
        return "f2x"
    def _committee(session_id: str, wave: int, prefix: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        return (stock, bull, bear)
    monkeypatch.setattr(run, "_fetch_wave", _fetch)
    monkeypatch.setattr(run, "_freeze_wave", _freeze)
    monkeypatch.setattr(run, "_committee_wave", _committee)
    out = run._run_wave2(w1, "tq?")
    assert out is not None and out["dossier_id"] == ""
    tail_dis = out["disagreement"]
    assert isinstance(tail_dis, CommitteeDisagreement) and tail_dis.freeze_id == "f2x"


def test_complete_wave2_tail_synth_and_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research.director import Wave1Result
    repo = ResearchRepository()
    run = _make_run(repo)
    sid = run._create_session("NVDA demand?", "2025-06-30T00:00:00+00:00", None)
    w1 = _real_wave1(run, sid)
    w2 = run._run_wave2(w1, "TQ follow-up?")
    assert w2 is not None
    tail = run._complete_wave2_tail(w1, "D1", "gate:x", w2)
    assert tail["stop_reason"] == "complete:wave2"
    assert tail["wave2_freeze_id"] == w2["freeze_id"]
    assert tail["wave2_evidence_ids"] == w2["evidence_ids"]
    assert tail["wave2_targeted"] == "TQ follow-up?"
    done = repo.get_session(sid)
    assert done.status == "completed" and done.final_result is not None
    w2none: dict[str, object] = {
        "result": Wave1Result(session_id=sid, wave_id=2, freeze_id="f2y",
                              evidence_ids=["E1"], disagreement=None),
        "targeted": "t", "freeze_id": "f2y", "evidence_ids": ["E1"], "dossier_id": "D1",
        "stock": None, "bull": None, "bear": None, "disagreement": None,
    }
    tail2 = run._complete_wave2_tail(w1, "D1", "gate:x", w2none)
    assert tail2["stop_reason"] == "complete:wave2"
    assert tail2["wave2_freeze_id"] == "f2y"
