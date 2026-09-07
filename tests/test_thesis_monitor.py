"""Trigger runs plus deterministic monitoring (monitor.tick)."""

import re

import pytest

import app.thesis.runner as runner_mod
from app.thesis.context import ContextBudgetExceeded, build_context
from app.thesis.monitor import CanonicalEvent, tick
from app.thesis.repository import ThesisRepository
from app.thesis.runner import run_trigger
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml

T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"


class _Src:
    name = "sec_filings"

    def __init__(self, events=(), fail=False, key="sec_filings"):
        self.events = list(events)
        self.fail = fail
        self.key = key
        self.calls = 0

    def query_since(self, checkpoint, *, known_at):
        self.calls += 1
        if self.fail:
            raise RuntimeError("source boom")
        return [e for e in self.events if e.known_at <= known_at]


class _Pi:
    """Fake run_thesis_pi: counts launches; ok-runs apply repo writes like Pi's tool calls."""

    def __init__(self, fail=False, write=None):
        self.fail = fail
        self.write = write
        self.calls = 0
        self.prompts = []

    def __call__(self, *, thesis_id, trigger_id, prompt, data_root, timeout_s=170):
        self.calls += 1
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("pi boom")
        if self.write is not None:
            self.write(thesis_id, trigger_id)


def _pi(monkeypatch, **kw):
    fake = _Pi(**kw)
    monkeypatch.setattr(runner_mod, "run_thesis_pi", fake)
    return fake


def _make(tmp_path, scope="NVDA", rule="new_filing", exprs=(), invalidators=()):
    r = ThesisRepository(tmp_path / "theses")
    t = r.create_thesis(f"{scope} thesis", scope=scope, claims=[f"{scope} demand grows"],
                        expressions=list(exprs), invalidators=list(invalidators))
    full = r.load_thesis(t.thesis_id)
    cid = full.claims[0].claim_id
    eids = [e.expression_id for e in full.expressions]
    raw = load_raw_yaml(tmp_path / "theses" / t.slug / "watch.yaml")
    raw["rules"].append({"rule_id": "rule:1", "rule_type": rule, "enabled": True,
                         "support_status": "supported", "support_reason": "",
                         "claim_ids": [cid], "expression_ids": list(eids)})
    atomic_write_yaml(tmp_path / "theses" / t.slug / "watch.yaml", raw, tmp_path / "theses")
    return r, t


def _ev(ref, known_at=T1, entity="NVDA", summary="NVDA files 10-K noting steady demand"):
    return CanonicalEvent(event_id=ref, canonical_ref=ref, source="sec_filings",
                          known_at=known_at, entity=entity, summary=summary)


def _journal_text(tmp_path, slug):
    return " ".join(p.read_text(encoding="utf-8")
                     for p in sorted((tmp_path / "theses" / slug / "journal").glob("*.md")))


def _journal_count(tmp_path, slug):
    return len(list((tmp_path / "theses" / slug / "journal").glob("*.md")))


# -- expression assessment via fake-Pi run_trigger outcomes ---------------------

def test_timing_mismatch_flagged_without_changing_thesis_state(tmp_path, monkeypatch):
    r, t = _make(tmp_path, exprs=[{"instrument": "option", "direction": "long",
                                   "structure": "long puts", "horizon": "short-term",
                                   "status": "active"}])
    full = r.load_thesis(t.thesis_id)
    eid = full.expressions[0].expression_id
    trig = r.create_trigger(t.thesis_id, claim_ids=[full.claims[0].claim_id],
                            expression_ids=[eid], canonical_refs=["ev:m"], summary="s")

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "expression_updates": [{"expression_id": eid, "status": "flagged"}],
            "evidence_refs": [{"canonical_ref": "ev:m", "summary": "30-day puts noted",
                               "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t",
                              "body": "timing mismatch: 30-day puts vs 2-year thesis; "
                                      "near-term filing cuts against puts timing",
                              "known_at": T1}}, "run:t1")

    fake = _pi(monkeypatch, write=_write)
    out = run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert out.processed and fake.calls == 1
    assert r.load_thesis(t.thesis_id).expressions[0].status == "flagged"
    assert r.load_state(t.thesis_id).assessment == "unresolved"
    body = _journal_text(tmp_path, t.slug)
    assert "timing mismatch" in body and "cuts against" in body


def test_bullish_equity_asks_no_options_questions(tmp_path, monkeypatch):
    r, t = _make(tmp_path, exprs=[{"instrument": "equity", "direction": "long",
                                   "structure": "equity", "horizon": "long-term",
                                   "status": "active"}])
    full = r.load_thesis(t.thesis_id)
    trig = r.create_trigger(t.thesis_id, claim_ids=[full.claims[0].claim_id],
                            canonical_refs=["ev:e"], summary="s")
    fake = _pi(monkeypatch)
    out = run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert out.processed and fake.calls == 1
    questions = r.load_questions(t.thesis_id)
    assert questions == []
    assert r.load_state(t.thesis_id).assessment == "unresolved"


def test_covered_call_without_portfolio_leaves_ownership_unresolved(tmp_path, monkeypatch):
    r, t = _make(tmp_path, exprs=[{"instrument": "option", "direction": "long",
                                   "structure": "covered call", "status": "active"}])
    full = r.load_thesis(t.thesis_id)
    trig = r.create_trigger(t.thesis_id, claim_ids=[full.claims[0].claim_id],
                            canonical_refs=["ev:c"], summary="s")

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "ev:c", "summary": "call overlay noted",
                               "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t",
                              "body": "ownership unresolved without portfolio read",
                              "known_at": T1}}, "run:t1")

    fake = _pi(monkeypatch, write=_write)
    out = run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert out.processed and fake.calls == 1
    assert r.load_thesis(t.thesis_id).expressions[0].deterministic_support == "unknown"
    assert "unresolved" in _journal_text(tmp_path, t.slug)


def test_long_puts_without_market_has_no_invented_prices_or_greeks(tmp_path, monkeypatch):
    r, t = _make(tmp_path, exprs=[{"instrument": "option", "direction": "long",
                                   "structure": "long puts", "status": "active"}])
    full = r.load_thesis(t.thesis_id)
    trig = r.create_trigger(t.thesis_id, claim_ids=[full.claims[0].claim_id],
                            canonical_refs=["ev:p"], summary="s")

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "ev:p",
                               "summary": "puts noted, no quote available",
                               "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t",
                              "body": "not evaluable without market read; no quotes fetched",
                              "known_at": T1}}, "run:t1")

    _pi(monkeypatch, write=_write)
    run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    body = _journal_text(tmp_path, t.slug)
    assert not re.search(r"\$\s*\d", body)
    assert not re.search(r"(?i)\b(delta|gamma|theta|vega|implied volatility)\b", body)


def test_unknown_and_processed_triggers_raise(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    fake = _pi(monkeypatch)
    with pytest.raises(KeyError):
        run_trigger(r, t.thesis_id, "trigger:nope", known_at=T2)
    trig = r.create_trigger(t.thesis_id, canonical_refs=["ev:1"], summary="s")
    out = run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert out.processed and fake.calls == 1
    with pytest.raises(ValueError):
        run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert fake.calls == 1


# -- monitoring ----------------------------------------------------------------

def test_irrelevant_event_produces_zero_pi_calls(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    fake = _pi(monkeypatch)
    src = _Src([_ev("ev:irr", entity="UNRELATEDCORPXYZ", summary="unrelated corp files")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert fake.calls == 0 and res.triggers_created == [] and res.no_op


def test_duplicate_event_produces_zero_second_pi_call(tmp_path, monkeypatch):
    r, t = _make(tmp_path)

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "ev:d", "summary": "NVDA files",
                               "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t", "body": "ok",
                              "known_at": T1}}, "run:t1")

    fake = _pi(monkeypatch, write=_write)
    src = _Src([_ev("ev:d")])
    first = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert fake.calls == 1 and len(first.triggers_created) == 1
    second = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert fake.calls == 1 and second.triggers_created == []


def test_identical_events_within_one_tick_coalesce_to_one_call(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    fake = _pi(monkeypatch)
    src = _Src([_ev("ev:same"), _ev("ev:same")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert len(res.triggers_created) == 1 and fake.calls == 1


def test_relevant_filing_produces_one_bounded_call(tmp_path, monkeypatch):
    r = ThesisRepository(tmp_path / "theses")
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["NVDA demand grows"])
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    raw = load_raw_yaml(tmp_path / "theses" / t.slug / "watch.yaml")
    raw["rules"].append({"rule_id": "rule:f", "rule_type": "new_filing", "enabled": True,
                         "support_status": "supported", "support_reason": "",
                         "claim_ids": [cid], "expression_ids": []})
    atomic_write_yaml(tmp_path / "theses" / t.slug / "watch.yaml", raw, tmp_path / "theses")

    class FilingSrc:
        calls = 0

        def query_since(self, cp, *, known_at):
            type(self).calls += 1
            return [CanonicalEvent(event_id="f1", canonical_ref="edgar:NVDA:10-K:f1",
                                   source="sec_filings", known_at=T1, entity="NVDA",
                                   summary="NVDA files 10-K")]

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "edgar:NVDA:10-K:f1",
                               "summary": "10-K notes steady demand", "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t",
                              "body": "reviewed filing; risk factors mention cyclicality",
                              "known_at": T1}}, "run:t1")

    fake = _pi(monkeypatch, write=_write)
    res = tick(r, t.thesis_id, {"sec_filings": FilingSrc()}, known_at=T2)
    assert fake.calls == 1 and len(res.triggers_created) == 1
    assert "cyclicality" in _journal_text(tmp_path, t.slug)


def test_simultaneous_meaningful_events_produce_bounded_calls(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    fake = _pi(monkeypatch)
    src = _Src([_ev("ev:a", summary="NVDA files 10-K"), _ev("ev:b", summary="NVDA 8-K event")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert len(res.triggers_created) == 2 and fake.calls == 2


def test_possible_invalidator_produces_one_call(tmp_path, monkeypatch):
    r, t = _make(tmp_path, rule="explicit_thesis_invalidator",
                 invalidators=["demand collapse scenario"])
    r.apply_research_result(t.thesis_id, {"evidence_refs": [
        {"evidence_id": "ev:seed", "thesis_id": t.thesis_id, "canonical_ref": "seed:1",
         "summary": "analyst warns of demand collapse scenario for NVDA", "known_at": T1}]},
        "run:seed")
    fake = _pi(monkeypatch)
    res = tick(r, t.thesis_id, {}, known_at=T2)
    assert fake.calls == 1 and len(res.triggers_created) == 1


def test_expression_impact_event_tags_expression_ids(tmp_path, monkeypatch):
    r, t = _make(tmp_path, exprs=[{"structure": "long puts", "status": "active"}])
    fake = _pi(monkeypatch)
    src = _Src([_ev("ev:x", summary="NVDA volatility event affects puts")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert fake.calls == 1
    trig = r.load_triggers(t.thesis_id)[0]
    assert trig.expression_ids != ()


def test_source_failure_leaves_checkpoint_unadvanced(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    before = r.load_checkpoint(t.thesis_id).to_dict()
    fake = _pi(monkeypatch)
    res = tick(r, t.thesis_id, {"sec_filings": _Src(fail=True)}, known_at=T2)
    assert fake.calls == 0 and res.triggers_created == []
    assert r.load_checkpoint(t.thesis_id).to_dict() == before


def test_pi_failure_leaves_pending_and_halts(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    src = _Src([_ev("ev:1"), _ev("ev:2")])
    fake = _pi(monkeypatch, fail=True)
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert res.runs == [] and len(res.triggers_created) == 2
    pending = [x for x in r.load_triggers(t.thesis_id) if x.status == "pending"]
    assert len(pending) == 2 and fake.calls == 1


def test_restart_processes_pending_once_without_dupes(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    src = _Src([_ev("ev:1")])
    bad = _pi(monkeypatch, fail=True)
    tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert bad.calls == 1
    journals_before = _journal_count(tmp_path, t.slug)

    def _write(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "ev:1", "summary": "NVDA files",
                               "known_at": T1}],
            "journal_entry": {"entry_id": "journal:t1", "title": "t", "body": "recovered",
                              "known_at": T1}}, "run:t1")

    monkeypatch.setattr(runner_mod, "run_thesis_pi", _Pi(write=_write))
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert res.triggers_created == []
    assert all(x.status == "processed" for x in r.load_triggers(t.thesis_id))
    assert _journal_count(tmp_path, t.slug) == journals_before + 1


def test_paused_and_closed_query_no_sources(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    fake = _pi(monkeypatch)
    r.pause_thesis(t.thesis_id)
    src = _Src([_ev("ev:1")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    assert src.calls == 0 and fake.calls == 0 and res.no_op
    r.resume_thesis(t.thesis_id)
    r.close_thesis(t.thesis_id)
    src2 = _Src([_ev("ev:1")])
    with pytest.raises(ValueError):
        tick(r, t.thesis_id, {"sec_filings": src2}, known_at=T2)
    assert src2.calls == 0


def test_two_theses_stay_isolated(tmp_path, monkeypatch):
    r = ThesisRepository(tmp_path / "theses")
    a = r.create_thesis("NVDA thesis", scope="NVDA", claims=["NVDA demand grows"])
    b = r.create_thesis("NVDA second thesis", scope="NVDA", claims=["NVDA demand grows"])
    for th in (a, b):
        cid = r.load_thesis(th.thesis_id).claims[0].claim_id
        raw = load_raw_yaml(tmp_path / "theses" / th.slug / "watch.yaml")
        raw["rules"].append({"rule_id": "rule:1", "rule_type": "new_filing",
                             "enabled": True, "support_status": "supported",
                             "support_reason": "", "claim_ids": [cid], "expression_ids": []})
        atomic_write_yaml(tmp_path / "theses" / th.slug / "watch.yaml", raw, tmp_path / "theses")
    fake = _pi(monkeypatch)
    tick(r, a.thesis_id, {"sec_filings": _Src([_ev("ev:only-a")])}, known_at=T2)
    assert len(r.load_triggers(a.thesis_id)) == 1
    assert r.load_triggers(b.thesis_id) == []


def test_tight_budget_fails_explicitly(tmp_path):
    r, t = _make(tmp_path)
    trig = r.create_trigger(t.thesis_id, canonical_refs=["ev:1"], summary="s")
    with pytest.raises(ContextBudgetExceeded):
        build_context(r, t.thesis_id, trig, known_at=T2, max_tokens=5)
    ok = build_context(r, t.thesis_id, trig, known_at=T2)
    assert ok.known_at == T2 and isinstance(ok.omitted_ids, list)


def test_evidence_refs_stay_compact_and_reject_bodies(tmp_path, monkeypatch):
    r, t = _make(tmp_path)
    full = r.load_thesis(t.thesis_id)
    trig = r.create_trigger(t.thesis_id, claim_ids=[full.claims[0].claim_id],
                            canonical_refs=["ev:b"], summary="s")

    def _bad(tid, trig_id):
        r.apply_research_result(tid, {
            "trigger_id": trig_id,
            "evidence_refs": [{"canonical_ref": "ev:b", "summary": "note",
                               "known_at": T1, "body": "FULL FILING BODY SMUGGLED"}],
            "journal_entry": {"entry_id": "journal:bad", "title": "t", "body": "b",
                              "known_at": T1}}, "run:bad")

    _pi(monkeypatch, write=_bad)
    with pytest.raises(ValueError):
        run_trigger(r, t.thesis_id, trig.trigger_id, known_at=T2)
    assert r.load_triggers(t.thesis_id)[0].status == "pending"
    assert list((tmp_path / "theses" / t.slug / "evidence").glob("*.yaml")) == []


def test_stored_pool_skips_missing_and_future_known_at(tmp_path):
    r, t = _make(tmp_path, rule="explicit_thesis_invalidator",
                 invalidators=["demand collapse scenario"])
    evdir = tmp_path / "theses" / t.slug / "evidence"
    for eid, ka in (("ev:noka", ""), ("ev:future", "2027-01-01T00:00:00+00:00")):
        atomic_write_yaml(evdir / f"{eid}.yaml",
                          {"schema_version": 1, "evidence_id": eid, "thesis_id": t.thesis_id,
                           "canonical_ref": "seed:1",
                           "summary": "analyst warns of demand collapse scenario for NVDA",
                           "known_at": ka}, tmp_path / "theses")
    trig = r.create_trigger(t.thesis_id, canonical_refs=["seed:2"],
                            summary="demand collapse scenario unfolding for NVDA")
    r.mark_trigger_processed(t.thesis_id, trig.trigger_id, "run:seed")
    res = tick(r, t.thesis_id, {}, known_at="2020-01-01T00:00:00+00:00")
    assert res.triggers_created == [] and res.no_op


def test_external_evidence_rule_loads_unsupported_and_never_live(tmp_path, monkeypatch):
    r, t = _make(tmp_path, rule="new_external_evidence")
    fake = _pi(monkeypatch)
    src = _Src([_ev("ev:ext", summary="NVDA external note")])
    res = tick(r, t.thesis_id, {"sec_filings": src}, known_at=T2)
    rules = r.load_watch_rules(t.thesis_id)
    assert rules[0].enabled is False and rules[0].support_status == "unsupported"
    assert "no production source" in rules[0].support_reason
    assert res.triggers_created == [] and fake.calls == 0 and src.calls == 0


def test_targetless_watch_add_rejected(tmp_path):
    r, t = _make(tmp_path)
    with pytest.raises(ValueError):
        r.apply_research_result(t.thesis_id, {"watch_add": [{
            "rule_id": "rule:targetless", "rule_type": "new_filing", "enabled": True,
            "support_status": "supported", "support_reason": "",
            "claim_ids": [], "expression_ids": []}]}, "run:targetless")


def test_watch_add_rejects_unknown_claim_and_expression_ids(tmp_path):
    r, t = _make(tmp_path)
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    with pytest.raises(ValueError):
        r.apply_research_result(t.thesis_id, {"watch_add": [{
            "rule_id": "rule:bad-claim", "rule_type": "new_filing", "enabled": True,
            "support_status": "supported", "support_reason": "",
            "claim_ids": ["claim:nope"], "expression_ids": []}]}, "run:bad")
    with pytest.raises(ValueError):
        r.apply_research_result(t.thesis_id, {"watch_add": [{
            "rule_id": "rule:bad-expr", "rule_type": "new_filing", "enabled": True,
            "support_status": "supported", "support_reason": "",
            "claim_ids": [cid], "expression_ids": ["expr:nope"]}]}, "run:bad")


def test_refinement_covers_only_new_targets_and_ignores_disabled(tmp_path):
    from app.thesis.intake import IntakeProposal, apply_refinement, plan_refinement

    r, t = _make(tmp_path)
    thesis = r.load_thesis(t.thesis_id)
    old_cid = thesis.claims[0].claim_id
    proposal = IntakeProposal.from_dict(
        {"user_thesis": thesis.user_thesis, "scope": "NVDA",
         "claims": [{"statement": "NVDA networking demand grows"}]}, "<test>")
    plan = plan_refinement(thesis, proposal)
    out = apply_refinement(r, t.thesis_id, plan, proposal)
    new_cid = plan["added_claims"][0]["claim_id"]
    assert len(out["rules_added"]) == 1
    assert set(out["rules_added"][0]["claim_ids"]) == {new_cid}
    assert old_cid not in out["rules_added"][0]["claim_ids"]
    # Repeat refinement adds nothing.
    plan2 = plan_refinement(r.load_thesis(t.thesis_id), proposal)
    out2 = apply_refinement(r, t.thesis_id, plan2, proposal)
    assert out2["rules_added"] == []
    # A disabled rule does not count as coverage and stays untouched.
    added_id = out["rules_added"][0]["rule_id"]
    raw = load_raw_yaml(tmp_path / "theses" / t.slug / "watch.yaml")
    for rule in raw["rules"]:
        if rule["rule_id"] == added_id:
            rule["enabled"] = False
    atomic_write_yaml(tmp_path / "theses" / t.slug / "watch.yaml", raw, tmp_path / "theses")
    thesis2 = r.load_thesis(t.thesis_id)
    proposal3 = IntakeProposal.from_dict(
        {"user_thesis": thesis2.user_thesis, "scope": "NVDA",
         "claims": [{"statement": "NVDA automotive demand grows"}]}, "<test>")
    plan3 = plan_refinement(thesis2, proposal3)
    out3 = apply_refinement(r, t.thesis_id, plan3, proposal3)
    newest_cid = plan3["added_claims"][0]["claim_id"]
    assert len(out3["rules_added"]) == 1
    assert {new_cid, newest_cid} <= set(out3["rules_added"][0]["claim_ids"])
    disabled = [x for x in r.load_watch_rules(t.thesis_id) if x.rule_id == added_id][0]
    assert disabled.enabled is False and set(disabled.claim_ids) == {new_cid}
