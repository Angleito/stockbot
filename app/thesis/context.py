"""Bounded point-in-time research context for thesis runs (stdlib + PyYAML only)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.thesis.models import EvidenceRef, JSONValue, ThesisMemory, ThesisQuestion, Trigger, WatchRule
from app.thesis.repository import ThesisRepository
from app.thesis.yaml import load_raw_yaml, load_yaml

# ponytail: token estimate is len(text)//4, no tokenizer dependency (pi_gateway uses same).

_JOURNAL_HEAD_LINES = 40


class ContextBudgetExceeded(ValueError):
    """The mandatory thesis packet alone exceeds the token budget."""


def _tokens(text: str) -> int:
    return len(text) // 4

def _journal_mtime(path: Path) -> float:
    """Sort key: journal file modification time (newest first with reverse=True)."""
    return path.stat().st_mtime


@dataclass
class ResearchContext:
    thesis_packet: dict[str, JSONValue]
    evidence_refs: list[dict[str, JSONValue]] = field(default_factory=list)
    journal_excerpts: list[dict[str, JSONValue]] = field(default_factory=list)
    included_ids: list[str] = field(default_factory=list)
    omitted_ids: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    known_at: str = ""


def _thesis_dir(repository: ThesisRepository, thesis_id: str) -> tuple[str, Path]:
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    return tid, repository.dir_for_thesis(tid)


def _journal_known_at(head: str) -> str | None:
    """Front-matter ``known_at`` of a journal excerpt head (None when absent)."""
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("known_at:"):
            return stripped.split(":", 1)[1].strip() or None
    return None


def _trigger_dict(trigger: Trigger | Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    if isinstance(trigger, Trigger):
        return trigger.to_dict()
    if isinstance(trigger, dict):
        return dict(trigger)
    raise ValueError(f"<context>: trigger must be a Trigger or mapping, got {type(trigger).__name__}")


def _pit_visible(value: str, cutoff: str) -> bool:
    """Chronological ``value <= cutoff``; unparseable values fail closed."""
    from app.thesis.monitor import _as_dt, _le  # local: monitor -> runner -> context

    if _as_dt(value) is None or _as_dt(cutoff) is None:
        return False
    return _le(value, cutoff)

def _build_context(
    repository: ThesisRepository,
    thesis_id: str,
    trigger: Trigger | Mapping[str, JSONValue],
    *,
    data_cutoff: str,
    max_tokens: int = 8_000,
    live: bool,
) -> ResearchContext:
    tid, thesis_dir = _thesis_dir(repository, thesis_id)
    tdict = _trigger_dict(trigger)
    if tdict.get("thesis_id", tid) != tid:
        raise ValueError(f"<context>: trigger belongs to {tdict.get('thesis_id')!r}, not {tid!r}")
    if live:
        thesis_dict = repository.load_thesis(tid).to_dict()
        state = dict(repository.load_state(tid).to_dict())
        questions: list[dict[str, JSONValue]] = [q.to_dict() for q in repository.load_questions(tid)]
        rules: list[dict[str, JSONValue]] = [r.to_dict() for r in repository.load_watch_rules(tid)]
        mem_path = thesis_dir / "memory.yaml"
        _mem_raw: list[object] = []
        if mem_path.is_file():
            raw_mem = load_raw_yaml(mem_path)
            _ml = raw_mem.get("memories", [])
            if not isinstance(_ml, list):
                raise ValueError(f"{mem_path}: 'memories' must be a list")
            for _m in _ml:
                if not isinstance(_m, dict):
                    raise ValueError(f"{mem_path}: memory must be a mapping")
                _mem_raw.append(ThesisMemory.from_dict(_m, str(mem_path)).to_dict())
        memories: list[dict[str, JSONValue]] = [dict(m) for m in _mem_raw if isinstance(m, dict)]
        visible_memories = list(memories)
        pit_omitted: list[str] = []
        packet: dict[str, JSONValue] = {
            "thesis": dict(thesis_dict),
            "state": state,
            "watch": {"thesis_id": tid, "rules": list[JSONValue](rules)},
            "questions": list[JSONValue](questions),
            "trigger": tdict,
        }
    else:
        snapshot = repository.load_state_as_of(tid, data_cutoff)
        state = dict(snapshot.state)
        _squestions = snapshot.questions.get("questions", [])
        if not isinstance(_squestions, list):
            raise ValueError("<context>: 'questions' must be a list")
        questions = []
        for _q in _squestions:
            if not isinstance(_q, dict):
                raise ValueError(f"{thesis_dir / 'questions.yaml'}: question must be a mapping")
            ThesisQuestion.from_dict(_q, str(thesis_dir / "questions.yaml"))
            questions.append(dict(_q))
        _srules = snapshot.watch.get("rules", [])
        if not isinstance(_srules, list):
            raise ValueError("<context>: 'rules' must be a list")
        rules = []
        for r in _srules:
            if not isinstance(r, dict):
                raise ValueError(f"{thesis_dir / 'watch.yaml'}: watch rule must be a mapping")
            rules.append(WatchRule.from_dict(r, str(thesis_dir / "watch.yaml")).to_dict())
        _smemories = snapshot.memory.get("memories", [])
        if not isinstance(_smemories, list):
            raise ValueError("<context>: 'memories' must be a list")
        memories = []
        for m in _smemories:
            if not isinstance(m, dict):
                raise ValueError(f"{thesis_dir / 'memory.yaml'}: memory must be a mapping")
            memories.append(ThesisMemory.from_dict(m, str(thesis_dir / "memory.yaml")).to_dict())
        # Memories: PIT-visible by created_at only; undated/unparseable fail closed.
        visible_memories = []
        pit_omitted = []
        for m in memories:
            _created = m.get("created_at")
            if isinstance(_created, str) and _created and _pit_visible(_created, data_cutoff):
                visible_memories.append(m)
            else:
                pit_omitted.append(m["memory_id"] if isinstance(m["memory_id"], str) else "")
        # Irreducible packet: thesis+trigger+state+watch+questions only.
        packet = {
            "thesis": dict(snapshot.thesis),
            "state": state,
            "watch": {"thesis_id": tid, "rules": list[JSONValue](rules)},
            "questions": list[JSONValue](questions),
            "trigger": tdict,
        }
    mandatory = _tokens(json.dumps(packet, sort_keys=True))
    if mandatory > max_tokens:
        breakdown = ", ".join(
            f"{k}~{_tokens(json.dumps(v, sort_keys=True))}" for k, v in packet.items()
        )
        raise ContextBudgetExceeded(
            f"<context>: mandatory packet ~{mandatory} tokens exceeds budget of {max_tokens}"
            f" ({breakdown})"
        )
    # Evidence: PIT-visible refs only (chronological, fail-closed), pending-trigger
    # refs first, then newest-first with stable evidence_id tiebreak. Full dicts.
    from app.thesis.monitor import _as_dt  # local: monitor -> runner -> context
    _crefs = tdict.get("canonical_refs")
    trigger_refs = set(_crefs) if isinstance(_crefs, list) else set()
    ordered: list[dict[str, JSONValue]] = []
    evidence_dir = thesis_dir / "evidence"
    if evidence_dir.is_dir():
        for f in sorted(evidence_dir.glob("*.yaml")):
            ref = load_yaml(f, EvidenceRef)
            if ref.thesis_id != tid:
                raise ValueError(f"{f}: evidence belongs to {ref.thesis_id!r}, not {tid!r}")
            if not ref.known_at or not _pit_visible(ref.known_at, data_cutoff):
                continue  # missing, unparseable, or future-known: never visible
            ordered.append(ref.to_dict())
    def _ev_key(e: dict[str, JSONValue]) -> tuple[int, float, str]:
        dt = _as_dt(str(e.get("known_at") or ""))
        ts = dt.timestamp() if dt is not None else float("-inf")
        return (0 if e.get("canonical_ref") in trigger_refs else 1, -ts, str(e.get("evidence_id") or ""))
    ordered.sort(key=_ev_key)
    included: list[str] = []
    omitted: list[str] = list(pit_omitted)
    used = mandatory
    evidence: list[dict[str, JSONValue]] = []
    for e in ordered:
        cost = _tokens(json.dumps(e, sort_keys=True))
        _eid = e.get("evidence_id")
        _eid_str = _eid if isinstance(_eid, str) else ""
        if used + cost > max_tokens:
            omitted.append(_eid_str)
            continue
        used += cost
        evidence.append(e)
        included.append(_eid_str)
    evidence_tokens = _tokens(json.dumps(evidence, sort_keys=True))
    def _packet_tokens(mem_list: list[dict[str, JSONValue]]) -> int:
        return _tokens(json.dumps({**packet, "memories": list[JSONValue](mem_list)}, sort_keys=True))
    # Over budget: trim oldest memories first (newest-last on disk).
    kept = list(visible_memories)
    while kept and _packet_tokens(kept) + evidence_tokens > max_tokens:
        _mid = kept.pop(0).get("memory_id")
        omitted.append(_mid if isinstance(_mid, str) else "")
    for m in kept:
        _mid2 = m.get("memory_id")
        included.append(_mid2 if isinstance(_mid2, str) else "")
    packet["memories"] = list[JSONValue](kept)
    used = _packet_tokens(kept) + evidence_tokens
    # Journals: newest-first excerpts, only while budget remains.
    journal_dir = thesis_dir / "journal"
    journal_files: list[Path] = []
    if journal_dir.is_dir():
        journal_files = sorted(
            (p for p in journal_dir.glob("*.md") if p.is_file()),
            key=_journal_mtime,
            reverse=True,
        )
    excerpts: list[dict[str, JSONValue]] = []
    for f in journal_files:
        try:
            head = "".join(f.read_text(encoding="utf-8").splitlines(keepends=True)[:_JOURNAL_HEAD_LINES])
        except OSError:
            omitted.append(f.stem)
            continue
        if not live:
            fm_known_at = _journal_known_at(head)
            if not fm_known_at or not _pit_visible(fm_known_at, data_cutoff):
                omitted.append(f.stem)  # missing, unparseable, or future-known: never visible
                continue
        cost = _tokens(head)
        if used + cost > max_tokens:
            omitted.append(f.stem)
            continue
        used += cost
        excerpts.append({"journal": f.stem, "excerpt": head})
        included.append(f"journal:{f.stem}")
    packet["omitted_counts"] = {
        "evidence": len(ordered) - len(evidence),
        "memories": len(memories) - len(kept),
        "journals": len(journal_files) - len(excerpts),
    }
    total = _tokens(
        json.dumps({"packet": packet, "evidence": evidence, "journals": excerpts}, sort_keys=True)
    )
    return ResearchContext(
        thesis_packet=packet,
        evidence_refs=evidence,
        journal_excerpts=excerpts,
        included_ids=included,
        omitted_ids=omitted,
        estimated_tokens=total,
        known_at=data_cutoff,
    )


def build_context(
    repository: ThesisRepository,
    thesis_id: str,
    trigger: Trigger | Mapping[str, JSONValue],
    *,
    known_at: str,
    max_tokens: int = 8_000,
) -> ResearchContext:
    """Assemble the PIT-bounded packet Pi may see for one trigger run."""
    if not isinstance(known_at, str) or not known_at:
        raise ValueError("<context>: 'known_at' must be a non-empty ISO string")
    return _build_context(repository, thesis_id, trigger, data_cutoff=known_at, max_tokens=max_tokens, live=False)


def build_live_context(
    repository: ThesisRepository,
    thesis_id: str,
    trigger: Trigger | Mapping[str, JSONValue],
    *,
    data_cutoff: str,
    max_tokens: int = 8_000,
) -> ResearchContext:
    """Assemble the live current-state packet with cutoff-bounded evidence."""
    if not isinstance(data_cutoff, str) or not data_cutoff:
        raise ValueError("<context>: 'data_cutoff' must be a non-empty ISO string")
    from app.thesis.monitor import _as_dt  # local: monitor -> runner -> context
    if _as_dt(data_cutoff) is None:
        raise ValueError(f"<context>: bad data_cutoff {data_cutoff!r}")
    return _build_context(repository, thesis_id, trigger, data_cutoff=data_cutoff, max_tokens=max_tokens, live=True)
