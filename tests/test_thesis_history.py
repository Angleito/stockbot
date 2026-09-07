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
BEFORE = "2025-12-31T00:00:00+00:00"


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
