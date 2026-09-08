"""PIT snapshot isolation for thesis history (no network)."""

import shutil

import pytest

from app.thesis.context import build_context
from app.thesis.models import HistoricalStateUnavailable
from app.thesis.repository import ThesisRepository

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"
BEFORE = "2025-12-31T00:00:00+00:00"


def _history_count(r, tid):
    return len(list((r.dir_for_thesis(tid) / "history").glob("*.yaml")))


def _repo(tmp_path) -> ThesisRepository:
    return ThesisRepository(tmp_path / "theses")


def _make(tmp_path, **kw):
    kw.setdefault("claims", [{"claim_id": "claim:c1", "statement": "NVDA demand stays strong"}])
    kw.setdefault("expressions", [{"expression_id": "expr:e1", "instrument": "equity",
                                   "direction": "long", "structure": "equity"}])
    return _repo(tmp_path).create_thesis("NVDA datacenter demand thesis",
                                         scope="NVDA", effective_at=T0, **kw)


def _set_claim_status(r, tid, cid, status, eff):
    claims = []
    for c in r.load_thesis(tid).claims:
        d = c.to_dict()
        if d["claim_id"] == cid:
            d["status"] = status
        claims.append(d)
    return r.update_thesis(tid, claims=claims, effective_at=eff)


def _set_expression_status(r, tid, eid, status, eff):
    exprs = []
    for e in r.load_thesis(tid).expressions:
        d = e.to_dict()
        if d["expression_id"] == eid:
            d["status"] = status
        exprs.append(d)
    return r.update_thesis(tid, expressions=exprs, effective_at=eff)


def _claim_status(snap, cid):
    return next(c["status"] for c in snap.thesis["claims"] if c["claim_id"] == cid)


def test_create_writes_v1(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA",
                        claims=[{"claim_id": "claim:c1", "statement": "s"}],
                        effective_at=T0)
    assert (r.dir_for_thesis(t.thesis_id) / "history" / "00000001.yaml").is_file()
    snap = r.load_state_as_of(t.thesis_id, T0)
    assert snap.version == 1
    assert snap.effective_at == t.created_at == T0


def test_claim_status_history(tmp_path):
    r = _repo(tmp_path)
    t = _make(tmp_path)
    tid = t.thesis_id
    _set_claim_status(r, tid, "claim:c1", "supported", T1)
    _set_claim_status(r, tid, "claim:c1", "challenged", T2)
    assert _claim_status(r.load_state_as_of(tid, T0), "claim:c1") == "unvalidated"
    assert _claim_status(r.load_state_as_of(tid, T1), "claim:c1") == "supported"
    assert _claim_status(r.load_state_as_of(tid, T2), "claim:c1") == "challenged"
    assert _claim_status(r.load_state_as_of(tid, T3), "claim:c1") == "challenged"


def test_assessment_and_expression_history(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    r.apply_research_result(tid, {"state": {"assessment": "strengthening"}},
                            "run:t1", effective_at=T1)
    _set_expression_status(r, tid, "expr:e1", "active", T1)
    r.apply_research_result(tid, {"state": {"assessment": "weakening"}},
                            "run:t3", effective_at=T3)
    _set_expression_status(r, tid, "expr:e1", "flagged", T3)

    def _expr(snap):
        return next(e["status"] for e in snap.thesis["expressions"]
                    if e["expression_id"] == "expr:e1")

    s0 = r.load_state_as_of(tid, T0)
    assert s0.state["assessment"] == "unresolved" and _expr(s0) == "undecided"
    s1 = r.load_state_as_of(tid, T1)
    assert s1.state["assessment"] == "strengthening" and _expr(s1) == "active"
    s2 = r.load_state_as_of(tid, T2)
    assert s2.state["assessment"] == "strengthening" and _expr(s2) == "active"
    s3 = r.load_state_as_of(tid, T3)
    assert s3.state["assessment"] == "weakening" and _expr(s3) == "flagged"


def _filing_rule(rid):
    return {"rule_id": rid, "rule_type": "new_filing", "enabled": True,
            "support_status": "supported", "support_reason": "",
            "claim_ids": ["claim:c1"], "expression_ids": []}


def test_questions_watch_history(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    r.apply_research_result(tid, {
        "questions_add": [{"question_id": "q:one", "text": "Does demand persist?"},
                          {"question_id": "q:two", "text": "Are pushouts broad?"}],
        "watch_add": [_filing_rule("rule:filing")],
    }, "run:t1", effective_at=T1)
    s1 = r.load_state_as_of(tid, T1)
    assert {q["question_id"]: q["status"] for q in s1.questions["questions"]} == \
        {"q:one": "open", "q:two": "open"}
    assert [x["rule_id"] for x in s1.watch["rules"]] == ["rule:filing"]

    r.answer_questions(tid, [{"question_id": "q:one", "answer": "Yes, steady."}],
                       effective_at=T3)
    short = dict(_filing_rule("rule:short"))
    short["rule_type"] = "new_short_interest_cycle"
    r.apply_research_result(tid, {
        "questions_answered": [{"question_id": "q:two", "answer": "No, narrow."}],
        "watch_add": [short],
    }, "run:t3", effective_at=T3)
    s3 = r.load_state_as_of(tid, T3)
    got = {q["question_id"]: q for q in s3.questions["questions"]}
    assert got["q:one"]["status"] == "answered" and got["q:one"]["answer"] == "Yes, steady."
    assert got["q:two"]["status"] == "answered" and got["q:two"]["answer"] == "No, narrow."
    assert [x["rule_id"] for x in s3.watch["rules"]] == ["rule:filing", "rule:short"]
    # T1 snapshot is untouched by the T3 answers.
    assert all(q["status"] == "open" for q in
               r.load_state_as_of(tid, T1).questions["questions"])


def test_same_effective_at_higher_version_wins(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    r.update_thesis(tid, scope="first", effective_at=T2)
    r.update_thesis(tid, scope="second", effective_at=T2)
    snap = r.load_state_as_of(tid, T2)
    assert snap.version == 3
    assert snap.thesis["scope"] == "second"


def test_legacy_lazy_migration(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    shutil.rmtree(r.dir_for_thesis(tid) / "history")
    r.update_thesis(tid, scope="migrated", effective_at=T3)
    baseline = r.load_thesis(tid).updated_at
    snap = r.load_state_as_of(tid, baseline)
    assert snap.version == 1
    assert snap.reason == "history_migration"
    assert snap.effective_at == baseline
    assert snap.thesis["scope"] == "migrated"
    with pytest.raises(HistoricalStateUnavailable):
        r.load_state_as_of(tid, T1)


def test_build_context_pit_regression(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    r.apply_research_result(tid, {"evidence_refs": [
        {"canonical_ref": "ev:A", "summary": "10-K notes steady demand", "known_at": T1}]},
        "run:t1", effective_at=T1)
    r.apply_research_result(tid, {"state": {"assessment": "strengthening"}},
                            "run:t2", effective_at=T2)
    r.apply_research_result(tid, {"evidence_refs": [
        {"canonical_ref": "ev:B", "summary": "note warns of pushouts", "known_at": T3}]},
        "run:t3", effective_at=T3)
    r.apply_research_result(tid, {"claim_updates": [
        {"claim_id": "claim:c1", "status": "challenged"}]}, "run:t4", effective_at=T4)
    trig = r.create_trigger(tid, canonical_refs=["ev:A"], summary="filing")

    ctx = build_context(r, tid, trig, known_at=T1)
    assert ctx.thesis_packet["state"]["assessment"] == "unresolved"
    assert ctx.thesis_packet["thesis"]["claims"][0]["status"] == "unvalidated"
    assert {e["canonical_ref"] for e in ctx.evidence_refs} == {"ev:A"}

    with pytest.raises(HistoricalStateUnavailable):
        build_context(r, tid, trig, known_at=BEFORE)

def test_backdated_research_never_moves_live(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    _set_claim_status(r, tid, "claim:c1", "challenged", T4)
    n = _history_count(r, tid)
    r.apply_research_result(tid, {
        "claim_updates": [{"claim_id": "claim:c1", "status": "supported"}],
        "evidence_refs": [],
        "journal_entry": {"title": "Backdated note", "body": "Seen at T1.", "known_at": T1},
    }, "run:backdate", effective_at=T1)
    assert _claim_status(r.load_state_as_of(tid, T1), "claim:c1") == "supported"
    assert _claim_status(r.load_state_as_of(tid, T5), "claim:c1") == "challenged"
    live = next(c.status for c in r.load_thesis(tid).claims if c.claim_id == "claim:c1")
    assert live == "challenged"
    assert _history_count(r, tid) == n + 1


def test_backdated_write_on_live_closed_writes_snapshot_only(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    _set_claim_status(r, tid, "claim:c1", "challenged", T4)
    r.close_thesis(tid)
    n = _history_count(r, tid)
    live_path = r.dir_for_thesis(tid) / "thesis.yaml"
    live_bytes = live_path.read_bytes()
    r.apply_research_result(tid, {
        "claim_updates": [{"claim_id": "claim:c1", "status": "supported"}],
        "state": {"assessment": "strengthening"},
    }, "run:backdate-closed", effective_at=T1)
    assert _claim_status(r.load_state_as_of(tid, T1), "claim:c1") == "supported"
    assert _claim_status(r.load_state_as_of(tid, T5), "claim:c1") == "challenged"
    assert r.load_thesis(tid).status == "closed"
    assert next(c.status for c in r.load_thesis(tid).claims if c.claim_id == "claim:c1") == "challenged"
    assert live_path.read_bytes() == live_bytes
    assert _history_count(r, tid) == n + 1
    r.pause_thesis(tid, effective_at=T2)
    assert r.load_state_as_of(tid, T2).thesis["status"] == "paused"
    assert r.load_thesis(tid).status == "closed"
    assert live_path.read_bytes() == live_bytes
    r.resume_thesis(tid, effective_at=T2)
    assert r.load_thesis(tid).status == "closed"
    assert live_path.read_bytes() == live_bytes


def test_evidence_journal_only_commit_mints_no_snapshot(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    n = _history_count(r, tid)
    v0 = r.load_state_as_of(tid, T1).version
    r.apply_research_result(tid, {
        "evidence_refs": [{"canonical_ref": "ev:A", "summary": "10-K steady", "known_at": T1}],
        "journal_entry": {"title": "Note", "body": "No state change.", "known_at": T1},
    }, "run:sidecar", effective_at=T1)
    assert _history_count(r, tid) == n
    assert r.load_state_as_of(tid, T1).version == v0
    assert _claim_status(r.load_state_as_of(tid, T1), "claim:c1") == "unvalidated"


def test_journal_gate_binds_known_at(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    trig = r.create_trigger(tid, canonical_refs=[], summary="filing")
    r.append_journal_entry(tid, {"trigger_id": trig.trigger_id, "title": "Run note",
                                 "body": "Processed.", "known_at": T3})
    assert r.has_journal_for_trigger(tid, trig.trigger_id, known_at=T2) is False
    assert r.has_journal_for_trigger(tid, trig.trigger_id, known_at=T3) is True


def test_historical_memory_stamps_effective_at(tmp_path):
    r = _repo(tmp_path)
    tid = _make(tmp_path).thesis_id
    r.apply_research_result(tid, {"memories_add": [{"memory_id": "m1", "text": "Desk note"}]},
                            "run:mem", effective_at=T1)
    mems = r.load_state_as_of(tid, T1).memory["memories"]
    assert next(m for m in mems if m["memory_id"] == "m1")["created_at"] == T1
    trig = r.create_trigger(tid, canonical_refs=[], summary="filing")
    assert "m1" in build_context(r, tid, trig, known_at=T1).included_ids
    # T0 predates the memory (BEFORE raises: no snapshot exists yet, covered above).
    assert "m1" not in build_context(r, tid, trig, known_at=T0).included_ids


def test_thesis_show_defaults_to_and_caps_at_cutoff(tmp_path):
    from app import tools as tools_mod
    from app.policy import Capability, RequestContext

    repo = ThesisRepository(tmp_path / "thesis")  # mirrors _thesis_repo_for(data_root)
    tid = repo.create_thesis("NVDA datacenter demand thesis", scope="NVDA",
                             claims=[{"claim_id": "claim:c1",
                                      "statement": "NVDA demand stays strong"}],
                             effective_at=T0).thesis_id
    _set_claim_status(repo, tid, "claim:c1", "challenged", T4)
    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                         data_root=tmp_path, as_of=T1)
    got = tools_mod.execute_tool("thesis_show", {"id": tid}, "test-model", context=ctx)
    assert "error" not in got
    assert [c["status"] for c in got["claims"] if c["claim_id"] == "claim:c1"] == ["unvalidated"]
    over = tools_mod.execute_tool("thesis_show", {"id": tid, "as_of": T4},
                                  "test-model", context=ctx)
    assert "error" in over and over.get("error_type") == "invalid_tool_arguments"


def test_thesis_create_at_cutoff_stamps_effective_at(tmp_path):
    from app import tools as tools_mod
    from app.policy import Capability, RequestContext

    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                         data_root=tmp_path, as_of=T1)
    got = tools_mod.execute_tool("thesis_create", {"user_thesis": "NVDA datacenter demand thesis",
                                                   "scope": "NVDA",
                                                   "claims": [{"statement": "NVDA demand stays strong"}]},
                                 "test-model", context=ctx)
    assert "error" not in got
    repo = ThesisRepository(tmp_path / "thesis")  # mirrors _thesis_repo_for(data_root)
    assert repo.load_thesis(got["thesis_id"]).created_at == T1
    assert repo.load_state_as_of(got["thesis_id"], T1).reason == "thesis_created"


def test_historical_thesis_refine_appends_without_touching_live(tmp_path):
    from app import tools as tools_mod
    from app.policy import Capability, RequestContext

    repo = ThesisRepository(tmp_path / "thesis")  # mirrors _thesis_repo_for(data_root)
    tid = repo.create_thesis("NVDA datacenter demand thesis", scope="NVDA",
                             claims=[{"claim_id": "claim:c1",
                                      "statement": "NVDA demand stays strong"}],
                             effective_at=T0).thesis_id
    _set_claim_status(repo, tid, "claim:c1", "challenged", T4)
    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                         data_root=tmp_path, as_of=T1)
    got = tools_mod.execute_tool("thesis_refine", {"id": tid,
                                                   "clarification": "Networking demand also stays strong",
                                                   "claims": [{"statement": "NVDA networking demand also strong"}]},
                                 "test-model", context=ctx)
    assert "error" not in got and got.get("applied") is True
    snap = repo.load_state_as_of(tid, T1)
    assert "NVDA networking demand also strong" in [c["statement"] for c in snap.thesis["claims"]]
    assert _claim_status(snap, "claim:c1") == "unvalidated"
    t1_ids = {c["claim_id"] for c in snap.thesis["claims"]}
    assert got.get("rules_added") and all(
        set(r.get("claim_ids", ())) <= t1_ids for r in got["rules_added"])
    live = repo.load_thesis(tid)
    assert [c.status for c in live.claims if c.claim_id == "claim:c1"] == ["challenged"]
    assert "NVDA networking demand also strong" not in [c.statement for c in live.claims]
    assert not (t1_ids - {"claim:c1"}) & {cid for r in repo.load_watch_rules(tid) for cid in r.claim_ids}


def test_historical_watch_list_and_trigger_journal_known_at(tmp_path):
    from app import tools as tools_mod
    from app.policy import Capability, RequestContext

    repo = ThesisRepository(tmp_path / "thesis")  # mirrors _thesis_repo_for(data_root)
    tid = repo.create_thesis("NVDA datacenter demand thesis", scope="NVDA",
                             claims=[{"claim_id": "claim:c1",
                                      "statement": "NVDA demand stays strong"}],
                             effective_at=T0).thesis_id
    repo.apply_research_result(tid, {"watch_add": [_filing_rule("rule:r1")]}, "", effective_at=T1)
    trig = repo.create_trigger(tid, claim_ids=["claim:c1"])
    repo.close_thesis(tid)
    assert repo.load_thesis(tid).status == "closed"
    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                         data_root=tmp_path, as_of=T1)
    listed = tools_mod.execute_tool("thesis_watch", {"id": tid}, "test-model", context=ctx)
    assert "error" not in listed
    assert listed["rules"] == repo.load_state_as_of(tid, T1).watch["rules"]
    ctx2 = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                          data_root=tmp_path, as_of=T2)
    ok = tools_mod.execute_tool("thesis_journal", {"id": tid, "body": "trigger review",
                                                   "trigger_id": trig.trigger_id, "known_at": T2},
                                "test-model", context=ctx2)
    assert "error" not in ok and ok["thesis_id"] == tid
    early = tools_mod.execute_tool("thesis_journal", {"id": tid, "body": "trigger review",
                                                      "trigger_id": trig.trigger_id, "known_at": T1},
                                   "test-model", context=ctx2)
    assert "error" in early


def test_pit_day_injection_and_unsafe_tool_guard(tmp_path, monkeypatch):
    from app import tools as tools_mod
    from app.policy import Capability, RequestContext

    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}),
                         data_root=tmp_path, as_of=T2)
    seen = {}

    def _fake_fundamentals(ticker, metric, as_of=None):
        seen.update(ticker=ticker, metric=metric, as_of=as_of)
        return {"ticker": ticker, "metric": metric, "as_of": as_of}

    monkeypatch.setattr(tools_mod.sec_facts, "get_fundamentals", _fake_fundamentals)
    got = tools_mod.execute_tool("get_fundamentals", {"ticker": "NVDA", "metric": "eps"},
                                 "test-model", context=ctx)
    assert "error" not in got
    assert seen["as_of"] == "2026-01-03"
    calls = []

    def _must_not_run(ticker, concept):
        calls.append((ticker, concept))
        raise AssertionError("current-only tool must not execute under a cutoff")

    monkeypatch.setattr(tools_mod.sec_facts, "get_xbrl_facts", _must_not_run)
    unsafe = tools_mod.execute_tool("get_xbrl_facts", {"ticker": "NVDA", "concept": "Revenue"},
                                    "test-model", context=ctx)
    assert unsafe.get("error_type") == "pit_unsafe_tool"
    assert calls == []
