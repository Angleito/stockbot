"""Historical replay T0-T5: PIT isolation, separate changes, idempotent resume."""

from pathlib import Path

from app.policy import Capability, RequestContext
from app.thesis.context import build_context
from app.thesis.monitor import CanonicalEvent, tick
from app.thesis.repository import ThesisRepository
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
T5 = "2026-01-06T00:00:00+00:00"


def _ctx() -> RequestContext:
    return RequestContext(principal_id="replay", capabilities=frozenset({Capability.RESEARCH}))


class _ReplaySource:
    name = "sec_filings"

    def __init__(self, events):
        self.events = list(events)
        self.calls = 0

    def query_since(self, checkpoint, *, known_at):
        self.calls += 1
        return [e for e in self.events if e.known_at <= known_at]


class _ReplayGateway:
    def __init__(self):
        self.calls = 0

    def complete_research(self, prompt, *, request_context, tools):
        self.calls += 1
        n = self.calls
        if n == 1:  # T3 relevant filing
            return {"evidence_refs": [{"canonical_ref": "edgar:NVDA:10-K:t3",
                                       "summary": "NVDA 10-K notes steady datacenter demand",
                                       "known_at": T3}],
                    "claim_updates": [],
                    "counterevidence": "",
                    "questions_add": [{"question_id": "q:t3", "text": "Does demand persist?"}],
                    "journal_summary": "T3 filing reviewed"}
        if n == 2:  # T4 contradictory
            return {"evidence_refs": [{"canonical_ref": "ext:nvda-note:t4",
                                       "summary": "note warns of NVDA order pushouts",
                                       "known_at": T4}],
                    "counterevidence": "pushout note contradicts steady-demand claim",
                    "questions_add": [{"question_id": "q:t4", "text": "Are pushouts broad?"}],
                    "journal_summary": "T4 counterevidence recorded"}
        # T5 major change
        return {"evidence_refs": [{"canonical_ref": "edgar:NVDA:8-K:t5",
                                   "summary": "NVDA 8-K discloses major guidance cut",
                                   "known_at": T5}],
                "expression_updates": [],
                "counterevidence": "guidance cut challenges accumulation timing",
                "questions_add": [{"question_id": "q:t5", "text": "Is accumulation delayed?"}],
                "journal_summary": "T5 major change reviewed"}


def _journals(root: Path, slug: str) -> list:
    return sorted((root / slug / "journal").glob("*.md"))


def test_replay_t0_through_t5_point_in_time_and_idempotent(tmp_path):
    root = tmp_path / "theses"
    r = ThesisRepository(root)
    t = r.create_thesis("NVDA datacenter demand thesis", scope="NVDA",
                        claims=["NVDA datacenter demand stays strong"],
                        expressions=[{"instrument": "equity", "direction": "long",
                                      "structure": "equity", "horizon": "long-term",
                                      "status": "active"}])
    full = r.load_thesis(t.thesis_id)
    cid, eid = full.claims[0].claim_id, full.expressions[0].expression_id
    raw = load_raw_yaml(root / t.slug / "watch.yaml")
    raw["rules"].append({"rule_id": "rule:1", "rule_type": "new_filing",
                         "enabled": True, "support_status": "supported",
                         "support_reason": "", "claim_ids": [cid], "expression_ids": [eid]})
    atomic_write_yaml(root / t.slug / "watch.yaml", raw, root)
    tid = t.thesis_id
    src = _ReplaySource([
        CanonicalEvent(event_id="t1", canonical_ref="ext:other:t1", source="sec_filings",
                       known_at=T1, entity="UNRELATEDCORPXYZ", summary="unrelated files"),
        CanonicalEvent(event_id="t2", canonical_ref="ext:other:t1", source="sec_filings",
                       known_at=T2, entity="UNRELATEDCORPXYZ", summary="unrelated files"),
        CanonicalEvent(event_id="t3", canonical_ref="edgar:NVDA:10-K:t3", source="sec_filings",
                       known_at=T3, entity="NVDA", summary="NVDA 10-K notes steady demand"),
        CanonicalEvent(event_id="t4", canonical_ref="ext:nvda-note:t4", source="sec_filings",
                       known_at=T4, entity="NVDA", summary="note warns of NVDA order pushouts"),
        CanonicalEvent(event_id="t5", canonical_ref="edgar:NVDA:8-K:t5", source="sec_filings",
                       known_at=T5, entity="NVDA", summary="NVDA 8-K major guidance cut"),
    ])
    gw = _ReplayGateway()
    ctx = _ctx()

    res1 = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T1)
    assert gw.calls == 0 and res1.triggers_created == [] and _journals(root, t.slug) == []
    res2 = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T2)
    assert gw.calls == 0 and res2.triggers_created == [] and _journals(root, t.slug) == []

    res3 = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T3)
    assert gw.calls == 1 and len(res3.triggers_created) == 1
    c3 = build_context(r, tid, r.load_triggers(tid)[0], known_at=T3)
    assert all(e["known_at"] <= T3 for e in c3.evidence_refs)
    assert len(_journals(root, t.slug)) == 1
    assert any(q.question_id == "q:t3" for q in r.load_questions(tid))

    res4 = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T4)
    assert gw.calls == 2 and len(res4.triggers_created) == 1
    c4 = build_context(r, tid, r.load_triggers(tid)[-1], known_at=T4)
    assert all(e["known_at"] <= T4 for e in c4.evidence_refs)
    body4 = _journals(root, t.slug)[-1].read_text(encoding="utf-8")
    assert "pushout" in body4 and len(_journals(root, t.slug)) == 2
    assert any(q.question_id == "q:t4" for q in r.load_questions(tid))

    res5 = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T5)
    assert gw.calls == 3 and len(res5.triggers_created) == 1
    c5 = build_context(r, tid, r.load_triggers(tid)[-1], known_at=T5)
    assert all(e["known_at"] <= T5 for e in c5.evidence_refs)
    assert len(_journals(root, t.slug)) == 3 == gw.calls
    assert any(q.question_id == "q:t5" for q in r.load_questions(tid))

    # Claim/expression changes stayed separate; counterevidence persisted.
    assert r.load_thesis(tid).claims[0].status == "unvalidated"
    assert r.load_thesis(tid).expressions[0].expression_id == eid
    journals_text = " ".join(p.read_text(encoding="utf-8") for p in _journals(root, t.slug))
    assert "pushout" in journals_text and "guidance cut" in journals_text

    # Restart from checkpoint resumes exact state without dupes.
    checkpoint_before = r.load_checkpoint(tid).to_dict()
    again = tick(r, tid, {"sec_filings": src}, gw, ctx, known_at=T5)
    assert gw.calls == 3 and again.triggers_created == []
    assert len(_journals(root, t.slug)) == 3
    assert r.load_checkpoint(tid).to_dict() == checkpoint_before
    assert len(r.load_triggers(tid)) == 3


def test_journal_known_at_gates_context(tmp_path):
    root = tmp_path / "theses"
    r = ThesisRepository(root)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["NVDA demand grows"])
    trig = r.create_trigger(t.thesis_id, canonical_refs=["ev:1"], summary="s")
    r.append_journal_entry(t.thesis_id, {"entry_id": "past", "title": "past", "body": "b",
                                         "trigger_id": trig.trigger_id, "known_at": T3})
    r.append_journal_entry(t.thesis_id, {"entry_id": "future", "title": "future", "body": "b",
                                         "trigger_id": trig.trigger_id, "known_at": T5})
    r.append_journal_entry(t.thesis_id, {"entry_id": "noka", "title": "noka", "body": "b",
                                         "trigger_id": trig.trigger_id})
    ctx = build_context(r, t.thesis_id, trig, known_at=T4)
    assert {e["journal"] for e in ctx.journal_excerpts} == {"past"}
    assert "journal:past" in ctx.included_ids
    assert "future" in ctx.omitted_ids and "noka" in ctx.omitted_ids


def test_external_evidence_rule_loads_unsupported_and_never_live(tmp_path):
    root = tmp_path / "theses"
    r = ThesisRepository(root)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["NVDA demand grows"])
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    raw = load_raw_yaml(root / t.slug / "watch.yaml")
    raw["rules"].append({"rule_id": "rule:ext", "rule_type": "new_external_evidence",
                         "enabled": True, "support_status": "supported",
                         "support_reason": "", "claim_ids": [cid], "expression_ids": []})
    atomic_write_yaml(root / t.slug / "watch.yaml", raw, root)
    src = _ReplaySource([CanonicalEvent(
        event_id="x1", canonical_ref="ext:nvda:x1", source="sec_filings",
        known_at=T3, entity="NVDA", summary="NVDA note")])
    gw = _ReplayGateway()
    res = tick(r, t.thesis_id, {"sec_filings": src}, gw, _ctx(), known_at=T4)
    rules = r.load_watch_rules(t.thesis_id)
    assert rules[0].enabled is False and rules[0].support_status == "unsupported"
    assert "no production source" in rules[0].support_reason
    assert res.triggers_created == [] and gw.calls == 0 and src.calls == 0
