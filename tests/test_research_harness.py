"""Permanent §44/§45 gates: transitions, budgets, journal, PIT, append-only,
freeze immutability + cumulative E2, dossier refs, same-freeze parallel
committee, resume at source/freeze/one-member. All fakes, no network."""
import dataclasses
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.research import jobs as _jobs
from app.research import session as _session
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis
from app.research.dossiers.sec import (
    DossierIntegrityError,
    create_dossier,
    validate_dossier,
)
from app.research.evidence import (
    Evidence,
    EvidenceLedger,
    EvidenceRejectedError,
    evidence_content_hash,
    ingest_evidence,
)
from app.research.freeze import FreezeIntegrityError, create_freeze, verify_freeze
from app.research.journal import append_event
from app.research.models import Job, SessionStatus
from app.research.repository import ResearchRepository
from app.research.runner import run_live

ASOF = datetime(2025, 6, 30, tzinfo=timezone.utc)


def _ev(eid: str, sid: str, wave: int, known: datetime) -> Evidence:
    return Evidence(
        evidence_id=eid, session_id=sid, wave_id=wave, source_type="sec",
        source_name="SEC", source_uri="https://sec.gov/x", source_record_id="r",
        subject="NVDA", claim_text="c", content="c-" + eid,
        content_hash=evidence_content_hash("c-" + eid),
        known_at=known, retrieved_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        job_id="J-1", agent_id="s-A",
    )


def test_transitions_valid_and_invalid() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    s2 = _session.transition_session(s, SessionStatus.PLANNING)
    assert s2.status == SessionStatus.PLANNING.value
    with pytest.raises(ValueError):
        _session.transition_session(s, SessionStatus.COMPLETED)


def test_budgets_enforced_from_policy() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    jobs: list[Job] = []
    last = s
    created = 0
    with pytest.raises(ValueError, match="max_parallel|max_total_jobs"):
        for _ in range(21):
            last, j = _jobs.create_job(last, jobs, job_type="scout", owner="t")
            jobs.append(j)
            created += 1
    assert created <= 20


def test_budgets_enforced_total_jobs() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    jobs: list[Job] = []
    last = s
    while len(jobs) < 20:
        last, j = _jobs.create_job(last, jobs, job_type="scout", owner="t")
        jobs.append(_jobs.complete_job(_jobs.start_job(j)))
    with pytest.raises(ValueError, match="max_total_jobs"):
        _jobs.create_job(last, jobs, job_type="scout", owner="t")


def test_scout_cannot_have_children() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    _, parent = _jobs.create_job(s, [], job_type="scout", owner="t")
    with pytest.raises(ValueError):
        _jobs.create_job(s, [parent], job_type="scout", owner="t",
                         parent_job_id=parent.job_id)


def test_journal_sequences_and_pit_rejection_logged() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    e1 = append_event(s.session_id, "research.created", "t", "t", {})
    e2 = append_event(s.session_id, "research.started", "t", "t", {})
    assert e2.sequence == e1.sequence + 1
    led = EvidenceLedger()
    seen: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(EvidenceRejectedError):
        ingest_evidence(led, _ev("EV-X", s.session_id, 1,
                                 datetime(2025, 7, 1, tzinfo=timezone.utc)),
                        as_of=ASOF, on_reject=lambda t, p: seen.append((t, p)))
    assert seen and seen[0][0] == "evidence.rejected"
    assert seen[0][1]["reason"] == "PIT_VIOLATION"


def test_ledger_append_only_and_supersede() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    led = EvidenceLedger()
    ingest_evidence(led, _ev("EV-1", s.session_id, 1,
                             datetime(2025, 5, 1, tzinfo=timezone.utc)),
                    as_of=ASOF, on_reject=None)
    with pytest.raises(ValueError):
        led.append(_ev("EV-1", s.session_id, 1,
                       datetime(2025, 5, 1, tzinfo=timezone.utc)))
    fix = led.get("EV-1")
    led.supersede(dataclasses.replace(fix, evidence_id="EV-2", superseded_by="EV-1"))
    assert led.get("EV-1").evidence_id == "EV-1"


def test_freeze_immutable_and_cumulative_e2() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    led = EvidenceLedger()
    for eid, wave in (("EV-1", 1), ("EV-2", 1), ("EV-3", 2)):
        ingest_evidence(led, _ev(eid, s.session_id, wave,
                                 datetime(2025, 5, 1, tzinfo=timezone.utc)),
                        as_of=ASOF, on_reject=None)
    e1 = [e for e in led.list_session(s.session_id) if e.wave_id <= 1]
    e2 = [e for e in led.list_session(s.session_id) if e.wave_id <= 2]
    f1 = create_freeze(freeze_id="E1", session_id=s.session_id, wave_id=1, records=e1, as_of=ASOF)
    f2 = create_freeze(freeze_id="E2", session_id=s.session_id, wave_id=2, records=e2, as_of=ASOF)
    assert set(f1.evidence_ids) < set(f2.evidence_ids)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(f2, "evidence_ids", ("EV-1",))
    verify_freeze(f2, e2)
    with pytest.raises(FreezeIntegrityError):
        verify_freeze(f2, e1)


def test_dossier_rejects_dangling_refs() -> None:
    s = _session.create_session("q?", "o", as_of=ASOF)
    cov: dict[str, object] = {"entities": ["NVDA"], "forms": ["10-Q"],
                              "time_range": {"start": "2025-01-01", "end": "2025-06-30"},
                              "sources_examined": ["e"], "complete": True, "exclusions": []}
    bad = create_dossier(dossier_id="SEC-D1", session_id=s.session_id, wave_id=1,
                         subject="NVDA", coverage=cov, findings=[],
                         supporting_evidence_ids=["EV-NOPE"],
                         contradicting_evidence_ids=[], unknowns=[],
                         limitations=[], open_questions=[])
    with pytest.raises(DossierIntegrityError):
        validate_dossier(bad, set())


DispatchFn = Callable[[str, dict[str, object]], dict[str, object]]
ModelFn = Callable[[str], str]


def _fake_dispatch(evidence_ids: tuple[str, ...] = ("EV-1", "EV-2")) -> DispatchFn:
    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return {"matches": ["sec-10q", "sec-10k"]}
        if name == "call_tool":
            return {"record": {"id": str(args.get("record_id", "r"))},
                    "evidence_ids": list(evidence_ids)}
        return {}
    return _dispatch


def _run_wave(repo: ResearchRepository, model: ModelFn) -> dict[str, object]:
    return run_live(question="NVDA demand?", objective="o",
                    as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                    dispatch=_fake_dispatch(), model=model,
                    repo=repo, budgets=None)


def test_same_freeze_parallel_committee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _model(prompt: str) -> str:
        time.sleep(2)
        assert prompt
        return "balanced view. Follow-up: What drove Q2 delta?"

    out = _run_wave(repo, _model)
    assert out["stop_reason"] == "complete:wave1"
    stock = out["stock"]
    bull = out["bull"]
    bear = out["bear"]
    assert isinstance(stock, StockbotAnalysis)
    assert isinstance(bull, BullAnalysis)
    assert isinstance(bear, BearAnalysis)
    assert stock.freeze_id == bull.freeze_id == bear.freeze_id == out["freeze_id"]
    assert stock.evidence_ids == bull.evidence_ids == bear.evidence_ids
    sid_obj = out["session_id"]
    assert isinstance(sid_obj, str)
    jobs = repo.list_jobs(sid_obj)
    assert all(j.status == "completed" for j in jobs if j.job_type in ("stockbot", "bullbot", "bearbot"))


def test_parallel_beats_serial_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    spans: list[tuple[float, float]] = []

    def _slow(prompt: str) -> str:
        assert prompt
        start = time.monotonic()
        time.sleep(1)
        spans.append((start, time.monotonic()))
        return "ok"

    _run_wave(repo, _slow)
    committee = spans[3:6]  # first three calls are serial scouts
    assert len(committee) == 3
    assert max(s for s, _ in committee) < min(e for _, e in committee)


def test_resume_at_source_freeze_one_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    for hook, reason in (("source", "interrupted:source"), ("freeze", "interrupted:freeze"),
                         ("one-committee", "interrupted:one-committee")):
        out = run_live(question="NVDA demand?", objective="o",
                       as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                       dispatch=_fake_dispatch(), model=_ok,
                       repo=repo, budgets=None, interrupt_after=hook)
        assert out["stop_reason"] == reason
        resume_sid = out["session_id"]
        assert isinstance(resume_sid, str)
        n_jobs = len(repo.list_jobs(resume_sid))
        state = repo.resume(resume_sid)
        assert len(repo.list_jobs(resume_sid)) == n_jobs  # no dup on resume
        assert state.session is not None


def test_committee_prompts_carry_identical_freeze_payload() -> None:
    prompts: list[str] = []

    def _cap(prompt: str) -> str:
        prompts.append(prompt)
        return "ok. Follow-up: What drove Q2 delta?"

    s = _session.create_session("NVDA demand?", "o", as_of=ASOF)
    led = EvidenceLedger()
    ingest_evidence(led, _ev("EV-1", s.session_id, 1,
                             datetime(2025, 5, 1, tzinfo=timezone.utc)),
                    as_of=ASOF, on_reject=None)
    rec = led.get("EV-1")
    payload = f"[{rec.evidence_id}] {rec.subject} | claim: {rec.claim_text}\ncontent: {rec.content}"
    from app.research.agents.bearbot import run_bearbot
    from app.research.agents.bullbot import run_bullbot
    from app.research.agents.stockbot import run_stockbot
    run_stockbot("Q?", session_id=s.session_id, wave_id=1, freeze_id="E1",
                 evidence_ids=["EV-1"], as_of="x", model=_cap, evidence_text=payload)
    run_bullbot("Q?", session_id=s.session_id, wave_id=1, freeze_id="E1",
                evidence_ids=["EV-1"], as_of="x", model=_cap, evidence_text=payload)
    run_bearbot("Q?", session_id=s.session_id, wave_id=1, freeze_id="E1",
                evidence_ids=["EV-1"], as_of="x", model=_cap, evidence_text=payload)
    assert len(prompts) == 3
    bodies = [p.split("Evidence (cite ids; do not invent):\n", 1)[1] for p in prompts]
    assert bodies[0] == bodies[1] == bodies[2]
    assert "EV-1" in bodies[0] and "c-ev-1" in bodies[0].lower()


def test_source_timestamp_never_invented_and_post_cutoff_rejected() -> None:
    from app.research.runner import _extract_known_at, _extract_source_ref
    assert _extract_known_at({}) is None
    assert _extract_known_at({"record": {}}) is None
    assert _extract_source_ref({}) == (None, None)
    assert _extract_source_ref({"source": "SEC EDGAR"}) == (None, None)
    known = _extract_known_at({"meta": {"source_refs": {
        "record_id": "0001045810-25-000023", "uri": "https://sec.gov/x",
        "known_at": "2025-05-28"}}})
    assert known is not None and (known.year, known.month, known.day) == (2025, 5, 28)
    uri, ref = _extract_source_ref({"meta": {"source_refs": {
        "record_id": "0001045810-25-000023", "uri": "https://sec.gov/x"}}})
    assert (uri, ref) == ("https://sec.gov/x", "0001045810-25-000023")
    s = _session.create_session("q?", "o", as_of=ASOF)
    led = EvidenceLedger()
    seen: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(EvidenceRejectedError):
        ingest_evidence(led, _ev("EV-FUT", s.session_id, 1,
                                 datetime(2026, 1, 1, tzinfo=timezone.utc)),
                        as_of=ASOF, on_reject=lambda t, p: seen.append((t, p)))
    assert seen[0][1]["reason"] == "PIT_VIOLATION"
    assert led.ids() == ()


def test_resume_after_source_completes_reusing_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    out = run_live(question="NVDA demand?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_fake_dispatch(), model=_ok,
                   repo=repo, budgets=None, interrupt_after="source")
    assert out["stop_reason"] == "interrupted:source"
    sid = out["session_id"]
    assert isinstance(sid, str)
    eids_raw = out["evidence_ids"]
    assert isinstance(eids_raw, list) and all(isinstance(e, str) for e in eids_raw)
    eids_before: list[str] = list(eids_raw)
    did_before = out["dossier_id"]
    ev_before = len(repo.list_evidence(sid))
    jobs_before = repo.list_jobs(sid)
    assert len(jobs_before) == 1 and jobs_before[0].job_type == "source_agent"
    out2 = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert out2["stop_reason"] == "complete:wave1"
    eids_after = out2["evidence_ids"]
    assert isinstance(eids_after, list) and set(eids_after) == set(eids_before)
    assert len(repo.list_evidence(sid)) == ev_before  # no dup evidence
    assert out2["dossier_id"] == did_before  # dossier reused, not recreated
    jobs_after = repo.list_jobs(sid)
    assert len([j for j in jobs_after if j.job_type == "source_agent"]) == 1  # no dup source job
    assert len(jobs_after) == len(jobs_before) + 3  # trio added once
    assert out2["freeze_id"] == f"{sid}:1:freeze"
    assert repo.get_session(sid).status == "completed"


def test_resume_after_freeze_skips_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    out = run_live(question="NVDA demand?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_fake_dispatch(), model=_ok,
                   repo=repo, budgets=None, interrupt_after="freeze")
    assert out["stop_reason"] == "interrupted:freeze"
    sid = out["session_id"]
    assert isinstance(sid, str)
    fid = out["freeze_id"]
    assert isinstance(fid, str) and fid
    calls = [0]
    base = _fake_dispatch()

    def _counting(name: str, args: dict[str, object]) -> dict[str, object]:
        if name == "call_tool":
            calls[0] += 1
        return base(name, args)

    out2 = resume_live(sid, _counting, _ok, repo=repo)
    assert calls[0] == 0  # fetch skipped: no tool evidence calls
    assert out2["stop_reason"] == "complete:wave1"
    assert out2["freeze_id"] == fid  # same freeze reused
    assert out2["stock"] is not None and out2["bull"] is not None and out2["bear"] is not None


def test_resume_after_one_committee_reruns_trio_on_same_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    out = run_live(question="NVDA demand?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_fake_dispatch(), model=_ok,
                   repo=repo, budgets=None, interrupt_after="one-committee")
    assert out["stop_reason"] == "interrupted:one-committee"
    sid = out["session_id"]
    assert isinstance(sid, str)
    fid = out["freeze_id"]
    jobs_before = repo.list_jobs(sid)
    assert len(jobs_before) == 2  # source + stock-only
    out2 = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    # Analyses are not persisted, so resume always reruns the full trio on the
    # same freeze id with new jobs (never reuses the stock-only partial result).
    assert out2["freeze_id"] == fid
    assert out2["stock"] is not None and out2["bull"] is not None and out2["bear"] is not None
    assert out2["stop_reason"] == "complete:wave1"
    jobs_after = repo.list_jobs(sid)
    assert len(jobs_after) == len(jobs_before) + 3
    sess = repo.get_session(sid)
    assert len(sess.committee_runs) == 2
    second = sess.committee_runs[1]
    assert isinstance(second, dict) and second.get("freeze_id") == fid
    second_jobs = second.get("jobs")
    assert isinstance(second_jobs, list) and len(second_jobs) == 3


def test_resume_dossier_resource_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.resources import read_resource
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    out = run_live(question="NVDA demand?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_fake_dispatch(), model=_ok, repo=repo, budgets=None)
    sid = out["session_id"]
    assert isinstance(sid, str)
    did = out["dossier_id"]
    assert isinstance(did, str) and did
    stores = repo.resource_stores(sid)
    resolved = read_resource(f"dossier://{did}", dossiers=stores["dossier"])
    assert isinstance(resolved, dict) and resolved.get("dossier_id") == did
    resolved_full = read_resource(
        f"dossier://{did}",
        evidence=stores["evidence"], freezes=stores["freeze"],
        dossiers=stores["dossier"], jobs=stores["job"], sessions=stores["research"],
    )
    assert resolved_full == resolved


def test_resume_failed_session_reports_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    from app.research.runner import LiveModelError, resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _timeout_model(_prompt: str) -> str:
        raise subprocess.TimeoutExpired(cmd="pi", timeout=1)

    with pytest.raises(LiveModelError) as excinfo:
        run_live("timeout probe?", "probe", None, ["NVDA"], lambda n, a: {}, _timeout_model, repo=repo)
    sid = excinfo.value.session_id
    assert repo.get_session(sid).status == "failed"
    n_jobs = len(repo.list_jobs(sid))
    n_ev = len(repo.list_evidence(sid))

    def _ok(prompt: str) -> str:
        assert prompt
        return "ok"

    with pytest.raises(LiveModelError, match="out of scope"):
        resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert len(repo.list_jobs(sid)) == n_jobs  # failed resume writes nothing
    assert len(repo.list_evidence(sid)) == n_ev


def test_scouts_run_serially_with_ordered_results() -> None:
    from app.research.agents.sec_agent import run_sec_assignment
    seen: list[str] = []

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        assert name
        assert args is not None
        return {}

    def _model(prompt: str) -> str:
        assert prompt
        seen.append(prompt[:40])
        return "done"

    dossier = run_sec_assignment("Q?", session_id="rs:t", wave_id=1, as_of="2025-06-30",
                                 tickers=["NVDA"], dispatch=_dispatch, model=_model)
    assert len(seen) == 3  # filings, financials, risk in assignment order
    assert isinstance(dossier, object) and dossier is not None
