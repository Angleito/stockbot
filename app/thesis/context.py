"""Bounded point-in-time research context for thesis runs (stdlib + PyYAML only)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.thesis.models import EvidenceRef, ThesisMemory, ThesisQuestion, WatchRule
from app.thesis.yaml import load_raw_yaml, load_yaml

# ponytail: token estimate is len(text)//4, no tokenizer dependency (pi_gateway uses same).

_JOURNAL_HEAD_LINES = 40


class ContextBudgetExceeded(ValueError):
    """The mandatory thesis packet alone exceeds the token budget."""


def _tokens(text: str) -> int:
    return len(text) // 4


@dataclass
class ResearchContext:
    thesis_packet: dict
    evidence_refs: list = field(default_factory=list)
    journal_excerpts: list = field(default_factory=list)
    included_ids: list = field(default_factory=list)
    omitted_ids: list = field(default_factory=list)
    estimated_tokens: int = 0
    known_at: str = ""


def _thesis_dir(repository: Any, thesis_id: str) -> tuple[str, Path]:
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


def _trigger_dict(trigger: Any) -> dict:
    if hasattr(trigger, "to_dict"):
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

def build_context(
    repository: Any,
    thesis_id: str,
    trigger: Any,
    *,
    known_at: str,
    max_tokens: int = 8_000,
) -> ResearchContext:
    """Assemble the PIT-bounded packet Pi may see for one trigger run."""
    if not isinstance(known_at, str) or not known_at:
        raise ValueError("<context>: 'known_at' must be a non-empty ISO string")
    tid, thesis_dir = _thesis_dir(repository, thesis_id)
    thesis = repository.load_thesis(tid)
    tdict = _trigger_dict(trigger)
    if tdict.get("thesis_id", tid) != tid:
        raise ValueError(f"<context>: trigger belongs to {tdict.get('thesis_id')!r}, not {tid!r}")

    state = repository.load_state(tid).to_dict()
    questions = [q.to_dict() if hasattr(q, "to_dict") else dict(q) for q in repository.load_questions(tid)]
    for q in questions:
        ThesisQuestion.from_dict(q, str(thesis_dir / "questions.yaml"))
    watch_raw = load_raw_yaml(thesis_dir / "watch.yaml")
    rules = [WatchRule.from_dict(r, str(thesis_dir / "watch.yaml")).to_dict() for r in watch_raw.get("rules", [])]
    memory_raw = load_raw_yaml(thesis_dir / "memory.yaml")
    memories = [
        ThesisMemory.from_dict(m, str(thesis_dir / "memory.yaml")).to_dict()
        for m in memory_raw.get("memories", [])
    ]
    # Memories: PIT-visible by created_at only; undated/unparseable fail closed.
    visible_memories: list[dict] = []
    pit_omitted: list[str] = []
    for m in memories:
        if (m.get("created_at") or "") and _pit_visible(m["created_at"], known_at):
            visible_memories.append(m)
        else:
            pit_omitted.append(m["memory_id"])

    # Irreducible packet: thesis+trigger+state+watch+questions only.
    packet = {
        "thesis": thesis.to_dict(),
        "state": state,
        "watch": {"thesis_id": tid, "rules": rules},
        "questions": questions,
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

    trigger_refs = set(tdict.get("canonical_refs") or [])
    ordered: list[dict] = []
    evidence_dir = thesis_dir / "evidence"
    if evidence_dir.is_dir():
        for f in sorted(evidence_dir.glob("*.yaml")):
            ref = load_yaml(f, EvidenceRef)
            if ref.thesis_id != tid:
                raise ValueError(f"{f}: evidence belongs to {ref.thesis_id!r}, not {tid!r}")
            if not ref.known_at or not _pit_visible(ref.known_at, known_at):
                continue  # missing, unparseable, or future-known: never visible
            ordered.append(ref.to_dict())

    def _ev_key(e: dict) -> tuple[int, float, str]:
        dt = _as_dt(e.get("known_at") or "")
        ts = dt.timestamp() if dt is not None else float("-inf")
        return (0 if e.get("canonical_ref") in trigger_refs else 1, -ts, e.get("evidence_id") or "")

    ordered.sort(key=_ev_key)

    included: list[str] = []
    omitted: list[str] = list(pit_omitted)
    used = mandatory
    evidence: list[dict] = []
    for e in ordered:
        cost = _tokens(json.dumps(e, sort_keys=True))
        if used + cost > max_tokens:
            omitted.append(e["evidence_id"])
            continue
        used += cost
        evidence.append(e)
        included.append(e["evidence_id"])
    evidence_tokens = _tokens(json.dumps(evidence, sort_keys=True))

    def _packet_tokens(mem_list: list) -> int:
        return _tokens(json.dumps({**packet, "memories": mem_list}, sort_keys=True))

    # Over budget: trim oldest memories first (newest-last on disk).
    kept = list(visible_memories)
    while kept and _packet_tokens(kept) + evidence_tokens > max_tokens:
        omitted.append(kept.pop(0)["memory_id"])
    for m in kept:
        included.append(m["memory_id"])
    packet["memories"] = kept
    used = _packet_tokens(kept) + evidence_tokens

    # Journals: newest-first excerpts, only while budget remains.
    journal_dir = thesis_dir / "journal"
    journal_files: list[Path] = []
    if journal_dir.is_dir():
        journal_files = sorted(
            (p for p in journal_dir.glob("*.md") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    excerpts: list[dict] = []
    for f in journal_files:
        try:
            head = "".join(f.read_text(encoding="utf-8").splitlines(keepends=True)[:_JOURNAL_HEAD_LINES])
        except OSError:
            omitted.append(f.stem)
            continue
        fm_known_at = _journal_known_at(head)
        if not fm_known_at or not _pit_visible(fm_known_at, known_at):
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
        known_at=known_at,
    )
