"""Offline tests for Phase 8 relationship promotion (no network)."""

from datetime import date, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import NoReturn

import pytest

from app.domain.evidence import relationships as R
from app.domain.evidence import relationship_evaluation as EV
from app.sec import store as sec_store
from app.sec.discovery import service as disc
from app.sec.discovery.service import search_sec_relationships
from app.sec.models import Filing

A = "sec:cik:0000000001"
B = "sec:cik:0000000002"
VERIFIED = {A: True, B: True}


def _as_seq(value: object):
    """list/tuple from a relationship-search envelope (discovery boundary)."""
    assert isinstance(value, (list, tuple))
    return value


def _as_dict(value: object):
    """dict from a relationship-search envelope (discovery boundary)."""
    assert isinstance(value, dict)
    return value


def _empty_rows(*args: object, **kwargs: object) -> list[dict[str, object]]:
    """Stateless empty row-list fake for store/query seams."""
    return []


def _empty_map(**kwargs: object) -> dict[str, object]:
    """Stateless empty mapping fake for state seams."""
    return {}


def _empty_text_hits(*args: object, **kwargs: object) -> SimpleNamespace:
    """Stateless no-hit filings-search fake."""
    return SimpleNamespace(text_hits=[])


def _fake_verified_revision(relationship_id: str | None, **kwargs: object) -> list[dict[str, str]]:
    """Verified single-revision fake for the revision seam."""
    return [{"revision_id": f"{relationship_id}:r0", "new_status": "verified"}]


def _proposed(rid: str = "rel:t1", label: str = "Supplier Of", known_at: str = "2024-01-01T00:00:00Z"):
    return R.propose_relationship(
        A, B, label, span="A supplies B", accession="a1",
        document_name="d1", extraction_method="llm", confidence=0.97,
        known_at=known_at, relationship_id=rid)


def _second(rel: R.Relationship, acc: str = "a2", doc: str = "d2", conf: float = 0.96):
    return R.attach_relationship_evidence(
        rel, source_span="A supplies B again", accession=acc,
        document_name=doc, extraction_method="llm", confidence=conf,
        known_at="2024-02-01T00:00:00Z")


def test_observe_then_propose_transitions() -> None:
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


def test_single_mention_stays_candidate() -> None:
    rel = _proposed()
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "no_change"
    assert rel.status == "candidate"
    assert "needs-two-distinct-sources" in reasons


def test_two_accessions_verify() -> None:
    rel = _proposed()
    _second(rel)
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "verified" and reasons == []
    assert rel.status == "verified"
    assert rel.current_revision_id == rel.revisions[-1].revision_id


def test_low_confidence_blocks_verify() -> None:
    rel = _proposed()
    _second(rel, conf=0.94)
    decision, reasons = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "no_change"
    assert rel.status == "candidate"
    assert any("below-0.95" in r for r in reasons)


def test_unverified_endpoints_block_verify() -> None:
    rel = _proposed()
    _second(rel)
    decision, reasons = R.evaluate_relationship(
        rel, endpoints_verified={A: True, B: False})
    assert decision == "no_change"
    assert any("endpoint" in r for r in reasons)


def test_counterevidence_rejects() -> None:
    rel = _proposed()
    _second(rel)
    R.attach_relationship_counterevidence(
        rel, source_span="A ended supply deal", accession="a3",
        document_name="d3", confidence=0.9,
        known_at="2024-03-01T00:00:00Z")
    decision, _ = R.evaluate_relationship(rel, endpoints_verified=VERIFIED)
    assert decision == "rejected"
    assert rel.status == "rejected"


def test_human_supersession_by_later_evidence() -> None:
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

def test_open_vocabulary_and_validation() -> None:
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


def test_deterministic_role_verifies_directly() -> None:
    rel = R.propose_relationship(
        A, B, "beneficial_owner", span="A owns 6% of B", accession="a1",
        document_name="d1", extraction_method="structured", confidence=1.0,
        deterministic=True, relationship_id="rel:det")
    assert rel.status == "verified"
    assert rel.revisions[-1].actor == "deterministic"


def test_revise_expired_and_guards() -> None:
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

def test_store_roundtrip_and_pit(tmp_path: Path) -> None:
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
    def _rev_order(row: dict[str, object]) -> int:
        return int(str(row["revision_id"]).rsplit(":r", 1)[1])

    revs.sort(key=_rev_order)
    assert [r["new_status"] for r in revs] == ["candidate", "candidate", "verified"]
    assert sec_store.query_relationship_evidence(
        "rel:store", as_of="2024-01-15", root=tmp_path)[0]["accession"] == "a1"


def test_search_groups_by_type_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        _empty_text_hits)
    out = search_sec_relationships(A, data_root=tmp_path)
    assert out["ciks"] == ("1",)
    groups = _as_dict(out["groups"])
    assert groups["beneficial_owner"]["verified"]
    assert groups["supplier_of"]["verified"]
    assert groups["customer_of"]["candidate"]
    # Mentions never flatten into verified links.
    for rtype, by_status in groups.items():
        assert "observed" not in by_status or rtype == "mention"
    backends = {a["backend"] for a in _as_seq(out["attempts"])}
    assert {"local-typed", "local-workflow", "local-mentions",
            "efts-mentions"} <= backends
    filtered = search_sec_relationships(
        A, relationship_types=["supplier_of"], data_root=tmp_path)
    assert set(_as_dict(filtered["groups"])) == {"supplier_of"}

# --- Phase 9: deterministic PIT walk-forward type evaluation ---
#
# Exponential price paths make forward excess date-independent, so the
# expected 1/5/20-day metrics have closed forms asserted below exactly.
DAY0 = date(2024, 1, 1)


def _day(i: int) -> str:
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


def _wf_instances(flip: bool = False):
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


def test_walkforward_metrics_and_activate(tmp_path: Path) -> None:
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["decision"] == "activate"
    assert out["new_state"] == "active"
    assert out["total_pit_safe"] == 120
    assert out["rows_written"] == 2
    assert len(str(out["inputs_hash"])) == 64
    for window in _as_seq(out["windows"]):
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


def test_pit_leak_blocks_promotion(tmp_path: Path) -> None:
    insts = _wf_instances()
    leaked = next(i for i in insts if str(i["instance_id"]).startswith("g-"))
    pred_date = leaked["prediction_date"]
    assert isinstance(pred_date, str)
    leaked["evidence_known_at"] = _day(CAL.index(pred_date) + 1)
    out = disc.evaluate_and_persist_type(
        "Supplier Of", insts, observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["total_pit_safe"] == 119
    assert sum(w["pit_violations"] for w in _as_seq(out["windows"])) == 1
    assert out["decision"] == "no_change"
    assert out["new_state"] == "unevaluated"


def test_two_below_baseline_windows_demote_with_history(tmp_path: Path) -> None:
    disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(flip=True), observations=_prices(),
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert all(w["below_baseline"] for w in _as_seq(out["windows"]))
    assert out["decision"] == "demote"
    assert out["new_state"] == "demoted"
    rows = sec_store.query_relationship_type_evaluations(
        "supplier_of", root=tmp_path)
    assert len(rows) == 4  # prior active states retained alongside demotions
    assert {r["decision"] for r in rows} == {"activate", "demote"}


def test_missing_market_marks_incomplete_and_leaves_type(tmp_path: Path) -> None:
    out = disc.evaluate_and_persist_type(
        "Supplier Of", _wf_instances(), observations=None,
        benchmark=BENCH, windows=[WA, WB], data_root=tmp_path)
    assert out["decision"] == "incomplete"
    assert all(not w["complete"] for w in _as_seq(out["windows"]))
    assert out["new_state"] == "unevaluated"
    state, _ = sec_store.latest_type_state("supplier_of", root=tmp_path)
    assert state == "unevaluated"


def test_human_decision_superseded_by_later_evaluation(tmp_path: Path) -> None:
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
        f"supersedes human {row['evaluation_id']}" in str(r["reason"] or "")
        for r in demote)


def test_ontology_boost_orders_but_never_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        _empty_text_hits)
    out = search_sec_relationships(A, data_root=tmp_path)
    assert set(_as_dict(out["groups"])) == {"supplier_of", "customer_of"}
    assert list(_as_dict(out["groups"])) == ["supplier_of", "customer_of"]


def test_relationship_search_limit_bounds_typed_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, object]] = []

    def _row(i: int) -> dict[str, str]:
        return {"filer_cik": "1234567", "subject_cik": "1234567",
                "accession": f"ACC-{i}", "document_name": "primary",
                "known_at": "2024-01-10T00:00:00Z"}

    def _saturated(query_fn_name: str):
        def _fake(*args: object, **kwargs: object) -> list[dict[str, str]]:
            seen.append((query_fn_name, kwargs.get("limit")))
            if query_fn_name == "beneficial" and not any(
                    s[0] == "beneficial" for s in seen[:-1]):
                return [_row(i) for i in range(51)]
            return []
        return _fake

    import app.sec.store as sec_store_mod
    monkeypatch.setattr(sec_store_mod, "query_beneficial_ownership",
                        _saturated("beneficial"))
    monkeypatch.setattr(sec_store_mod, "query_insider_transactions",
                        _saturated("insider"))
    monkeypatch.setattr(sec_store_mod, "query_13f_holdings",
                        _saturated("holdings"))
    monkeypatch.setattr(sec_store_mod, "query_transactions",
                        _saturated("transactions"))
    monkeypatch.setattr(sec_store_mod, "query_offerings",
                        _saturated("offerings"))
    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence",
                        _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_relationship_revisions",
                        _empty_rows)
    monkeypatch.setattr(sec_store_mod, "search_document_text",
                        _empty_rows)
    monkeypatch.setattr("app.sec.client.search_sec_filings",
                        _empty_text_hits)
    monkeypatch.setattr("app.storage.duckdb.query", _empty_rows)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)

    out = search_sec_relationships("1234567", limit=50, exhaustive=False, data_root=tmp_path)
    assert seen
    assert all(isinstance(limit, int) and limit <= 51 for _, limit in seen)
    typed = next(a for a in _as_seq(out["attempts"]) if a["backend"] == "local-typed")
    assert typed["status"] == "partial"

    seen.clear()
    search_sec_relationships("1234567", limit=50, exhaustive=True,
                             data_root=tmp_path)
    assert any(limit == disc._LOCAL_EXHAUSTIVE_GUARD for _, limit in seen)


def test_relationship_search_exhaustive_propagates_guard_and_bounds_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def _typed_row(i: int) -> dict[str, str]:
        return {"filer_cik": "1234567", "subject_cik": "1234567",
                "accession": f"TACC-{i}", "document_name": "primary",
                "known_at": "2024-01-10T00:00:00Z"}

    def _track(name: str, rows: list[dict[str, str]]):
        def _fake(*args: object, **kwargs: object) -> list[dict[str, str]]:
            received[name] = kwargs.get("limit")
            return list(rows)
        return _fake

    import app.sec.store as sec_store_mod
    typed_rows = [_typed_row(i) for i in range(5)]
    monkeypatch.setattr(sec_store_mod, "query_beneficial_ownership", _track("typed", typed_rows))
    monkeypatch.setattr(sec_store_mod, "query_insider_transactions", _track("typed", typed_rows))
    monkeypatch.setattr(sec_store_mod, "query_13f_holdings", _track("typed", typed_rows))
    monkeypatch.setattr(sec_store_mod, "query_transactions", _track("typed", typed_rows))
    monkeypatch.setattr(sec_store_mod, "query_offerings", _track("typed", typed_rows))
    ev_rows = [{"relationship_id": f"rel:e{i}", "relationship_type": "supplier_of",
                "accession": f"EACC-{i}"} for i in range(5)]
    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence", _track("workflow", ev_rows))
    monkeypatch.setattr(sec_store_mod, "query_relationship_revisions",
                        _fake_verified_revision)
    doc_rows = [{"accession": f"DACC-{i}", "document_name": "primary",
                 "text": f"mention text {i}", "known_at": "2024-01-10T00:00:00Z"}
                for i in range(5)]
    monkeypatch.setattr(sec_store_mod, "search_document_text", _track("local", doc_rows))
    hits = [SimpleNamespace(accession_no=f"FACC-{i}", matched_document="primary",
                            query="1234567") for i in range(5)]
    efts_limits: dict[str, object] = {}
    def _fake_efts(*args: object, **kwargs: object) -> SimpleNamespace:
        efts_limits["limit"] = kwargs.get("limit")
        return SimpleNamespace(text_hits=list(hits))
    monkeypatch.setattr("app.sec.client.search_sec_filings", _fake_efts)
    monkeypatch.setattr("app.storage.duckdb.query", _empty_rows)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)

    out = search_sec_relationships("1234567", limit=2, exhaustive=True, data_root=tmp_path)
    assert received["workflow"] == disc._LOCAL_EXHAUSTIVE_GUARD
    assert received["local"] == disc._LOCAL_EXHAUSTIVE_GUARD
    assert efts_limits["limit"] == disc._LOCAL_EXHAUSTIVE_GUARD
    assert received["typed"] == disc._LOCAL_EXHAUSTIVE_GUARD
    typed = _as_seq(out["typed"])
    relationships = _as_seq(out["relationships"])
    mentions = _as_seq(out["mentions"])
    assert len(typed) <= 2
    assert len(relationships) <= 2
    assert len(mentions) <= 2
    assert typed[0]["accession"] == "TACC-0"
    grouped = [e for g in _as_dict(out["groups"]).values() for v in g.values() for e in v]
    assert len(grouped) == len(typed) + len(relationships) + len(mentions)


def test_relationship_search_bounded_preserves_cheap_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    received = {}

    def _track(name: str):
        def _fake(*args: object, **kwargs: object) -> list[dict[str, object]]:
            received[name] = kwargs.get("limit")
            return []
        return _fake

    import app.sec.store as sec_store_mod
    monkeypatch.setattr(sec_store_mod, "query_beneficial_ownership", _track("typed"))
    monkeypatch.setattr(sec_store_mod, "query_insider_transactions", _track("typed"))
    monkeypatch.setattr(sec_store_mod, "query_13f_holdings", _track("typed"))
    monkeypatch.setattr(sec_store_mod, "query_transactions", _track("typed"))
    monkeypatch.setattr(sec_store_mod, "query_offerings", _track("typed"))
    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence", _track("workflow"))
    monkeypatch.setattr(sec_store_mod, "query_relationship_revisions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "search_document_text", _track("local"))
    efts_limits = {}
    def _fake_efts(*args: object, **kwargs: object) -> SimpleNamespace:
        efts_limits["limit"] = kwargs.get("limit")
        return SimpleNamespace(text_hits=[])
    monkeypatch.setattr("app.sec.client.search_sec_filings", _fake_efts)
    monkeypatch.setattr("app.storage.duckdb.query", _empty_rows)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)

    search_sec_relationships("1234567", limit=7, exhaustive=False, data_root=tmp_path)
    # typed routes probe at limit+1 via the unchanged _fetch_typed path;
    # workflow/local/EFTS pass their cap straight through.
    assert received["typed"] == 8
    assert received["workflow"] == 7
    assert received["local"] == min(7, 20)
    assert efts_limits["limit"] == min(7, 20)


def test_relationship_search_guard_boundary_marks_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.store as sec_store_mod
    guard = disc._LOCAL_EXHAUSTIVE_GUARD
    monkeypatch.setattr(sec_store_mod, "query_beneficial_ownership", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_insider_transactions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_13f_holdings", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_transactions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_offerings", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_relationship_revisions",
                        _fake_verified_revision)
    monkeypatch.setattr(sec_store_mod, "search_document_text", _empty_rows)
    monkeypatch.setattr("app.sec.client.search_sec_filings",
                        _empty_text_hits)
    monkeypatch.setattr("app.storage.duckdb.query", _empty_rows)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)

    def _guard_rows(n: int):
        def _fake(*args: object, **kwargs: object) -> list[dict[str, str]]:
            return [{"relationship_id": "rel:g",
                    "relationship_type": "supplier_of"}] * n
        return _fake

    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence",
                        _guard_rows(guard))
    out = search_sec_relationships("1234567", exhaustive=True, data_root=tmp_path)
    attempt = next(a for a in _as_seq(out["attempts"]) if a["backend"] == "local-workflow")
    assert attempt["status"] == "partial"

    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence",
                        _guard_rows(guard - 1))
    out = search_sec_relationships("1234567", exhaustive=True, data_root=tmp_path)
    attempt = next(a for a in _as_seq(out["attempts"]) if a["backend"] == "local-workflow")
    assert attempt["status"] == "complete"

def _stub_quiet_routes(monkeypatch: pytest.MonkeyPatch):
    import app.sec.store as sec_store_mod
    monkeypatch.setattr(sec_store_mod, "query_beneficial_ownership", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_insider_transactions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_13f_holdings", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_transactions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_offerings", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_relationship_evidence", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "query_relationship_revisions", _empty_rows)
    monkeypatch.setattr(sec_store_mod, "search_document_text", _empty_rows)
    monkeypatch.setattr("app.sec.client.search_sec_filings",
                        _empty_text_hits)
    monkeypatch.setattr("app.storage.duckdb.query", _empty_rows)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)




def test_relationship_search_efts_partial_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_quiet_routes(monkeypatch)
    hits = [SimpleNamespace(accession_no="FACC-1", matched_document="primary", query="1234567")]
    def _fake_efts_partial(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            text_hits=list(hits),
            coverage=SimpleNamespace(status="partial"),
            warnings=("efts capped",), errors=())

    monkeypatch.setattr("app.sec.client.search_sec_filings", _fake_efts_partial)
    out = search_sec_relationships("1234567", limit=50, exhaustive=False, data_root=tmp_path)
    attempt = next(a for a in _as_seq(out["attempts"]) if a["backend"] == "efts-mentions")
    assert attempt["status"] == "partial"
    assert "efts capped" in _as_seq(out["warnings"])


def test_relationship_meta_resolves_single_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.models import EntityCandidate
    _stub_quiet_routes(monkeypatch)
    cand = EntityCandidate(cik=320193, name="Meta Platforms Inc", tickers=("META",),
                           exchange=None, match_source="exact-ticker", match_score=1.0,
                           match_type="exact_ticker", verification_status="verified",
                           entity_id="sec:cik:0000320193")
    def _fake_find(query: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(entities=(cand,), coverage=SimpleNamespace(status="complete"),
                               errors=())

    monkeypatch.setattr(disc, "find_sec_entities", _fake_find)
    import app.sec.store as _s

    def _fake_meta_rows(*args: object, **kwargs: object) -> list[dict[str, str]]:
        return [{"accession": "ACC-M", "subject_cik": "320193",
                "filer_cik": "123", "subject_name": "Meta",
                "filer_name": "Owner", "known_at": "2024-01-01T00:00:00Z"}]

    monkeypatch.setattr(_s, "query_beneficial_ownership", _fake_meta_rows)
    out = search_sec_relationships("META", data_root=tmp_path)
    assert out["ciks"] == ("320193",)
    assert out["typed"]


def test_relationship_ambiguous_stops_with_no_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.models import EntityCandidate
    _stub_quiet_routes(monkeypatch)
    cands = tuple(EntityCandidate(cik=i, name=f"Amb {i}", tickers=(), exchange=None,
                                  match_source="company-search", match_score=0.9,
                                  match_type="normalized", verification_status="ambiguous",
                                  entity_id=None) for i in (1, 2))
    def _fake_find_ambiguous(query: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(entities=cands,
                               coverage=SimpleNamespace(status="complete"),
                               errors=())

    monkeypatch.setattr(disc, "find_sec_entities", _fake_find_ambiguous)
    out = search_sec_relationships("Ambiguous Co", data_root=tmp_path)
    assert out["typed"] == [] and out["relationships"] == [] and out["mentions"] == []
    assert any(a["backend"] == "entity-resolution" and a["status"] == "ambiguous" for a in _as_seq(out["attempts"]))


def test_inverse_returns_manager_with_issuer_entity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.storage import parquet as _pq
    import app.sec.store as _smod
    monkeypatch.setattr(_smod, "query_beneficial_ownership", _empty_rows)
    monkeypatch.setattr(_smod, "query_insider_transactions", _empty_rows)
    monkeypatch.setattr(_smod, "query_13f_holdings", _empty_rows)
    monkeypatch.setattr(_smod, "query_transactions", _empty_rows)
    monkeypatch.setattr(_smod, "query_offerings", _empty_rows)
    monkeypatch.setattr(_smod, "query_relationship_evidence", _empty_rows)
    monkeypatch.setattr(_smod, "query_relationship_revisions", _empty_rows)
    monkeypatch.setattr(_smod, "search_document_text", _empty_rows)
    monkeypatch.setattr("app.sec.client.search_sec_filings",
                        _empty_text_hits)
    monkeypatch.setattr(disc, "get_type_states", _empty_map)
    now = "2024-06-01T00:00:00Z"
    _pq.write_rows("entities", [{"entity_id": "sec:cik:0000320193", "name": "Apple Inc.",
                                 "entity_type": "company", "sic": None, "source": "sec-submissions",
                                 "known_at": "2024-01-01T00:00:00Z", "retrieved_at": now,
                                 "content_hash": None, "parser_version": "1"}], root=tmp_path / "parquet")
    from app.sec import insider as _ins
    h = _ins._holding_row_to_record({"Cusip": "037833100", "Issuer": "Apple Inc.",
                                     "ReportPeriod": "2024-03-31"},
                                    manager_name="Berkshire", manager_cik="1067983",
                                    accession_no="ACC-INV", report_period="2024-03-31",
                                    filed_at="2024-05-15", document_name="infotable.xml",
                                    known_at="2024-05-15T00:00:00Z", source_url=None,
                                    source_row=1)
    _ins.observe_13f_security(h, raw_archive_path="/tmp/p", content_hash="hi",
                              retrieved_at=now, root=tmp_path)
    import app.sec.store as _s
    _s.store_13f_holding(h.to_dict(), root=tmp_path)
    def _fake_find_unexpected(query: str, **kwargs: object) -> NoReturn:
        raise RuntimeError("should use direct CIK")

    def _fake_verify(cik: int | str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(cik=int(str(cik).strip()), name="Mgr",
                               verification_status="verified")

    monkeypatch.setattr(disc, "find_sec_entities", _fake_find_unexpected)
    monkeypatch.setattr(disc, "verify_sec_entity", _fake_verify)
    out = search_sec_relationships("sec:cik:0000320193", data_root=tmp_path)
    rows = [r for r in _as_seq(out["typed"]) if r.get("relationship_type") == "holding_manager"]
    assert rows and rows[0]["to_entity_id"] == "sec:cik:0000320193"
    assert out["managers"]
    # Unmapped issuer stays partial.
    out2 = search_sec_relationships("sec:cik:0000000009", data_root=tmp_path)
    inv_attempts = [a for a in _as_seq(out2["attempts"]) if a["backend"] == "local-13f-inverse"]
    assert inv_attempts and inv_attempts[0]["status"] == "partial"
    # Query failure records failed.
    def _fake_holdings_down(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("db down")

    monkeypatch.setattr(_s, "query_13f_holdings_for_issuer", _fake_holdings_down)
    out3 = search_sec_relationships("sec:cik:0000320193", data_root=tmp_path)
    assert any(a["backend"] == "local-13f-inverse" and a["status"] == "failed" for a in _as_seq(out3["attempts"]))


def test_hydrate_transaction_and_offering_real_normalizers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.archive as sec_archive
    import app.sec.documents as sec_docs
    import app.sec.store as sec_store_mod
    def _fake_doc(accession_no: str, document_name: str | None = None, **kwargs: object) -> dict[str, object]:
        return {"text": "Merger with Target Co for $10 per share",
                "document_name": "primary.htm", "url": "http://x"}

    def _fake_archive(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(payload_path="/tmp/p",
                               retrieved_at="2024-01-02T00:00:00Z",
                               sha256="h")

    def _fake_edgar_filing(accession_no: str) -> SimpleNamespace:
        return SimpleNamespace(obj=lambda: None)

    monkeypatch.setattr(sec_docs, "get_sec_document", _fake_doc)
    monkeypatch.setattr(sec_archive, "archive_sec_document", _fake_archive)
    monkeypatch.setattr(sec_docs, "get_by_accession_number", _fake_edgar_filing)
    import app.sec.offerings as _off

    def _fake_terms(accession_no: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr(_off, "load_terms", _fake_terms)
    f4 = Filing(form="S-4", accession_no="ACC-S4", filed_at="2024-02-01",
                filer_cik=111, filer_name="Acquirer Inc", accepted_at=None,
                known_at="2024-02-01T00:00:00Z", report_period=None,
                primary_document="primary.htm", is_amendment=False,
                amendment_of=None, source="http://x",
                subject_cik=222, subject_name="Target Co")
    n, ok, err = disc._hydrate_relationship_filing(f4, data_root=tmp_path)
    assert err is None and n >= 1
    assert sec_store_mod.query_transactions(accession="ACC-S4", root=tmp_path)
    f3 = Filing(form="S-3", accession_no="ACC-S3", filed_at="2024-03-01",
                filer_cik=333, filer_name="Issuer Inc", accepted_at=None,
                known_at="2024-03-01T00:00:00Z", report_period=None,
                primary_document="primary.htm", is_amendment=False,
                amendment_of=None, source="http://x")
    n, ok, err = disc._hydrate_relationship_filing(f3, data_root=tmp_path)
    assert err is None and sec_store_mod.query_offerings(accession="ACC-S3", root=tmp_path)
    f424 = Filing(form="424B5", accession_no="ACC-424", filed_at="2024-04-01",
                  filer_cik=333, filer_name="Issuer Inc", accepted_at=None,
                  known_at="2024-04-01T00:00:00Z", report_period=None,
                  primary_document="primary.htm", is_amendment=False,
                  amendment_of=None, source="http://x")
    n, ok, err = disc._hydrate_relationship_filing(f424, data_root=tmp_path)
    assert err is None and sec_store_mod.query_offerings(accession="ACC-424", root=tmp_path)


def test_warehouse_batch_includes_amendments() -> None:
    seen: dict[str, object] = {}
    def _fake_query(*, forms: list[str] | None = None, start_date: str | None = None, end_date: str | None = None, limit: int | None = None, root: object = None) -> list[dict[str, object]]:
        seen["forms"] = forms
        seen["limit"] = limit
        return [
            {"accession": "ACC-1", "form": "4", "filer_cik": "123", "filer_name": "A",
             "filed_at": "2024-02-15T00:00:00Z", "known_at": "2024-02-15T00:00:00Z"},
            {"accession": "ACC-2", "form": "4/A", "filer_cik": "123", "filer_name": "A",
             "filed_at": "2024-02-20T00:00:00Z", "known_at": "2024-02-20T00:00:00Z"},
        ]
    store = ModuleType("fake_sec_store")
    setattr(store, "query_filings", _fake_query)
    rows, exhausted, error = disc._warehouse_batch(store, "4", "2024-01-01", "2024-03-31")
    assert error is None and exhausted is True and len(rows) == 2
    assert seen["forms"] == ["4", "4/A"] and seen["limit"] is None
    assert {f.form for f in rows} == {"4", "4/A"}


def test_warehouse_failure_marks_partial_and_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.store as sec_store_mod
    source, form = disc.BACKFILL_SOURCE, "10-K"
    assert not disc._needs_typed_hydration(form)
    qs, qe = disc._quarter_dates(2024, 1)
    partition = disc._partition_for_quarter(2024, 1)
    key = f"{form}/{partition}"
    sec_store_mod.store_checkpoint("sec-backfill", source, key, "complete", root=tmp_path)
    job_id = sec_store_mod.enqueue_backfill_job(source, form, qs, qe, root=tmp_path)
    job: dict[str, object] = {"id": job_id, "source": source, "form": form,
           "start_date": qs, "end_date": qe, "batch_size": 50}
    def _boom(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("warehouse down")
    monkeypatch.setattr(sec_store_mod, "query_filings", _boom)
    assert disc.run_backfill_job(job, data_root=tmp_path) is False
    from app.storage import duckdb as _duck
    ck_rows = _duck.query(
        "SELECT * FROM ingestion_checkpoints WHERE pipeline = ? AND source = ? AND key = ?",
        ["sec-backfill", source, key], data_root=tmp_path)
    assert any((r or {}).get("status") in ("partial", "failed") for r in ck_rows)
    cov = sec_store_mod.query_coverage(source=source, form=form, date_partition=partition,
                                       root=tmp_path)
    assert cov and all(r.get("status") != "complete" for r in cov)

def test_hydrate_amendment_forms_use_base_parsers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.archive as sec_archive
    import app.sec.documents as sec_docs
    import app.sec.insider as sec_insider
    import app.sec.ownership as sec_own
    import app.sec.store as sec_store_mod
    def _fake_doc(accession_no: str, document_name: str | None = None, **kwargs: object) -> dict[str, object]:
        return {"text": "t", "document_name": "primary",
                "url": "http://x"}

    def _fake_archive(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            payload_path="/tmp/p", retrieved_at="r", sha256="h")

    def _fake_load_ownership(accession_no: str) -> object:
        return object()

    monkeypatch.setattr(sec_docs, "get_sec_document", _fake_doc)
    monkeypatch.setattr(sec_archive, "archive_sec_document", _fake_archive)
    monkeypatch.setattr(sec_insider, "load_ownership", _fake_load_ownership)
    seen4: dict[str, object] = {}

    def _fake_ownership_records(obj: object, **kwargs: object) -> list[SimpleNamespace]:
        seen4.update(form=kwargs.get("form"))
        return [SimpleNamespace(to_dict=lambda: {"x": 1})]

    w4: list[dict[str, object]] = []

    def _fake_store_insider(d: dict[str, object], **kwargs: object) -> int:
        return (w4.append(d), 1)[1]

    monkeypatch.setattr(sec_insider, "normalize_ownership_filing",
                        _fake_ownership_records)
    monkeypatch.setattr(sec_store_mod, "store_insider_transaction",
                        _fake_store_insider)
    f4 = Filing(form="4/A", accession_no="ACC-4A", filed_at="2024-01-01",
                filer_cik=123, filer_name="F", accepted_at=None,
                known_at="2024-01-01", report_period=None,
                primary_document="primary", is_amendment=True,
                amendment_of=None, source="http://x")
    n, _ok, err = disc._hydrate_relationship_filing(f4, data_root=tmp_path)
    assert "unsupported form" not in (err or "") and n == 1 and w4 and seen4["form"] == "4/A"
    def _fake_load_schedule(accession_no: str) -> object:
        return object()

    monkeypatch.setattr(sec_own, "load_schedule", _fake_load_schedule)
    seen13: dict[str, object] = {}

    def _fake_schedule_records(schedule: object, **kwargs: object) -> list[SimpleNamespace]:
        seen13.update(form=kwargs.get("form"))
        return [SimpleNamespace(to_dict=lambda: {"x": 1})]

    wb: list[dict[str, object]] = []

    def _fake_store_beneficial(d: dict[str, object], **kwargs: object) -> int:
        return (wb.append(d), 1)[1]

    monkeypatch.setattr(sec_own, "normalize_schedule",
                        _fake_schedule_records)
    monkeypatch.setattr(sec_store_mod, "store_beneficial_ownership",
                        _fake_store_beneficial)
    f13 = Filing(form="SC 13D/A", accession_no="ACC-13A", filed_at="2024-01-01",
                 filer_cik=123, filer_name="F", accepted_at=None,
                 known_at="2024-01-01", report_period=None,
                 primary_document="primary", is_amendment=True,
                 amendment_of=None, source="http://x")
    n, _ok, err = disc._hydrate_relationship_filing(f13, data_root=tmp_path)
    assert "unsupported form" not in (err or "") and n == 1 and wb and seen13["form"] == "SC 13D/A"
    def _fake_edgar_13f(accession_no: str) -> SimpleNamespace:
        return SimpleNamespace(obj=lambda: SimpleNamespace(infotable=[{"a": 1}]))

    monkeypatch.setattr(sec_docs, "get_by_accession_number", _fake_edgar_13f)
    seenhf: dict[str, object] = {}

    def _fake_holdings_records(table: object, **kwargs: object) -> list[SimpleNamespace]:
        seenhf.update(form=kwargs.get("form"))
        return [SimpleNamespace(to_dict=lambda: {"y": 2})]

    wh: list[dict[str, object]] = []

    def _fake_store_holding(d: dict[str, object], **kwargs: object) -> int:
        return (wh.append(d), 1)[1]

    monkeypatch.setattr(sec_insider, "normalize_13f_holdings",
                        _fake_holdings_records)
    monkeypatch.setattr(sec_store_mod, "store_13f_holding",
                        _fake_store_holding)
    fh = Filing(form="13F-HR/A", accession_no="ACC-HA", filed_at="2024-01-01",
                filer_cik=123, filer_name="F", accepted_at=None,
                known_at="2024-01-01", report_period="2024-01-01",
                primary_document="primary", is_amendment=True,
                amendment_of=None, source="http://x")
    n, _ok, err = disc._hydrate_relationship_filing(fh, data_root=tmp_path)
    assert "unsupported form" not in (err or "") and n == 1 and wh and seenhf["form"] == "13F-HR/A"
