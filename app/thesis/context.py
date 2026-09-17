"""Bounded point-in-time research context for thesis runs (stdlib + PyYAML only)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.thesis.models import (
    EvidenceRef,
    JSONValue,
    ThesisMemory,
    ThesisQuestion,
    ThesisStateSnapshot,
    Trigger,
    WatchRule,
)
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


@dataclass
class _PacketParts:
    """Mandatory packet inputs: thesis/state/watch/questions/trigger + memories."""

    packet: dict[str, JSONValue]
    memories: list[dict[str, JSONValue]]
    visible_memories: list[dict[str, JSONValue]]
    pit_omitted: list[str]


def _check_trigger_owner(tdict: dict[str, JSONValue], tid: str) -> None:
    """Trigger must belong to the thesis being built."""
    if tdict.get("thesis_id", tid) != tid:
        raise ValueError(f"<context>: trigger belongs to {tdict.get('thesis_id')!r}, not {tid!r}")


def _live_memories(thesis_dir: Path) -> list[dict[str, JSONValue]]:
    """Live memory rows from memory.yaml (empty when absent)."""
    mem_path = thesis_dir / "memory.yaml"
    if not mem_path.is_file():
        return []
    raw_mem = load_raw_yaml(mem_path)
    rows = raw_mem.get("memories", [])
    if not isinstance(rows, list):
        raise ValueError(f"{mem_path}: 'memories' must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    out: list[dict[str, JSONValue]] = []
    for m in rows:
        if not isinstance(m, dict):
            raise ValueError(f"{mem_path}: memory must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        out.append(ThesisMemory.from_dict(m, str(mem_path)).to_dict())
    return [dict(m) for m in out if isinstance(m, dict)]


def _live_packet(repository: ThesisRepository, tid: str, thesis_dir: Path, tdict: dict[str, JSONValue]) -> _PacketParts:
    """Live packet: current thesis/state/questions/rules plus all memories."""
    thesis_dict = repository.load_thesis(tid).to_dict()
    state = dict(repository.load_state(tid).to_dict())
    questions: list[dict[str, JSONValue]] = [q.to_dict() for q in repository.load_questions(tid)]
    rules: list[dict[str, JSONValue]] = [r.to_dict() for r in repository.load_watch_rules(tid)]
    memories = _live_memories(thesis_dir)
    packet: dict[str, JSONValue] = {
        "thesis": dict(thesis_dict),
        "state": state,
        "watch": {"thesis_id": tid, "rules": list[JSONValue](rules)},
        "questions": list[JSONValue](questions),
        "trigger": tdict,
    }
    return _PacketParts(packet=packet, memories=memories, visible_memories=list(memories), pit_omitted=[])


def _snapshot_questions(snapshot: ThesisStateSnapshot, thesis_dir: Path) -> list[dict[str, JSONValue]]:
    """Validated snapshot question rows."""
    rows = snapshot.questions.get("questions", [])
    if not isinstance(rows, list):
        raise ValueError("<context>: 'questions' must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    out: list[dict[str, JSONValue]] = []
    for q in rows:
        if not isinstance(q, dict):
            raise ValueError(f"{thesis_dir / 'questions.yaml'}: question must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        ThesisQuestion.from_dict(q, str(thesis_dir / "questions.yaml"))
        out.append(dict(q))
    return out


def _snapshot_rules(snapshot: ThesisStateSnapshot, thesis_dir: Path) -> list[dict[str, JSONValue]]:
    """Validated snapshot watch-rule rows."""
    rows = snapshot.watch.get("rules", [])
    if not isinstance(rows, list):
        raise ValueError("<context>: 'rules' must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    out: list[dict[str, JSONValue]] = []
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError(f"{thesis_dir / 'watch.yaml'}: watch rule must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        out.append(WatchRule.from_dict(r, str(thesis_dir / "watch.yaml")).to_dict())
    return out


def _snapshot_memories(snapshot: ThesisStateSnapshot, thesis_dir: Path) -> list[dict[str, JSONValue]]:
    """Validated snapshot memory rows."""
    rows = snapshot.memory.get("memories", [])
    if not isinstance(rows, list):
        raise ValueError("<context>: 'memories' must be a list")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    out: list[dict[str, JSONValue]] = []
    for m in rows:
        if not isinstance(m, dict):
            raise ValueError(f"{thesis_dir / 'memory.yaml'}: memory must be a mapping")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        out.append(ThesisMemory.from_dict(m, str(thesis_dir / "memory.yaml")).to_dict())
    return out


def _pit_memories(
    memories: list[dict[str, JSONValue]], data_cutoff: str
) -> tuple[list[dict[str, JSONValue]], list[str]]:
    """PIT-visible memories by created_at; undated/unparseable fail closed."""
    visible: list[dict[str, JSONValue]] = []
    omitted: list[str] = []
    for m in memories:
        created = m.get("created_at")
        if isinstance(created, str) and created and _pit_visible(created, data_cutoff):
            visible.append(m)
        else:
            mid = m.get("memory_id")
            omitted.append(mid if isinstance(mid, str) else "")
    return visible, omitted


def _snapshot_packet(
    repository: ThesisRepository, tid: str, thesis_dir: Path, tdict: dict[str, JSONValue], data_cutoff: str
) -> _PacketParts:
    """Snapshot packet at the cutoff: thesis+trigger+state+watch+questions only."""
    snapshot = repository.load_state_as_of(tid, data_cutoff)
    state = dict(snapshot.state)
    questions = _snapshot_questions(snapshot, thesis_dir)
    rules = _snapshot_rules(snapshot, thesis_dir)
    memories = _snapshot_memories(snapshot, thesis_dir)
    visible, omitted = _pit_memories(memories, data_cutoff)
    packet: dict[str, JSONValue] = {
        "thesis": dict(snapshot.thesis),
        "state": state,
        "watch": {"thesis_id": tid, "rules": list[JSONValue](rules)},
        "questions": list[JSONValue](questions),
        "trigger": tdict,
    }
    return _PacketParts(packet=packet, memories=memories, visible_memories=visible, pit_omitted=omitted)


def _check_budget(packet: dict[str, JSONValue], max_tokens: int) -> int:
    """Mandatory-packet token gate; returns the mandatory cost."""
    mandatory = _tokens(json.dumps(packet, sort_keys=True))
    if mandatory <= max_tokens:
        return mandatory
    breakdown = ", ".join(f"{k}~{_tokens(json.dumps(v, sort_keys=True))}" for k, v in packet.items())
    raise ContextBudgetExceeded(
        f"<context>: mandatory packet ~{mandatory} tokens exceeds budget of {max_tokens} ({breakdown})"
    )


def _evidence_order_key(trigger_refs: set[str]) -> Callable[[dict[str, JSONValue]], tuple[int, float, str]]:
    """Sort key: pending-trigger refs first, then newest, stable by evidence_id."""
    from app.thesis.monitor import _as_dt  # local: monitor -> runner -> context

    def _ev_key(e: dict[str, JSONValue]) -> tuple[int, float, str]:
        dt = _as_dt(str(e.get("known_at") or ""))
        ts = dt.timestamp() if dt is not None else float("-inf")
        return (0 if e.get("canonical_ref") in trigger_refs else 1, -ts, str(e.get("evidence_id") or ""))

    return _ev_key


def _load_evidence(
    repository: ThesisRepository, tid: str, thesis_dir: Path, tdict: dict[str, JSONValue], data_cutoff: str
) -> list[dict[str, JSONValue]]:
    """PIT-visible evidence refs, trigger refs first then newest-first."""
    _crefs = tdict.get("canonical_refs")
    trigger_refs: set[str] = {c for c in _crefs if isinstance(c, str)} if isinstance(_crefs, list) else set()
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
    ordered.sort(key=_evidence_order_key(trigger_refs))
    return ordered


def _fit_evidence(
    ordered: list[dict[str, JSONValue]], omitted: list[str], used: int, max_tokens: int
) -> tuple[list[dict[str, JSONValue]], list[str], int, list[str]]:
    """Evidence fitting the remaining budget; returns (evidence, included, used, omitted)."""
    evidence: list[dict[str, JSONValue]] = []
    included: list[str] = []
    for e in ordered:
        cost = _tokens(json.dumps(e, sort_keys=True))
        eid = e.get("evidence_id")
        eid_str = eid if isinstance(eid, str) else ""
        if used + cost > max_tokens:
            omitted.append(eid_str)
            continue
        used += cost
        evidence.append(e)
        included.append(eid_str)
    return evidence, included, used, omitted


def _trim_memories(
    packet: dict[str, JSONValue],
    visible: list[dict[str, JSONValue]],
    evidence_tokens: int,
    max_tokens: int,
    omitted: list[str],
) -> tuple[list[dict[str, JSONValue]], list[str]]:
    """Oldest-first memory trim until packet + evidence fit; returns (kept, included)."""
    kept = list(visible)
    while kept and _packet_tokens(packet, kept) + evidence_tokens > max_tokens:
        mid = kept.pop(0).get("memory_id")
        omitted.append(mid if isinstance(mid, str) else "")
    included: list[str] = []
    for m in kept:
        mid2 = m.get("memory_id")
        included.append(mid2 if isinstance(mid2, str) else "")
    return kept, included


def _packet_tokens(packet: dict[str, JSONValue], mem_list: list[dict[str, JSONValue]]) -> int:
    """Token cost of the packet with one memory list."""
    return _tokens(json.dumps({**packet, "memories": list[JSONValue](mem_list)}, sort_keys=True))


def _journal_head(f: Path, omitted: list[str]) -> str | None:
    """First bounded lines of one journal file; None when unreadable."""
    try:
        return "".join(f.read_text(encoding="utf-8").splitlines(keepends=True)[:_JOURNAL_HEAD_LINES])
    except OSError:
        omitted.append(f.stem)
        return None


def _journal_visible(head: str, live: bool, data_cutoff: str) -> bool:
    """False for missing/unparseable/future-known journal front-matter (PIT only)."""
    if live:
        return True
    fm_known_at = _journal_known_at(head)
    return bool(fm_known_at and _pit_visible(fm_known_at, data_cutoff))


def _fit_journal(head: str, stem: str, used: int, max_tokens: int) -> bool:
    """True when one journal head still fits the remaining budget."""
    return used + _tokens(head) <= max_tokens


def _journal_files(thesis_dir: Path) -> list[Path]:
    """Journal files newest-first."""
    journal_dir = thesis_dir / "journal"
    if not journal_dir.is_dir():
        return []
    return sorted(
        (p for p in journal_dir.glob("*.md") if p.is_file()),
        key=_journal_mtime,
        reverse=True,
    )


def _collect_one_journal(
    f: Path, head: str, live: bool, data_cutoff: str, used: int, max_tokens: int, omitted: list[str]
) -> tuple[dict[str, JSONValue] | None, int]:
    """One journal file: (excerpt, used) or (None, used) when skipped."""
    if not _journal_visible(head, live, data_cutoff):
        omitted.append(f.stem)  # missing, unparseable, or future-known: never visible
        return None, used
    if not _fit_journal(head, f.stem, used, max_tokens):
        omitted.append(f.stem)
        return None, used
    return {"journal": f.stem, "excerpt": head}, used + _tokens(head)


def _collect_journals(
    thesis_dir: Path, live: bool, data_cutoff: str, used: int, max_tokens: int, omitted: list[str], included: list[str]
) -> tuple[list[dict[str, JSONValue]], int]:
    """Newest-first journal excerpts while budget remains."""
    excerpts: list[dict[str, JSONValue]] = []
    for f in _journal_files(thesis_dir):
        head = _journal_head(f, omitted)
        if head is None:
            continue
        excerpt, used = _collect_one_journal(f, head, live, data_cutoff, used, max_tokens, omitted)
        if excerpt is None:
            continue
        excerpts.append(excerpt)
        included.append(f"journal:{f.stem}")
    return excerpts, used


def _finalize_packet(
    packet: dict[str, JSONValue],
    ordered: list[dict[str, JSONValue]],
    evidence: list[dict[str, JSONValue]],
    memories: list[dict[str, JSONValue]],
    kept: list[dict[str, JSONValue]],
    journals_omitted: int,
    excerpts: list[dict[str, JSONValue]],
) -> None:
    """Omitted counts for evidence/memories/journals."""
    _ = excerpts
    packet["omitted_counts"] = {
        "evidence": len(ordered) - len(evidence),
        "memories": len(memories) - len(kept),
        "journals": journals_omitted,
    }


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
    _check_trigger_owner(tdict, tid)
    if live:
        parts = _live_packet(repository, tid, thesis_dir, tdict)
    else:
        parts = _snapshot_packet(repository, tid, thesis_dir, tdict, data_cutoff)
    packet = parts.packet
    mandatory = _check_budget(packet, max_tokens)
    # Evidence: PIT-visible refs only (chronological, fail-closed), pending-trigger
    # refs first, then newest-first with stable evidence_id tiebreak. Full dicts.
    ordered = _load_evidence(repository, tid, thesis_dir, tdict, data_cutoff)
    omitted: list[str] = list(parts.pit_omitted)
    evidence, included, used, omitted = _fit_evidence(ordered, omitted, mandatory, max_tokens)
    evidence_tokens = _tokens(json.dumps(evidence, sort_keys=True))
    # Over budget: trim oldest memories first (newest-last on disk).
    kept, mem_included = _trim_memories(packet, parts.visible_memories, evidence_tokens, max_tokens, omitted)
    included.extend(mem_included)
    packet["memories"] = list[JSONValue](kept)
    used = _packet_tokens(packet, kept) + evidence_tokens
    # Journals: newest-first excerpts, only while budget remains.
    journal_dir = thesis_dir / "journal"
    journal_dir = thesis_dir / "journal"
    journal_total = len(list(journal_dir.glob("*.md"))) if journal_dir.is_dir() else 0
    excerpts, used = _collect_journals(thesis_dir, live, data_cutoff, used, max_tokens, omitted, included)
    _finalize_packet(packet, ordered, evidence, parts.memories, kept, journal_total - len(excerpts), excerpts)
    total = _tokens(json.dumps({"packet": packet, "evidence": evidence, "journals": excerpts}, sort_keys=True))
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
