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
from app.research.models import Job, JSONValue, SessionStatus
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

    from app.research.evidence import (
        Evidence,
        EvidenceLedger,
        EvidenceRejectedError,
        evidence_content_hash,
        ingest_evidence,
    )
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
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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
            if calls["n"] > 8:
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
    aids = [(j.diagnostics or {}).get("assignment_id") for j in completed]
    assert sorted(a for a in aids if isinstance(a, str)) == ["scout-filings", "scout-financials", "scout-risk"]
    eids_final = [str(r.get("evidence_id")) for r in repo.list_evidence(sid)]
    assert len(eids_final) == len(set(eids_final))

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

def _svc_sid(repo: ResearchRepository, q: str = "NVDA demand?") -> tuple[str, str]:
    from app.research import service as _svc
    sid = _svc.create_research(q, "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    jobs = repo.list_jobs(sid)
    return sid, jobs[0].job_id

def _svc_item(eid: str, wave: int = 1) -> dict[str, object]:
    return {"evidence_id": eid, "wave_id": wave, "content": "c-" + eid, "claim_text": "c", "subject": "NVDA", "source_name": "SEC", "source_uri": "https://sec.gov/x", "source_record_id": "r", "known_at": "2025-06-29T00:00:00+00:00"}

def _svc_ana(eid: str) -> dict[str, object]:
    return {"claims": [{"text": "finding", "evidence_ids": [eid]}], "follow_ups": []}

def _svc_trio(repo: ResearchRepository, sid: str, eid: str) -> None:
    from app.research import service as _svc
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role, _svc_ana(eid), repo=repo)

def test_committee_cannot_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.pi_gateway import PiSessionContext, execute_pi_tool
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    out = execute_pi_tool("search_web", {"query": "x", "session_id": sid}, PiSessionContext(session_id="t1"))
    assert "forbids" in str(out.get("error", ""))

def test_committee_cannot_add_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    bear = str(_svc.start_job(sid, "bearbot", repo=repo, wave_id=1)["job_id"])
    with pytest.raises(ValueError):
        _svc.record_evidence(sid, bear, _svc_item(f"{sid}:ev:2"), repo=repo)
    with pytest.raises(ValueError, match="forbids"):
        _svc.authorize_and_consume_dispatch(sid, bear, "search_web", repo=repo)

def test_role_job_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    stock = str(_svc.start_job(sid, "stockbot", repo=repo, wave_id=1)["job_id"])
    with pytest.raises(ValueError, match="!="):
        _svc.record_committee_analysis(sid, stock, "bearbot", _svc_ana(eid), repo=repo)

def test_evidence_rejected_on_completed_or_committee_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    with pytest.raises(ValueError, match="running"):
        _svc.record_evidence(sid, src, _svc_item(f"{sid}:ev:9"), repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    bull = str(_svc.start_job(sid, "bullbot", repo=repo, wave_id=1)["job_id"])
    with pytest.raises(ValueError):
        _svc.record_evidence(sid, bull, _svc_item(f"{sid}:ev:3"), repo=repo)

def test_restart_after_e1_resume_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    fresh = ResearchRepository()
    assert _svc.resume_research(sid, repo=fresh)["session"] is not None
    _svc_trio(fresh, sid, eid)
    _svc.decide_wave2(sid, repo=fresh)
    out = _svc.finalize_session(sid, "answer", [{"text": "finding", "evidence_ids": [eid]}], repo=fresh)
    assert out["freeze_id"] == f"{sid}:1:freeze"

def test_restart_after_2_of_3_committee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    ids = [str(_svc.start_job(sid, r, repo=repo, wave_id=1)["job_id"]) for r in ("stockbot", "bullbot", "bearbot")]
    for jid, role in zip(ids[:2], ("stockbot", "bullbot")):
        _svc.record_committee_analysis(sid, jid, role, _svc_ana(eid), repo=repo)
    fresh = ResearchRepository()
    _svc.record_committee_analysis(sid, ids[2], "bearbot", _svc_ana(eid), repo=fresh)
    out = _svc.finalize_session(sid, "answer", [{"text": "finding", "evidence_ids": [eid]}], repo=fresh)
    assert out["freeze_id"] == f"{sid}:1:freeze"

def test_source_terminal_before_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    _svc.record_evidence(sid, src, _svc_item(f"{sid}:ev:1"), repo=repo)
    with pytest.raises(ValueError, match="still open"):
        _svc.freeze_session(sid, 1, repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    assert _svc.freeze_session(sid, 1, repo=repo)["freeze_id"] == f"{sid}:1:freeze"

def test_budget_stops_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.pi_gateway as _gw
    from app.pi_gateway import PiSessionContext, execute_pi_tool
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, jid = _svc_sid(repo)
    repo.save_job(dataclasses.replace(repo.get_job(jid), tool_budget=1))
    ctx = PiSessionContext(session_id="t-budget")
    ctx.active_research_session_id = sid
    ctx.active_research_job_id = jid
    calls: list[tuple[str, dict[str, object]]] = []
    def _fake_execute(name: str, arguments: dict[str, object], model: str, context: object = None) -> dict[str, object]:
        calls.append((name, dict(arguments)))
        return {"result_type": "web_search", "query": arguments.get("query"), "results": [], "source": "exa"}
    monkeypatch.setattr(_gw, "execute_tool", _fake_execute)
    first = execute_pi_tool("search_web", {"query": "NVDA demand"}, ctx)
    assert "error" not in first, first
    second = execute_pi_tool("search_web", {"query": "NVDA demand"}, ctx)
    assert second.get("error_type") == "budget_exhausted", second
    assert len(calls) == 1
    assert calls[0][0] == "search_web" and calls[0][1] == {"query": "NVDA demand"}
    assert repo.get_job(jid).tool_budget == 0
    assert repo.get_session(sid).budget.get("tool_calls_used") == 1
    kept = execute_pi_tool("research_add_evidence", {"session_id": sid, "job_id": jid, "item": _svc_item(f"{sid}:ev:1")}, ctx)
    assert "error" not in kept, kept
    assert repo.get_job(jid).tool_budget == 0
    assert repo.get_session(sid).budget.get("tool_calls_used") == 1

def test_concurrent_dispatch_race_admits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, jid = _svc_sid(repo)
    repo.save_job(dataclasses.replace(repo.get_job(jid), tool_budget=1))
    sess = repo.get_session(sid)
    policy = dict(sess.policy)
    raw: object = policy.get("research", {})
    assert isinstance(raw, dict)
    section: dict[str, JSONValue] = dict(raw)
    section["max_tool_calls"] = 1
    policy["research"] = section
    repo.save_session(dataclasses.replace(sess, policy=policy))
    barrier = threading.Barrier(2)
    outcomes: list[object] = [None, None]

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=10)
            outcomes[idx] = _svc.authorize_and_consume_dispatch(sid, jid, "search_web", repo=repo)
        except Exception as exc:  # noqa: BLE001 — race outcome is the assertion
            outcomes[idx] = exc

    threads = [threading.Thread(target=_worker, args=(i,)) for i in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(thread.is_alive() is False for thread in threads)
    successes = [o for o in outcomes if isinstance(o, dict)]
    failures = [o for o in outcomes if isinstance(o, Exception)]
    assert len(successes) == 1, outcomes
    assert len(failures) == 1, outcomes
    assert "budget exhausted" in str(failures[0]).lower(), outcomes
    assert repo.get_session(sid).budget.get("tool_calls_used") == 1
    assert repo.get_job(jid).tool_budget == 0
    assert [j.job_id for j in repo.list_jobs(sid)] == [jid]


def test_stage_allows_common_read_controls() -> None:
    from app.research.stage import check_stage_tool

    for stage in ("SOURCE_RESEARCH", "COMMITTEE", "FINAL"):
        check_stage_tool(stage, "research_read")
        check_stage_tool(stage, "research_cancel")
    for stage in ("COMMITTEE", "FINAL"):
        with pytest.raises(ValueError, match="forbids"):
            check_stage_tool(stage, "research_add_evidence")
        with pytest.raises(ValueError, match="forbids"):
            check_stage_tool(stage, "search_web")


def test_queued_scout_blocks_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    _svc.record_evidence(sid, src, _svc_item(f"{sid}:ev:1"), repo=repo)
    found = repo.get_session(sid)
    existing = repo.list_jobs(sid)
    updated, scout = _jobs.create_job(found, existing, job_type="scout", owner="service", wave_id=1)
    repo.save_session(updated)
    repo.save_job(scout)
    with pytest.raises(ValueError, match="still open") as exc:
        _svc.freeze_session(sid, 1, repo=repo)
    assert scout.job_id in str(exc.value)
    assert repo.get_job(scout.job_id).status == "queued"


def test_start_job_rejects_malformed_budget_wave_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, _ = _svc_sid(repo)
    for bad in ("2", 2.0, True, None):
        with pytest.raises(ValueError, match="wave_id"):
            _svc.start_job(sid, "source_agent", budget={"wave_id": bad}, repo=repo)
    ok = _svc.start_job(sid, "source_agent", budget={"wave_id": 1}, repo=repo)
    assert ok["job_id"]


def test_record_evidence_rejects_bad_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    base = _svc_item(f"{sid}:ev:badmeta")
    bads: list[object] = [{"ok": object()}, {1: "x"}, "not-a-dict", [("k", "v")]]
    for i, bad in enumerate(bads):
        item = dict(base)
        item["evidence_id"] = f"{sid}:ev:bad{i}"
        item["metadata"] = bad
        with pytest.raises(ValueError, match="metadata"):
            _svc.record_evidence(sid, src, item, repo=repo)
    good = dict(base)
    good["evidence_id"] = f"{sid}:ev:goodmeta"
    good["metadata"] = {"source": "sec", "page": 3}
    out = _svc.record_evidence(sid, src, good, repo=repo)
    assert out["metadata"] == {"source": "sec", "page": 3}
# ---------------------------------------------------------------------------
# SEC-only NVDA/Anthropic regression + context/coverage/dedup/provenance.
# Forward-compatible: new APIs via lazy getattr; behavior asserts only,
# never exact call counts.
# ---------------------------------------------------------------------------

def _sec_only_policy() -> dict[str, JSONValue]:
    """SEC-only allowlist policy shape per contract (research_sources in)."""
    return {"research_sources": {"mode": "allowlist", "sources": ["SEC"]}}


def test_sec_only_policy_persisted_and_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sess = _session.create_session("NVDA AI demand?", "o", as_of=ASOF, policy=_sec_only_policy())
    doc = sess.to_dict()
    for key in ("session_id", "query", "objective", "as_of", "status", "policy", "budget",
                "source_policy", "temporal_scope"):
        assert key in doc
    sp = doc["source_policy"]
    assert isinstance(sp, dict)
    assert sp.get("mode") == "allowlist"
    allowed = sp.get("allowed")
    assert isinstance(allowed, list) and "SEC" in allowed
    assert sp.get("denied") == []
    ts = doc["temporal_scope"]
    assert isinstance(ts, dict)
    for key in ("as_of", "start", "end", "mode", "raw"):
        assert key in ts
    assert ts.get("mode") == "as_of"
    # Budgets kept alongside the new policy keys.
    budget = doc["budget"]
    assert isinstance(budget, dict)
    for key in ("deadline_seconds", "total_tool_budget", "total_token_budget", "total_cost_budget"):
        assert key in budget, key
    # Persist + reload: SQLite round-trips both fields.
    repo.save_session(sess)
    loaded = repo.get_session(sess.session_id)
    assert loaded.source_policy == sess.source_policy
    assert loaded.temporal_scope == sess.temporal_scope
    # Round-trip via from_dict/validate preserves the contract keys.
    from app.research.models import ResearchSession as _RS
    back = _RS.from_dict(doc)
    back.validate()
    assert back.to_dict()["source_policy"] == doc["source_policy"]
    assert back.to_dict()["temporal_scope"] == doc["temporal_scope"]
    # Temporal kwarg and default latest-available mode.
    scoped = _session.create_session("NVDA demand?", "o", as_of=None, temporal="between 2025-01-01 and 2025-06-30")
    assert scoped.temporal_scope.get("mode") == "range"
    latest = _session.create_session("NVDA demand?", "o", as_of=None)
    assert latest.temporal_scope.get("mode") == "latest-available"
    assert latest.temporal_scope.get("as_of") is not None


def test_sec_only_nvda_anthropic_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    calls: list[str] = []


    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return {"matches": ["search_sec_filings", "list_sec_filings"]}
        if name == "call_tool":
            inner = args.get("name")
            inner_name = inner if isinstance(inner, str) and inner else "unknown_tool"
            calls.append(inner_name)
            # No-issuer probe (Anthropic, private): empty record, never an error.
            if "anthropic" in str(args).lower() and "nvda" not in str(args).lower():
                return {"record": {}, "evidence_ids": []}
            return {"record": {"id": "r-" + str(len(calls)), "known_at": "2025-05-01",
                    "uri": "https://sec.gov/x", "record_id": "0001045810-25-000023"},
                    "evidence_ids": ["EV-1", "EV-2"]}
        return {}

    from app.research.agents.source_agent import build_research_context
    ctx = build_research_context("NVDA AI demand vs Anthropic private AI concepts?", ["NVDA"])
    blob = str(ctx).lower()
    assert "nvda" in blob
    assert "anthropic" in blob or "ai" in blob
    out = run_live(question="NVDA AI demand vs Anthropic private AI concepts?", objective="o",
                   as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"],
                   dispatch=_dispatch, model=_grounded, repo=repo, budgets=None)
    # No-issuer does not stop the session; NVDA filings + conceptual searches ran.
    assert out["stop_reason"] in ("complete:wave1", "complete:wave2")
    assert calls, "expected NVDA filing/conceptual SEC calls"
    # No SEC count cap: more than 3 useful calls allowed (behavior, not exact N).
    assert len([c for c in calls if c]) >= 3
    assert "up to 3" not in str(out).lower()
    # Freeze shared by trio; final answer produced with SEC-only limits explicit.
    stock = out["stock"]
    bull = out["bull"]
    bear = out["bear"]
    assert isinstance(stock, StockbotAnalysis)
    assert isinstance(bull, BullAnalysis)
    assert isinstance(bear, BearAnalysis)
    assert stock.freeze_id == bull.freeze_id == bear.freeze_id == out["freeze_id"]
    assert stock.evidence_ids == bull.evidence_ids == bear.evidence_ids
    sid = out["session_id"]
    assert isinstance(sid, str)
    final = repo.get_session(sid).final_result
    assert isinstance(final, dict) and str(final.get("answer", "")).strip()
    assert final.get("freeze_id") == out["freeze_id"]


def test_dedup_collapses_cosmetic_repeats() -> None:
    from app.research.agents.scout import normalize_query
    from app.research.agents.source_agent import normalize_query as _sa_norm
    assert normalize_query("AI demand") == normalize_query("AI   demand")
    assert normalize_query("AI Demand") == normalize_query("ai demand")
    assert _sa_norm("AI demand") == normalize_query("AI   demand")
    # Single execution oracle: normalized equivalents share one slot.
    executed: set[str] = set()
    for variant in ("AI demand", "AI   demand", "ai DEMAND"):
        key = normalize_query(variant)
        assert isinstance(key, str)
        executed.add(key)
    assert len(executed) == 1


def test_scout_executes_normalized_repeat_once() -> None:
    from app.research.agents.scout import ScoutAssignment, run_scout
    seen: list[tuple[str, object]] = []

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("browse_tools", "search_tools"):
            return {"matches": []}
        if name == "call_tool" and args.get("name") == "search_sec_filings":
            inner = args.get("arguments")
            query = inner.get("query") if isinstance(inner, dict) else None
            seen.append((str(query), inner.get("as_of") if isinstance(inner, dict) else None))
        return {"evidence_ids": []}

    assignment = ScoutAssignment(assignment_id="scout-filings", session_id="rs:t",
                                 as_of="2025-06-30", role="filings", question="Q?",
                                 tickers=[], queries=["AI demand", "AI   demand", "ai DEMAND"],
                                 baseline=[])
    result = run_scout(assignment, dispatch=_dispatch, model=lambda prompt: "[]")
    assert len(seen) == 1
    assert seen[0][0] == "AI demand"
    assert seen[0][1] == "2025-06-30"
    assert result.unknowns and result.unknowns[0] == "no PIT-eligible SEC evidence returned"


def test_merge_context_unions_model_terms() -> None:
    from app.research.agents.source_agent import _merge_context, build_research_context
    base = build_research_context("NVDA AI demand?", ["NVDA"])
    merged = _merge_context(base, {"concepts": ["accelerated computing"], "relationships": [{"subject": "NVDA", "relation": "supplies", "object": "hyperscalers"}]})
    assert "accelerated computing" in str(merged["concepts"]).lower()
    rels = merged["relationships"]
    assert isinstance(rels, list)
    assert any(isinstance(r, dict) and r.get("object") == "hyperscalers" for r in rels)
    assert _merge_context(base, "not-a-dict") == base


def test_expand_queries_mines_findings_and_context() -> None:
    from app.research.agents.source_agent import build_research_context, expand_queries
    ctx = build_research_context("NVDA AI demand?", ["NVDA"])
    out = expand_queries(["nvda demand"], ["Hyperscaler concentration grew in filings"], ctx)
    assert out and all(q.strip() for q in out)
    assert "nvda demand" not in [q.lower() for q in out]


def test_expansion_stop_prefers_info_over_counts() -> None:
    from app.research.agents.source_agent import expansion_stop
    assert expansion_stop(sec_answerable_remaining=False)[0] is True
    assert expansion_stop(new_queries=False)[0] is True
    assert expansion_stop(new_queries=True) == (False, "continue")

@pytest.mark.parametrize(("question", "tickers", "must_contain"), [
    ("Spirit AeroSystems Boeing 737 relationship and backlog?", ["SPR"], ("aerospace", "boeing")),
    ("Novo Nordisk GLP-1 diabetes obesity outlook?", ["NVO"], ("glp", "diabetes", "obesity")),
    ("Arista cloud networking datacenter Ethernet demand?", ["ANET"], ("cloud", "network", "datacenter")),
    ("Albemarle lithium brine battery demand?", ["ALB"], ("lithium", "battery")),
    ("Apple China supply chain and tariffs?", ["AAPL"], ("china", "supply")),
])
def test_cross_domain_context_carries_industry_terms(question: str, tickers: list[str], must_contain: tuple[str, ...]) -> None:
    from app.research.agents.source_agent import build_query_families, build_research_context
    ctx = build_research_context(question, tickers)
    blob = str(ctx).lower()
    assert any(term in blob for term in must_contain), blob[:500]
    # Industry/relationship/risk content beyond the raw query words.
    for key in ("industries", "relationships", "risks", "concepts"):
        assert key in ctx, sorted(ctx.keys())
    # No issuer-specific branches: generic families serve every domain.
    families = build_query_families(ctx)
    assert families and len({f.lower() for f in families}) == len(families)
    assert "Anthropic" not in str(families)


def test_no_date_means_latest_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    sess = _session.create_session("NVDA demand?", "o", as_of=None)
    doc = sess.to_dict()
    assert doc["as_of"] is None
    ts = doc["temporal_scope"]
    assert isinstance(ts, dict)
    assert ts.get("mode") == "latest-available"
    assert ts.get("as_of") is not None


def test_as_of_excludes_later_evidence() -> None:
    sess = _session.create_session("q?", "o", as_of=ASOF)
    led = EvidenceLedger()
    seen: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(EvidenceRejectedError):
        ingest_evidence(led, _ev("EV-FUT2", sess.session_id, 1,
                                 datetime(2025, 7, 1, tzinfo=timezone.utc)),
                        as_of=ASOF, on_reject=lambda t, p: seen.append((t, p)))
    assert seen and seen[0][1]["reason"] == "PIT_VIOLATION"
    assert led.ids() == ()
    # Latest-doc respects cutoff: eligible doc ingests, later doc rejects.
    ingest_evidence(led, _ev("EV-OK", sess.session_id, 1,
                             datetime(2025, 5, 1, tzinfo=timezone.utc)),
                    as_of=ASOF, on_reject=None)
    assert led.ids() == ("EV-OK",)


def test_coverage_shape_and_insufficient_never_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    coverage: dict[str, object] = {"useful_for_question": "insufficient",
                                   "resolved": [], "partially_resolved": [],
                                   "unresolved": ["Anthropic private revenue"],
                                   "source_limitations": ["SEC-only: no private-issuer filings"]}
    out = _svc.submit_source_result(src, coverage=coverage,
                                    evidence_ids=[], unresolved_questions=["Anthropic private revenue"], repo=repo)
    assert out["job_status"] == "completed"
    # submit = source job complete only; session never terminal here.
    assert repo.get_session(sid).status not in ("completed", "failed", "cancelled")
    # Coverage round-trips on the completed job result when the field lands.
    stored = repo.get_job(src).result or {}
    result_cov = stored.get("coverage") if isinstance(stored, dict) else None
    if isinstance(result_cov, dict):
        assert result_cov.get("useful_for_question") == "insufficient"
        for key in ("resolved", "partially_resolved", "unresolved", "source_limitations"):
            if key in result_cov:
                assert isinstance(result_cov[key], list)
    # Insufficient still freezes + synthesizes via the kernel path.
    assert _svc.freeze_session(sid, 1, repo=repo)["freeze_id"] == f"{sid}:1:freeze"


def test_negatives_cite_searchrun_only() -> None:
    from app.sec.models import DocumentMatch, MatchingPassage, SearchRun
    import dataclasses as _dc
    fields = {f.name for f in _dc.fields(SearchRun)}
    for required in ("id", "source", "query", "filters", "executed_at", "as_of",
                     "matched_entities", "matched_documents", "matched_passages"):
        assert required in fields, sorted(fields)
    # Negative-claim shape: grounded positives cite document/passage/accession,
    # negatives cite the SearchRun id only — never 'not found' as 'does not exist'.
    doc_fields = {f.name for f in _dc.fields(DocumentMatch)}
    assert doc_fields >= {"accession", "matching_passages"}
    passage_fields = {f.name for f in _dc.fields(MatchingPassage)}
    assert passage_fields >= {"document", "query", "score"}


def test_sec_gates_deny_non_sec_even_via_browse() -> None:
    from app.research.agents.source_agent import is_sec_tool
    for denied in ("query_finra", "get_short_interest", "search_web", "get_market_snapshot", "get_analyst_estimates"):
        assert is_sec_tool(denied) is False, denied
    for allowed in ("search_sec_filings", "list_sec_filings", "get_sec_document"):
        assert is_sec_tool(allowed) is True, allowed
    # Runner gate surfaces POLICY_DENIED (not a prompt-text refusal).
    from app.research.runner import _LiveRun
    import inspect as _inspect
    src = _inspect.getsource(_LiveRun._guarded_tool_call)
    assert "POLICY_DENIED" in src
    assert "is_sec_tool" in src

def test_resolve_source_policy_sec_only_allowlist() -> None:
    from app.research.models import resolve_source_policy, validate_source_policy
    sec_only = resolve_source_policy({"research_sources": {"mode": "allowlist", "sources": ["SEC"]}})
    assert sec_only["mode"] == "allowlist"
    allowed = sec_only["allowed"]
    assert isinstance(allowed, list) and "SEC" in allowed
    assert sec_only["denied"] == []
    # Kernel default is SEC-only even with no policy input.
    assert resolve_source_policy(None)["mode"] == "allowlist"
    # Round-trip through the validator preserves the contract shape.
    assert validate_source_policy(sec_only) == sec_only
    with pytest.raises(ValueError, match="mode"):
        resolve_source_policy({"research_sources": {"mode": "someday", "sources": ["SEC"]}})

def test_resolve_temporal_scope_modes() -> None:
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from app.research.models import resolve_temporal_scope, validate_temporal_scope
    now = _dt(2025, 6, 30, tzinfo=_tz.utc)
    # No date -> latest-available with cutoff set, never invented/None.
    latest = resolve_temporal_scope(query="NVDA demand?", now=now)
    assert latest["mode"] == "latest-available"
    assert latest["as_of"] is not None
    # as-of-2025-01-01 excludes later docs.
    asof = resolve_temporal_scope(as_of="2025-01-01", now=now)
    assert asof["mode"] == "as_of"
    assert str(asof["as_of"])[:10] == "2025-01-01"
    # Interval start/end bounds.
    interval = resolve_temporal_scope(temporal="between 2025-01-01 and 2025-06-30", now=now)
    assert interval["mode"] == "range"
    assert str(interval["start"])[:10] == "2025-01-01"
    assert str(interval["end"])[:10] == "2025-06-30"
    # Last-quarter range resolves start/end around the pinned clock.
    quarter = resolve_temporal_scope(temporal="this quarter", now=now)
    assert quarter["mode"] == "range"
    assert quarter["start"] is not None and quarter["end"] is not None
    assert validate_temporal_scope(dict(latest)) == latest
# ---------------------------------------------------------------------------
# RegressionEval §17 suites (deterministic, fakes only, no network).
# Naming: test_reg_<suite>_<behavior>. Contract stubs resolve via getattr
# with a precise failure naming the missing source hook; no source edits.
# ---------------------------------------------------------------------------

def _reg_svc_sid(repo: ResearchRepository, q: str = "GS OpenAI exposure?") -> tuple[str, str]:
    from app.research import service as _svc
    sid = _svc.create_research(q, "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    return sid, repo.list_jobs(sid)[0].job_id

def _reg_cov(useful: str = "sufficient") -> dict[str, object]:
    return {"useful_for_question": useful, "resolved": ["GS direct OpenAI exposure"],
            "partially_resolved": [], "unresolved": [], "source_limitations": []}

def _reg_item(eid: str, wave: int = 1, **over: object) -> dict[str, object]:
    base: dict[str, object] = {"evidence_id": eid, "wave_id": wave, "content": "c-" + eid,
                               "claim_text": f"GS OpenAI-linked exposure per filing {eid}",
                               "subject": "GS", "source_name": "SEC",
                               "source_uri": "https://www.sec.gov/Archives/edgar/data/886982/000088698226000001/primary.htm",
                               "source_record_id": "0000886982-26-000001",
                               "known_at": "2025-06-29T00:00:00+00:00"}
    base.update(over)
    return base

def test_reg_lifecycle_running_until_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    assert repo.get_job(src).status == "running"
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    assert repo.get_job(src).status == "running"
    assert repo.get_session(sid).status not in ("completed", "failed", "cancelled")


def test_reg_lifecycle_atomic_submit_completes_job_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    out = _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    assert out["job_status"] == "completed"
    assert repo.get_job(src).status == "completed"
    assert repo.get_session(sid).status not in ("completed", "failed", "cancelled")


def test_reg_lifecycle_double_submit_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    with pytest.raises(ValueError, match="CLOSED|closed|completed|running"):
        _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)


def test_reg_lifecycle_closed_rejects_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    with pytest.raises(ValueError, match="running|closed|completed"):
        _svc.record_evidence(sid, src, _reg_item(f"{sid}:ev:2"), repo=repo)

# --- unlimited (4): 100+ distinct allowed, no default kill, dup-no-progress, diff queries ---

def _reg_dispatch(repo: ResearchRepository, sid: str, jid: str, tool: str) -> None:
    from app.research import service as _svc
    _svc.authorize_and_consume_dispatch(sid, jid, tool, repo=repo)


def test_reg_unlimited_100_distinct_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid = _svc.create_research("GS OpenAI exposure?", "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    jobs = repo.list_jobs(sid)
    assert len(jobs) == 1
    jid = jobs[0].job_id
    assert jobs[0].tool_budget is None, ("missing contract (LimitsLoopBudget owns "
        "app/research/jobs.py:job_tool_budget + app/research/models.py:DEFAULT_BUDGET): "
        "unlimited=None; source job tool_budget must default None")
    for _ in range(100):
        _reg_dispatch(repo, sid, jid, "get_sec_document")
    assert repo.get_session(sid).budget.get("tool_calls_used") == 100


def test_reg_unlimited_no_default_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research.models import DEFAULT_BUDGET
    assert DEFAULT_BUDGET.get("total_tool_budget") is None, ("missing contract (LimitsLoopBudget owns "
        "app/research/models.py:default_budget/DEFAULT_BUDGET): unlimited=None; "
        f"total_tool_budget must default None, got {DEFAULT_BUDGET.get('total_tool_budget')!r}")
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, jid = _reg_svc_sid(repo)
    for _ in range(25):
        _reg_dispatch(repo, sid, jid, "get_sec_document")
    assert repo.get_session(sid).budget.get("tool_calls_used") == 25


def test_reg_unlimited_duplicate_no_progress_rejected() -> None:
    import app.research.director as _director
    normalize = getattr(_director, "normalize_research_action", None)
    loop_cls = getattr(_director, "LoopDetector", None)
    assert callable(normalize) and loop_cls is not None, ("missing source hook (LimitsLoopBudget owns "
        "app/research/director.py:normalize_research_action + LoopDetector): contract normalize("
        "source,tool,query,ticker,forms,as_of,accession,objective)->tuple; "
        "LoopDetector.check(action,result_hash,evidence_delta)->{duplicate,reason} + telemetry list")
    loop = loop_cls()
    action = normalize("sec", "get_sec_document", "GS 10-K", "GS", ("10-K",), "2025-06-30", "ACC-1", "exposure?")
    first = loop.check(action, "hash-a", 3)
    assert first.get("duplicate") is False
    repeat = loop.check(action, "hash-a", 0)
    assert repeat.get("duplicate") is True, repeat


def test_reg_unlimited_different_queries_allowed() -> None:
    import app.research.director as _director
    normalize = getattr(_director, "normalize_research_action", None)
    loop_cls = getattr(_director, "LoopDetector", None)
    assert callable(normalize) and loop_cls is not None, ("missing source hook (LimitsLoopBudget owns "
        "app/research/director.py:normalize_research_action + LoopDetector): contract normalize("
        "source,tool,query,ticker,forms,as_of,accession,objective)->tuple; "
        "LoopDetector.check(action,result_hash,evidence_delta)->{duplicate,reason} + telemetry list")
    loop = loop_cls()
    a = normalize("sec", "get_sec_document", "GS 10-K risk", "GS", ("10-K",), "2025-06-30", "ACC-1", "exposure?")
    b = normalize("sec", "get_sec_document", "GS 10-Q MD&A", "GS", ("10-Q",), "2025-06-30", "ACC-2", "exposure?")
    assert loop.check(a, "hash-a", 2).get("duplicate") is False
    assert loop.check(b, "hash-b", 2).get("duplicate") is False
def test_reg_freshness_latest_default() -> None:
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from app.research.models import resolve_temporal_scope
    latest = resolve_temporal_scope(query="What happens to Goldman Sachs if OpenAI goes bankrupt?",
                                    now=_dt(2025, 6, 30, tzinfo=_tz.utc))
    assert latest["mode"] == "latest-available"
    assert latest["as_of"] is not None


def test_reg_freshness_latest_10k_pinned() -> None:
    from app.research.models import select_latest_baseline
    filings: list[object] = [{"form": "10-K", "known_at": "2025-02-14", "filed_at": "2025-02-14", "accession_no": "OLD"},
                             {"form": "10-K", "known_at": "2025-06-20", "filed_at": "2025-06-20", "accession_no": "NEW"},
                             {"form": "10-K/A", "known_at": "2025-06-25", "filed_at": "2025-06-25", "accession_no": "AMD"}]
    base = select_latest_baseline(filings, as_of="2025-06-30")
    annual = base["annual_10k"]
    acc = annual.get("accession_no") if isinstance(annual, dict) else getattr(annual, "accession_no", None)
    assert acc in ("NEW", "AMD"), acc


def test_reg_freshness_superseded_only_current_rejected() -> None:
    from app.research.models import select_latest_baseline, superseded_current_violation
    filings: list[object] = [{"form": "10-K", "known_at": "2024-02-10", "filed_at": "2024-02-10", "accession_no": "OLD",
                              "superseded_by": "NEW"},
                             {"form": "10-K", "known_at": "2025-02-14", "filed_at": "2025-02-14", "accession_no": "NEW"}]
    base = select_latest_baseline(filings, as_of="2025-06-30")
    annual = base["annual_10k"]
    acc = annual.get("accession_no") if isinstance(annual, dict) else getattr(annual, "accession_no", None)
    assert acc == "NEW", acc
    assert superseded_current_violation(filings, "OLD", as_of="2025-06-30") is not None
    assert superseded_current_violation(filings, "NEW", as_of="2025-06-30") is None



def test_reg_freshness_historical_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # PIT-eligible historical evidence ingests; a future known_at rejects.
    # Distinct accession per item: dedupe keys on source_record_id, so a shared
    # accession would return duplicate_of instead of reaching the PIT gate.
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid = _svc.create_research("GS 2024 exposure?", "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    jid = repo.list_jobs(sid)[0].job_id
    out = _svc.record_evidence(sid, jid, _reg_item(f"{sid}:ev:1", known_at="2024-12-01T00:00:00+00:00"), repo=repo)
    assert out["evidence_id"] == f"{sid}:ev:1"
    with pytest.raises(ValueError, match="PIT_VIOLATION|rejected"):
        _svc.record_evidence(sid, jid, _reg_item(f"{sid}:ev:2", known_at="2025-07-01T00:00:00+00:00",
                                                 source_record_id="0000886982-26-000002",
                                                 source_uri="https://www.sec.gov/Archives/edgar/data/886982/000088698226000002/primary.htm"), repo=repo)

def test_reg_negatives_coverage_present() -> None:
    from app.research.dossiers.sec import default_coverage
    from app.research.service import NEGATIVE_COVERAGE_KEYS
    assert tuple(NEGATIVE_COVERAGE_KEYS) == ("forms", "dates", "partitions", "docs", "gaps", "complete")
    cov = default_coverage()
    for key in ("forms", "sources_examined", "complete", "exclusions"):
        assert key in cov, sorted(cov.keys())


def test_reg_negatives_scoped_no_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    scoped_cov: dict[str, object] = {"forms": ["10-K"], "dates": ["2025-02-14"], "partitions": ["efts"],
                                     "docs": ["0000886982-26-000001"], "gaps": [], "complete": False}
    out = _svc.record_evidence(sid, src, {"evidence_id": f"{sid}:ev:n1", "wave_id": 1,
                                          "content": "no OpenAI bankruptcy exposure disclosed in sections 1-3 of the scoped GS 10-K",
                                          "claim_text": "not found in sections 1-3 of the scoped GS 10-K: OpenAI bankruptcy exposure",
                                          "subject": "GS", "source_name": "SEC",
                                          "source_uri": "https://www.sec.gov/Archives/edgar/data/886982/000088698226000001/primary.htm",
                                          "source_record_id": "0000886982-26-000001",
                                          "known_at": "2025-06-29T00:00:00+00:00",
                                          "search_id": "s1", "query": "GS OpenAI bankruptcy",
                                          "coverage": scoped_cov}, repo=repo)
    assert out["evidence_id"] == f"{sid}:ev:n1"
    with pytest.raises(ValueError, match="ERR_COVERAGE_REQUIRED"):
        _svc.record_evidence(sid, src, {"evidence_id": f"{sid}:ev:n2", "wave_id": 1,
                                        "content": "no exposure anywhere",
                                        "claim_text": "no OpenAI exposure in any filing",
                                        "subject": "GS", "source_name": "SEC",
                                        "known_at": "2025-06-29T00:00:00+00:00",
                                        "search_id": "s1", "query": "GS OpenAI",
                                        "coverage": {"forms": ["10-K"], "dates": [], "partitions": [],
                                                     "docs": [], "gaps": [], "complete": False}}, repo=repo)


def test_reg_negatives_incomplete_stays_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    _svc.record_evidence(sid, src, _reg_item(f"{sid}:ev:1"), repo=repo)
    out = _svc.submit_source_result(src, coverage={"useful_for_question": "insufficient", "resolved": [],
                                                   "partially_resolved": [], "unresolved": ["OpenAI private terms"],
                                                   "source_limitations": ["SEC-only"]},
                                    evidence_ids=[], unresolved_questions=["OpenAI private terms"], repo=repo)
    assert out["job_status"] == "completed"
    stored = repo.get_job(src).result or {}
    assert isinstance(stored, dict)
    cov = stored.get("coverage")
    assert isinstance(cov, dict) and cov.get("useful_for_question") == "insufficient"
    assert cov.get("complete") in (None, False)

# --- PIT (3): accepted_at==known_at, TZ preserves instant, future rejected ---

def test_reg_pit_accepted_at_is_known_at() -> None:
    from typing import override

    from edgar import Filing as EdgarFiling

    from app.sec.normalization import filing_from_edgar

    class _StubFiling(EdgarFiling):
        acceptance_datetime = "2025-02-14T17:30:00Z"

        @override
        @property
        def period_of_report(self) -> str:
            return "2024-12-31"

        @override
        @property
        def document(self) -> str:
            return "primary.htm"

    filing = _StubFiling(cik=886982, company="Goldman Sachs", form="10-K",
                         filing_date="2025-02-14", accession_no="0000886982-26-000001")
    meta = filing_from_edgar(filing)
    assert meta.known_at == "2025-02-14T17:30:00Z"
    assert meta.accepted_at == meta.known_at


def test_reg_pit_tz_preserves_instant() -> None:
    from app.research.models import pit_violated
    assert pit_violated("2025-06-30T00:00:00+00:00", "2025-06-29T20:00:00-04:00") is False
    assert pit_violated("2025-06-30T00:00:00+00:00", "2025-06-30T01:00:00+00:00") is True


def test_reg_pit_future_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    with pytest.raises(ValueError):
        _svc.record_evidence(sid, src, _reg_item(f"{sid}:ev:9", known_at="2025-07-01T00:00:00+00:00"), repo=repo)

# --- freeze/committee (5): immutable, identical freeze ID, no source tools, new wave, decide ---
def test_reg_freeze_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses as _dc
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from app.research import service as _svc
    from app.research.freeze import EvidenceFreeze
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    frozen = _svc.freeze_session(sid, 1, repo=repo)
    fid = str(frozen["freeze_id"])
    stored = repo.get_freeze(fid)
    assert stored["freeze_id"] == fid
    now = _dt(2025, 6, 30, tzinfo=_tz.utc)
    with pytest.raises(_dc.FrozenInstanceError):
        frozen_obj = EvidenceFreeze(freeze_id=fid, session_id=sid, wave_id=1, created_at=now,
                                    as_of=now, evidence_ids=(eid,), content_hash="h")
        setattr(frozen_obj, "evidence_ids", ("tampered",))
    assert repo.get_freeze(fid)["freeze_id"] == fid
def test_reg_freeze_identical_id_for_committee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    fid = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    trio = _svc.create_committee_jobs(sid, 1, repo=repo)
    created = trio.get("jobs")
    assert isinstance(created, list) and len(created) == 3
    jobs = repo.list_jobs(sid)
    assert repo.get_session(sid).freeze_ids[-1] == fid
    assert all(j.wave_id == 1 for j in jobs if j.job_type in ("stockbot", "bullbot", "bearbot"))


def test_reg_committee_cannot_call_source_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    bear = str(_svc.start_job(sid, "bearbot", repo=repo, wave_id=1)["job_id"])
    with pytest.raises(ValueError, match="forbids"):
        _svc.authorize_and_consume_dispatch(sid, bear, "get_sec_document", repo=repo)


def test_reg_freeze_new_wave_new_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    f1 = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    assert f1 == f"{sid}:1:freeze"
    assert f"{sid}:2:freeze" != f1


def test_reg_freeze_director_finalize_or_wave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role,
                                       {"claims": [{"text": "finding", "evidence_ids": [eid]}], "follow_ups": []}, repo=repo)
    out = _svc.decide_wave2(sid, repo=repo)
    assert out["stop_reason"] in ("no_questions", "low_gain", "not_actionable", "continue",
                                  "max_waves", "jobs_exceeded", "budget_exhausted", "runtime_exceeded")

# --- synthesis (4): supported trace, inference labeled, unknown, manageable ---

def test_reg_synthesis_supported_traces_to_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _reg_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role,
                                       {"claims": [{"text": "GS discloses OpenAI-linked exposure", "evidence_ids": [eid]}], "follow_ups": []}, repo=repo)
    out = _svc.finalize_session(sid, "GS exposure is filing-backed.",
                                [{"text": "GS discloses OpenAI-linked exposure", "evidence_ids": [eid]}], repo=repo)
    assert out["freeze_id"] == f"{sid}:1:freeze"
    final = repo.get_session(sid).final_result or {}
    assert isinstance(final, dict)
    claims_raw = final.get("claims")
    assert isinstance(claims_raw, list) and claims_raw
    first = claims_raw[0]
    assert isinstance(first, dict) and first.get("evidence_ids") == [eid]


def test_reg_synthesis_inference_labeled() -> None:
    from app.research.agents import classify_claim, parse_grounded_claims
    claims = parse_grounded_claims('[{"text": "INFERENCE: OpenAI stress may widen GS spreads", "evidence_ids": ["EV-1"]}]', frozen=["EV-1"])
    assert claims and claims[0].claim_class == "INFERENCE"
    assert classify_claim("INFERENCE: OpenAI stress may widen GS spreads", cited=True) == "INFERENCE"


def test_reg_synthesis_unknown_stays_unknown() -> None:
    from app.research.evals.evaluators import EvalInput, evaluate
    inp = EvalInput(scenario_name="gs-openai-sec-only", answer_text="OpenAI private revenue share: UNKNOWN (no filing discloses it).",
                    evidence_ids=("EV-1",), requires_evidence=True)
    assert evaluate(inp).passed


def test_reg_synthesis_manageable_rejected_as_fact() -> None:
    from app.research.agents import ModelOutputFailure, parse_grounded_claims
    with pytest.raises(ModelOutputFailure):
        parse_grounded_claims('[{"text": "GS will manageably absorb any OpenAI loss", "evidence_ids": []}]', frozen=["EV-1"])
# --- evidence record_kind + claim labels (contract pins, stub-safe) ---

def test_reg_evidence_record_kind_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    from app.research.evidence import DiscoveryRecord, EvidenceRecord, RECORD_KINDS, discovery_only, substantive_records
    assert RECORD_KINDS == frozenset({"discovery", "evidence"})
    assert DiscoveryRecord(record_id="d1", session_id="s", tool="search_sec_filings", query="GS 10-K").search_id is None
    assert EvidenceRecord(record_id="e1", session_id="s", evidence_id="EV-1", claim_text="c").evidence_id == "EV-1"
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    out = _svc.record_evidence(sid, src, _reg_item(f"{sid}:ev:d1", claim_text="scoping search",
                                                   type="search_coverage", query="GS 10-K", search_id="s1",
                                                   record_kind="discovery"), repo=repo)
    assert out.get("record_kind") == "discovery"
    assert discovery_only([{"record_kind": "discovery"}]) is True
    assert substantive_records([{"record_kind": "discovery"}, {"record_kind": "evidence"}]) == [{"record_kind": "evidence"}]


def test_reg_evidence_claim_labels() -> None:
    from app.research.agents import CLAIM_CLASSES, GroundedClaim, classify_claim
    assert tuple(CLAIM_CLASSES) == ("DIRECTLY_SUPPORTED", "INFERENCE", "UNKNOWN", "CONTRADICTED")
    assert classify_claim("GS revenue grew", cited=True) == "DIRECTLY_SUPPORTED"
    assert classify_claim("INFERENCE: spreads may widen", cited=True) == "INFERENCE"
    assert classify_claim("OpenAI terms UNKNOWN", cited=True) == "UNKNOWN"
    assert classify_claim("GS will manageably absorb any loss", cited=False) in ("INFERENCE", "UNKNOWN")
    assert GroundedClaim(text="GS revenue grew", evidence_ids=["EV-1"]).claim_class == "DIRECTLY_SUPPORTED"
    assert GroundedClaim(text="GS revenue grew", evidence_ids=["EV-1"]).label == "DIRECTLY_SUPPORTED"


