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


def _grounded(prompt: str) -> str:
    """Fake grounded model: cite only freeze/acquired ids listed in the prompt."""
    import json as _json
    import re as _re
    seen: list[str] = []
    for line in prompt.splitlines():
        stripped = line.strip()
        bracket = _re.match(r"^\[([^\[\]]+)\]", stripped)
        if bracket is not None:
            token = str(bracket.group(1)).strip()
            if token and token not in seen:
                seen.append(token)
            continue
        acquired = _re.match(r"^-\s+(\S+)", stripped)
        if acquired is not None:
            token = str(acquired.group(1)).strip()
            if (token.startswith("EV-") or ":sec:" in token) and token not in seen:
                seen.append(token)
    if not seen:
        claims: list[dict[str, object]] = []
    else:
        claims = [{"text": f"grounded finding {i}", "evidence_ids": [eid]} for i, eid in enumerate(seen[:6])]
    if "Temporary assignment" in prompt:
        return _json.dumps(claims)
    return _json.dumps({"claims": claims, "follow_ups": []})

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
                         subject="NVDA", coverage=cov,
                         findings=[{"text": "dangling", "evidence_ids": ["EV-NOPE"]}],
                         unknowns=[], limitations=[], open_questions=[])
    with pytest.raises(DossierIntegrityError):
        validate_dossier(bad, set())


DispatchFn = Callable[[str, dict[str, object]], dict[str, object]]
ModelFn = Callable[[str], str]


def _fake_dispatch(evidence_ids: tuple[str, ...] = ("EV-1", "EV-2")) -> DispatchFn:
    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return {"matches": ["sec-10q", "sec-10k"]}
        if name == "call_tool":
            return {"record": {"id": str(args.get("record_id", "r")), "known_at": "2025-05-01"},
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
        return _grounded(prompt)

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
        return _grounded(prompt)

    _run_wave(repo, _slow)
    committee = spans[3:6]  # first three calls are serial scouts
    assert len(committee) == 3
    assert max(s for s, _ in committee) < min(e for _, e in committee)


def test_resume_at_source_freeze_one_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return _grounded(prompt)

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
        return _grounded(prompt)

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
        return _grounded(prompt)

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
    assert len(jobs_before) == 4 and sum(1 for j in jobs_before if j.job_type == "source_agent") == 1 and sum(1 for j in jobs_before if j.job_type == "scout") == 3
    out2 = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert out2["stop_reason"] == "complete:wave1"
    eids_after = out2["evidence_ids"]
    assert isinstance(eids_after, list) and set(eids_after) == set(eids_before)
    assert len(repo.list_evidence(sid)) == ev_before  # no dup evidence
    assert out2["dossier_id"] == did_before  # dossier reused, not recreated
    jobs_after = repo.list_jobs(sid)
    assert len([j for j in jobs_after if j.job_type == "source_agent"]) == 1  # no dup source job
    assert len([j for j in jobs_after if j.job_type == "scout"]) == 3  # completed scouts reused, no dup scouts
    assert len(jobs_after) == len(jobs_before) + 3  # trio added once
    assert out2["freeze_id"] == f"{sid}:1:freeze"
    assert repo.get_session(sid).status == "completed"


def test_resume_after_freeze_skips_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _ok(prompt: str) -> str:
        assert prompt
        return _grounded(prompt)

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
        return _grounded(prompt)

    out = run_live(question="NVDA demand?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_fake_dispatch(), model=_ok,
                   repo=repo, budgets=None, interrupt_after="one-committee")
    assert out["stop_reason"] == "interrupted:one-committee"
    sid = out["session_id"]
    assert isinstance(sid, str)
    fid = out["freeze_id"]
    jobs_before = repo.list_jobs(sid)
    assert len(jobs_before) == 5  # source + 3 scouts + stock-only
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
        return _grounded(prompt)

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
        return _grounded(prompt)

    with pytest.raises(LiveModelError, match="out of scope"):
        resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert len(repo.list_jobs(sid)) == n_jobs  # failed resume writes nothing
    assert len(repo.list_evidence(sid)) == n_ev


def test_scouts_run_serially_with_ordered_results() -> None:
    from app.research.agents.scout import run_scout
    from app.research.agents.sec_agent import run_sec_assignment
    seen: list[str] = []

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        assert name
        assert args is not None
        return {}

    def _model(prompt: str) -> str:
        assert prompt
        seen.append(prompt[:40])
        return "[]"

    dossier = run_sec_assignment("Q?", session_id="rs:t", wave_id=1, as_of="2025-06-30",
                                 tickers=["NVDA"], dispatch=_dispatch, model=_model,
                                 spawn=lambda a: run_scout(a, dispatch=_dispatch, model=_model))
    assert len(seen) == 3  # filings, financials, risk in assignment order
    assert isinstance(dossier, object) and dossier is not None

def test_pit_unverified_historical_rejected_current_accepted() -> None:
    from datetime import datetime, timezone
    from app.research.evidence import Evidence, EvidenceLedger, EvidenceRejectedError, evidence_content_hash, ingest_evidence
    hist = datetime(2025, 6, 30, tzinfo=timezone.utc)
    def _mk(eid: str, known: datetime | None) -> Evidence:
        content = "content"
        return Evidence(eid, "rs:pit", 1, "sec", "search_sec_filings", "s", "claim", content, evidence_content_hash(content), datetime(2025, 5, 1, tzinfo=timezone.utc), "https://sec.gov/x", "r1", None, known, None, "job:1", "sec_scout", (), (), None, None, {}, None)
    led = EvidenceLedger()
    seen: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(EvidenceRejectedError) as ei:
        ingest_evidence(led, _mk("EV-U1", None), as_of=hist, on_reject=lambda t, p: seen.append((t, p)))
    assert ei.value.reason == "PIT_UNVERIFIED"
    assert seen[0][1]["reason"] == "PIT_UNVERIFIED"
    assert led.ids() == ()
    out = ingest_evidence(led, _mk("EV-U2", None), as_of=None)
    assert out.evidence_id == "EV-U2"
    from app.research.agents.scout import _is_pit_eligible
    assert _is_pit_eligible(None, "2025-06-30") is False
    assert _is_pit_eligible(None, "unbounded") is True

def test_budget_resume_hydrates_cumulative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    def _ok(prompt: str) -> str:
        assert prompt
        return _grounded(prompt)
    out = run_live(question="NVDA demand?", objective="o", as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"], dispatch=_fake_dispatch(), model=_ok, repo=repo, budgets=None, interrupt_after="source")
    assert out["stop_reason"] == "interrupted:source"
    sid = out["session_id"]
    assert isinstance(sid, str)
    sess = repo.get_session(sid)
    used = sess.budget.get("tool_calls_used")
    assert isinstance(used, int) and used > 0
    out2 = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert out2["stop_reason"] == "complete:wave1"
    sess2 = repo.get_session(sid)
    used2 = sess2.budget.get("tool_calls_used")
    assert isinstance(used2, int) and used2 == used  # no reset to zero on resume


def test_two_wave_e2_tree_and_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    def _model(prompt: str) -> str:
        assert prompt
        import json as _json
        base = _grounded(prompt)
        try:
            decoded: object = _json.loads(base)
        except Exception:
            return base
        if isinstance(decoded, dict):
            decoded["follow_ups"] = ["What drove Q2 delta?"]
            return _json.dumps(decoded)
        return base
    out = run_live(question="NVDA historical demand?", objective="o", as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"], dispatch=_fake_dispatch(), model=_model, repo=repo, budgets=None)
    assert out["stop_reason"] == "complete:wave2"
    sid = out["session_id"]
    assert isinstance(sid, str)
    jobs = repo.list_jobs(sid)
    src_jobs = [j for j in jobs if j.job_type == "source_agent"]
    scout_jobs = [j for j in jobs if j.job_type == "scout"]
    committee_jobs = [j for j in jobs if j.job_type in ("stockbot", "bullbot", "bearbot")]
    assert len(src_jobs) == 2 and len(scout_jobs) == 6 and len(committee_jobs) == 6
    src_ids = {j.job_id for j in src_jobs}
    assert all(j.parent_job_id in src_ids for j in scout_jobs)
    assert all(j.status == "completed" for j in jobs)
    assert len(jobs) == 14
    sess = repo.get_session(sid)
    assert len(sess.freeze_ids) >= 2
    raw1: object = repo.get_freeze(sess.freeze_ids[0]).get("evidence_ids", [])
    raw2: object = repo.get_freeze(sess.freeze_ids[-1]).get("evidence_ids", [])
    e1 = {e for e in raw1 if isinstance(e, str)} if isinstance(raw1, list) else set()
    e2 = {e for e in raw2 if isinstance(e, str)} if isinstance(raw2, list) else set()
    assert e1 and e2 and e1 < e2  # cumulative E2 superset of E1

def test_grounded_claims_reject_unknown_and_empty() -> None:
    import json as _json
    from app.research.agents import ModelOutputFailure, parse_grounded_claims
    claims = parse_grounded_claims(_json.dumps([{"text": "revenue grew", "evidence_ids": ["EV-1"]}]), frozen=["EV-1", "EV-2"])
    assert [c.text for c in claims] and claims[0].evidence_ids == ["EV-1"]
    with pytest.raises(ModelOutputFailure):
        parse_grounded_claims(_json.dumps([{"text": "invented trend", "evidence_ids": ["EV-999"]}]), frozen=["EV-1"])
    with pytest.raises(ModelOutputFailure):
        parse_grounded_claims(_json.dumps([{"text": "Revenue grew with no citation.", "evidence_ids": []}]), frozen=["EV-1"])


def test_committee_refs_derive_from_claims_not_whole_freeze() -> None:
    import json as _json
    from app.research.agents import claims_refs
    from app.research.agents.stockbot import run_stockbot
    analysis = run_stockbot(
        "Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
        evidence_ids=["EV-1", "EV-2"], as_of="x",
        model=lambda prompt: _json.dumps({"claims": [{"text": "only first matters", "evidence_ids": ["EV-1"]}], "follow_ups": []}),
        evidence_text="[EV-1] a\n[EV-2] b",
    )
    assert claims_refs(analysis.claims) == ["EV-1"]
    assert [c.evidence_ids for c in analysis.claims] == [["EV-1"]]


def test_committee_unknown_id_fails_model_output() -> None:
    import json as _json
    from app.research.agents import ModelOutputFailure
    from app.research.agents.bearbot import run_bearbot
    from app.research.agents.bullbot import run_bullbot
    with pytest.raises(ModelOutputFailure):
        run_bullbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
                    evidence_ids=["EV-1"], as_of="x",
                    model=lambda prompt: _json.dumps({"claims": [{"text": "bad", "evidence_ids": ["EV-999"]}], "follow_ups": []}), evidence_text="[EV-1] a")
    with pytest.raises(ModelOutputFailure):
        run_bearbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
                    evidence_ids=["EV-1"], as_of="x",
                    model=lambda prompt: _json.dumps({"claims": [{"text": "Bearish with no citation.", "evidence_ids": []}], "follow_ups": []}), evidence_text="[EV-1] a")


def test_scout_findings_cite_only_acquired_ids() -> None:
    from app.research.agents import ModelOutputFailure
    from app.research.agents.scout import ScoutAssignment, run_scout
    assignment = ScoutAssignment(assignment_id="scout-filings", session_id="rs:t",
                                 as_of="2025-06-30", role="filings",
                                 question="Q?", tickers=[])
    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        assert name and args is not None
        if name == "browse_tools":
            return {}
        return {"evidence_ids": [{"evidence_id": "EV-1", "known_at": "2025-05-01"}]}
    import json as _json
    good = run_scout(assignment, dispatch=_dispatch, model=lambda prompt: _json.dumps([{"text": "found", "evidence_ids": ["EV-1"]}]))
    assert [c.evidence_ids for c in good.findings] == [["EV-1"]]
    with pytest.raises(ModelOutputFailure):
        run_scout(assignment, dispatch=_dispatch, model=lambda prompt: _json.dumps([{"text": "bad", "evidence_ids": ["EV-999"]}]))
    with pytest.raises(ModelOutputFailure):
        run_scout(assignment, dispatch=_dispatch, model=lambda prompt: _json.dumps([{"text": "Something factual uncited.", "evidence_ids": []}]))
def test_dossier_preserves_per_claim_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    out = _run_wave(repo, _grounded)
    assert out["stop_reason"] == "complete:wave1"
    stock = out["stock"]
    bull = out["bull"]
    bear = out["bear"]
    assert isinstance(stock, StockbotAnalysis)
    assert isinstance(bull, BullAnalysis)
    assert isinstance(bear, BearAnalysis)
    from app.research.agents import claims_refs as _crefs
    for analysis in (stock, bull, bear):
        frozen = set(analysis.evidence_ids)
        for claim in analysis.claims:
            assert claim.evidence_ids and set(claim.evidence_ids) <= frozen
        assert set(_crefs(analysis.claims)) <= frozen
    from app.research.synthesis.committee import CommitteeDisagreement
    from app.research.synthesis.final import synthesize_final
    sid = out["session_id"]
    fid = out["freeze_id"]
    eids = out["evidence_ids"]
    disagreement = out["disagreement"]
    assert isinstance(sid, str) and isinstance(fid, str)
    assert isinstance(eids, list) and isinstance(disagreement, CommitteeDisagreement)
    synth = synthesize_final("NVDA demand?", session_id=sid, wave_id=1,
                             freeze_id=fid, as_of="x", stock=stock,
                             bull=bull, bear=bear, disagreement=disagreement)
    from app.research.agents import claims_refs as _crefs2
    assert set(_crefs2(synth.claims)) <= set(e for e in eids if isinstance(e, str))

def test_resume_partial_source_reuses_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3
    from app.research.repository import get_research_db_path
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    calls = {"n": 0}
    base = _fake_dispatch()
    def _crash(name: str, args: dict[str, object]) -> dict[str, object]:
        if name == "call_tool":
            calls["n"] += 1
            if calls["n"] > 3:
                raise KeyboardInterrupt("simulated crash after first scout")
        return base(name, args)
    def _ok(prompt: str) -> str:
        return _grounded(prompt)
    import pytest as _pt
    with _pt.raises(KeyboardInterrupt):
        run_live(question="NVDA demand?", objective="o", as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"], dispatch=_crash, model=_ok, repo=repo, budgets=None)
    db = get_research_db_path()
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT DISTINCT session_id FROM jobs").fetchall()
    assert len(rows) == 1
    sid = str(rows[0][0])
    jobs_before = repo.list_jobs(sid)
    src_before = [j for j in jobs_before if j.job_type == "source_agent"]
    scout_before = [j for j in jobs_before if j.job_type == "scout"]
    assert len(src_before) == 1 and src_before[0].status == "running"
    assert len([j for j in scout_before if j.status == "completed"]) == 1
    out = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert out["stop_reason"] == "complete:wave1"
    jobs_after = repo.list_jobs(sid)
    assert len([j for j in jobs_after if j.job_type == "source_agent"]) == 1
    completed = [j for j in jobs_after if j.job_type == "scout" and j.status == "completed"]
    assert len(completed) == 3
    aids = [(j.diagnostics or {}).get("assignment_id") for j in completed]
    assert sorted(a for a in aids if isinstance(a, str)) == ["scout-filings", "scout-financials", "scout-risk"]
    assert len(repo.list_evidence(sid)) == 9

def test_run_resume_produce_complete_append_only_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.evals.traces import get_trace_events, list_traces
    from app.research.runner import resume_live
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repo = ResearchRepository()
    def _ok(prompt: str) -> str:
        return _grounded(prompt)
    out = run_live(question="NVDA demand?", objective="o", as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"], dispatch=_fake_dispatch(), model=_ok, repo=repo, budgets=None, interrupt_after="source")
    sid = out["session_id"]
    assert isinstance(sid, str)
    traces = list_traces(sid)
    assert len(traces) == 1
    tid = traces[0].trace_id
    evs = get_trace_events(tid)
    types = [e.event_type for e in evs]
    for required in ("trace.opened", "tool.completed", "evidence.ingested", "job.created", "job.completed", "model.completed"):
        assert required in types
    seqs = [e.seq for e in evs]
    assert len(set(seqs)) == len(seqs) and seqs == sorted(seqs)
    tools = [e for e in evs if e.event_type == "tool.completed"]
    assert tools and all(isinstance(e.payload.get("tool"), str) and e.payload.get("tool") for e in tools)
    assert all("args" in e.payload and "evidence_id" in e.payload for e in tools)
    models = [e for e in evs if e.event_type == "model.completed"]
    assert models and all("prompt" in e.payload and "output" in e.payload for e in models)
    discs = [e for e in evs if e.event_type == "discovery.completed"]
    assert discs and all("args" in e.payload and "matches" in e.payload for e in discs)
    n_before = len(evs)
    out2 = resume_live(sid, _fake_dispatch(), _ok, repo=repo)
    assert out2["stop_reason"] == "complete:wave1"
    assert list_traces(sid)[0].trace_id == tid
    evs2 = get_trace_events(tid)
    assert len(evs2) > n_before
    seqs2 = [e.seq for e in evs2]
    assert len(set(seqs2)) == len(seqs2) and seqs2 == sorted(seqs2)
    all_types = [e.event_type for e in evs2]
    assert "trace.resumed" in all_types
    resumed = next(e for e in evs2 if e.event_type == "trace.resumed")
    assert resumed.payload.get("trace_id") == tid and resumed.payload.get("session_id") == sid
    from app.research.evals.traces import get_trace
    header = get_trace(tid)
    assert header is not None and header.status == "completed" and header.conclusion
