"""Filesystem repository: the ownership boundary for thesis YAML/journal/trigger writes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import yaml

from app.thesis import models
from app.thesis.models import (
    SCHEMA_VERSION,
    Checkpoint,
    EvidenceRef,
    QuestionStatus,
    Thesis,
    ThesisClaim,
    ThesisMemory,
    ThesisQuestion,
    ThesisState,
    TradeExpression,
    Trigger,
    TriggerStatus,
    WatchRule,
    new_claim_id,
    new_evidence_id,
    new_expression_id,
    new_journal_id,
    new_memory_id,
    new_question_id,
    new_requirement_id,
    new_thesis_id,
    new_trigger_id,
    slugify,
)
from app.thesis.yaml import atomic_write_yaml, load_raw_yaml, load_yaml, thesis_lock

STATE_FILES = ("thesis.yaml", "state.yaml", "watch.yaml", "questions.yaml", "memory.yaml", "checkpoint.yaml")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(trigger_or_entry_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", trigger_or_entry_id)


def _best_effort_thesis_id(thesis_file: Path) -> str | None:
    """Read a ``thesis_id`` from an unloadable thesis.yaml (None when unreadable)."""
    try:
        data = yaml.safe_load(thesis_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("thesis_id"), str) and data["thesis_id"]:
        return data["thesis_id"]
    return None


def _journal_front_matter(entry_id: str, thesis_id: str, created: str, d: dict) -> str:
    """Journal front matter; ``known_at`` persists when the entry carries it (PIT gate)."""
    known = f"known_at: {d.get('known_at')}\n" if d.get("known_at") else ""
    return (
        f"---\nentry_id: {entry_id}\nthesis_id: {thesis_id}\ncreated_at: {created}\n"
        f"{known}run_id: {d.get('run_id', '')}\ntrigger_id: {d.get('trigger_id', '')}\n---\n"
    )


def _coerce_claim(item: Any, _path: str = "<create>") -> ThesisClaim:
    if isinstance(item, ThesisClaim):
        return item
    if isinstance(item, str):
        return ThesisClaim(claim_id=new_claim_id(), statement=item)
    if isinstance(item, dict):
        d = dict(item)
        d.setdefault("claim_id", new_claim_id())
        d.setdefault("status", "unvalidated")
        return ThesisClaim.from_dict(d, _path)
    raise ValueError(f"{_path}: claim must be str, mapping, or ThesisClaim, got {type(item).__name__}")


def _coerce_expression(item: Any, _path: str = "<create>") -> TradeExpression:
    if isinstance(item, TradeExpression):
        return item
    if isinstance(item, dict):
        d = dict(item)
        d.setdefault("expression_id", new_expression_id())
        return TradeExpression.from_dict(d, _path)
    raise ValueError(f"{_path}: expression must be a mapping or TradeExpression, got {type(item).__name__}")


class ThesisRepository:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- internal helpers -------------------------------------------------

    def _scan_all(self) -> tuple[dict[str, Path], dict[str, str], dict[str, str]]:
        """Healthy id->dir plus quarantine maps (dir name->reason, thesis_id->reason).

        One corrupt thesis.yaml never hides a healthy sibling: per-child load
        failures (malformed YAML, unknown schema, slug/owner mismatch) are
        skipped and recorded. Duplicate thesis IDs stay loud.
        """
        found: dict[str, Path] = {}
        bad_dirs: dict[str, str] = {}
        bad_ids: dict[str, str] = {}
        if not self.root.is_dir():
            return found, bad_dirs, bad_ids
        loaded: list[tuple[Path, Thesis]] = []
        for child in sorted(self.root.iterdir()):
            thesis_file = child / "thesis.yaml"
            if not child.is_dir() or not thesis_file.is_file():
                continue
            try:
                loaded.append((child, load_yaml(thesis_file, Thesis)))
            except ValueError as exc:
                reason = str(exc)
                bad_dirs[child.name] = reason
                tid = _best_effort_thesis_id(thesis_file)
                if tid is not None:
                    bad_ids.setdefault(tid, reason)
        seen: dict[str, Path] = {}
        for child, thesis in loaded:
            if thesis.thesis_id in seen:
                raise ValueError(
                    f"duplicate thesis ID {thesis.thesis_id!r} in {seen[thesis.thesis_id]} and {child}"
                )
            seen[thesis.thesis_id] = child
        for child, thesis in loaded:
            if child.name != thesis.slug:
                reason = f"{child}: directory name does not match thesis slug {thesis.slug!r}"
                bad_dirs[child.name] = reason
                bad_ids.setdefault(thesis.thesis_id, reason)
                continue
            found[thesis.thesis_id] = child
        return found, bad_dirs, bad_ids

    def _scan(self) -> dict[str, Path]:
        """Map thesis_id -> thesis dir by scanning validated thesis.yaml files."""
        return self._scan_all()[0]

    def list_quarantine(self) -> dict[str, str]:
        """Map quarantined directory name -> load-failure reason (inspect aid)."""
        return self._scan_all()[1]

    def _unknown_thesis(self, id_or_slug: str, bad_dirs: dict[str, str], bad_ids: dict[str, str]) -> NoReturn:
        if id_or_slug in bad_dirs:
            raise ValueError(bad_dirs[id_or_slug])
        if id_or_slug in bad_ids:
            raise ValueError(bad_ids[id_or_slug])
        if bad_dirs:
            names = ", ".join(sorted(bad_dirs))
            raise ValueError(f"unknown thesis: {id_or_slug!r} (quarantined: {names})")
        raise KeyError(f"unknown thesis: {id_or_slug!r}")

    def dir_for_thesis(self, thesis_id: str) -> Path:
        """Resolve the owning dir for ``thesis_id`` (re-checked every mutation)."""
        found, bad_dirs, bad_ids = self._scan_all()
        if thesis_id not in found:
            # Also allow slug lookup for reads.
            for tid, d in found.items():
                thesis = load_yaml(d / "thesis.yaml", Thesis)
                if thesis.slug == thesis_id:
                    return d
            self._unknown_thesis(thesis_id, bad_dirs, bad_ids)
        return found[thesis_id]

    _dir_for = dir_for_thesis

    def _load_in_dir(self, thesis_dir: Path) -> Thesis:
        thesis = load_yaml(thesis_dir / "thesis.yaml", Thesis)
        if thesis_dir.name != thesis.slug:
            raise ValueError(f"{thesis_dir}: directory name does not match thesis slug {thesis.slug!r}")
        return thesis

    def _check_owner(self, thesis_dir: Path, thesis_id: str) -> Thesis:
        thesis = self._load_in_dir(thesis_dir)
        if thesis.thesis_id != thesis_id:
            raise ValueError(f"{thesis_dir}: thesis_id mismatch: file has {thesis.thesis_id!r}, expected {thesis_id!r}")
        return thesis

    def load_thesis(self, id_or_slug: str) -> Thesis:
        found, bad_dirs, bad_ids = self._scan_all()
        if id_or_slug in found:
            return self._load_in_dir(found[id_or_slug])
        for tid, d in found.items():
            if self._load_in_dir(d).slug == id_or_slug:
                return self._load_in_dir(d)
        self._unknown_thesis(id_or_slug, bad_dirs, bad_ids)

    def list_theses(self) -> list[Thesis]:
        return [self._load_in_dir(d) for _, d in sorted(self._scan().items())]

    def load_state(self, id_or_slug: str) -> ThesisState:
        d = self._dir_for(self._resolve_id(id_or_slug))
        return load_yaml(d / "state.yaml", ThesisState)

    def load_questions(self, id_or_slug: str) -> list[ThesisQuestion]:
        d = self._dir_for(self._resolve_id(id_or_slug))
        raw = load_raw_yaml(d / "questions.yaml")
        self._check_file_owner(raw, str(d / "questions.yaml"), self._resolve_id(id_or_slug))
        return [ThesisQuestion.from_dict(q, str(d / "questions.yaml")) for q in raw.get("questions", [])]

    def load_triggers(self, id_or_slug: str, *, include_processed: bool = True) -> list[Trigger]:
        thesis_id = self._resolve_id(id_or_slug)
        d = self._dir_for(thesis_id)
        out: list[Trigger] = []
        inbox = d / "inbox"
        if inbox.is_dir():
            for f in sorted(inbox.glob("*.yaml")):
                raw = load_raw_yaml(f)
                self._check_file_owner(raw, str(f), thesis_id)
                t = Trigger.from_dict(raw, str(f))
                if not include_processed and t.status == TriggerStatus.PROCESSED.value:
                    continue
                out.append(t)
        out.sort(key=lambda t: t.created_at)
        return out

    def _resolve_id(self, id_or_slug: str) -> str:
        found, bad_dirs, bad_ids = self._scan_all()
        if id_or_slug in found:
            return id_or_slug
        for tid, d in found.items():
            if self._load_in_dir(d).slug == id_or_slug:
                return tid
        self._unknown_thesis(id_or_slug, bad_dirs, bad_ids)

    @staticmethod
    def _check_file_owner(raw: dict, path: str, thesis_id: str) -> None:
        if raw.get("thesis_id") != thesis_id:
            raise ValueError(f"{path}: thesis_id mismatch: file has {raw.get('thesis_id')!r}, expected {thesis_id!r}")

    def answer_questions(self, thesis_id: str, answered: list[dict]) -> None:
        """Mark questions answered (validate-then-replace questions.yaml, lock-held)."""
        if not answered:
            return
        thesis_dir = self.dir_for_thesis(thesis_id)
        with thesis_lock(thesis_dir):
            path = str(thesis_dir / "questions.yaml")
            raw = load_raw_yaml(thesis_dir / "questions.yaml")
            self._check_file_owner(raw, path, thesis_id)
            by_id = {q.get("question_id"): q for q in raw.get("questions", [])}
            for a in answered:
                target = by_id.get(a["question_id"])
                if target is None:
                    raise ValueError(f"{path}: question {a['question_id']!r} vanished mid-run")
                target["status"] = QuestionStatus.ANSWERED.value
                target["answer"] = a["answer"]
            for q in raw.get("questions", []):
                ThesisQuestion.from_dict(q, path)
            atomic_write_yaml(thesis_dir / "questions.yaml", raw, self.root)

    def normalize_watch(self, thesis_id: str) -> None:
        """Heal watch.yaml: rules without a deterministic backing stay disabled/unsupported."""
        from app.thesis.monitor import SUPPORTED_HANDLERS  # local: monitor owns the handler table

        thesis_dir = self.dir_for_thesis(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            path = thesis_dir / "watch.yaml"
            raw = load_raw_yaml(path)
            self._check_file_owner(raw, str(path), thesis_id)
            changed = False
            for r in raw.get("rules", []):
                if r.get("rule_type") in SUPPORTED_HANDLERS:
                    continue
                if not r.get("enabled") and r.get("support_status") == "unsupported":
                    continue
                r["enabled"] = False
                r["support_status"] = "unsupported"
                if not r.get("support_reason"):
                    r["support_reason"] = (
                        f"no deterministic monitor backing for {r.get('rule_type')!r}; never queried")
                changed = True
            if changed:
                atomic_write_yaml(path, raw, self.root)

    def load_watch_rules(self, thesis_id: str) -> list[WatchRule]:
        """Read validated watch rules (ID-or-slug lookup)."""
        thesis = self.load_thesis(thesis_id)
        thesis_dir = self.dir_for_thesis(thesis.thesis_id)
        path = str(thesis_dir / "watch.yaml")
        raw = load_raw_yaml(thesis_dir / "watch.yaml")
        self._check_file_owner(raw, path, thesis.thesis_id)
        return [WatchRule.from_dict(r, path) for r in raw.get("rules", [])]


    # -- creation ---------------------------------------------------------

    def create_thesis(
        self,
        user_thesis: str,
        scope: str = "unknown",
        claims: list = (),
        assumptions: list = (),
        invalidators: list = (),
        unknowns: list = (),
        expressions: list = (),
        requirements: list = (),
    ) -> Thesis:
        if not isinstance(user_thesis, str) or not user_thesis.strip():
            raise ValueError("<create>: 'user_thesis' must be a non-empty string")
        thesis_id = new_thesis_id()
        now = _utcnow()
        thesis = Thesis(
            thesis_id=thesis_id,
            slug="tmp",  # replaced below; Thesis.validate does not check slug shape
            status=models.ThesisStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
            user_thesis=user_thesis.strip(),
            scope=scope or "unknown",
            claims=tuple(_coerce_claim(c) for c in (claims or [])),
            assumptions=tuple(assumptions or []),
            invalidators=tuple(invalidators or []),
            unknowns=tuple(unknowns or []),
            expressions=tuple(_coerce_expression(e) for e in (expressions or [])),
            requirements=tuple(),
        )
        # Coerce requirement dicts now that expression IDs exist.
        req_objs = []
        for r in requirements or []:
            if isinstance(r, dict):
                d = dict(r)
                d.setdefault("requirement_id", new_requirement_id())
                req_objs.append(models.ExpressionRequirement.from_dict(d, "<create>"))
            else:
                req_objs.append(r)
        thesis = Thesis(
            thesis_id=thesis.thesis_id, slug="tmp", status=thesis.status, created_at=thesis.created_at,
            updated_at=thesis.updated_at, user_thesis=thesis.user_thesis, scope=thesis.scope,
            claims=thesis.claims, assumptions=thesis.assumptions, invalidators=thesis.invalidators,
            unknowns=thesis.unknowns, expressions=thesis.expressions, requirements=tuple(req_objs),
        )
        thesis.validate("<create>")

        words = re.findall(r"[A-Za-z0-9]+", user_thesis)[:8] or ["thesis"]
        try:
            base = slugify("-".join(words))
        except ValueError:
            base = "thesis"
        self.root.mkdir(parents=True, exist_ok=True)
        slug = base
        n = 2
        while (self.root / slug).exists():  # shortest numeric suffix on collision
            slug = f"{base}-{n}"
            n += 1
        thesis = Thesis(
            thesis_id=thesis.thesis_id, slug=slug, status=thesis.status, created_at=thesis.created_at,
            updated_at=thesis.updated_at, user_thesis=thesis.user_thesis, scope=thesis.scope,
            claims=thesis.claims, assumptions=thesis.assumptions, invalidators=thesis.invalidators,
            unknowns=thesis.unknowns, expressions=thesis.expressions, requirements=thesis.requirements,
        )
        thesis_dir = self.root / slug
        (thesis_dir / "inbox").mkdir(parents=True)
        (thesis_dir / "evidence").mkdir(parents=True)
        (thesis_dir / "journal").mkdir(parents=True)
        with thesis_lock(thesis_dir):
            atomic_write_yaml(thesis_dir / "thesis.yaml", thesis.to_dict(), self.root)
            atomic_write_yaml(
                thesis_dir / "state.yaml",
                ThesisState(thesis_id=thesis_id, assessment="unresolved").to_dict(), self.root,
            )
            atomic_write_yaml(
                thesis_dir / "watch.yaml",
                {"schema_version": SCHEMA_VERSION, "thesis_id": thesis_id, "rules": []}, self.root,
            )
            atomic_write_yaml(
                thesis_dir / "questions.yaml",
                {"schema_version": SCHEMA_VERSION, "thesis_id": thesis_id, "questions": []}, self.root,
            )
            atomic_write_yaml(
                thesis_dir / "memory.yaml",
                {"schema_version": SCHEMA_VERSION, "thesis_id": thesis_id, "memories": []}, self.root,
            )
            atomic_write_yaml(
                thesis_dir / "checkpoint.yaml",
                Checkpoint(thesis_id=thesis_id).to_dict(), self.root,
            )
        return thesis

    # -- mutations (all hold the thesis lock + validate candidate first) ---

    def update_thesis(self, id_or_slug: str, **patch: Any) -> Thesis:
        thesis_id = self._resolve_id(id_or_slug)
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            if "thesis_id" in patch and patch["thesis_id"] != thesis_id:
                raise ValueError(f"{thesis_dir}/thesis.yaml: 'thesis_id' is immutable")
            data = thesis.to_dict()
            data.update(patch)
            data["thesis_id"] = thesis_id
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            return candidate

    def _set_status(self, thesis_id: str, status: str) -> Thesis:
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            if thesis.status == models.ThesisStatus.CLOSED.value:
                raise ValueError(f"{thesis_dir}/thesis.yaml: thesis {thesis_id!r} is closed; cannot change status")
            data = thesis.to_dict()
            data["status"] = status
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            return candidate

    def pause_thesis(self, thesis_id: str) -> Thesis:
        return self._set_status(thesis_id, models.ThesisStatus.PAUSED.value)

    def resume_thesis(self, thesis_id: str) -> Thesis:
        return self._set_status(thesis_id, models.ThesisStatus.ACTIVE.value)

    def close_thesis(self, thesis_id: str) -> Thesis:
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            data = thesis.to_dict()
            data["status"] = models.ThesisStatus.CLOSED.value
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            return candidate

    def append_journal_entry(self, thesis_id: str, entry: dict) -> Path:
        """Idempotent by entry/journal id: retrying with the same id is a no-op."""
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            d = dict(entry)
            entry_id = d.get("entry_id") or d.get("journal_id") or new_journal_id()
            for existing in (thesis_dir / "journal").glob("*.md"):
                try:
                    head = existing.read_text(encoding="utf-8").split("---")
                    if len(head) >= 3 and f"entry_id: {entry_id}" in head[1]:
                        return existing  # idempotent retry
                except OSError:
                    continue
            created = d.get("created_at") or _utcnow()
            title = d.get("title", f"Journal {entry_id}")
            body = d.get("body", d.get("summary", ""))
            dest = thesis_dir / "journal" / f"{_safe_name(entry_id)}.md"
            # Keep journal writes inside root (same traversal guard as YAML).
            from app.thesis.yaml import _resolve_inside  # local to avoid export churn

            _resolve_inside(self.root, dest)
            dest.write_text(
                _journal_front_matter(entry_id, thesis_id, created, d)
                + f"# {title}\n\n{body}\n",
                encoding="utf-8",
            )
            return dest

    def create_trigger(
        self,
        thesis_id: str,
        trigger_type: str = "new_external_evidence",
        importance: str = "medium",
        claim_ids: list = (),
        expression_ids: list = (),
        canonical_refs: list = (),
        summary: str = "",
        metadata: dict | None = None,
    ) -> Trigger:
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            if thesis.status == models.ThesisStatus.CLOSED.value:
                raise ValueError(f"{thesis_dir}: thesis {thesis_id!r} is closed; cannot create triggers")
            known_claims = {c.claim_id for c in thesis.claims}
            known_exprs = {e.expression_id for e in thesis.expressions}
            for cid in claim_ids or []:
                if cid not in known_claims:
                    raise ValueError(f"{thesis_dir}: trigger references absent claim {cid!r}")
            for eid in expression_ids or []:
                if eid not in known_exprs:
                    raise ValueError(f"{thesis_dir}: trigger references absent expression {eid!r}")
            trigger = Trigger(
                trigger_id=new_trigger_id(),
                thesis_id=thesis_id,
                created_at=_utcnow(),
                status=TriggerStatus.PENDING.value,
                trigger_type=trigger_type,
                importance=importance,
                claim_ids=tuple(claim_ids or []),
                expression_ids=tuple(expression_ids or []),
                canonical_refs=tuple(canonical_refs or []),
                summary=summary,
                metadata=dict(metadata or {}),
            )
            # Validate enums before persisting.
            Trigger.from_dict(trigger.to_dict(), str(thesis_dir / "inbox"))
            dest = thesis_dir / "inbox" / f"{_safe_name(trigger.trigger_id)}.yaml"
            atomic_write_yaml(dest, {"schema_version": SCHEMA_VERSION, **trigger.to_dict()}, self.root)
            return trigger

    def mark_trigger_processed(self, thesis_id: str, trigger_id: str, run_id: str = "", metadata: dict | None = None) -> Trigger:
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            target = thesis_dir / "inbox" / f"{_safe_name(trigger_id)}.yaml"
            if not target.is_file():
                # Fall back to scanning (older filenames): never delete, just find.
                matches = []
                for f in (thesis_dir / "inbox").glob("*.yaml"):
                    raw = load_raw_yaml(f)
                    if raw.get("trigger_id") == trigger_id:
                        matches.append(f)
                if not matches:
                    raise KeyError(f"unknown trigger: {trigger_id!r}")
                target = matches[0]
            raw = load_raw_yaml(target)
            self._check_file_owner(raw, str(target), thesis_id)
            raw["status"] = TriggerStatus.PROCESSED.value
            raw["processed_at"] = _utcnow()
            if run_id:
                raw["run_id"] = run_id
            if metadata:
                merged = dict(raw.get("metadata") or {})
                merged.update(metadata)
                raw["metadata"] = merged
            updated = Trigger.from_dict(raw, str(target))
            atomic_write_yaml(target, {"schema_version": SCHEMA_VERSION, **updated.to_dict()}, self.root)
            return updated

    def load_checkpoint(self, id_or_slug: str) -> Checkpoint:
        """Read validated checkpoint.yaml (lock-held; KeyError on unknown thesis)."""
        thesis_id = self._resolve_id(id_or_slug)
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            raw = load_raw_yaml(thesis_dir / "checkpoint.yaml")
            self._check_file_owner(raw, str(thesis_dir / "checkpoint.yaml"), thesis_id)
            return Checkpoint.from_dict(raw, str(thesis_dir / "checkpoint.yaml"))

    def save_checkpoint(self, thesis_id: str, checkpoint: Checkpoint | dict) -> Checkpoint:
        """Validate-then-replace checkpoint.yaml (lock-held, atomic)."""
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            data = checkpoint.to_dict() if isinstance(checkpoint, Checkpoint) else dict(checkpoint)
            data["thesis_id"] = thesis_id
            candidate = Checkpoint.from_dict(data, str(thesis_dir / "checkpoint.yaml"))
            atomic_write_yaml(thesis_dir / "checkpoint.yaml", candidate.to_dict(), self.root)
            return candidate


    def apply_research_result(self, thesis_id: str, result: dict, run_id: str = "") -> dict:
        """Runner writeback in fixed order; de-dup additions by ID (crash-retry safe).

        Order: evidence refs -> state -> questions -> memory -> watch -> journal
        -> trigger processed metadata. Never deletes triggers.
        """
        thesis_dir = self._dir_for(thesis_id)
        outcome: dict[str, Any] = {}
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            if thesis.status == models.ThesisStatus.CLOSED.value:
                raise ValueError(f"{thesis_dir}: thesis {thesis_id!r} is closed; refusing research writeback")

            # 1. evidence refs (one file per ref, skip existing IDs)
            for ref in result.get("evidence_refs", []) or []:
                d = dict(ref)
                d.setdefault("evidence_id", new_evidence_id())
                d["thesis_id"] = thesis_id
                ev = EvidenceRef.from_dict(d, str(thesis_dir / "evidence"))
                dest = thesis_dir / "evidence" / f"{_safe_name(ev.evidence_id)}.yaml"
                if dest.is_file():
                    continue
                atomic_write_yaml(dest, {"schema_version": SCHEMA_VERSION, **ev.to_dict()}, self.root)
            outcome["evidence"] = len(result.get("evidence_refs", []) or [])

            # 2. state.yaml (full-replace candidate, validated)
            if result.get("state") is not None:
                sdata = result["state"] if isinstance(result["state"], dict) else result["state"].to_dict()
                sdata = dict(sdata)
                sdata["thesis_id"] = thesis_id
                candidate = ThesisState.from_dict(sdata, str(thesis_dir / "state.yaml"))
                atomic_write_yaml(thesis_dir / "state.yaml", candidate.to_dict(), self.root)

            # 3. questions (append by ID, skip known)
            if result.get("questions_add"):
                raw = load_raw_yaml(thesis_dir / "questions.yaml")
                self._check_file_owner(raw, str(thesis_dir / "questions.yaml"), thesis_id)
                known = {q.get("question_id") for q in raw.get("questions", [])}
                for q in result["questions_add"]:
                    d = dict(q)
                    d.setdefault("question_id", new_question_id())
                    qobj = ThesisQuestion.from_dict(d, str(thesis_dir / "questions.yaml"))
                    if qobj.question_id in known:
                        continue
                    raw.setdefault("questions", []).append(qobj.to_dict())
                    known.add(qobj.question_id)
                atomic_write_yaml(thesis_dir / "questions.yaml", raw, self.root)

            # 4. memory (append by ID, skip known)
            if result.get("memories_add"):
                raw = load_raw_yaml(thesis_dir / "memory.yaml")
                self._check_file_owner(raw, str(thesis_dir / "memory.yaml"), thesis_id)
                known = {m.get("memory_id") for m in raw.get("memories", [])}
                for m in result["memories_add"]:
                    d = dict(m)
                    d.setdefault("memory_id", new_memory_id())
                    d.setdefault("created_at", _utcnow())
                    mobj = ThesisMemory.from_dict(d, str(thesis_dir / "memory.yaml"))
                    if mobj.memory_id in known:
                        continue
                    raw.setdefault("memories", []).append(mobj.to_dict())
                    known.add(mobj.memory_id)
                atomic_write_yaml(thesis_dir / "memory.yaml", raw, self.root)

            # 5. watch (append rules by ID with cross-ref checks)
            if result.get("watch_add"):
                raw = load_raw_yaml(thesis_dir / "watch.yaml")
                self._check_file_owner(raw, str(thesis_dir / "watch.yaml"), thesis_id)
                known = {r.get("rule_id") for r in raw.get("rules", [])}
                claim_ids = {c.claim_id for c in thesis.claims}
                expr_ids = {e.expression_id for e in thesis.expressions}
                for r in result["watch_add"]:
                    d = dict(r)
                    d.setdefault("rule_id", f"rule:{__import__('uuid').uuid4()}")
                    robj = WatchRule.from_dict(d, str(thesis_dir / "watch.yaml"))
                    for cid in robj.claim_ids:
                        if cid not in claim_ids:
                            raise ValueError(f"{thesis_dir}/watch.yaml: rule references absent claim {cid!r}")
                    for eid in robj.expression_ids:
                        if eid not in expr_ids:
                            raise ValueError(f"{thesis_dir}/watch.yaml: rule references absent expression {eid!r}")
                    if robj.rule_id in known:
                        continue
                    raw.setdefault("rules", []).append(robj.to_dict())
                    known.add(robj.rule_id)
                atomic_write_yaml(thesis_dir / "watch.yaml", raw, self.root)

            # 6. journal (idempotent by entry id)
            if result.get("journal_entry") is not None:
                jd = dict(result["journal_entry"])
                jd.setdefault("run_id", run_id)
                if result.get("trigger_id"):
                    jd.setdefault("trigger_id", result["trigger_id"])
                # Inline (already holding the lock): reuse file-level logic without re-locking.
                entry_id = jd.get("entry_id") or jd.get("journal_id") or new_journal_id()
                existing = None
                for f in (thesis_dir / "journal").glob("*.md"):
                    try:
                        if f"entry_id: {entry_id}" in f.read_text(encoding="utf-8").split("---")[1]:
                            existing = f
                            break
                    except (OSError, IndexError):
                        continue
                if existing is None:
                    from app.thesis.yaml import _resolve_inside  # local to avoid export churn

                    dest = thesis_dir / "journal" / f"{_safe_name(entry_id)}.md"
                    _resolve_inside(self.root, dest)
                    dest.write_text(
                        _journal_front_matter(entry_id, thesis_id, _utcnow(), jd)
                        + f"# {jd.get('title', 'Research run')}\n\n{jd.get('body', jd.get('summary', ''))}\n",
                        encoding="utf-8",
                    )
                    outcome["journal"] = str(dest)
                else:
                    outcome["journal"] = str(existing)

            # 7. trigger processed metadata (never delete)
            if result.get("trigger_id"):
                raw_trigger = None
                tpath = thesis_dir / "inbox" / f"{_safe_name(result['trigger_id'])}.yaml"
                if tpath.is_file():
                    raw_trigger = load_raw_yaml(tpath)
                    self._check_file_owner(raw_trigger, str(tpath), thesis_id)
                    raw_trigger["status"] = TriggerStatus.PROCESSED.value
                    raw_trigger["processed_at"] = _utcnow()
                    if run_id:
                        raw_trigger["run_id"] = run_id
                    updated = Trigger.from_dict(raw_trigger, str(tpath))
                    atomic_write_yaml(tpath, {"schema_version": SCHEMA_VERSION, **updated.to_dict()}, self.root)
                    outcome["trigger"] = updated.trigger_id
        return outcome
