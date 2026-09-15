"""Resid slice: branch coverage for backdated apply paths + snapshot helpers.

Targets (tests-only, no source edits):
  repository: _apply_backdated_locked, _backdated_side_effects,
      _backdated_section_folds, _asof_ids, _copy_section_list
  intake: _snapshot_coverage, _snapshot_ids
"""

from collections.abc import Mapping
from pathlib import Path
from typing import override

import pytest

from app.thesis.intake import _snapshot_coverage, _snapshot_ids
from app.thesis.models import SCHEMA_VERSION, JSONValue, ThesisStateSnapshot
from app.thesis.repository import (
    ThesisRepository,
    _ApplyFold,
    _ApplyInputs,
    _copy_section_list,
)

T0 = "2026-01-01T00:00:00+00:00"
MAR = "2026-03-01T00:00:00+00:00"
APR = "2026-04-01T00:00:00+00:00"
MAY = "2026-05-01T00:00:00+00:00"
JUN = "2026-06-01T00:00:00+00:00"


def _repo(tmp_path: Path) -> ThesisRepository:
    return ThesisRepository(tmp_path / "theses")


def _inputs(**over: object) -> _ApplyInputs:
    claim_updates: list[object] = []
    expression_updates: list[object] = []
    evidence_refs: list[object] = []
    questions_add: list[object] = []
    memories_add: list[object] = []
    watch_add: list[object] = []
    questions_answered: list[object] = []
    trigger_id = ""
    state: object = None
    journal_entry: Mapping[str, object] | None = None
    lists: dict[str, list[object]] = {
        "claim_updates": claim_updates, "expression_updates": expression_updates,
        "evidence_refs": evidence_refs, "questions_add": questions_add,
        "memories_add": memories_add, "watch_add": watch_add,
        "questions_answered": questions_answered,
    }
    for key, slot in lists.items():
        if key in over:
            value: object = over[key]
            slot.extend(value if isinstance(value, list) else [value])
    if "trigger_id" in over:
        raw_trigger: object = over["trigger_id"]
        if isinstance(raw_trigger, str):
            trigger_id = raw_trigger
    if "state" in over:
        state = over["state"]
    if "journal_entry" in over:
        raw_journal: object = over["journal_entry"]
        if isinstance(raw_journal, Mapping) or raw_journal is None:
            journal_entry = raw_journal
    return _ApplyInputs(claim_updates, expression_updates, trigger_id,
                        evidence_refs, state, questions_add,
                        memories_add, watch_add, questions_answered, journal_entry)


def _section(key: str, rows: list[object], tid: str) -> dict[str, JSONValue]:
    out: dict[str, JSONValue] = {"schema_version": SCHEMA_VERSION, "thesis_id": tid}
    rows_json: list[JSONValue] = [r if isinstance(r, (str, int, float, bool)) or r is None or isinstance(r, (list, dict)) else str(r) for r in rows]
    out[key] = rows_json
    return out


# -- intake._snapshot_ids ----------------------------------------------------

def test_snapshot_ids_filters_to_string_ids() -> None:
    rows = [{"claim_id": "c1"}, {"claim_id": 5}, {"other": "x"},
            "junk", None, {"claim_id": "c2"}]
    assert _snapshot_ids(rows, "claim_id") == ["c1", "c2"]


def test_snapshot_ids_non_list_is_empty() -> None:
    assert _snapshot_ids("nope", "claim_id") == []
    assert _snapshot_ids(None, "claim_id") == []
    not_rows: object = {"claim_id": "c1"}
    assert _snapshot_ids(not_rows, "claim_id") == []


# -- intake._snapshot_coverage ------------------------------------------------
def _state(thesis: dict[str, JSONValue]) -> ThesisStateSnapshot:
    return ThesisStateSnapshot(thesis_id="t1", version=1, effective_at=JUN,
                               recorded_at=JUN, reason="test", thesis=thesis)


def _thesis_rows(snap: ThesisStateSnapshot, key: str) -> list[JSONValue]:
    rows = snap.thesis.get(key, [])
    assert isinstance(rows, list)
    return rows


def _thesis_row(snap: ThesisStateSnapshot, section: str, key: str, i: int) -> dict[str, JSONValue]:
    rows = snap.thesis.get(key, []) if section == "thesis" else snap.questions.get(key, [])
    assert isinstance(rows, list)
    row = rows[i]
    assert isinstance(row, dict)
    return row


def _snap(thesis: dict[str, object], watch: object = None) -> ThesisStateSnapshot:
    thesis_json: dict[str, JSONValue] = {}
    for key, value in thesis.items():
        if isinstance(value, str):
            thesis_json[key] = value
        elif isinstance(value, list):
            items: list[JSONValue] = []
            for entry in value:
                if isinstance(entry, dict):
                    row: dict[str, JSONValue] = {}
                    for k, v in entry.items():
                        if isinstance(v, (str, int, float, bool)) or v is None:
                            row[k] = v
                        elif isinstance(v, (list, dict)):
                            row[k] = v
                    items.append(row)
            thesis_json[key] = items
    watch_json: dict[str, JSONValue] = watch if isinstance(watch, dict) else {}
    snap = ThesisStateSnapshot(thesis_id="t1", version=1, effective_at=JUN,
                               recorded_at=JUN, reason="test", thesis=thesis_json,
                               watch=watch_json)
    return snap


class _StubRepo(ThesisRepository):
    def __init__(self, snap: ThesisStateSnapshot) -> None:
        self._snap = snap
        self.seen: list[tuple[str, str]] = []

    @override
    def load_state_as_of(self, thesis_id: str, known_at: str) -> ThesisStateSnapshot:
        self.seen.append((thesis_id, known_at))
        return self._snap


def test_snapshot_coverage_folds_eligible_rules_only() -> None:
    snap = _snap(
        {"scope": "NVDA", "claims": [{"claim_id": "c1"}],
         "expressions": [{"expression_id": "e1"}]},
        {"rules": [
            {"rule_type": "new_filing", "enabled": True,
             "support_status": "supported", "claim_ids": ["c1", "cx"],
             "expression_ids": ["e1"]},
            {"rule_type": "filing_change", "enabled": True,
             "support_status": "supported", "claim_ids": "c1",
             "expression_ids": ("e1",)},
            {"rule_type": "new_filing", "enabled": False,
             "support_status": "supported", "claim_ids": ["c1"],
             "expression_ids": []},
            {"rule_type": "nope", "enabled": True,
             "support_status": "supported", "claim_ids": ["c1"],
             "expression_ids": []},
            "junk",
        ]},
    )
    repo = _StubRepo(snap)
    scope, cids, eids, cc, ce = _snapshot_coverage(repo, "t1", JUN)
    assert repo.seen == [("t1", JUN)]
    assert (scope, cids, eids) == ("NVDA", ["c1"], ["e1"])
    assert cc == {"new_filing": {"c1", "cx"}, "filing_change": set()}
    assert ce == {"new_filing": {"e1"}, "filing_change": {"e1"}}

def test_snapshot_coverage_non_string_scope_and_missing_sections() -> None:
    snap = _snap({"scope": 7})
    repo = _StubRepo(snap)
    scope, cids, eids, cc, ce = _snapshot_coverage(repo, "t1", JUN)
    assert (scope, cids, eids, cc, ce) == ("unknown", [], [], {}, {})


def test_snapshot_coverage_absent_watch_is_empty() -> None:
    snap = _snap({"scope": "NVDA", "claims": [{"claim_id": "c1"}], "expressions": []})
    scope, cids, eids, cc, ce = _snapshot_coverage(_StubRepo(snap), "t1", JUN)
    assert (scope, cids, eids, cc, ce) == ("NVDA", ["c1"], [], {}, {})


def test_copy_section_list_clones_dicts_ignores_non_list() -> None:
    src = {"schema_version": 7, "questions": [{"a": 1}, "s", 5]}
    raw, entries = _copy_section_list(src, "questions", "t1", SCHEMA_VERSION)
    assert raw["schema_version"] == 7 and entries == [{"a": 1}, "s", 5]
    src_rows = src["questions"]
    assert isinstance(src_rows, list)
    assert entries[0] is not src_rows[0]
    bad, bad_entries = _copy_section_list({"questions": "nope"}, "questions", "t1", 9)
    assert (bad["schema_version"], bad_entries) == (9, [])


def test_asof_ids_collects_present_ids() -> None:
    base = _state({"claims": [{"claim_id": "c1"}, {"claim_id": "c2"}]})
    assert ThesisRepository._asof_ids(base, "claims", "claim") == {"c1", "c2"}


def test_asof_ids_edge_shapes() -> None:
    assert ThesisRepository._asof_ids(_state({"claims": "x"}), "claims", "claim") == set()
    assert ThesisRepository._asof_ids(_state({}), "claims", "claim") == set()
    assert ThesisRepository._asof_ids(_state({"claims": [{"claim_id": "c1"}]}), "claims", "claim") == {"c1"}




# -- repository._backdated_section_folds -----------------------------------------

def test_backdated_section_folds_empty_is_clean() -> None:
    r = _repo(Path("/tmp"))
    q, m, w = (_section("questions", [], "t1"), _section("memories", [], "t1"),
               _section("rules", [], "t1"))
    flags = r._backdated_section_folds(Path("/tmp"), JUN, _inputs(), {}, q, m, w)
    assert flags == _ApplyFold(False, False, False, False, False, False)
    assert flags.dirty(False) is False


def test_backdated_section_folds_all_arms(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    t = r.create_thesis("fold thesis", scope="NVDA", claims=["c"], effective_at=T0)
    thesis_dir = r.dir_for_thesis(t.thesis_id)
    q, m, w = (_section("questions", [], t.thesis_id), _section("memories", [], t.thesis_id),
               _section("rules", [], t.thesis_id))
    thesis_d: dict[str, object] = {"claims": [{"claim_id": "c1"}], "expressions": []}
    inputs = _inputs(
        state={"assessment": "supported"},
        questions_add=[{"question_id": "q:d1", "text": "why?"}],
        memories_add=[{"memory_id": "m:d1", "text": "note"}],
        watch_add=[{"rule_id": "rule:d1", "rule_type": "new_filing", "claim_ids": ["c1"]}],
        questions_answered=[{"question_id": "q:d1", "answer": "yes"}],
    )
    flags = r._backdated_section_folds(thesis_dir, JUN, inputs, thesis_d, q, m, w)
    assert flags == _ApplyFold(False, True, True, True, True, True)
    answered = q["questions"]
    assert isinstance(answered, list) and isinstance(answered[0], dict) and answered[0]["status"] == "answered"


# -- backdated apply end to end (locked + side effects) --------------------------

def _make_thesis(r: ThesisRepository) -> tuple[str, str, str]:
    t = r.create_thesis("backdated thesis", scope="NVDA", claims=["demand holds"],
                        expressions=[{"structure": "equity"}], effective_at=T0)
    live = r.load_thesis(t.thesis_id)
    r.apply_research_result(t.thesis_id, {"state": {
        "thesis_id": t.thesis_id, "assessment": "supported",
        "claim_assessments": {}, "expression_assessments": {}}}, "run:live")
    return t.thesis_id, live.claims[0].claim_id, live.expressions[0].expression_id


def test_backdated_apply_full_persists_snapshot_only(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    tid, cid, eid = _make_thesis(r)
    snap = r.load_state_as_of(tid, JUN)
    snap_claims = _thesis_rows(snap, "claims")
    trig = r.create_trigger(tid, claim_ids=[cid], canonical_refs=["canon:bk"],
                            summary="backdated trigger", summary_origin="deterministic")
    before = r.list_state_versions(tid)
    out = r.apply_research_result(tid, {
        "claim_updates": [{"claim_id": cid, "status": "supported"}],
        "expression_updates": [{"expression_id": eid, "status": "flagged"}],
        "trigger_id": trig.trigger_id,
        "state": {"thesis_id": tid, "assessment": "supported",
                  "claim_assessments": {cid: "supported"}, "expression_assessments": {}},
        "questions_add": [{"question_id": "q:bk1", "text": "why?"}],
        "memories_add": [{"memory_id": "m:bk1", "text": "note"}],
        "watch_add": [{"rule_id": "rule:bk1", "rule_type": "new_filing", "claim_ids": [cid]}],
        "questions_answered": [{"question_id": "q:bk1", "answer": "backdated answer"}],
        "journal_entry": {"entry_id": "journal:bk1", "title": "backdated note",
                          "body": "plain findings", "known_at": JUN},
    }, "run:bk", effective_at=JUN)
    assert out["evidence"] == 0
    assert str(out["journal"]).endswith(".md")
    assert out["trigger"] == trig.trigger_id
    assert r.list_state_versions(tid) == before + [max(before) + 1]
    # snapshot-only: live files untouched, as-of view carries the patch
    assert r.load_thesis(tid).claims[0].status == "unvalidated"
    asof = r.load_state_as_of(tid, JUN)
    asof_claims = _thesis_rows(asof, "claims")
    assert isinstance(asof_claims[0], dict) and asof_claims[0]["status"] == "supported"
    asof_exprs = _thesis_rows(asof, "expressions")
    assert isinstance(asof_exprs[0], dict) and asof_exprs[0]["status"] == "flagged"
    asof_answer = _thesis_row(asof, "questions", "questions", 0)
    assert asof_answer["answer"] == "backdated answer"


def test_backdated_apply_empty_writes_no_snapshot(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    tid, _, _ = _make_thesis(r)
    before = r.list_state_versions(tid)
    out = r.apply_research_result(tid, {}, "run:empty", effective_at=MAR)
    assert out == {"evidence": 0}
    assert r.list_state_versions(tid) == before


def test_backdated_apply_closed_snapshot_refuses(tmp_path: Path) -> None:
    r = _repo(tmp_path)
    t = r.create_thesis("closing thesis", scope="NVDA", claims=["c"], effective_at=T0)
    r.apply_research_result(t.thesis_id, {"state": {
        "thesis_id": t.thesis_id, "assessment": "supported",
        "claim_assessments": {}, "expression_assessments": {}}}, "run:live")
    r.close_thesis(t.thesis_id, effective_at=APR)  # snapshot-only close; live stays active
    with pytest.raises(ValueError, match="closed"):
        r.apply_research_result(t.thesis_id, {}, "run:bk", effective_at=MAY)
