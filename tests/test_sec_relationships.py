"""Offline tests for Phase 8 relationship promotion (no network)."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.domain.evidence import relationships as R
from app.domain.evidence import relationship_evaluation as EV
from app.sec import store as sec_store
from app.sec.discovery import service as disc
from app.sec.discovery.service import search_sec_relationships

A = "sec:cik:0000000001"
B = "sec:cik:0000000002"
VERIFIED = {A: True, B: True}


def _proposed(rid="rel:t1", label="Supplier Of", known_at="2024-01-01T00:00:00Z"):
    return R.propose_relationship(
        A, B, label, span="A supplies B", accession="a1",
        document_name="d1", extraction_method="llm", confidence=0.97,
        known_at=known_at, relationship_id=rid)


def _second(rel, acc="a2", doc="d2", conf=0.96):
    return R.attach_relationship_evidence(
        rel, source_span="A supplies B again", accession=acc,
        document_name=doc, extraction_method="llm", confidence=conf,
        known_at="2024-02-01T00:00:00Z")


def test_observe_then_propose_transitions():
    rel = R.observe_relationship(
        A, B, "mentioned with", span="A ... B", accession="a0",
        document_name="d0", known_at="2024-01-01T00:00:00Z",
        relationship_id="rel:obs")
    assert rel.status == "observed"
    assert rel.revisions[0].previous_status in (None, "unknown")
    rel2 = _proposed()
    assert rel2.status == "candidate"
    assert rel2.relationship_type == "supplier_of"
    assert rel2.raw_label == "Supplier Of"


def test_single_mention_stays_candidate():
    rel = _proposed()
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "no_change"
    assert rel.status == "candidate"
    assert "needs-two-distinct-sources" in reasons


def test_two_accessions_verify():
    rel = _proposed()
    _second(rel)
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "verified" and reasons == []
    assert rel.status == "verified"
    assert rel.current_revision_id == rel.revisions[-1].revision_id


def test_low_confidence_blocks_verify():
    rel = _proposed()
    _second(rel, conf=0.94)
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "no_change"
    assert rel.status == "candidate"
    assert any("below-0.95" in r for r in reasons)


def test_unverified_endpoints_block_verify():
    rel = _proposed()
    _second(rel)
    decision, reasons = R.evaluate_relationship(
        rel, endpoints_verified={A: True, B: False})
    assert decision == "no_change"
    assert any("endpoint" in r for r in reasons)


def test_counterevidence_rejects():
    rel = _proposed()
    _second(rel)
    R.attach_relationship_counterevidence(
        rel, source_span="A ended supply deal", accession="a3",
        document_name="d3", confidence=0.9,
        known_at="2024-03-01T00:00:00Z")
    decision, _ = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "rejected"
    assert rel.status == "rejected"


def test_human_supersession_by_later_evidence():
    rel = _proposed(rid="rel:sup")
    _second(rel)
    R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert rel.status == "verified"
    human = R.revise_relationship_status(
        rel, "rejected", actor="human", reason="analyst judged stale")
    assert rel.status == "rejected"
    new_ev = R.attach_relationship_evidence(
        rel, source_span="A renewed multi-year supply deal",
        accession="a9", document_name="d9", extraction_method="llm",
        confidence=0.98, known_at="2024-06-01T00:00:00Z")
    rev = R.supersede_relationship(
        rel, evidence=[new_ev], actor="human",
        reason=f"later qualifying evidence {new_ev.evidence_id} window 2024-06")
    assert rel.status == "verified"
    assert rev.superseded_revision_id == rel.revisions[-2].revision_id
    assert human.revision_id in [r.revision_id for r in rel.revisions]

def test_open_vocabulary_and_validation():
    rel = R.propose_relationship(
        A, B, "Strategic Alliance!!", span="A allied with B",
        accession="a1", document_name="d1", confidence=0.9,
        relationship_id="rel:open")
    assert rel.relationship_type == "strategic_alliance"
    assert rel.raw_label == "Strategic Alliance!!"
    assert R.validate_relationship(rel) == []
    bad = R.propose_relationship(
        A, A, "self loop", span="x", accession="a1", document_name="d1",
        relationship_id="rel:bad")
    assert "invalid-direction" in R.validate_relationship(bad)
    naked = R.propose_relationship(
        None, B, "ghost", span="x", accession="a1", document_name="d1",
        relationship_id="rel:naked")
    assert "unresolved-endpoint" in R.validate_relationship(naked)


def test_deterministic_role_verifies_directly():
    rel = R.propose_relationship(
        A, B, "beneficial_owner", span="A owns 6% of B", accession="a1",
        document_name="d1", extraction_method="structured", confidence=1.0,
        deterministic=True, relationship_id="rel:det")
    assert rel.status == "verified"
    assert rel.revisions[-1].actor == "deterministic"


def test_revise_expired_and_guards():
    rel = _proposed(rid="rel:exp")
    R.revise_relationship_status(rel, "expired", actor="human",
                                 reason="contract window ended")
    assert rel.status == "expired"
    try:
        R.revise_relationship_status(rel, "bogus", reason="x")
    except ValueError:
        pass
    else:
        raise AssertionError("bad status accepted")
    try:
        R.revise_relationship_status(rel, "verified", reason="  ")
    except ValueError:
        pass
    else:
        raise AssertionError("empty reason accepted")

def test_store_roundtrip_and_pit(tmp_path):
    rel = _proposed(rid="rel:store")
    _second(rel)
    R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    for ev in rel.evidence:
        assert sec_store.store_relationship_evidence(ev.to_dict(), root=tmp_path) == 1
    for rev in rel.revisions:
        assert sec_store.store_relationship_revision(rev.to_dict(), root=tmp_path) == 1
    # Deterministic reruns write nothing.
    assert sec_store.store_relationship_evidence(
        rel.evidence[0].to_dict(), root=tmp_path) == 0
    rows = sec_store.query_relationship_evidence("rel:store", root=tmp_path)
    assert len(rows) == 2
    assert {r["accession"] for r in rows} == {"a1", "a2"}
    revs = sec_store.query_relationship_revisions("rel:store", root=tmp_path)
    revs.sort(key=lambda r: int(str(r["revision_id"]).rsplit(":r", 1)[1]))
    assert [r["new_status"] for r in revs] == ["candidate", "candidate", "verified"]
    assert sec_store.query_relationship_evidence(
        "rel:store", as_of="2024-01-15", root=tmp_path)[0]["accession"] == "a1"


def test_search_groups_by_type_and_status(tmp_path, monkeypatch):
    assert sec_store.store_beneficial_ownership({
        "accession": "0000000000-24-000001", "document_name": "primary",
        "subject_cik": "1", "subject_name": "B Corp",
        "filer_cik": "2", "filer_name": "A Fund",
        "known_at": "2024-01-10T00:00:00Z"}, root=tmp_path) == 1
    verified = _proposed(rid="rel:g1")
    _second(verified)
    R.evaluate_relationship(verified, endpoints_verified=VERIFIED)
    candidate = R.propose_relationship(
        A, B, "Customer Of", span="B buys from A", accession="a5",
        document_name="d5", confidence=0.9,
        known_at="2024-03-01T00:00:00Z", relationship_id="rel:g2")
    for rel in (verified, candidate):
        for ev in rel.evidence:
            sec_store.store_relationship_evidence(ev.to_dict(), root=tmp_path)
        for rev in rel.revisions:
            sec_store.store_relationship_revision(rev.to_dict(), root=tmp_path)
    monkeypatch.setattr(
        "app.sec.client.search_sec_filings",
        lambda *a, **k: SimpleNamespace(text_hits=[]))
    out = search_sec_relationships(A, data_root=tmp_path)
    assert out["ciks"] == ("1",)
    assert out["groups"]["beneficial_owner"]["verified"]
    assert out["groups"]["supplier_of"]["verified"]
    assert out["groups"]["customer_of"]["candidate"]
    # Mentions never flatten into verified links.
    for rtype, by_status in out["groups"].items():
        assert "observed" not in by_status or rtype == "mention"
    backends = {a["backend"] for a in out["attempts"]}
    assert {"local-typed", "local-workflow", "local-mentions",
            "efts-mentions"} <= backends
    filtered = search_sec_relationships(
        A, relationship_types=["supplier_of"], data_root=tmp_path)
    assert set(filtered["groups"]) == {"supplier_of"}

# --- Phase 9: deterministic PIT walk-forward type evaluation ---
#
# Exponential price paths make forward excess date-independent, so the
# expected 1/5/20-day metrics have closed forms asserted below exactly.
DAY0 = date(2024, 1, 1)


def _day(i):
    return (DAY0 + timedelta(days=i)).isoformat()


CAL = [_day(i) for i in range(120)]
BENCH = {d: 100.0 * (1.005 ** i) for i, d in enumerate(CAL)}
WA = (_day(31), _day(59))
WB = (_day(60), _day(90))
EXP = {h: 1.01 ** h - 1.005 ** h for h in (1, 5, 20)}


def _prices():
    obs = {}
    for i, d in enumerate(CAL):
        obs[("sec:good", d)] = 100.0 * (1.01 ** i)
        obs[("sec:bad", d)] = 100.0
    return obs


def _wf_instances(flip=False):
    """120 PIT-safe instances over two chronological windows.

    Normal: the model retrieves 50 relevant outperformers per window while
    the matched baseline retrieves 10 irrelevant flat names. Flipped: the
    model retrieves the flat names and the baseline the outperformers, so
    both retrieval utility and the market composite fall below baseline.
    """
    insts = []
    for ws, _we in (WA, WB):
        s = CAL.index(ws)
        for k in range(50):
            d = _day(s + (k % 10))
            insts.append({
                "instance_id": f"g-{ws}-{k}", "relationship_type": "Supplier Of",
                "entity_id": "sec:good", "prediction_date": d,
                "evidence_known_at": _day(CAL.index(d) - 1),
                "relevant": True, "predicted": not flip,
                "baseline_predicted": flip, "agent_useful": True})
        for k in range(10):
            d = _day(s + 20 + (k % 5))
            insts.append({
                "instance_id": f"b-{ws}-{k}", "relationship_type": "Supplier Of",
                "entity_id": "sec:bad", "prediction_date": d,
                "evidence_known_at": _day(CAL.index(d) - 1),
                "relevant": False, "predicted": flip,
                "baseline_predicted": not flip})
    return insts


def test_walkforward_metrics_and_activate(tmp_path):
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["decision"] == "activate"
    assert out["new_state"] == "active"
    assert out["total_pit_safe"] == 120
    assert out["rows_written"] == 2
    assert len(out["inputs_hash"]) == 64
    for window in out["windows"]:
        assert window["complete"] and window["qualifying"]
        assert window["n_instances"] == 60 and window["n_pit_safe"] == 60
        assert window["pit_violations"] == 0
        assert window["retrieval"]["f1"] == 1.0
        assert window["identity_accuracy"] == 1.0
        for h in (1, 5, 20):
            cell = window["market"][str(h)]
            assert cell["n"] == 50
            assert cell["mean_excess"] == pytest.approx(EXP[h])
            assert cell["volatility"] == pytest.approx(0.0, abs=1e-9)
            assert cell["max_drawdown"] == 0.0
        assert window["market_composite"] == pytest.approx(sum(EXP.values()) / 3)
        assert window["market_composite"] > window["baseline_market_composite"]
    rows = sec_store.query_relationship_type_evaluations(
        "supplier_of", root=tmp_path)
    assert len(rows) == 2  # one row per window; history retained
    assert {r["decision"] for r in rows} == {"activate"}
    assert {r["new_state"] for r in rows} == {"active"}
    assert {r["inputs_hash"] for r in rows} == {out["inputs_hash"]}
    assert all(r["metrics_json"] and r["evaluation_id"] for r in rows)
    state, _ = sec_store.latest_type_state("supplier_of", root=tmp_path)
    assert state == "active"
    # Deterministic reruns over identical inputs write nothing.
    again = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert again["rows_written"] == 0


def test_pit_leak_blocks_promotion(tmp_path):
    insts = _wf_instances()
    leaked = next(i for i in insts if i["instance_id"].startswith("g-"))
    leaked["evidence_known_at"] = _day(CAL.index(leaked["prediction_date"]) + 1)
    out = disc.evaluate_and_persist_type(
        "Supplier Of", insts, observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["total_pit_safe"] == 119
    assert sum(w["pit_violations"] for w in out["windows"]) == 1
    assert out["decision"] == "no_change"
    assert out["new_state"] == "unevaluated"


def test_two_below_baseline_windows_demote_with_history(tmp_path):
    disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(flip=True), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert all(w["below_baseline"] for w in out["windows"])
    assert out["decision"] == "demote"
    assert out["new_state"] == "demoted"
    rows = sec_store.query_relationship_type_evaluations(
        "supplier_of", root=tmp_path)
    assert len(rows) == 4  # prior active states retained alongside demotions
    assert {r["decision"] for r in rows} == {"activate", "demote"}


def test_missing_market_marks_incomplete_and_leaves_type(tmp_path):
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=None,
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["decision"] == "incomplete"
    assert all(not w["complete"] for w in out["windows"])
    assert out["new_state"] == "unevaluated"
    state, _ = sec_store.latest_type_state("supplier_of", root=tmp_path)
    assert state == "unevaluated"


def test_human_decision_superseded_by_later_evaluation(tmp_path):
    row = disc.record_type_decision(
        "Supplier Of", "active", reason="analyst override", data_root=tmp_path)
    assert row["actor"] == "human" and row["new_state"] == "active"
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(flip=True), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["decision"] == "demote"
    rows = sec_store.query_relationship_type_evaluations(
        "supplier_of", root=tmp_path)
    demote = [r for r in rows if r["decision"] == "demote"]
    assert demote and all(
        f"supersedes human {row['evaluation_id']}" in (r["reason"] or "")
        for r in demote)


def test_ontology_boost_orders_but_never_filters(tmp_path, monkeypatch):
    assert EV.ontology_boost("Supplier Of", ["supplier_of"], []) == 1.5
    assert EV.ontology_boost("Customer Of", ["supplier_of"], ["customer_of"]) == 0.5
    assert EV.ontology_boost("Other", ["supplier_of"], ["customer_of"]) == 1.0
    verified = _proposed(rid="rel:o1")
    _second(verified)
    R.evaluate_relationship(verified, endpoints_verified=VERIFIED)
    candidate = R.propose_relationship(
        A, B, "Customer Of", span="B buys from A", accession="a9",
        document_name="d9", confidence=0.9,
        known_at="2024-03-01T00:00:00Z", relationship_id="rel:o2")
    for rel in (verified, candidate):
        for ev in rel.evidence:
            sec_store.store_relationship_evidence(ev.to_dict(), root=tmp_path)
        for rev in rel.revisions:
            sec_store.store_relationship_revision(rev.to_dict(), root=tmp_path)
    disc.record_type_decision(
        "Supplier Of", "active", reason="walk-forward gate", data_root=tmp_path)
    disc.record_type_decision(
        "Customer Of", "demoted", reason="below baseline", data_root=tmp_path)
    monkeypatch.setattr(
        "app.sec.client.search_sec_filings",
        lambda *a, **k: SimpleNamespace(text_hits=[]))
    out = search_sec_relationships(A, data_root=tmp_path)
    assert set(out["groups"]) == {"supplier_of", "customer_of"}
    assert list(out["groups"]) == ["supplier_of", "customer_of"]
