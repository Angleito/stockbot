"""Permanent §44/§45 gates: transitions, budgets, journal, PIT, append-only,
freeze immutability + cumulative E2, dossier refs, same-freeze parallel
committee, resume at source/freeze/one-member. All fakes, no network."""
import dataclasses
import itertools
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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


def _committee_output(claims: list[dict[str, object]], follow_ups: Sequence[object] = ()) -> str:
    """Rich committee envelope (executive_view, claims, channels, materiality, uncertainties,
    what_would_change, follow_ups): the role contract every trio prompt now demands."""
    import json as _json
    ids: list[str] = []
    for claim in claims:
        raw_ids = claim.get("evidence_ids")
        if isinstance(raw_ids, list):
            ids.extend(eid for eid in raw_ids if isinstance(eid, str))
    channels: list[dict[str, object]] = (
        [{"text": "frozen-evidence channel", "direction": "pressure", "evidence_ids": [ids[0]]}] if ids else []
    )
    return _json.dumps({
        "executive_view": "Balanced read of the frozen evidence.",
        "claims": claims,
        "impact_channels": channels,
        "materiality": {"overall": "medium", "reasoning": "effect size read off the frozen evidence"},
        "uncertainties": ["the freeze does not settle timing"],
        "what_would_change": ["a new filing disclosing the terms"],
        "follow_ups": list(follow_ups),
    })


def _grounded(prompt: str, follow_ups: Sequence[object] = ()) -> str:
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
    return _committee_output(claims, follow_ups)

def _ev(eid: str, sid: str, wave: int, known: datetime) -> Evidence:
    return Evidence(
        evidence_id=eid, session_id=sid, wave_id=wave, source_type="sec",
        source_name="SEC", source_uri="https://sec.gov/x", source_record_id="r",
        subject="NVDA", claim_text="c", content="c-" + eid,
        content_hash=evidence_content_hash("c-" + eid),
        known_at=known, retrieved_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        job_id="J-1", agent_id="s-A",
    )


def _frozen_write(obj: object, name: str, value: object) -> None:
    """Write one attribute through the instance __setattr__ (frozen dataclasses raise)."""
    setattr(obj, name, value)


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
    """Unlimited by default: no max_total_jobs ceiling until policy configures one."""
    s = _session.create_session("q?", "o", as_of=ASOF)
    jobs: list[Job] = []
    last = s
    for _ in range(21):
        last, j = _jobs.create_job(last, jobs, job_type="scout", owner="t")
        jobs.append(_jobs.complete_job(_jobs.start_job(j)))
    assert len(jobs) == 21  # no configured cap: creation never raises
    policy: dict[str, JSONValue] = dict(last.policy)
    raw: object = policy.get("research", {})
    assert isinstance(raw, dict)
    section: dict[str, JSONValue] = dict(raw)
    section["max_total_jobs"] = 21
    policy["research"] = section
    capped = dataclasses.replace(last, policy=policy)
    with pytest.raises(ValueError, match="max_total_jobs"):
        _jobs.create_job(capped, jobs, job_type="scout", owner="t")


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
        _frozen_write(f2, "evidence_ids", ("EV-1",))
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


def _fake_document(index: int, args: Mapping[str, object]) -> dict[str, object]:
    """One raw filing document result (accession + document_name + passage): the only evidence shape."""
    raw_accession = args.get("accession_no")
    accession = (raw_accession.strip() if isinstance(raw_accession, str) and raw_accession.strip()
                 else f"0000320193-25-{index:06d}")
    raw_document = args.get("document_name")
    document = (raw_document.strip() if isinstance(raw_document, str) and raw_document.strip()
                else f"nvda-20250331-{index}.htm")
    return {
        "content": f"NVDA {document}: accelerated computing demand grew in the reporting period.",
        "accession_no": accession,
        "document_name": document,
        "matching_passage": "Data Center revenue grew year over year on accelerated computing demand.",
        "known_at": "2025-05-01",
        "uri": f"https://www.sec.gov/Archives/edgar/data/1045810/{accession.replace('-', '')}/{document}",
    }


def _fake_dispatch() -> DispatchFn:
    """SEC fake: searches return navigation packets (top_hits), document opens return raw passages.

    Only ``get_sec_document``/``get_sec_filing`` results carrying an accession, a
    document name, and a passage become evidence; every other result is navigation.
    """
    counter = itertools.count(1)

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name in ("search_tools", "browse_tools"):
            return {"matches": ["search_sec_filings", "list_sec_filings", "get_sec_document", "get_sec_filing"]}
        if name == "call_tool":
            n = next(counter)
            inner = str(args.get("name") or "")
            inner_args = args.get("arguments")
            requested: dict[str, object] = inner_args if isinstance(inner_args, dict) else {}
            if inner in ("get_sec_document", "get_sec_filing"):
                return _fake_document(n, requested)
            return {"search_id": f"s{n}", "query": str(requested.get("query") or ""), "count": 1,
                    "top_hits": [{"accession": f"0000320193-25-{n:06d}",
                                  "document": f"nvda-20250331-{n}.htm", "form": "10-Q"}]}
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
    assert isinstance(eids_after, list)
    # The resumed run reuses exactly the persisted wave evidence: every dossier-cited id
    # is in it, it never advertises a navigation artifact, and nothing new was fetched.
    persisted = [dict(r) for r in repo.list_evidence(sid)]
    evidence_rows = {str(r.get("evidence_id")) for r in persisted if r.get("record_kind") == "evidence"}
    discovery_rows = {str(r.get("evidence_id")) for r in persisted if r.get("record_kind") == "discovery"}
    assert set(eids_before) <= set(eids_after) == evidence_rows
    assert discovery_rows and not (set(eids_after) & discovery_rows)
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
    """A material wave-1 follow-up drives a second wave on cumulative evidence; an explicit
    configured max_waves (never an architectural ceiling) then settles the run at complete:wave2."""
    from app.research.director import DirectorBudgets

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()

    def _model(prompt: str) -> str:
        assert prompt
        return _grounded(prompt, ["What drove the Q2 delta?"])

    out = run_live(question="NVDA historical demand?", objective="o", as_of="2025-06-30T00:00:00+00:00", tickers=["NVDA"], dispatch=_fake_dispatch(), model=_model, repo=repo, budgets=DirectorBudgets(max_waves=2))
    assert out["stop_reason"] == "complete:wave2"
    assert str(out["wave_decision"]).startswith("max_waves")  # explicit int honored, waves are sequence numbers
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
    # The committee's wave-1 request (never evidence) drove the targeted wave-2 source job.
    wave2_src = src_jobs[-1]
    assert (wave2_src.diagnostics or {}).get("question") == "What drove the Q2 delta?"
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
    from app.research.agents import claims_refs
    from app.research.agents.stockbot import run_stockbot
    analysis = run_stockbot(
        "Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
        evidence_ids=["EV-1", "EV-2"], as_of="x",
        model=lambda prompt: _committee_output([{"text": "only first matters", "evidence_ids": ["EV-1"]}]),
        evidence_text="[EV-1] a\n[EV-2] b",
    )
    assert claims_refs(analysis.claims) == ["EV-1"]
    assert [c.evidence_ids for c in analysis.claims] == [["EV-1"]]


def test_committee_unknown_id_fails_model_output() -> None:
    from app.research.agents import ModelOutputFailure
    from app.research.agents.bearbot import run_bearbot
    from app.research.agents.bullbot import run_bullbot
    with pytest.raises(ModelOutputFailure, match="EV-999"):
        run_bullbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
                    evidence_ids=["EV-1"], as_of="x",
                    model=lambda prompt: _committee_output([{"text": "bad", "evidence_ids": ["EV-999"]}]), evidence_text="[EV-1] a")
    with pytest.raises(ModelOutputFailure, match="uncited"):
        run_bearbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1",
                    evidence_ids=["EV-1"], as_of="x",
                    model=lambda prompt: _committee_output([{"text": "Bearish with no citation.", "evidence_ids": []}]), evidence_text="[EV-1] a")


def test_scout_findings_cite_only_acquired_ids() -> None:
    """Scout boundary: only acquired ids ground; a bad record is dropped and reported, never fatal."""
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
    # A fabricated id loses its own claim; the grounded sibling survives with a limitation.
    mixed = run_scout(assignment, dispatch=_dispatch, model=lambda prompt: _json.dumps(
        [{"text": "found", "evidence_ids": ["EV-1"]}, {"text": "bad", "evidence_ids": ["EV-999"]}]))
    assert [c.text for c in mixed.findings] == ["found"]
    assert any("dropped" in line for line in mixed.limitations)
    # An uncited non-unknown claim is dropped too: nothing ungrounded reaches the dossier.
    uncited = run_scout(assignment, dispatch=_dispatch, model=lambda prompt: _json.dumps(
        [{"text": "Something factual uncited.", "evidence_ids": []}]))
    assert uncited.findings == []
    assert any("dropped" in line for line in uncited.limitations)
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
    frozen_raw: object = repo.get_freeze(fid).get("evidence_ids", [])
    frozen_ids = {e for e in frozen_raw if isinstance(e, str)} if isinstance(frozen_raw, list) else set()
    assert frozen_ids  # the freeze is the committee's evidence universe
    for claim in synth.claims:
        assert claim.evidence_ids and set(claim.evidence_ids) <= frozen_ids
        # Per-claim mapping survives synthesis: same text -> exactly the trio's cited ids.
        expected = {eid for analysis in (stock, bull, bear) for c in analysis.claims
                    if c.text == claim.text for eid in c.evidence_ids}
        assert set(claim.evidence_ids) == expected
    # Refs derive from the trio's per-claim mapping, never the whole freeze.
    assert set(_crefs2(synth.claims)) == set(_crefs(stock.claims)) | set(_crefs(bull.claims)) | set(_crefs(bear.claims))

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
            # Deterministic cut (the fake makes a fixed call sequence per scout):
            # two scouts completed, the third still running when the crash lands.
            if calls["n"] > 20:
                raise KeyboardInterrupt("simulated crash mid-source-fetch")
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
    assert len([j for j in scout_before if j.status == "completed"]) == 2
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
    for required in ("trace.opened", "tool.completed", "evidence.ingested", "discovery.ingested",
                     "job.created", "job.completed", "model.completed"):
        assert required in types
    seqs = [e.seq for e in evs]
    assert len(set(seqs)) == len(seqs) and seqs == sorted(seqs)
    tools = [e for e in evs if e.event_type == "tool.completed"]
    assert tools and all(isinstance(e.payload.get("tool"), str) and e.payload.get("tool") for e in tools)
    # Every completion carries its args plus exactly one kind marker: the evidence id
    # (raw document opened) or record_kind=discovery (navigation artifact, never citable).
    assert all("args" in e.payload for e in tools)
    assert all(("evidence_id" in e.payload) != (e.payload.get("record_kind") == "discovery") for e in tools)
    assert any("evidence_id" in e.payload for e in tools)
    assert any(e.payload.get("record_kind") == "discovery" for e in tools)
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
    """Observed-fact item with raw document provenance (accession + document + passage)."""
    return {"evidence_id": eid, "wave_id": wave, "content": "c-" + eid, "claim_text": "c-" + eid,
            "subject": "NVDA", "source_name": "SEC", "source_uri": "https://sec.gov/x",
            "source_record_id": "0000320193-25-000079", "document_name": "nvda-20250331.htm",
            "matching_passage": "Accelerated computing demand grew in the reporting period.",
            "known_at": "2025-06-29T00:00:00+00:00"}

def _committee_analysis(eid: str, follow_ups: Sequence[object] = (), claim_text: str = "finding",
                        claim_type: str = "inference") -> dict[str, object]:
    """Rich committee envelope mapping for service.record_committee_analysis (all required keys)."""
    return {
        "executive_view": f"{claim_text} read over the freeze.",
        "claims": [{"text": claim_text, "claim_type": claim_type, "evidence_ids": [eid]}],
        "impact_channels": [{"text": "exposure channel", "direction": "pressure", "evidence_ids": [eid]}],
        "materiality": {"overall": "medium", "reasoning": "read off the freeze"},
        "uncertainties": ["open terms"],
        "what_would_change": ["a new filing disclosing the terms"],
        "follow_ups": list(follow_ups),
    }


def _svc_ana(eid: str) -> dict[str, object]:
    return _committee_analysis(eid)

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
    # §3: explicit per-job kernel exhaustion passes through verbatim (no budget_exhausted collapse).
    assert "tool_budget exhausted" in str(second.get("error", "")), second
    assert second.get("error_type") != "budget_exhausted", second
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


def test_job_runtime_identity_merges_partial_then_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """attach_job_runtime accumulates accepted keys in diagnostics across partial calls."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    _sid, src = _svc_sid(repo)
    partial = _svc.attach_job_runtime(
        src, {"runtime": "omp", "runtime_agent_id": "agent-1", "not_accepted": "x"}, repo=repo)
    assert partial["diagnostics"] == {"runtime": "omp", "runtime_agent_id": "agent-1"}
    done = _svc.attach_job_runtime(src, {
        "runtime_agent_id": "", "runtime_parent_agent_id": "agent-0",
        "runtime_task_call_id": "call-7", "runtime_agent_type": "scout",
        "runtime_session_file": "/tmp/session.jsonl"}, repo=repo)
    assert done["diagnostics"] == {
        "runtime": "omp", "runtime_agent_id": "agent-1", "runtime_parent_agent_id": "agent-0",
        "runtime_task_call_id": "call-7", "runtime_agent_type": "scout",
        "runtime_session_file": "/tmp/session.jsonl"}
    assert ResearchRepository().get_job(src).diagnostics == done["diagnostics"]
    with pytest.raises(_svc.ResearchNotFound, match="unknown job_id"):
        _svc.attach_job_runtime("nope", {"runtime": "omp"}, repo=repo)


def test_job_fail_cancel_transitions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fail_job marks failed with category/message; cancel_job marks cancelled; terminal is a no-op."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    _, src = _svc_sid(repo)
    with pytest.raises(ValueError):
        _svc.fail_job(src, "bogus-category", "msg", repo=repo)
    assert repo.get_job(src).status == "running"
    failed = _svc.fail_job(src, "timeout", "deadline hit", repo=repo)
    assert failed["status"] == "failed"
    failure = failed["failure"]
    assert isinstance(failure, dict)
    assert failure["category"] == "timeout"
    assert failure["message"] == "deadline hit"
    assert repo.get_job(src).status == "failed"
    assert _svc.cancel_job(src, repo=repo)["status"] == "failed"
    assert _svc.fail_job(src, "timeout", "again", repo=repo)["status"] == "failed"
    _, src2 = _svc_sid(repo)
    cancelled = _svc.cancel_job(src2, repo=repo)
    assert cancelled["status"] == "cancelled"
    assert repo.get_job(src2).status == "cancelled"
    assert _svc.fail_job(src2, "timeout", "late", repo=repo)["status"] == "cancelled"
    with pytest.raises(_svc.ResearchNotFound, match="unknown job_id"):
        _svc.fail_job("nope", "timeout", "msg", repo=repo)
    with pytest.raises(_svc.ResearchNotFound, match="unknown job_id"):
        _svc.cancel_job("nope", repo=repo)

def test_start_job_owner_override_records_omp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing budget.owner seam records OMP-created jobs as owner=omp; default stays pi."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    assert repo.get_job(src).owner == "pi"
    created = _svc.start_job(sid, "source_agent", budget={"owner": "omp"}, repo=repo)
    assert created["owner"] == "omp"
    assert ResearchRepository().get_job(str(created["job_id"])).owner == "omp"


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


def test_scout_material_events_args_always_carry_since() -> None:
    """The harness emits schema-valid role calls: `since` is required, bounded or not.

    Live defect: unbounded sessions (as_of "unbounded"/blank) built get_material_events
    with no `since`, so the tool layer answered invalid_tool_arguments and the filings
    scout silently lost its material-events coverage. The unbounded window floors at the
    documented 2024-01-01 because there is no cutoff to measure a lookback back from.
    """
    from app.research.agents.scout import ScoutAssignment, run_scout
    from app.tools import _validate_tool_arguments

    calls: list[tuple[str, dict[str, object]]] = []

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name == "browse_tools":
            return {"matches": []}
        inner = args.get("name")
        inner_args = args.get("arguments")
        assert isinstance(inner, str) and isinstance(inner_args, dict)
        calls.append((inner, inner_args))
        return {"evidence_ids": []}

    def _role_calls(as_of: str) -> list[tuple[str, dict[str, object]]]:
        calls.clear()
        run_scout(ScoutAssignment(assignment_id="scout-filings", session_id="rs:t", as_of=as_of,
                                  role="filings", question="What changed at NVDA?", tickers=["NVDA"]),
                  dispatch=_dispatch, model=lambda prompt: "[]")
        # Every emitted role call passes the real schema check, not a hand-written shape.
        for tool, args in calls:
            assert _validate_tool_arguments(tool, args) is None, (as_of, tool, args)
        return list(calls)

    for no_cutoff in ("unbounded", "", "   "):
        events = [args for tool, args in _role_calls(no_cutoff) if tool == "get_material_events"]
        assert events and [args["since"] for args in events] == ["2024-01-01"], no_cutoff
        # No cutoff: nothing is date-filtered on the way to the tool.
        assert all("as_of" not in args for args in events)
        searches = [args for tool, args in calls if tool == "search_sec_filings"]
        assert searches and all("as_of" not in args for args in searches)

    events = [args for tool, args in _role_calls("2026-08-10") if tool == "get_material_events"]
    assert [args["since"] for args in events] == ["2025-08-10"]
    assert [args["as_of"] for args in events] == ["2026-08-10"]
    searches = [args for tool, args in calls if tool == "search_sec_filings"]
    assert searches and all(args["as_of"] == "2026-08-10" for args in searches)


def test_pit_eligibility_short_circuits_only_without_a_cutoff() -> None:
    """No bounded as_of means no PIT filtering; a bounded as_of keeps the strict rule."""
    from app.research.agents.scout import _is_pit_eligible

    dated = "2011-03-16T16:33:51+00:00"
    for no_cutoff in ("unbounded", "", "   ", None):
        # The sentinel reaches only the scout (the ledger gate sees None), so a non-ISO
        # as_of must mean "no cutoff" instead of raising inside the PIT gate.
        assert _is_pit_eligible(dated, no_cutoff) is True, no_cutoff
        assert _is_pit_eligible(None, no_cutoff) is True, no_cutoff
    assert _is_pit_eligible(dated, "2025-06-30") is True
    assert _is_pit_eligible("2025-07-01T00:00:00+00:00", "2025-06-30") is False
    assert _is_pit_eligible(None, "2025-06-30") is False
    assert _is_pit_eligible("", "2025-06-30") is False
    assert _is_pit_eligible(7, "2025-06-30") is False
    # A bounded cutoff that cannot be parsed is never verifiable: fail closed, never admit.
    assert _is_pit_eligible(dated, "2025-06-30T25:00:00+00:00") is False


def test_scout_unbounded_citations_reach_the_model() -> None:
    """An unbounded scout keeps its dated citations: the model gets citable ids.

    Live defect: 64/64 unbounded citations were rejected as PIT-ineligible, so the
    prompt listed no ids and every claim was dropped as uncited.
    """
    from app.research.agents.scout import ScoutAssignment, ScoutResult, run_scout

    prompts: list[str] = []
    journal: list[str] = []

    def _dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
        if name == "browse_tools":
            return {"matches": []}
        return {"evidence_ids": [{"evidence_id": "EV-1", "known_at": "2011-03-16T16:33:51+00:00",
                                  "claim_text": "Data center revenue grew"}],
                "top_hits": []}

    def _model(prompt: str) -> str:
        prompts.append(prompt)
        return "[]"

    def _journal(kind: str, payload: dict[str, object]) -> None:
        journal.append(kind)

    def _scout(no_cutoff: str) -> ScoutResult:
        prompts.clear()
        journal.clear()
        return run_scout(
            ScoutAssignment(assignment_id="scout-filings", session_id="rs:t", as_of=no_cutoff,
                            role="filings", question="What changed at NVDA?", tickers=["NVDA"]),
            dispatch=_dispatch, model=_model, journal=_journal)

    for no_cutoff in ("unbounded", ""):
        result = _scout(no_cutoff)
        assert "evidence.rejected" not in journal, no_cutoff
        assert "EV-1 (known_at=2011-03-16T16:33:51+00:00) :: Data center revenue grew" in prompts[0]
        assert result.limitations == [], no_cutoff
        assert result.unknowns == [], no_cutoff


def test_scout_prompt_renders_context_baseline_and_queries() -> None:
    """Scout prompt: context lines + relationships, latest-filing baseline, assigned queries."""
    from app.research.agents.scout import ScoutAssignment, build_scout_prompt

    assignment = ScoutAssignment(
        assignment_id="scout-filings", session_id="rs:t", as_of="2026-08-10", role="filings",
        question="NVDA data center growth?", tickers=["NVDA"],
        context={"primary_entities": ["NVDA"], "concepts": ["accelerated computing", "  "],
                 "relationships": [{"subject": "NVDA", "relation": "supplies", "object": "hyperscalers"},
                                   {"subject": "NVDA", "relation": "  ", "object": "dropped"},
                                   "not-a-triple"]},
        baseline=["NVDA 10-Q data center segment", "NVDA 10-K competition"],
        queries=["NVDA data center revenue"])
    prompt = build_scout_prompt(assignment)
    assert "Context primary_entities: NVDA" in prompt
    assert "Context concepts: accelerated computing" in prompt
    assert "Context relationships: NVDA supplies hyperscalers" in prompt
    assert "dropped" not in prompt and "not-a-triple" not in prompt
    assert "Latest-filing baseline (as_of-filtered, target searches with its terms):" in prompt
    assert "- NVDA 10-K competition" in prompt
    assert "Assigned queries (search each; skip exact repeats already executed):" in prompt
    assert "- NVDA data center revenue" in prompt

    bare = build_scout_prompt(ScoutAssignment(assignment_id="scout-risk", session_id="rs:t",
                                               as_of="unbounded", role="risk", question="Q?",
                                               tickers=[]))
    assert "Context " not in bare and "Latest-filing baseline" not in bare
    assert "Assigned queries" not in bare and "NO ticker and NO cik" not in bare
    assert "scope tickers TBD" in bare and "As of: unbounded" in bare
    assert "Workflow:" in bare and "Respond with JSON only" in bare


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
    from app.research.agents.source_agent import (
        build_query_families,
        build_research_context,
    )
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


def test_private_counterparty_queries_reach_filings_scout() -> None:
    """MSFT/OpenAI: the private counterparty is searched unscoped, never dead-ended."""
    from app.research.agents.source_agent import (
        _grouped_queries,
        build_research_context,
        decompose_question,
    )
    question = "What happens to Microsoft if OpenAI goes bankrupt?"
    groups = _grouped_queries(build_research_context(question, ["MSFT"]))
    assert {"OpenAI", "OpenAI exposure", "OpenAI agreement", "OpenAI 8-K"} <= set(groups["cp"])

    def _catalog_only(name: str, args: dict[str, object]) -> dict[str, object]:
        return {}  # discovery hint only; no query execution on this path

    assignments = decompose_question(question, session_id="rs:t", as_of="2026-08-10",
                                     tickers=["MSFT"], dispatch=_catalog_only)
    filings = next(a for a in assignments if a.role == "filings")
    assert "OpenAI" in filings.queries  # bare name -> unscoped EDGAR full-text search
    # The counterparty family is wired into the filings role and nowhere else.
    for other in (a for a in assignments if a.role != "filings"):
        assert set(groups["cp"]).isdisjoint(other.queries)


def test_no_counterparty_question_keeps_existing_families() -> None:
    """NVDA-only question: counterparty family stays empty, every other family unchanged."""
    from app.research.agents.source_agent import (
        _grouped_queries,
        build_research_context,
    )
    ctx = build_research_context("NVDA data center revenue growth", ["NVDA"])
    assert ctx["related_entities"] == []
    assert _grouped_queries(ctx) == {
        "cp": [],
        "a": ["NVDA"],
        "b": [],
        "c": [],
        "d": [],
        "e": [],
        "f": ["NVDA risk factors"],
        "related": [],
        "supp_bare": ["data", "center", "revenue", "growth"],
        "supp_filing": ["data 8-K", "data proxy", "data N-PX", "data agreement",
                        "center 8-K", "center proxy", "center N-PX", "center agreement",
                        "revenue 8-K", "revenue proxy", "revenue N-PX", "revenue agreement",
                        "growth 8-K", "growth proxy", "growth N-PX", "growth agreement"],
        "supp_risk": ["data risk factor", "center risk factor", "revenue risk factor",
                      "growth risk factor"],
    }


def test_counterparty_family_caps_entities_and_variants() -> None:
    """Counterparty family: six entities max, four variants each, scope tickers excluded."""
    from app.research.agents.source_agent import _grouped_queries
    ctx: dict[str, object] = {"primary_entities": ["MSFT"],
                              "related_entities": ["MSFT", *(f"Counter {i}" for i in range(8))]}
    cp = _grouped_queries(ctx)["cp"]
    assert len(cp) == 24  # 6 entities x (bare + exposure/agreement/8-K)
    assert "MSFT" not in cp and "Counter 6" not in cp


def test_scout_prompt_demands_global_search_for_non_filers() -> None:
    """Scout prompt: counterparty queries are listed as global - no ticker, no cik, no scope shortcut."""
    from app.research.agents.scout import ScoutAssignment, build_scout_prompt
    assignment = ScoutAssignment(assignment_id="scout-filings", session_id="rs:t", as_of="2026-08-10",
                                 role="filings", question="What happens to Microsoft if OpenAI goes bankrupt?",
                                 tickers=["MSFT"], queries=["MSFT OpenAI"],
                                 unscoped_queries=["OpenAI", "OpenAI exposure"])
    prompt = build_scout_prompt(assignment)
    assert "NO ticker and NO cik" in prompt
    assert "- OpenAI exposure" in prompt
    assert "never conclude a relationship is absent from a scoped search" in prompt
    # The branch must be actionable: open the hits, don't just search them.
    assert "open the top two hits with get_sec_document" in prompt
    # ...and it leads the prompt, before the scoped query list.
    assert prompt.index("Counterparty branch first") < prompt.index("Assigned queries")
    # Without counterparty queries the block is absent: no instruction drift for ordinary questions.
    scoped_only = build_scout_prompt(ScoutAssignment(
        assignment_id="scout-filings", session_id="rs:t", as_of="2026-08-10", role="filings",
        question="NVDA data center revenue growth", tickers=["NVDA"], queries=["NVDA"]))
    assert "NO ticker and NO cik" not in scoped_only


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
    import dataclasses as _dc

    from app.sec.models import DocumentMatch, MatchingPassage, SearchRun
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
    import inspect as _inspect

    from app.research.runner import _LiveRun
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
    """sufficient now needs the full structured envelope + no residual branches/questions."""
    return {"useful_for_question": useful, "resolved": ["GS direct OpenAI exposure"],
            "partially_resolved": [], "unresolved": [], "source_limitations": [],
            "major_entities_investigated": ["GS", "OpenAI"],
            "relationship_types_checked": ["investment", "commercial"],
            "forms_examined": ["10-K"], "exhibits_examined": ["EX-10.1"],
            "material_open_questions": [], "search_runs": ["s1"],
            "covered_branches": ["GS direct OpenAI exposure"]}

def _absence_cov(**over: object) -> dict[str, object]:
    """Full absence-observation coverage envelope (forms/dates/partitions/entities/docs/gaps + flags)."""
    cov: dict[str, object] = {"forms": ["10-K"], "dates": ["2025-02-14"], "partitions": ["efts"],
                              "entities": ["GS"], "docs": ["0000886982-26-000001"], "gaps": [],
                              "pagination_complete": True, "complete": True}
    cov.update(over)
    return cov


def _reg_item(eid: str, wave: int = 1, **over: object) -> dict[str, object]:
    base: dict[str, object] = {"evidence_id": eid, "wave_id": wave, "content": "c-" + eid,
                               "claim_text": f"GS OpenAI-linked exposure per filing {eid}",
                               "subject": "GS", "source_name": "SEC",
                               "source_uri": "https://www.sec.gov/Archives/edgar/data/886982/000088698226000001/primary.htm",
                               "source_record_id": "0000886982-26-000001",
                               "document_name": "gs-10q-20260331.htm",
                               "matching_passage": "The firm discloses OpenAI-linked exposure in the filing.",
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


# --- dispatch-boundary loop gate: exact no-progress repeats carry research_loop_detected ---
def test_dispatch_loop_gate_first_allowed_repeat_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First dispatch runs; the identical immediate repeat is refused and journaled, never re-executed."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    args = {"query": "GS OpenAI exposure", "ticker": "GS", "forms": ["10-K"]}
    first = _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    assert first["tool_calls_used"] == 1
    with pytest.raises(ValueError, match="research_loop_detected"):
        _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    blocked = [e for e in repo.list_events(sid) if e.event_type == "research_loop_detected"]
    assert len(blocked) == 1
    assert blocked[0].payload["reason"] == "research_loop_detected"
    assert blocked[0].payload["tool"] == "search_sec_filings"
    assert blocked[0].payload["query"] == "GS OpenAI exposure"
    assert blocked[0].payload["job_id"] == src


def test_dispatch_loop_gate_new_evidence_readmits_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """New evidence is progress: the same action runs again, then its own zero-progress repeat is refused."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    args = {"query": "GS OpenAI exposure"}
    _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    _svc.record_evidence(sid, src, _reg_item(f"{sid}:ev:1"), repo=repo)
    again = _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    assert again["tool_calls_used"] == 2
    with pytest.raises(ValueError, match="research_loop_detected"):
        _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)


def test_dispatch_loop_gate_survives_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tracked actions are persisted: a fresh repository still refuses the no-progress repeat."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    args = {"query": "GS OpenAI exposure"}
    _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    fresh = ResearchRepository()
    with pytest.raises(ValueError, match="research_loop_detected"):
        _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=fresh)


def test_dispatch_loop_gate_distinct_arguments_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One tool with different arguments (documents, queries, forms) is a different action every time."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    calls = [
        ("get_sec_document", {"accession_no": "0000320193-25-000079", "document_name": "nvda-20250331.htm"}),
        ("get_sec_document", {"accession_no": "0000320193-25-000079", "document_name": "nvda-20250331ex10.htm"}),
        ("search_sec_filings", {"query": "GS OpenAI exposure"}),
        ("search_sec_filings", {"query": "GS OpenAI exposure", "forms": ["10-Q"]}),
    ]
    for tool, args in calls:
        _svc.authorize_and_consume_dispatch(sid, src, tool, arguments=args, repo=repo)
    assert repo.get_session(sid).budget.get("tool_calls_used") == len(calls)
    assert [e for e in repo.list_events(sid) if e.event_type == "research_loop_detected"] == []


def test_dispatch_loop_gate_state_is_per_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracking is per session+job: another job in the same session may run the same action."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    args = {"query": "GS OpenAI exposure"}
    _svc.authorize_and_consume_dispatch(sid, src, "search_sec_filings", arguments=args, repo=repo)
    scout = str(_svc.start_job(sid, "scout", repo=repo, wave_id=1)["job_id"])
    assert _svc.authorize_and_consume_dispatch(sid, scout, "search_sec_filings", arguments=args, repo=repo)["job_id"] == scout


def test_dispatch_loop_gate_gateway_repeat_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The model path passes arguments through the gateway: the repeat is refused before the tool runs."""
    import app.pi_gateway as _gw
    from app.pi_gateway import PiSessionContext, execute_pi_tool
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, jid = _reg_svc_sid(repo)
    ctx = PiSessionContext(session_id="t-loop")
    ctx.active_research_session_id = sid
    ctx.active_research_job_id = jid
    calls: list[tuple[str, dict[str, object]]] = []
    def _fake_execute(name: str, arguments: dict[str, object], model: str, context: object = None) -> dict[str, object]:
        calls.append((name, dict(arguments)))
        return {"result_type": "sec_search", "query": arguments.get("query"), "count": 1, "results": []}
    monkeypatch.setattr(_gw, "execute_tool", _fake_execute)
    first = execute_pi_tool("search_sec_filings", {"query": "GS OpenAI exposure"}, ctx)
    assert "error" not in first, first
    second = execute_pi_tool("search_sec_filings", {"query": "GS OpenAI exposure"}, ctx)
    assert "research_loop_detected" in str(second.get("error", "")), second
    assert len(calls) == 1  # the repeat never reached the tool
    third = execute_pi_tool("search_sec_filings", {"query": "GS 10-Q risk factors"}, ctx)
    assert "error" not in third, third
    assert len(calls) == 2


def test_dispatch_loop_gate_no_staged_job_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged session with no active job keeps today's refusal verbatim; the loop gate never sees it."""
    from app.pi_gateway import PiSessionContext, execute_pi_tool
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, _src = _reg_svc_sid(repo)
    ctx = PiSessionContext(session_id="t-nojob")
    ctx.active_research_session_id = sid
    out = execute_pi_tool("search_sec_filings", {"query": "GS OpenAI exposure"}, ctx)
    assert out.get("error_type") == "invalid_research_context"
    assert "Active research job is required" in str(out.get("error"))
    assert repo.get_session(sid).budget.get("tool_calls_used", 0) == 0
    assert [e for e in repo.list_events(sid) if e.event_type == "research_loop_detected"] == []


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
    """The closed vocabularies carry the claim/provenance contract; default coverage keeps its keys."""
    from app.research.dossiers.sec import default_coverage
    from app.research.evidence import CLAIM_KINDS, PROVENANCE_KINDS
    assert CLAIM_KINDS == ("observed_fact", "absence_observation")
    assert PROVENANCE_KINDS == ("sec_source", "search_run", "none")
    cov = default_coverage()
    for key in ("forms", "sources_examined", "complete", "exclusions"):
        assert key in cov, sorted(cov.keys())


def test_reg_negatives_scoped_no_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scoped no-hit observation records as a search-run absence with its full coverage envelope."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    scoped_cov = _absence_cov(complete=False, gaps=["sections 4-6 unread"], pagination_complete=True)
    out = _svc.record_evidence(sid, src, {"evidence_id": f"{sid}:ev:n1", "wave_id": 1,
                                          "content": "no OpenAI bankruptcy exposure disclosed in sections 1-3 of the scoped GS 10-K",
                                          "claim_text": "not found in sections 1-3 of the scoped GS 10-K: OpenAI bankruptcy exposure",
                                          "claim_kind": "absence_observation",
                                          "subject": "GS", "source_name": "SEC",
                                          "known_at": "2025-06-29T00:00:00+00:00",
                                          "search_id": "s1", "query": "GS OpenAI bankruptcy",
                                          "coverage": scoped_cov}, repo=repo)
    assert out["evidence_id"] == f"{sid}:ev:n1"
    assert out.get("claim_kind") == "absence_observation"
    prov = out.get("provenance")
    assert isinstance(prov, dict)
    assert prov.get("kind") == "search_run"
    assert prov.get("search_id") == "s1" and prov.get("query") == "GS OpenAI bankruptcy"
    stored = repo.list_evidence(sid)[0]
    meta = stored.get("metadata")
    assert isinstance(meta, dict) and meta.get("search_coverage") == scoped_cov
    with pytest.raises(ValueError, match="ERR_COVERAGE_REQUIRED"):
        _svc.record_evidence(sid, src, {"evidence_id": f"{sid}:ev:n2", "wave_id": 1,
                                        "content": "no exposure anywhere",
                                        "claim_text": "no OpenAI exposure in any filing",
                                        "claim_kind": "absence_observation",
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
        _frozen_write(frozen_obj, "evidence_ids", ("tampered",))
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
                                       _committee_analysis(eid), repo=repo)
    out = _svc.decide_wave2(sid, repo=repo)
    # §3: director gate set has no budget_exhausted (deleted); runtime_exceeded stays.
    assert out["stop_reason"] in ("no_questions", "low_gain", "not_actionable", "continue",
                                  "max_waves", "jobs_exceeded", "runtime_exceeded")

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
                                       _committee_analysis(eid, claim_text="GS discloses OpenAI-linked exposure"), repo=repo)
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
    """A declared inference stays labeled inference through synthesis: a citation never upgrades it."""
    from app.research.agents import GroundedClaim
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    from app.research.synthesis.committee import compute_disagreement
    from app.research.synthesis.final import synthesize_final

    claim = GroundedClaim(text="OpenAI stress may widen GS spreads", evidence_ids=["EV-1"])
    assert claim.claim_type == "inference"
    stock = StockbotAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"],
                             as_of="x", question="Q?", answer="balanced", base_case="balanced", claims=[claim])
    bull = BullAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"], as_of="x",
                        question="Q?", stance="bullish", bull_case="resilient", claims=[claim])
    bear = BearAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"], as_of="x",
                        question="Q?", stance="bearish", bear_case="contagion", claims=[claim])
    synth = synthesize_final("Q?", session_id="rs:t", wave_id=1, freeze_id="F1", as_of="x",
                             stock=stock, bull=bull, bear=bear,
                             disagreement=compute_disagreement(stock, bull, bear))
    raw_claims = synth.to_dict()["claims"]
    assert isinstance(raw_claims, list)
    rows = [r for r in raw_claims if isinstance(r, dict) and r.get("text") == claim.text]
    assert rows and all(r.get("claim_type") == "inference" for r in rows)
    assert "(inference)" in synth.answer and "observed_fact" not in synth.answer


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
    from app.research.evidence import (
        RECORD_KINDS,
        DiscoveryRecord,
        EvidenceRecord,
        discovery_only,
        substantive_records,
    )
    assert RECORD_KINDS == frozenset({"discovery", "evidence"})
    assert DiscoveryRecord(record_id="d1", session_id="s", tool="search_sec_filings", query="GS 10-K").search_id is None
    assert EvidenceRecord(record_id="e1", session_id="s", evidence_id="EV-1", claim_text="c").evidence_id == "EV-1"
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _reg_svc_sid(repo)
    # A scoping search records as a discovery row: search-run provenance, never citable.
    out = _svc.record_evidence(sid, src, {"evidence_id": f"{sid}:ev:d1", "wave_id": 1,
                                          "content": "scoping search over GS filings",
                                          "claim_text": "scoping search", "claim_kind": "absence_observation",
                                          "subject": "GS", "source_name": "SEC",
                                          "known_at": "2025-06-29T00:00:00+00:00",
                                          "search_id": "s1", "query": "GS 10-K",
                                          "coverage": _absence_cov(), "record_kind": "discovery"}, repo=repo)
    assert out.get("record_kind") == "discovery"
    assert out.get("claim_kind") == "absence_observation"
    assert out.get("provenance") == {"kind": "search_run", "search_id": "s1", "query": "GS 10-K"}
    assert discovery_only([{"record_kind": "discovery"}]) is True
    assert substantive_records([{"record_kind": "discovery"}, {"record_kind": "evidence"}]) == [{"record_kind": "evidence"}]


def test_reg_evidence_claim_labels() -> None:
    """Claim type is declared, never inferred from wording: absent type stays inference, and
    observed_fact/contradicted need at least one freeze id (unknown may cite none)."""
    from app.research.agents import (
        CLAIM_TYPES,
        GroundedClaim,
        ModelOutputFailure,
        parse_grounded_claims,
    )
    assert tuple(CLAIM_TYPES) == ("observed_fact", "inference", "unknown", "contradicted")
    assert GroundedClaim(text="GS revenue grew", evidence_ids=["EV-1"]).claim_type == "inference"
    declared = parse_grounded_claims(
        '[{"text": "GS revenue grew", "claim_type": "observed_fact", "evidence_ids": ["EV-1"]}]', frozen=["EV-1"])
    assert declared[0].claim_type == "observed_fact"
    # Wording and citation never type a claim: an undeclared confident sentence stays inference.
    undeclared = parse_grounded_claims(
        '[{"text": "GS revenue grew per the filing, definitely observed.", "evidence_ids": ["EV-1"]}]', frozen=["EV-1"])
    assert undeclared[0].claim_type == "inference"
    with pytest.raises(ModelOutputFailure):
        parse_grounded_claims('[{"text": "GS revenue grew", "claim_type": "observed_fact", "evidence_ids": []}]',
                              frozen=["EV-1"])
    with pytest.raises(ModelOutputFailure):
        parse_grounded_claims('[{"text": "unclear", "claim_type": "shrug", "evidence_ids": ["EV-1"]}]', frozen=["EV-1"])
    unknown = parse_grounded_claims('[{"text": "OpenAI terms UNKNOWN", "claim_type": "unknown", "evidence_ids": []}]',
                                    frozen=["EV-1"])
    assert unknown[0].claim_type == "unknown" and unknown[0].evidence_ids == []


def test_crap_source_policy_modes_and_errors() -> None:
    from app.research.models import resolve_source_policy, source_domain_allowed
    # Bare research_sources mapping (no outer key) resolves identically.
    bare = resolve_source_policy({"mode": "all", "sources": []})
    assert bare["mode"] == "all" and bare["allowed"] == []
    # Mode "all" admits any domain not denied; allowlist pins SEC.
    assert source_domain_allowed(bare, "anything.example") is True
    assert source_domain_allowed(None, None) is True
    sec = resolve_source_policy({"research_sources": {"mode": "allowlist", "sources": [" SEC "]}})
    assert sec["allowed"] == ["SEC"]
    assert source_domain_allowed(sec, "SEC") is True
    assert source_domain_allowed(sec, "web") is False
    denied = {"allowed": ["SEC"], "denied": ["SEC"], "mode": "allowlist"}
    assert source_domain_allowed(denied, "SEC") is False
    with pytest.raises(ValueError, match="policy"):
        resolve_source_policy("nope")
    with pytest.raises(ValueError, match="research_sources"):
        resolve_source_policy({"research_sources": "nope"})
    with pytest.raises(ValueError, match="sources"):
        resolve_source_policy({"research_sources": {"mode": "allowlist", "sources": []}})
    with pytest.raises(ValueError, match="sources"):
        resolve_source_policy({"research_sources": {"mode": "all", "sources": [""]}})
    with pytest.raises(ValueError, match="source_domain"):
        source_domain_allowed(sec, "  ")


def test_crap_temporal_patterns() -> None:
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.research.models import resolve_temporal_scope
    now = _dt(2025, 6, 30, tzinfo=_tz.utc)
    assert resolve_temporal_scope(temporal="show all history please", now=now)["mode"] == "unbounded"
    assert resolve_temporal_scope(temporal="as of 2024-03-31", now=now)["as_of"] == "2024-03-31"
    last2 = resolve_temporal_scope(temporal="last 2 years", now=now)
    assert last2["mode"] == "range" and str(last2["start"])[:4] == "2023"
    lastyr = resolve_temporal_scope(temporal="last year", now=now)
    assert (lastyr["start"], lastyr["end"]) == ("2024-01-01", "2024-12-31")
    assert resolve_temporal_scope(temporal="before earnings", now=now)["mode"] == "as_of"
    assert resolve_temporal_scope(temporal="latest available data", now=now)["mode"] == "latest-available"
    assert resolve_temporal_scope(temporal="most recent filing", now=now)["mode"] == "as_of"
    with pytest.raises(ValueError, match="after end"):
        resolve_temporal_scope(temporal="between 2025-06-30 and 2025-01-01", now=now)
    with pytest.raises(ValueError, match="temporal"):
        resolve_temporal_scope(temporal="  ", now=now)
    with pytest.raises(ValueError, match="invalid calendar"):
        resolve_temporal_scope(temporal="as of 2025-02-30", now=now)


def test_crap_baseline_current_and_quarterly_picks() -> None:
    from app.research.models import select_latest_baseline
    filings: list[object] = [
        {"form": "10-Q", "known_at": "2025-03-31", "accession_no": "Q1"},
        {"form": "10-Q", "known_at": "2025-06-20", "accession_no": "Q2"},
        {"form": "8-K", "known_at": "2025-06-10", "accession_no": "K1"},
        {"form": "8-K", "known_at": "2025-06-25", "accession_no": "K2"},
        {"form": "10-K", "known_at": "2030-01-01", "accession_no": "FUTURE"},
        {"form": "DEF-14A", "known_at": "2025-01-01", "accession_no": "PROXY"},
        {"form": "10-K"},
    ]
    base = select_latest_baseline(filings, as_of="2025-06-30")
    q = base["quarterly_10q"]
    assert isinstance(q, dict) and q.get("accession_no") == "Q2"
    mats = base["material_8k"]
    assert isinstance(mats, list)
    first = mats[0]
    assert isinstance(first, dict) and first.get("accession_no") == "K2"
    assert all(isinstance(f, dict) and f.get("accession_no") != "FUTURE" for f in mats)


def test_crap_superseded_violation_edges() -> None:
    from app.research.models import superseded_current_violation
    assert superseded_current_violation([{"form": "8-K", "known_at": "2025-01-01"}], "X") is None
    filings: list[object] = [
        {"form": "10-K", "known_at": "2024-02-10", "accession_no": "OLD", "superseded_by": "NEW"},
        {"form": "10-K", "known_at": "2025-02-14", "accession_no": "NEW"},
    ]
    assert superseded_current_violation(filings, [], as_of="2025-06-30") is None
    assert superseded_current_violation(filings, ["NEW", 42, " "], as_of="2025-06-30") is None
    assert superseded_current_violation(filings, 42, as_of="2025-06-30") is None
    hit = superseded_current_violation(filings, " OLD ", as_of="2025-06-30")
    assert hit is not None and "NEW" in hit


def test_crap_discovery_only_edges() -> None:
    from app.research.evidence import (
        DiscoveryRecord,
        discovery_only,
        substantive_records,
    )
    assert discovery_only([]) is False
    assert discovery_only("nope") is False
    assert discovery_only([{"record_kind": "discovery"}, {"record_kind": "evidence"}]) is False
    assert discovery_only([{"metadata": {"record_kind": "discovery"}}]) is True
    assert discovery_only([DiscoveryRecord(record_id="d", session_id="s", tool="t", query="q")]) is False
    assert discovery_only([{"record_kind": "discovery", "metadata": {}}]) is True
    assert substantive_records([{"record_kind": "discovery"}, {}]) == [{}]

# ---------------------------------------------------------------------------
# Evals slice: committee invariants + finalization UX (offline fakes only).
# Same freeze across the trio; 3 distinct jobs created before the run;
# concurrent; completed-job cross-role write rejected; roles cannot mutate the
# freeze; claims resolve to frozen evidence; research_requests stay separate
# from evidence. Finalize success auto-renders a substantive structured answer
# in the same turn; a bare "finalized/N claims" with no answer fails.
# ---------------------------------------------------------------------------

def _eval_sid(repo: ResearchRepository) -> tuple[str, str]:
    from app.research import service as _svc
    sid = _svc.create_research("MSFT OpenAI exposure?", "o", as_of="2026-08-10T00:00:00+00:00", repo=repo)
    return sid, repo.list_jobs(sid)[0].job_id


def _eval_item(eid: str, wave: int = 1) -> dict[str, object]:
    return {"evidence_id": eid, "wave_id": wave, "content": "c-" + eid,
            "claim_text": "MSFT Azure OpenAI-linked exposure per filing " + eid,
            "subject": "MSFT", "source_name": "SEC",
            "source_uri": "https://www.sec.gov/Archives/edgar/data/789790/000095017026001234/primary.htm",
            "source_record_id": "0000950170-26-001234",
            "document_name": "msft-10q-20260630.htm",
            "matching_passage": "Azure OpenAI-linked exposure is disclosed in the filing.",
            "known_at": "2026-08-01T00:00:00+00:00"}


def _eval_cov() -> dict[str, object]:
    return {"useful_for_question": "sufficient", "resolved": ["MSFT OpenAI exposure"],
            "partially_resolved": [], "unresolved": [], "source_limitations": [],
            "major_entities_investigated": ["MSFT", "OpenAI"],
            "relationship_types_checked": ["investment", "commercial"],
            "forms_examined": ["10-K"], "exhibits_examined": ["EX-10.1"],
            "material_open_questions": [], "search_runs": ["s1"],
            "covered_branches": ["MSFT Azure OpenAI-linked exposure"]}


def _eval_trio_ids(repo: ResearchRepository, sid: str, eid: str) -> tuple[str, str, str, str]:
    from app.research import service as _svc
    fid = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    trio = _svc.create_committee_jobs(sid, 1, repo=repo)
    created = trio.get("jobs")
    assert isinstance(created, list) and len(created) == 3 and len(set(created)) == 3
    return fid, str(created[0]), str(created[1]), str(created[2])


def test_eval_committee_same_freeze_and_trio_created_before_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _eval_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _eval_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_eval_cov(), evidence_ids=[eid], repo=repo)
    fid, stock_jid, bull_jid, bear_jid = _eval_trio_ids(repo, sid, eid)
    assert repo.get_session(sid).freeze_ids[-1] == fid
    assert len({stock_jid, bull_jid, bear_jid}) == 3
    for jid, role in ((stock_jid, "stockbot"), (bull_jid, "bullbot"), (bear_jid, "bearbot")):
        job = repo.get_job(jid)
        assert job.job_type == role and job.status == "running"
        _svc.record_committee_analysis(sid, jid, role,
                                       _committee_analysis(eid), repo=repo)
    assert all(repo.get_job(jid).status == "completed" for jid in (stock_jid, bull_jid, bear_jid))
    assert repo.get_session(sid).freeze_ids[-1] == fid


def test_eval_committee_cross_role_write_rejected_and_freeze_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses as _dc
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.research import service as _svc
    from app.research.freeze import EvidenceFreeze
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _eval_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _eval_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_eval_cov(), evidence_ids=[eid], repo=repo)
    fid, stock_jid, _bull_jid, _bear_jid = _eval_trio_ids(repo, sid, eid)
    with pytest.raises(ValueError, match="!="):
        _svc.record_committee_analysis(sid, stock_jid, "bearbot",
                                       _committee_analysis(eid), repo=repo)
    now = _dt(2026, 8, 10, tzinfo=_tz.utc)
    with pytest.raises(_dc.FrozenInstanceError):
        frozen_obj = EvidenceFreeze(freeze_id=fid, session_id=sid, wave_id=1, created_at=now,
                                    as_of=now, evidence_ids=(eid,), content_hash="h")
        _frozen_write(frozen_obj, "evidence_ids", ("tampered",))
    _svc.record_committee_analysis(sid, stock_jid, "stockbot",
                                   _committee_analysis(eid, ["Probe Azure terms?"]),
                                   repo=repo)
    assert repo.get_freeze(fid)["freeze_id"] == fid


def test_eval_committee_claims_resolve_and_requests_not_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _eval_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _eval_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_eval_cov(), evidence_ids=[eid], repo=repo)
    _fid, stock_jid, _bull_jid, _bear_jid = _eval_trio_ids(repo, sid, eid)
    with pytest.raises(ValueError):
        _svc.record_committee_analysis(sid, stock_jid, "stockbot",
                                       _committee_analysis("EV-NOPE"), repo=repo)
    n_evidence = len(repo.list_evidence(sid))
    _svc.record_committee_analysis(sid, stock_jid, "stockbot",
                                   _committee_analysis(eid, ["Probe Azure terms?"]), repo=repo)
    assert len(repo.list_evidence(sid)) == n_evidence


def test_eval_finalize_renders_answer_same_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service as _svc
    from app.research.evals.evaluators import EvalInput, evaluate
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _eval_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _eval_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_eval_cov(), evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role,
                                       _committee_analysis(eid, claim_text="MSFT Azure exposure"), repo=repo)
    _svc.decide_wave2(sid, repo=repo)
    out = _svc.finalize_session(sid, "MSFT Azure exposure is filing-backed.",
                                [{"text": "MSFT Azure exposure", "evidence_ids": [eid]}], repo=repo)
    final = repo.get_session(sid).final_result or {}
    assert isinstance(final, dict) and str(final.get("answer", "")).strip()
    assert "filing-backed" in str(final.get("answer"))
    assert out["freeze_id"] == f"{sid}:1:freeze"
    rendered = _svc.inspect_research(sid, repo=repo)
    assert str(rendered.get("final", "") or final.get("answer", "")).strip()
    bare = EvalInput(scenario_name="msft-openai-bankruptcy-sec-only",
                     answer_text="finalized 1 claims", evidence_ids=(eid,),
                     requires_evidence=True, finalized_claim_count=1, answered=False)
    assert "finalized-without-answer" in evaluate(bare).violations


# ---------------------------------------------------------------------------
# Phase 16 invariants: raw-provenance evidence integrity, scoped absence,
# sufficiency envelope, committee freeze atomicity + role boundary, declared
# claim typing, the per-freeze wave gate (N > 2 never finalizes by itself),
# and finalize rendering (answer only in final_result).
# External behavior only: returned payloads, persisted rows, raised error codes.
# ---------------------------------------------------------------------------

def _inv_sid(repo: ResearchRepository, q: str = "NVDA OpenAI exposure?") -> tuple[str, str]:
    """Fresh session with its running source job: the only record_evidence boundary."""
    from app.research import service as _svc
    sid = _svc.create_research(q, "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    return sid, repo.list_jobs(sid)[0].job_id


def _typed_committee_analysis(eid: str, text: str, claim_type: str) -> dict[str, object]:
    """Rich committee envelope with one explicitly typed claim."""
    env = _committee_analysis(eid, claim_text=text)
    env["claims"] = [{"text": text, "claim_type": claim_type, "evidence_ids": [eid]}]
    return env


def test_invariant_search_hit_is_never_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A search hit is navigation: no raw passage means ERR_RAW_SOURCE_REQUIRED and no persisted row."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    hit = {"evidence_id": f"{sid}:ev:1", "wave_id": 1,
           "content": "NVDA 10-Q hit: accelerated computing demand",
           "claim_text": "search hit", "subject": "NVDA", "source_name": "SEC",
           "search_id": "s1", "query": "NVDA demand", "known_at": "2025-06-29T00:00:00+00:00"}
    with pytest.raises(ValueError, match="ERR_RAW_SOURCE_REQUIRED") as excinfo:
        _svc.record_evidence(sid, src, hit, repo=repo)
    assert "navigation artifact" in str(excinfo.value)
    assert repo.list_evidence(sid) == []


def test_invariant_search_id_never_passes_as_accession(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A search id offered as the filing accession is a provenance mismatch; junk fails the format gate."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    with pytest.raises(ValueError, match="ERR_PROVENANCE_MISMATCH"):
        _svc.record_evidence(sid, src, {**_svc_item(f"{sid}:ev:1"), "source_record_id": "s1", "search_id": "s1"},
                             repo=repo)
    with pytest.raises(ValueError, match="ERR_ACCESSION_FORMAT"):
        _svc.record_evidence(sid, src, {**_svc_item(f"{sid}:ev:2"), "source_record_id": "s-1"}, repo=repo)
    assert repo.list_evidence(sid) == []


def test_invariant_raw_document_is_accepted_and_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Accession + document_name + passage is the accepted fact shape; a bare 18-digit run normalizes."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    out = _svc.record_evidence(sid, src, {**_svc_item(eid), "source_record_id": "000032019325000079"}, repo=repo)
    assert out["evidence_id"] == eid
    assert out.get("claim_kind") == "observed_fact"
    assert out.get("provenance") == {
        "kind": "sec_source", "accession_no": "0000320193-25-000079",
        "document_name": "nvda-20250331.htm",
        "passage": "Accelerated computing demand grew in the reporting period.",
        "source_uri": "https://sec.gov/x",
    }
    assert out.get("record_kind") == "evidence"
    assert out.get("source_record_id") == "0000320193-25-000079"


def test_invariant_dossier_summary_is_never_observed_fact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dossier/model summaries carry no raw passage: declared observed_fact they fail closed."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    summary = {"evidence_id": f"{sid}:ev:1", "wave_id": 1,
               "content": "Dossier summary: NVDA demand is rising.",
               "claim_text": "summary finding", "subject": "NVDA", "source_name": "SEC",
               "known_at": "2025-06-29T00:00:00+00:00"}
    with pytest.raises(ValueError, match="ERR_ACCESSION_FORMAT"):
        _svc.record_evidence(sid, src, summary, repo=repo)
    cited_only = {**summary, "evidence_id": f"{sid}:ev:2", "source_record_id": "0000320193-25-000079",
                  "document_name": "nvda-20250331.htm"}
    with pytest.raises(ValueError, match="ERR_RAW_SOURCE_REQUIRED"):
        _svc.record_evidence(sid, src, cited_only, repo=repo)
    assert repo.list_evidence(sid) == []


def test_invariant_absence_observation_needs_search_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Absence = search run + full coverage + no accession; partial envelopes fail closed."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    base = {"evidence_id": f"{sid}:ev:a1", "wave_id": 1, "content": "no NVDA capacity constraint disclosed",
            "claim_text": "not disclosed in the searched scope", "claim_kind": "absence_observation",
            "subject": "NVDA", "source_name": "SEC", "known_at": "2025-06-29T00:00:00+00:00",
            "search_id": "s1", "query": "NVDA capacity"}
    out = _svc.record_evidence(sid, src, {**base, "coverage": _absence_cov()}, repo=repo)
    assert out.get("claim_kind") == "absence_observation"
    prov = out.get("provenance")
    assert isinstance(prov, dict) and prov.get("kind") == "search_run" and prov.get("search_id") == "s1"
    bad_covs: list[dict[str, object]] = [
        {"forms": ["10-K"]},                      # missing keys
        _absence_cov(pagination_complete="yes"),  # flag is not a bool
        _absence_cov(complete=None),              # flag is not a bool
    ]
    for i, bad_cov in enumerate(bad_covs):
        with pytest.raises(ValueError, match="ERR_COVERAGE_REQUIRED"):
            _svc.record_evidence(sid, src, {**base, "evidence_id": f"{sid}:ev:b{i}", "coverage": bad_cov}, repo=repo)
    with pytest.raises(ValueError, match="ERR_COVERAGE_REQUIRED"):
        _svc.record_evidence(sid, src, {**base, "evidence_id": f"{sid}:ev:q", "query": "", "coverage": _absence_cov()},
                             repo=repo)
    with pytest.raises(ValueError, match="ERR_PROVENANCE_MISMATCH"):
        _svc.record_evidence(sid, src, {**base, "evidence_id": f"{sid}:ev:x",
                                        "source_record_id": "0000320193-25-000079",
                                        "coverage": _absence_cov()}, repo=repo)
    # The same absence wording is not a fact without raw provenance.
    with pytest.raises(ValueError, match="ERR_RAW_SOURCE_REQUIRED"):
        _svc.record_evidence(sid, src, {**base, "evidence_id": f"{sid}:ev:f", "claim_kind": "observed_fact"},
                             repo=repo)
    ids = {str(r.get("evidence_id")) for r in repo.list_evidence(sid)}
    assert ids == {f"{sid}:ev:a1"}  # only the fully scoped absence landed


def test_invariant_claim_kind_never_routed_by_wording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative wording is gated by provenance, not lexis: raw provenance accepts it, none fails closed."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    wording = "No OpenAI exposure exists anywhere in the GS filings and the business is unaffected."
    eid = f"{sid}:ev:1"
    out = _svc.record_evidence(sid, src, {**_svc_item(eid), "claim_text": wording, "content": wording,
                                          "matching_passage": wording}, repo=repo)
    assert out.get("claim_kind") == "observed_fact"
    prov = out.get("provenance")
    assert isinstance(prov, dict) and prov.get("kind") == "sec_source" and prov.get("passage") == wording
    bare = {"evidence_id": f"{sid}:ev:2", "wave_id": 1, "content": wording, "claim_text": wording,
            "subject": "GS", "source_name": "SEC", "known_at": "2025-06-29T00:00:00+00:00"}
    with pytest.raises(ValueError, match="ERR_ACCESSION_FORMAT|ERR_RAW_SOURCE_REQUIRED") as excinfo:
        _svc.record_evidence(sid, src, bare, repo=repo)
    assert "negat" not in str(excinfo.value).lower()  # no lexical negativity gate anywhere


def test_invariant_absence_language_stays_scoped_in_synthesis() -> None:
    """Absence renders as searched-scope language, never as real-world nonexistence."""
    from app.research.agents import GroundedClaim
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    from app.research.synthesis.committee import compute_disagreement
    from app.research.synthesis.final import (
        SEC_SCOPE_ABSENCE,
        scoped_absence,
        synthesize_final,
    )
    from app.tool_render import render_final_result

    raw_claim = "The OpenAI relationship does not exist"
    assert scoped_absence(raw_claim) == f"{SEC_SCOPE_ABSENCE}: {raw_claim}"
    assert scoped_absence("  ").startswith(SEC_SCOPE_ABSENCE)
    claim = GroundedClaim(text=raw_claim, claim_type="unknown", evidence_ids=[])
    stock = StockbotAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"],
                             as_of="x", question="Q?", answer="balanced", base_case="balanced", claims=[claim])
    bull = BullAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"], as_of="x",
                        question="Q?", stance="bullish", bull_case="resilient", claims=[claim])
    bear = BearAnalysis(session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"], as_of="x",
                        question="Q?", stance="bearish", bear_case="contagion", claims=[claim])
    synth = synthesize_final("Q?", session_id="rs:t", wave_id=1, freeze_id="F1", as_of="x",
                             stock=stock, bull=bull, bear=bear,
                             disagreement=compute_disagreement(stock, bull, bear))
    assert all(item.startswith(SEC_SCOPE_ABSENCE) for item in synth.absence_observations)
    assert synth.absence_observations == [f"{SEC_SCOPE_ABSENCE}: {raw_claim}"]
    assert f"- {SEC_SCOPE_ABSENCE}" in synth.answer
    rendered = render_final_result(synth.to_dict())
    assert SEC_SCOPE_ABSENCE in rendered


def test_invariant_sufficient_needs_structured_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sufficient` needs the full structured envelope; the legacy bare-sufficient fallback is gone."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    legacy: dict[str, object] = {"useful_for_question": "sufficient", "resolved": ["NVDA demand"],
                                 "partially_resolved": [], "unresolved": [], "source_limitations": []}
    with pytest.raises(ValueError, match="ERR_COVERAGE_REQUIRED") as excinfo:
        _svc.submit_source_result(src, coverage=legacy, evidence_ids=[eid], repo=repo)
    assert "major_entities_investigated" in str(excinfo.value)  # the missing envelope is named
    assert repo.get_job(src).status == "running"  # a failed gate never completes the job
    assert repo.get_session(sid).status not in ("completed", "synthesizing", "failed")


def test_invariant_sufficient_rejects_residual_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any residual branch/question in a sufficient envelope fails; a clean envelope completes."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    residuals: list[tuple[str, object]] = [("remaining_branches", ["private-lab terms"]),
                                           ("material_open_questions", ["Is the exposure hedged?"]),
                                           ("routes_unsearched", ["8-K exhibits"]),
                                           ("major_entities_missing", ["OpenAI"])]
    for key, value in residuals:
        cov: dict[str, object] = dict(_reg_cov())
        cov[key] = value
        with pytest.raises(ValueError, match="ERR_COVERAGE_INCOMPLETE"):
            _svc.submit_source_result(src, coverage=cov, evidence_ids=[eid], repo=repo)
    with pytest.raises(ValueError, match="ERR_COVERAGE_INCOMPLETE"):
        _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid],
                                  unresolved_questions=["still open?"], repo=repo)
    assert repo.get_job(src).status == "running"
    out = _svc.submit_source_result(src, coverage=_reg_cov(), evidence_ids=[eid], repo=repo)
    assert out["job_status"] == "completed"
    # Unknown search_run ids stay advisory (readable SEC ledger -> warnings, never a failure).
    assert isinstance(out.get("warnings"), list)
    dossiers = repo.list_dossiers(sid)
    assert dossiers
    stored_cov = dossiers[-1].get("coverage")
    assert isinstance(stored_cov, dict)
    assert stored_cov.get("source_domain") == "SEC" and stored_cov.get("source_sufficiency") == "sufficient"


def test_invariant_committee_trio_running_before_roles_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """All three committee jobs exist RUNNING before any role finishes; all share one freeze."""
    import sqlite3

    from app.research.repository import get_research_db_path

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    db = str(get_research_db_path())

    def _committee_statuses() -> dict[str, str]:
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT job_type, status FROM jobs WHERE job_type IN ('stockbot','bullbot','bearbot')").fetchall()
        return {str(job_type): str(status) for job_type, status in rows}

    snapshots: list[dict[str, str]] = []

    def _model(prompt: str) -> str:
        if "Temporary assignment" not in prompt:  # a committee role call, not a scout
            snapshots.append(_committee_statuses())
        return _grounded(prompt)

    out = _run_wave(repo, _model)
    assert out["stop_reason"] == "complete:wave1"
    assert len(snapshots) == 3  # one snapshot per role model call
    assert all(len(snap) == 3 and set(snap.values()) == {"running"} for snap in snapshots)
    sid = out["session_id"]
    assert isinstance(sid, str)
    stock = out["stock"]
    bull = out["bull"]
    bear = out["bear"]
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    assert isinstance(stock, StockbotAnalysis) and isinstance(bull, BullAnalysis) and isinstance(bear, BearAnalysis)
    assert stock.freeze_id == bull.freeze_id == bear.freeze_id == out["freeze_id"]
    stored = {str((repo.get_job(j.job_id).result or {}).get("freeze_id"))
              for j in repo.list_jobs(sid) if j.job_type in ("stockbot", "bullbot", "bearbot")}
    assert stored == {out["freeze_id"]}


def test_invariant_committee_roles_stay_out_of_the_tool_lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every committee role job is freeze-only: SEC/research tools and evidence writes are forbidden."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    trio = _svc.create_committee_jobs(sid, 1, repo=repo)
    created = trio.get("jobs")
    assert isinstance(created, list) and len(created) == 3
    for jid in created:
        for tool in ("search_sec_filings", "get_sec_document", "get_sec_filing",
                     "research_add_evidence", "search_web"):
            with pytest.raises(ValueError, match="forbids"):
                _svc.authorize_and_consume_dispatch(sid, str(jid), tool, repo=repo)
        with pytest.raises(ValueError):
            _svc.record_evidence(sid, str(jid), _svc_item(f"{sid}:ev:2"), repo=repo)


def test_invariant_committee_follow_ups_route_through_director_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Committee requests drive the next-wave decision (never evidence) or stop at the gate."""
    from app.research import service as _svc
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    follow_up = "Do the exhibits cover the Azure terms?"
    for i, role in enumerate(("stockbot", "bullbot", "bearbot")):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role,
                                       _committee_analysis(eid, [follow_up] if i == 0 else []), repo=repo)
    assert len(repo.list_evidence(sid)) == 1  # requests never become evidence
    out = _svc.decide_next_wave(sid, repo=repo)
    assert out["authorized"] is True and out["targeted_question"] == follow_up
    sess = repo.get_session(sid)
    assert sess.current_wave == 2 and sess.status == "targeted_research" and sess.targeted_question == follow_up
    # A request outside the SEC lane routes to a non-authorized gate instead of a wave.
    sid2, src2 = _inv_sid(repo, "GS OpenAI exposure?")
    eid2 = f"{sid2}:ev:1"
    _svc.record_evidence(sid2, src2, {**_svc_item(eid2), "subject": "GS"}, repo=repo)
    _svc.complete_job(src2, {}, repo=repo)
    _svc.freeze_session(sid2, 1, repo=repo)
    off_lane = {"question": "What does the press say about the terms?", "suggested_source": "web"}
    for i, role in enumerate(("stockbot", "bullbot", "bearbot")):
        jid = str(_svc.start_job(sid2, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid2, jid, role,
                                       _committee_analysis(eid2, [off_lane] if i == 0 else []), repo=repo)
    out2 = _svc.decide_next_wave(sid2, repo=repo)
    assert out2["authorized"] is False and out2["stop_reason"] == "not_actionable"
    assert repo.get_session(sid2).status == "synthesizing"
    assert len(repo.list_evidence(sid2)) == 1


def test_invariant_claims_only_envelope_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A claims+follow_ups-only committee envelope fails ERR_COMMITTEE_ENVELOPE_INCOMPLETE everywhere."""
    import json as _json

    from app.research import service as _svc
    from app.research.agents import ModelOutputFailure
    from app.research.agents.stockbot import run_stockbot

    old_shape = _json.dumps({"claims": [{"text": "finding", "evidence_ids": ["EV-1"]}], "follow_ups": []})
    with pytest.raises(ModelOutputFailure, match="ERR_COMMITTEE_ENVELOPE_INCOMPLETE"):
        run_stockbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"], as_of="x",
                     model=lambda prompt: old_shape, evidence_text="[EV-1] a")
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _inv_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    jid = str(_svc.start_job(sid, "stockbot", repo=repo, wave_id=1)["job_id"])
    with pytest.raises(ValueError, match="ERR_COMMITTEE_ENVELOPE_INCOMPLETE"):
        _svc.record_committee_analysis(sid, jid, "stockbot",
                                       {"claims": [{"text": "finding", "evidence_ids": [eid]}],
                                        "follow_ups": []}, repo=repo)
    assert repo.get_job(jid).status == "running"  # the incomplete envelope never closes the job


def test_invariant_claim_type_is_declared_never_promoted() -> None:
    """Absent type stays inference; observed_fact/contradicted need a freeze id; citations never upgrade."""
    from app.research.agents import ModelOutputFailure
    from app.research.agents.stockbot import run_stockbot

    def _run(model: Callable[[str], str]) -> StockbotAnalysis:
        return run_stockbot("Q?", session_id="rs:t", wave_id=1, freeze_id="F1", evidence_ids=["EV-1"],
                            as_of="x", model=model, evidence_text="[EV-1] a")

    undeclared = _run(lambda prompt: _committee_output([{"text": "Confirmed: revenue grew.", "evidence_ids": ["EV-1"]}]))
    assert [c.claim_type for c in undeclared.claims] == ["inference"]
    assert undeclared.claims[0].evidence_ids == ["EV-1"]
    declared = _run(lambda prompt: _committee_output(
        [{"text": "Revenue grew.", "claim_type": "observed_fact", "evidence_ids": ["EV-1"]}]))
    assert [c.claim_type for c in declared.claims] == ["observed_fact"]
    with pytest.raises(ModelOutputFailure, match="uncited"):
        _run(lambda prompt: _committee_output(
            [{"text": "Revenue grew.", "claim_type": "observed_fact", "evidence_ids": []}]))
    with pytest.raises(ModelOutputFailure, match="EV-999"):
        _run(lambda prompt: _committee_output(
            [{"text": "Revenue grew.", "claim_type": "observed_fact", "evidence_ids": ["EV-999"]}]))


def test_invariant_director_waves_continue_past_two() -> None:
    """A material question advances wave N -> N+1 forever on new evidence; counts never finalize."""
    from app.research.agents import ResearchRequest
    from app.research.agents.bearbot import BearAnalysis
    from app.research.agents.bullbot import BullAnalysis
    from app.research.agents.stockbot import StockbotAnalysis
    from app.research.director import (
        DirectorBudgets,
        DirectorDeps,
        Wave1Result,
        decide_next_wave,
    )
    from app.research.synthesis.committee import CommitteeDisagreement

    stops: list[tuple[str, str]] = []

    def _committee(_sid: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        raise AssertionError("this gate test never runs a committee")

    deps = DirectorDeps(create_session=lambda q, a: "rs:x", fetch_wave_evidence=lambda s: [],
                        create_freeze=lambda s: "F1", run_committee=_committee,
                        record_stop=lambda s, r: stops.append((s, r)))

    def _wave(wave: int, question: str) -> Wave1Result:
        request = ResearchRequest(question=question, why_material="unresolved in the last freeze",
                                  requested_source_domain="SEC", expected_gain="medium")
        disagreement = CommitteeDisagreement(session_id="rs:x", wave_id=wave, freeze_id=f"F{wave}",
                                             requested_research=[request])
        return Wave1Result(session_id="rs:x", wave_id=wave, freeze_id=f"F{wave}",
                           evidence_ids=["EV-1"], disagreement=disagreement, question="Is GS exposed?")

    novelty = {"new_raw_documents": 1, "new_evidence_records": 2, "new_relationships": 1,
               "resolved_questions": 1, "zero_novelty_waves": 0, "duplicate_actions_blocked": 0}
    questions = {"What drove the Q2 delta?": 2, "Which exhibits cover the terms?": 3,
                 "How does the hedge work?": 4, "What if the hedge fails?": 5}
    for question, wave in questions.items():
        decision = decide_next_wave(_wave(wave - 1, question), deps=deps, budgets=DirectorBudgets(),
                                    waves_used=wave - 1, novelty=novelty)
        assert decision.authorized is True and decision.stop_reason == "continue"
        assert decision.targeted_question == question and decision.targeted_domain == "SEC"
        assert len(stops) == wave - 1
    # Convergence, not a wave number, is what stops the run.
    zero = dict.fromkeys(("new_raw_documents", "new_evidence_records", "new_relationships",
                          "resolved_questions"), 0)
    zero.update({"zero_novelty_waves": 2, "duplicate_actions_blocked": 0})
    stopped = decide_next_wave(_wave(5, "What else?"), deps=deps, budgets=DirectorBudgets(),
                               waves_used=5, novelty=zero)
    assert stopped.authorized is False and stopped.stop_reason == "no_novelty"


def test_invariant_finalize_card_never_duplicates_the_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The answer lives once in final_result; the finalize card is a short confirmation, not the answer."""
    from app.research import service as _svc
    from app.tool_render import render_tool_result
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _eval_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _eval_item(eid), repo=repo)
    _svc.submit_source_result(src, coverage=_eval_cov(), evidence_ids=[eid], repo=repo)
    _svc.freeze_session(sid, 1, repo=repo)
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role, _committee_analysis(eid), repo=repo)
    _svc.decide_wave2(sid, repo=repo)
    answer = "MSFT Azure exposure is filing-backed and the agreement terms stay undisclosed."
    out = _svc.finalize_session(sid, answer, [{"text": "MSFT Azure exposure", "evidence_ids": [eid]}], repo=repo)
    final = repo.get_session(sid).final_result
    assert isinstance(final, dict)
    assert final.get("answer") == answer and str(final.get("content", "")).strip()
    assert final.get("freeze_id") == f"{sid}:1:freeze"
    card = render_tool_result(out)
    assert "Research finalized" in card
    assert f"{sid}:1:freeze" in card
    assert "Claims:" in card and "impact channels:" in card
    assert answer not in card and "filing-backed" not in card
    assert "Answer delivered separately" in card
    # The answer is stored once (final_result.answer); the card only counts what was frozen.
    assert str(final.get("content", "")).startswith("Bottom line:")



# --- property invariants: accession + typed-provenance parsing (plan: raw-source-boundary) ---

_ACCESSION_ALPHABET = "0123456789abcdefABCDEF-"


@settings(max_examples=200, derandomize=True)
@given(st.text(max_size=24))
def test_prop_accession_normalization_is_idempotent(text: str) -> None:
    """Any accepted accession normalizes to the canonical dashed form and stays stable."""
    from app.research.evidence import ACCESSION_RE, normalize_accession

    try:
        canonical = normalize_accession(text)
    except ValueError:
        return
    assert ACCESSION_RE.match(canonical)
    assert normalize_accession(canonical) == canonical


@settings(max_examples=200, derandomize=True)
@given(st.text(alphabet=_ACCESSION_ALPHABET, max_size=24), st.text(alphabet="0123456789abcdef", min_size=1, max_size=12))
def test_prop_provenance_accepts_exactly_canonical_accessions(blob: str, search_id: str) -> None:
    """sec_source provenance accepts exactly what accession normalization accepts; search runs stay search runs."""
    from app.research.evidence import (
        EvidenceIntegrityError,
        normalize_accession,
        validate_provenance,
    )

    item: dict[str, object] = {"kind": "sec_source", "accession_no": blob, "document_name": "d.htm", "passage": "p"}
    try:
        canonical = normalize_accession(blob)
    except ValueError:
        with pytest.raises(EvidenceIntegrityError):
            validate_provenance(item)
        return
    assert validate_provenance(item)["accession_no"] == canonical
    assert validate_provenance({"kind": "search_run", "search_id": search_id, "query": "q"})["kind"] == "search_run"


def test_telemetry_derived_from_persisted_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan Phase 13: search/evidence telemetry derives from persisted rows + journal, never model counts."""
    from app.research import service as _svc
    from app.research.journal import append_event

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, _svc_item(eid), repo=repo)
    repo.save_event(append_event(sid, "tool.completed", "runner", "runner", {"tool": "search_sec_filings", "args": {"query": "NVDA OpenAI supply"}}))
    coverage: dict[str, object] = {"useful_for_question": "insufficient", "resolved": [], "partially_resolved": [],
                                   "unresolved": ["private contract terms"], "source_limitations": ["SEC-only"]}
    out = _svc.submit_source_result(src, coverage=coverage, evidence_ids=[], repo=repo)
    telemetry = out["telemetry"]
    assert isinstance(telemetry, dict)
    # Journal-derived searches/queries (no coverage search_runs in this submission).
    assert telemetry.get("searches_count") == 1
    assert telemetry.get("queries_attempted") == ["NVDA OpenAI supply"]
    # Row-derived document/filing/evidence counts.
    assert telemetry.get("evidence_records") == 1
    assert telemetry.get("filings_opened") == 1
    assert telemetry.get("documents_opened") == 1 and telemetry.get("raw_documents_used") == 1
    # Relationships are relationship rows; an evidence row is not a relationship.
    assert "material_relationships_found" not in telemetry
    assert telemetry.get("telemetry_gaps") == []


def test_telemetry_gaps_mark_underivable_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No persisted search activity -> searches/queries are null and named in telemetry_gaps, never guessed."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    coverage: dict[str, object] = {"useful_for_question": "insufficient", "resolved": [], "partially_resolved": [],
                                   "unresolved": ["nothing yet"], "source_limitations": ["SEC-only"]}
    out = _svc.submit_source_result(src, coverage=coverage, evidence_ids=[], repo=repo)
    telemetry = out["telemetry"]
    assert isinstance(telemetry, dict)
    assert telemetry.get("searches_count") is None and telemetry.get("queries_attempted") is None
    gaps = telemetry.get("telemetry_gaps")
    assert isinstance(gaps, list) and "searches_count" in gaps and "queries_attempted" in gaps
    assert telemetry.get("evidence_records") == 0


def test_accession_forms_normalize_to_canonical() -> None:
    """Canonical + bare-18-digit forms normalize; search-id-shaped values are rejected outright."""
    from app.research.evidence import normalize_accession

    assert normalize_accession("0000320193-25-000079") == "0000320193-25-000079"
    assert normalize_accession("000032019325000079") == "0000320193-25-000079"
    for bad in ("7768855a3f91", "0000320193-25-00007", "0000320193/25/000079", "", "0000320193-25-000079x"):
        with pytest.raises(ValueError):
            normalize_accession(bad)


# ---------------------------------------------------------------------------
# CRAP gate: novelty/director-gate walk, telemetry signal derivation, committee
# trio atomicity, claim shaping. External behavior only: returned payloads,
# persisted rows, raised errors.
# ---------------------------------------------------------------------------

def _nov_row(sid: str, eid: str, wave: int, *, accession: str = "", document: str = "",
             record_id: str = "", subject: str = "NVDA", digest: str | None = None) -> dict[str, object]:
    """Persisted evidence row for the novelty walk: raw provenance, record-id fallback, or no identity."""
    row: dict[str, object] = {"evidence_id": eid, "session_id": sid, "wave_id": wave, "subject": subject,
                              "content_hash": digest or f"h-{eid}"}
    if accession:
        row["provenance"] = {"kind": "sec_source", "accession_no": accession,
                             "document_name": document, "passage": "p"}
    if record_id:
        row["source_record_id"] = record_id
        row["source_name"] = "SEC 10-K"
    return row


def test_gate_novelty_walks_rows_relationships_and_streak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Novelty counts only documents/rows the run does not already hold; dossier relationships count once."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, _ = _svc_sid(repo)
    repo.save_evidence(_nov_row(sid, f"{sid}:ev:1", 1, accession="0000320193-25-000079", document="a.htm"))
    repo.save_evidence(_nov_row(sid, f"{sid}:ev:2", 1, record_id="r-1"))  # accession fallback identity
    repo.save_evidence(_nov_row(sid, f"{sid}:ev:3", 1, subject="AMD"))  # no identity at all
    repo.save_evidence(_nov_row(sid, f"{sid}:ev:4", 2, accession="0000320193-25-000079", document="a.htm",
                               digest=f"h-{sid}:ev:1"))  # re-read of a held document
    repo.save_evidence(_nov_row(sid, f"{sid}:ev:5", 2, accession="0000320193-25-000081", document="b.htm", subject="AMD"))
    repo.save_event(append_event(sid, "research_loop_detected", "runner", "runner", {}))
    repo.save_event(append_event(sid, "wave.novelty", "runner", "runner", {"zero_novelty_actions": 2}))
    found = dataclasses.replace(repo.get_session(sid), current_wave=2)
    repo.save_session(found)
    novelty = _svc._gate_novelty(repo, found)
    assert set(novelty) == {"new_raw_documents", "new_evidence_records", "new_entities", "new_relationships",
                            "new_material_claims", "resolved_questions", "new_questions",
                            "zero_novelty_waves", "duplicate_actions_blocked", "zero_novelty_actions"}
    # Only ev:5 (new document) is new; ev:4 is the same document+hash the run already held.
    assert novelty["new_raw_documents"] == 1 and novelty["new_evidence_records"] == 1
    assert novelty["new_entities"] == 0  # NVDA + AMD were already subjects of the prior wave
    assert novelty["new_relationships"] == 0 and novelty["new_material_claims"] == 0
    assert novelty["resolved_questions"] == 0 and novelty["new_questions"] == 0
    assert novelty["zero_novelty_waves"] == 0  # wave 2 contributed a fresh row
    assert novelty["duplicate_actions_blocked"] == 1 and novelty["zero_novelty_actions"] == 2

    # A dossier relationship is a persisted novelty signal; a re-listed one is not new again.
    repo.save_dossier({"dossier_id": f"{sid}:2:sec", "session_id": sid, "wave_id": 2, "coverage": {},
                       "relationships": [{"kind": "supplier"}, "junk", {"kind": "customer"}]})
    with_rels = _svc._gate_novelty(repo, found)
    assert with_rels["new_relationships"] == 2  # the non-mapping entry is not a relationship
    assert with_rels["new_evidence_records"] == 1
    repo.save_dossier({"dossier_id": f"{sid}:1:sec", "session_id": sid, "wave_id": 1, "coverage": {},
                       "relationships": [{"kind": "supplier"}]})
    # A relationship any other wave already lists is not fresh for the current wave.
    assert _svc._gate_novelty(repo, found)["new_relationships"] == 1

    # A wave with no evidence rows counts into the zero-novelty streak, back to the last productive wave.
    ahead = dataclasses.replace(repo.get_session(sid), current_wave=4)
    assert _svc._gate_novelty(repo, ahead)["zero_novelty_waves"] == 2  # waves 4 and 3 added nothing

    # Unreadable dossier storage means no relationship signal, never a guessed one.
    def _boom(_session_id: str) -> list[dict[str, object]]:
        raise RuntimeError("dossier storage unavailable")
    monkeypatch.setattr(repo, "list_dossiers", _boom)
    degraded = _svc._gate_novelty(repo, found)
    assert degraded["new_relationships"] == 0 and degraded["new_evidence_records"] == 1


def test_telemetry_ledger_unresolvable_and_journal_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable search_runs ledger falls back to journal queries; an unreadable ledger does the same."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, src = _svc_sid(repo)
    repo.save_event(append_event(sid, "tool.completed", "runner", "runner",
                                 {"tool": "search_sec_filings", "args": {"query": "NVDA 10-K"}}))
    coverage: dict[str, object] = {"useful_for_question": "insufficient", "resolved": [], "partially_resolved": [],
                                   "unresolved": [], "source_limitations": [], "search_runs": ["sr:not-persisted"]}
    out = _svc.submit_source_result(src, coverage=coverage, evidence_ids=[], repo=repo)
    telemetry = out["telemetry"]
    assert isinstance(telemetry, dict)
    assert telemetry.get("searches_count") == 1  # the coverage's search ids, not the journal call
    assert telemetry.get("queries_attempted") == ["NVDA 10-K"]  # journal fallback: the ledger holds no query
    assert telemetry.get("telemetry_gaps") == []

    def _boom(_search_id: str, **_kwargs: object) -> None:
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr("app.sec.store.query_search", _boom)
    degraded = _svc._derive_telemetry(repo, sid, coverage, [])
    assert degraded.get("searches_count") == 1 and degraded.get("queries_attempted") == ["NVDA 10-K"]
    assert degraded.get("telemetry_gaps") == []


def test_telemetry_no_coverage_relationships_and_storage_degradation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No persisted coverage/identity is a named gap; relationships come from dossiers or are omitted."""
    from app.research import service as _svc

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, _ = _svc_sid(repo)
    repo.save_evidence({"evidence_id": f"{sid}:ev:1", "session_id": sid, "wave_id": 1, "subject": "NVDA"})
    bare = _svc._derive_telemetry(repo, sid)
    assert bare.get("branches_covered") is None and bare.get("branches_remaining") is None
    assert bare.get("filings_opened") is None and bare.get("documents_opened") is None
    assert bare.get("raw_documents_used") is None and bare.get("evidence_records") == 1
    gaps = bare.get("telemetry_gaps")
    assert isinstance(gaps, list)
    assert {"branches_covered", "branches_remaining", "filings_opened", "documents_opened",
            "raw_documents_used", "searches_count", "queries_attempted"} <= set(gaps)
    assert "material_relationships_found" not in bare  # no dossier: the key is omitted, never zero

    repo.save_dossier({"dossier_id": f"{sid}:1:sec", "session_id": sid, "wave_id": 1, "coverage": {},
                       "relationships": [{"kind": "supplier"}, "junk"]})
    with_rels = _svc._derive_telemetry(repo, sid)
    assert with_rels.get("material_relationships_found") == 1
    assert with_rels.get("evidence_records") == 1

    # The coverage read succeeds, the relationship read then fails: omit the key, keep the rest.
    inner = repo.list_dossiers
    calls = itertools.count()
    def _flaky(session_id: str) -> list[dict[str, JSONValue]]:
        if next(calls) == 0:
            return inner(session_id)
        raise RuntimeError("dossier storage unavailable")
    monkeypatch.setattr(repo, "list_dossiers", _flaky)
    degraded = _svc._derive_telemetry(repo, sid)
    assert "material_relationships_found" not in degraded
    assert degraded.get("evidence_records") == 1


def test_committee_jobs_partial_trio_rollback_leaves_reused_role_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A trio failing mid-creation fails only the jobs this call started; a reused role is left running."""
    from app.research import service as _svc
    from app.research.models import FailureCategory

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    repo = ResearchRepository()
    sid, _ = _svc_sid(repo)
    found = repo.get_session(sid)
    cur, preexisting = _jobs.create_job(found, repo.list_jobs(sid), job_type="bullbot", owner="pi", wave_id=1)
    repo.save_session(cur)
    repo.save_job(_jobs.start_job(preexisting))
    real_start = _jobs.start_job
    calls = itertools.count()
    def _flaky(job: Job) -> Job:
        if next(calls) == 1:
            raise RuntimeError("start failed")
        return real_start(job)
    monkeypatch.setattr(_jobs, "start_job", _flaky)
    with pytest.raises(RuntimeError, match="start failed"):
        _svc.create_committee_jobs(sid, 1, repo=repo)
    committee = {j.job_type: j for j in repo.list_jobs(sid) if j.job_type in ("stockbot", "bullbot", "bearbot")}
    assert set(committee) == {"stockbot", "bullbot"}  # bearbot never persisted
    assert committee["bullbot"].job_id == preexisting.job_id and committee["bullbot"].status == "running"
    rolled_back = committee["stockbot"]
    assert rolled_back.status == "failed"
    failure = rolled_back.failure
    assert failure is not None and failure.category == FailureCategory.COMMITTEE_DEADLOCK.value
    assert failure.message == "partial trio: RuntimeError"

    # A retry reuses the running role, creates the missing one, and flags the pending freeze.
    monkeypatch.setattr(_jobs, "start_job", real_start)
    retry = _svc.create_committee_jobs(sid, 1, repo=repo)
    retried = retry["jobs"]
    assert isinstance(retried, list) and len(retried) == 3 and len(set(retried)) == 3
    assert retried[0] == rolled_back.job_id and retried[1] == preexisting.job_id
    verb = retry["pending_next_action"]
    assert isinstance(verb, dict) and verb.get("freeze_pending") == f"{sid}:1:freeze"


def test_finalize_claims_shapes_and_guards() -> None:
    """Caller claims narrow to text+evidence_ids; every guard keeps its pinned ValueError code."""
    from app.research import service as _svc
    from app.research.agents import ModelOutputFailure

    shaped = _svc._finalize_claims(
        "sid",
        [{"claim": "  inference from the freeze  ", "evidence_ids": ("EV-1",), "claim_type": None},
         {"claim_text": "declared fact", "evidence_ids": ["EV-1"], "claim_type": "observed_fact"}],
        ["EV-1"],
    )
    assert [(c.text, c.claim_type, c.evidence_ids) for c in shaped] == [
        ("inference from the freeze", "inference", ["EV-1"]),
        ("declared fact", "observed_fact", ["EV-1"]),
    ]
    # A claim with no text under any alias is never silently grounded.
    with pytest.raises(ModelOutputFailure, match="non-empty text"):
        _svc._finalize_claims("sid", [{"evidence_ids": ["EV-1"]}], ["EV-1"])
    not_a_list: object = {"text": "x"}
    with pytest.raises(ValueError, match="must be a list"):
        _svc._finalize_claims("sid", not_a_list, ["EV-1"])
    with pytest.raises(ValueError, match="non-empty grounded"):
        _svc._finalize_claims("sid", [], ["EV-1"])
    non_mapping: object = "x"
    with pytest.raises(ValueError, match="each claim must be a mapping"):
        _svc._finalize_claims("sid", [non_mapping], ["EV-1"])
    with pytest.raises(ValueError, match="JSON-able"):
        _svc._finalize_claims("sid", [{"text": object(), "evidence_ids": ["EV-1"]}], ["EV-1"])


def test_unbounded_as_of_is_no_cutoff_for_every_pit_gate() -> None:
    """`unbounded` means no cutoff: dated documents stay eligible and never `pit_violated`."""
    from app.research.agents.scout import _is_pit_eligible
    from app.research.models import NO_CUTOFF_AS_OF, pit_unverified, pit_violated

    assert NO_CUTOFF_AS_OF == frozenset({"unbounded"})
    dated = "2011-03-16T16:33:51+00:00"
    # The live defect: pit_violated raised on the sentinel, so every dated document was rejected.
    assert pit_violated("unbounded", dated) is False
    assert pit_unverified("unbounded", None) is False
    assert _is_pit_eligible(dated, "unbounded") is True
    assert _is_pit_eligible(None, "unbounded") is True
    # Bounded scopes keep the strict rule.
    assert pit_violated("2025-06-30T00:00:00+00:00", "2026-01-01T00:00:00+00:00") is True
    assert _is_pit_eligible("2025-01-01T00:00:00+00:00", "2025-06-30T00:00:00+00:00") is True
    assert _is_pit_eligible("2026-01-01T00:00:00+00:00", "2025-06-30T00:00:00+00:00") is False
    assert _is_pit_eligible(None, "2025-06-30T00:00:00+00:00") is False


def test_counterparty_queries_are_assigned_unscoped() -> None:
    """A private counterparty's queries reach the filings scout as global (no ticker/cik) queries."""
    from app.research.agents.scout import build_scout_prompt
    from app.research.agents.source_agent import (
        _grouped_queries,
        build_research_context,
        decompose_question,
    )

    ctx = build_research_context("What happens to Microsoft if OpenAI goes bankrupt?", ["MSFT"])
    groups = _grouped_queries(ctx)
    assert "OpenAI" in groups["cp"]
    assignments = decompose_question("What happens to Microsoft if OpenAI goes bankrupt?",
                                     session_id="rs:t", as_of="2026-08-10", tickers=["MSFT"],
                                     dispatch=lambda name, args: {})
    filings = next(a for a in assignments if a.role == "filings")
    assert "OpenAI" in filings.unscoped_queries
    prompt = build_scout_prompt(filings)
    assert "NO ticker and NO cik" in prompt
    # Other scouts keep their scoped assignment: no global counterparty work.
    assert all(not a.unscoped_queries for a in assignments if a.role != "filings")
    # A question without a counterparty assigns no global queries.
    plain = decompose_question("NVDA data center revenue growth", session_id="rs:t", as_of="2026-08-10",
                              tickers=["NVDA"], dispatch=lambda name, args: {})
    assert all(not a.unscoped_queries for a in plain)
