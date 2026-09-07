"""Filesystem/model round trip for the thesis repository (no network)."""

import threading
from pathlib import Path

import pytest
import yaml

from app.thesis.models import Thesis
from app.thesis.repository import ThesisRepository
from app.thesis.runner import ThesisResearchResult, _apply_answered
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml


def _repo(tmp_path) -> ThesisRepository:
    return ThesisRepository(tmp_path / "theses")


def test_round_trip_create_load_list_update(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA demand stays strong", scope="NVDA", claims=["demand holds"])
    assert r.load_thesis(t.thesis_id).slug == t.slug
    assert r.load_thesis(t.slug).thesis_id == t.thesis_id
    assert [x.thesis_id for x in r.list_theses()] == [t.thesis_id]
    updated = r.update_thesis(t.thesis_id, scope="NVDA datacenter")
    assert updated.scope == "NVDA datacenter" and updated.thesis_id == t.thesis_id


def test_zero_and_multiple_expressions(tmp_path):
    r = _repo(tmp_path)
    t0 = r.create_thesis("undecided thesis", scope="NVDA", claims=["c"])
    assert r.load_thesis(t0.thesis_id).expressions == ()
    t1 = r.create_thesis(
        "two ways to play NVDA",
        scope="NVDA",
        claims=["c"],
        expressions=[
            {"intent": "bearish", "instrument": "option", "direction": "long", "structure": "long puts"},
            {"intent": "bullish", "instrument": "equity", "direction": "long", "structure": "equity"},
        ],
    )
    got = r.load_thesis(t1.thesis_id).expressions
    assert len(got) == 2 and {e.instrument for e in got} == {"option", "equity"}


def test_unknown_open_vocab_structure_retained_verbatim(tmp_path):
    r = _repo(tmp_path)
    weird = "diagonalized quantum butterfly 7:11!!"
    t = r.create_thesis("weird structure", scope="NVDA", claims=["c"],
                        expressions=[{"structure": weird}])
    assert r.load_thesis(t.thesis_id).expressions[0].structure == weird


def test_unknown_literals_retained(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("unknown thesis", claims=["c"])
    loaded = r.load_thesis(t.thesis_id)
    assert loaded.scope == "unknown"
    assert loaded.expressions == () and loaded.unknowns == ()


def test_thesis_vs_expression_assessments_stored_separately(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    r.apply_research_result(t.thesis_id, {"state": {
        "thesis_id": t.thesis_id, "assessment": "supported",
        "claim_assessments": {"c1": "supported"},
        "expression_assessments": {"e1": "flagged"}}}, "run:x")
    state = r.load_state(t.thesis_id)
    assert state.assessment == "supported"
    assert state.claim_assessments == {"c1": "supported"}
    assert state.expression_assessments == {"e1": "flagged"}
    assert r.load_thesis(t.thesis_id).claims[0].status == "unvalidated"


def test_malformed_and_schema_mismatch_yaml_rejected(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    thesis_file = tmp_path / "theses" / t.slug / "thesis.yaml"
    thesis_file.write_text("{ unclosed: [,,,\n", encoding="utf-8")
    with pytest.raises(ValueError):
        r.load_thesis(t.thesis_id)
    thesis_file.write_text(yaml.safe_dump({"schema_version": 99, "thesis_id": t.thesis_id}),
                           encoding="utf-8")
    with pytest.raises(ValueError):
        r.load_thesis(t.thesis_id)


def test_atomic_write_failure_leaves_old_valid_data(tmp_path, monkeypatch):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    thesis_file = tmp_path / "theses" / t.slug / "thesis.yaml"
    before = thesis_file.read_text(encoding="utf-8")
    monkeypatch.setattr(yaml, "safe_dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        r.update_thesis(t.thesis_id, scope="CHANGED")
    assert thesis_file.read_text(encoding="utf-8") == before
    assert r.load_thesis(t.thesis_id).scope == "NVDA"


def test_traversal_and_symlink_escape_rejected(tmp_path):
    r = _repo(tmp_path)
    r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    with pytest.raises(ValueError):
        atomic_write_yaml(tmp_path / "theses" / ".." / "escape.yaml",
                           {"schema_version": 1}, tmp_path / "theses")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "theses" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        atomic_write_yaml(tmp_path / "theses" / "link" / "evil.yaml",
                           {"schema_version": 1}, tmp_path / "theses")
    assert not (outside / "evil.yaml").exists()


def test_concurrent_writes_serialized(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("concurrent thesis", scope="NVDA", claims=["c"])
    threads = [threading.Thread(target=r.append_journal_entry,
                                args=(t.thesis_id, {"entry_id": f"entry:{i}", "title": f"t{i}", "body": "b"}))
               for i in range(10)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert len(list((tmp_path / "theses" / t.slug / "journal").glob("*.md"))) == 10


def test_duplicate_ids_rejected(tmp_path):
    with pytest.raises(ValueError):
        Thesis.from_dict({"schema_version": 1, "thesis_id": "thesis:x", "slug": "s", "status": "active",
                          "created_at": "", "updated_at": "", "user_thesis": "u", "scope": "unknown",
                          "claims": [{"claim_id": "dup", "statement": "a", "status": "unvalidated"},
                                     {"claim_id": "dup", "statement": "b", "status": "unvalidated"}],
                          "assumptions": [], "invalidators": [], "unknowns": [],
                          "expressions": [], "requirements": []}, "<t>")
    r = _repo(tmp_path)
    r.create_thesis("t", scope="NVDA", claims=["c"])
    with pytest.raises(ValueError):
        Thesis.from_dict({"schema_version": 1, "thesis_id": "thesis:y", "slug": "s", "status": "active",
                          "created_at": "", "updated_at": "", "user_thesis": "u", "scope": "unknown",
                          "claims": [], "assumptions": [], "invalidators": [], "unknowns": [],
                          "expressions": [{"expression_id": "dup", "structure": "a"},
                                          {"expression_id": "dup", "structure": "b"}],
                          "requirements": []}, "<t>")


def test_cross_thesis_refs_rejected(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    with pytest.raises(ValueError):
        r.create_trigger(t.thesis_id, claim_ids=["claim:absent"], canonical_refs=["x"], summary="s")
    with pytest.raises(ValueError):
        r.apply_research_result(t.thesis_id, {"watch_add": [{
            "rule_id": "rule:x", "rule_type": "new_external_evidence",
            "claim_ids": ["claim:absent"]}]}, "run:x")


def test_pause_resume_close_transitions_enforced(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    assert r.pause_thesis(t.thesis_id).status == "paused"
    assert r.resume_thesis(t.thesis_id).status == "active"
    assert r.close_thesis(t.thesis_id).status == "closed"
    with pytest.raises(ValueError):
        r.pause_thesis(t.thesis_id)
    with pytest.raises(ValueError):
        r.resume_thesis(t.thesis_id)
    with pytest.raises(ValueError):
        r.create_trigger(t.thesis_id, canonical_refs=["x"], summary="s")


def test_quarantine_lists_corrupt_child_while_healthy_ids_proceed(tmp_path):
    r = _repo(tmp_path)
    a = r.create_thesis("healthy thesis", scope="NVDA", claims=["c"])
    b = r.create_thesis("doomed thesis", scope="NVDA", claims=["c"])
    bfile = tmp_path / "theses" / b.slug / "thesis.yaml"
    bfile.write_text("{ unclosed: [,,,\n", encoding="utf-8")
    assert [x.thesis_id for x in r.list_theses()] == [a.thesis_id]
    assert r.load_thesis(a.thesis_id).slug == a.slug
    assert r.load_thesis(a.slug).thesis_id == a.thesis_id
    with pytest.raises(ValueError):
        r.load_thesis(b.slug)
    with pytest.raises(ValueError, match="quarantined"):
        r.load_thesis(b.thesis_id)
    q = r.list_quarantine()
    assert set(q) == {b.slug} and q[b.slug]
    bfile.write_text(yaml.safe_dump({"schema_version": 99, "thesis_id": b.thesis_id}),
                     encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        r.load_thesis(b.thesis_id)


def test_duplicate_thesis_ids_stay_loud(tmp_path):
    import shutil

    r = _repo(tmp_path)
    a = r.create_thesis("first thesis", scope="NVDA", claims=["c"])
    shutil.copytree(tmp_path / "theses" / a.slug, tmp_path / "theses" / "clone-dir")
    with pytest.raises(ValueError, match="duplicate thesis ID"):
        r.list_theses()


def test_answer_questions_marks_answered(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("q thesis", scope="NVDA", claims=["c"])
    r.answer_questions(t.thesis_id, [])  # empty is a no-op
    r.apply_research_result(t.thesis_id, {"questions_add": [{"question_id": "q:1", "text": "why?"}]},
                            "run:x")
    r.answer_questions(t.thesis_id, [{"question_id": "q:1", "answer": "because"}])
    (q,) = r.load_questions(t.thesis_id)
    assert (q.status, q.answer) == ("answered", "because")
    r.apply_research_result(t.thesis_id, {"questions_add": [{"question_id": "q:2", "text": "when?"}]},
                            "run:x")
    _apply_answered(r, t.thesis_id, ({"question_id": "q:2", "answer": "soon"},))
    assert {q.question_id: (q.status, q.answer) for q in r.load_questions(t.thesis_id)} == {
        "q:1": ("answered", "because"), "q:2": ("answered", "soon")}
    with pytest.raises(ValueError):
        r.answer_questions(t.thesis_id, [{"question_id": "q:absent", "answer": "x"}])


def test_normalize_watch_heals_and_load_watch_rules(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("w thesis", scope="NVDA", claims=["c"])
    cid = r.load_thesis(t.thesis_id).claims[0].claim_id
    watch = tmp_path / "theses" / t.slug / "watch.yaml"
    raw = load_raw_yaml(watch)
    raw["rules"].append({"rule_id": "rule:odd", "rule_type": "price_moon",
                         "enabled": True, "support_status": "supported",
                         "support_reason": "", "claim_ids": [cid], "expression_ids": []})
    atomic_write_yaml(watch, raw, tmp_path / "theses")
    r.normalize_watch(t.thesis_id)
    (rule,) = r.load_watch_rules(t.thesis_id)
    assert rule.rule_id == "rule:odd" and rule.enabled is False
    assert rule.support_status == "unsupported" and rule.support_reason

def _evidence_files(repo: ThesisRepository, thesis_id: str) -> dict:
    thesis = repo.load_thesis(thesis_id)
    evdir = repo.dir_for_thesis(thesis.thesis_id) / "evidence"
    out = {}
    for f in sorted(evdir.glob("*.yaml")):
        raw = load_raw_yaml(f)
        if isinstance(raw, dict) and raw.get("thesis_id") == thesis_id:
            out[raw.get("evidence_id")] = raw
    return out


def test_provenance_bound_rejects_forged_future_ref_but_keeps_visible(tmp_path):
    r = _repo(tmp_path)
    t = r.create_thesis("NVDA thesis", scope="NVDA", claims=["c"])
    cutoff = "2026-01-01T10:00:00+00:00"
    journal = {"entry_id": "journal:hist", "title": "t", "body": "b", "known_at": cutoff}
    r.apply_research_result(t.thesis_id, {"evidence_refs": [
        {"evidence_id": "ev:visible", "canonical_ref": "V", "summary": "v",
         "known_at": "2026-01-01T09:00:00+00:00"},
        {"evidence_id": "ev:future", "canonical_ref": "F", "summary": "f",
         "known_at": "2026-02-01T00:00:00+00:00"}]}, "run:seed")
    trig = r.create_trigger(t.thesis_id, canonical_refs=["V"], summary="s")
    with pytest.raises(ValueError, match="foreign canonical_ref"):
        r.apply_research_result(t.thesis_id, {"trigger_id": trig.trigger_id,
            "evidence_refs": [{"evidence_id": "ev:forged", "canonical_ref": "F",
                               "summary": "forged", "known_at": "2026-01-01T09:30:00+00:00"}],
            "journal_entry": dict(journal)}, "run:x")
    stored = _evidence_files(r, t.thesis_id)
    assert "ev:forged" not in stored and stored["ev:future"]["canonical_ref"] == "F"
    out = r.apply_research_result(t.thesis_id, {"trigger_id": trig.trigger_id,
        "evidence_refs": [{"evidence_id": "ev:ok", "canonical_ref": "V",
                           "summary": "v2", "known_at": cutoff}],
        "journal_entry": dict(journal, entry_id="journal:hist-ok")}, "run:y")
    assert out["evidence"] == 1
    stored = _evidence_files(r, t.thesis_id)
    assert stored["ev:ok"]["canonical_ref"] == "V"


def test_evidence_pit_compares_chronologically_not_lexically(tmp_path):
    cutoff = "2026-01-01T10:00:00+00:00"

    def check(known_at: str) -> None:
        ThesisResearchResult.from_dict(
            {"evidence_refs": [{"canonical_ref": "V", "summary": "v", "known_at": known_at}]},
            thesis_id="thesis:t", claim_ids=set(), expression_ids=set(),
            question_ids=set(), known_at=cutoff)
    check("2026-01-01T11:00:00+02:00")  # 09:00Z: lexically after, chronologically before
    check("2026-01-01T12:00:00+02:00")  # equal instant
    for bad in ("2026-01-01T10:30:00+00:00", "2026-01-01T09:30:00-01:00", "not-a-time", ""):
        with pytest.raises(ValueError):
            check(bad)
