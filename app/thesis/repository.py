"""Filesystem repository: the ownership boundary for thesis YAML/journal/trigger writes."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

import yaml

from app.thesis import models
from app.thesis.models import (
    SCHEMA_VERSION,
    Checkpoint,
    EvidenceRef,
    ExpressionRequirement,
    HistoricalStateUnavailable,
    QuestionStatus,
    Thesis,
    ThesisClaim,
    ThesisMemory,
    ThesisQuestion,
    ThesisState,
    ThesisStateSnapshot,
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
    require_watch_targets,
    slugify,
)
from app.thesis.yaml import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
    load_raw_yaml,
    load_yaml,
    thesis_lock,
)

STATE_FILES = ("thesis.yaml", "state.yaml", "watch.yaml", "questions.yaml", "memory.yaml", "checkpoint.yaml")

# Evidence refs carry canonical refs + summaries only. Anything else smuggles
# bodies or model-invented provenance (URLs, publication/retrieval metadata).
_FORBIDDEN_EVIDENCE_KEYS = frozenset({
    "body", "content", "filing_body", "full_text", "document_text",
    "url", "urls", "link", "links", "source_url",
    "provenance", "publication", "published_at", "retrieved_at", "retrieval", "origin",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _safe_name(trigger_or_entry_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", trigger_or_entry_id)


def _trigger_created(trigger: Trigger) -> str:
    return trigger.created_at


def _best_effort_thesis_id(thesis_file: Path) -> str | None:
    """Read a ``thesis_id`` from an unloadable thesis.yaml (None when unreadable)."""
    try:
        data = yaml.safe_load(thesis_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("thesis_id"), str) and data["thesis_id"]:
        return data["thesis_id"]
    return None


def _journal_front_matter(entry_id: str, thesis_id: str, created: str, d: dict[str, Any]) -> str:
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
        out.sort(key=_trigger_created)
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
    def _check_file_owner(raw: dict[str, Any], path: str, thesis_id: str) -> None:
        if raw.get("thesis_id") != thesis_id:
            raise ValueError(f"{path}: thesis_id mismatch: file has {raw.get('thesis_id')!r}, expected {thesis_id!r}")

    def _latest_effective_locked(self, thesis_dir: Path) -> tuple[datetime, int] | None:
        """Latest (effective_at, version) in history; None when empty (caller takes the live path)."""
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        hdir = thesis_dir / "history"
        if not hdir.is_dir():
            return None
        pairs: list[tuple[datetime, int]] = []
        for f in hdir.glob("*.yaml"):
            try:
                version = int(f.stem)
            except ValueError:
                continue
            try:
                raw = load_raw_yaml(f)
            except Exception:
                continue
            eff_raw = raw.get("effective_at") if isinstance(raw, dict) else None
            dt = _as_dt(str(eff_raw)) if eff_raw is not None else None
            if dt is None:
                continue
            pairs.append((dt, version))
        if not pairs:
            return None
        return (max(dt for dt, _ in pairs), max(v for _, v in pairs))

    def _write_snapshot_from_dicts_locked(
        self, thesis_dir: Path, thesis_id: str, *, effective_at: str, reason: str,
        run_id: str = "", trigger_id: str = "",
        thesis: dict[str, Any], state: dict[str, Any], questions: dict[str, Any], watch: dict[str, Any], memory: dict[str, Any],
    ) -> ThesisStateSnapshot:
        """Write one versioned snapshot from caller-supplied dicts; caller must hold thesis_lock."""
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        if _as_dt(effective_at) is None:
            raise ValueError(f"{thesis_dir}/history: bad effective_at {effective_at!r}")
        hdir = thesis_dir / "history"
        hdir.mkdir(parents=True, exist_ok=True)
        existing = []
        for f in hdir.glob("*.yaml"):
            try:
                existing.append(int(f.stem))
            except ValueError:
                continue
        version = (max(existing) if existing else 0) + 1
        snap = ThesisStateSnapshot(
            thesis_id=thesis_id, version=version, effective_at=effective_at, recorded_at=_utcnow(),
            reason=reason, run_id=run_id or "", trigger_id=trigger_id or "",
            thesis=thesis, state=state,
            questions=questions, watch=watch, memory=memory,
        )
        dest = hdir / f"{version:08d}.yaml"
        validated = ThesisStateSnapshot.from_dict(snap.to_dict(), str(dest))
        atomic_write_yaml(dest, validated.to_dict(), self.root)
        return validated

    def _snapshot_state_locked(
        self, thesis_dir: Path, thesis_id: str, *, effective_at: str, reason: str,
        run_id: str = "", trigger_id: str = "",
    ) -> ThesisStateSnapshot:
        """Write one versioned history snapshot; caller must hold thesis_lock."""
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        if _as_dt(effective_at) is None:
            raise ValueError(f"{thesis_dir}/history: bad effective_at {effective_at!r}")
        thesis = load_yaml(thesis_dir / "thesis.yaml", Thesis)
        if thesis.thesis_id != thesis_id:
            raise ValueError(
                f"{thesis_dir}: thesis_id mismatch: file has {thesis.thesis_id!r}, expected {thesis_id!r}")
        state = load_yaml(thesis_dir / "state.yaml", ThesisState)
        qpath, wpath, mpath = (str(thesis_dir / "questions.yaml"), str(thesis_dir / "watch.yaml"),
                                str(thesis_dir / "memory.yaml"))
        questions = load_raw_yaml(thesis_dir / "questions.yaml")
        self._check_file_owner(questions, qpath, thesis_id)
        watch = load_raw_yaml(thesis_dir / "watch.yaml")
        self._check_file_owner(watch, wpath, thesis_id)
        memory = load_raw_yaml(thesis_dir / "memory.yaml")
        self._check_file_owner(memory, mpath, thesis_id)
        existing = []
        for f in (thesis_dir / "history").glob("*.yaml"):
            try:
                existing.append(int(f.stem))
            except ValueError:
                continue
        if not existing and reason != "thesis_created":
            reason = "history_migration"
            effective_at = thesis.updated_at if _as_dt(thesis.updated_at) is not None else _utcnow()
        return self._write_snapshot_from_dicts_locked(
            thesis_dir, thesis_id, effective_at=effective_at, reason=reason,
            run_id=run_id, trigger_id=trigger_id,
            thesis=thesis.to_dict(), state=state.to_dict(),
            questions=dict(questions), watch=dict(watch), memory=dict(memory),
        )

    def load_state_as_of(self, thesis_id: str, known_at: str) -> ThesisStateSnapshot:
        """Latest snapshot with effective_at <= known_at; fail closed before history."""
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        cutoff = _as_dt(known_at)
        if cutoff is None:
            raise ValueError(f"<history>: bad known_at {known_at!r}")
        thesis_dir = self._dir_for(thesis_id)
        hdir = thesis_dir / "history"
        files: list[Path] = sorted(hdir.glob("*.yaml")) if hdir.is_dir() else []
        if not files:
            raise HistoricalStateUnavailable(
                f"{hdir}: no state for {thesis_id!r} as of {known_at!r} (no history)")
        snaps = [ThesisStateSnapshot.from_dict(load_raw_yaml(f), str(f)) for f in files]
        eligible = [s for s in snaps if (d := _as_dt(s.effective_at)) is not None and d <= cutoff]
        if not eligible:
            earliest = min(s.effective_at for s in snaps)
            raise HistoricalStateUnavailable(
                f"{hdir}: no state for {thesis_id!r} as of {known_at!r} (earliest {earliest!r})")
        eligible.sort(key=lambda s: (_as_dt(s.effective_at), s.version))  # type: ignore[return-value]
        return eligible[-1]

    def list_state_versions(self, thesis_id: str) -> list[int]:
        """Sorted history version ints (debug/tests only)."""
        thesis_dir = self._dir_for(thesis_id)
        hdir = thesis_dir / "history"
        if not hdir.is_dir():
            return []
        out = []
        for f in sorted(hdir.glob("*.yaml")):
            try:
                out.append(int(f.stem))
            except ValueError:
                continue
        return sorted(out)


    def answer_questions(self, thesis_id: str, answered: list[dict[str, Any]], *, effective_at: str | None = None) -> None:
        """Mark questions answered (validate-then-replace questions.yaml, lock-held)."""
        if not answered:
            return
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<answer>: bad effective_at {effective_at!r}")
        thesis_dir = self.dir_for_thesis(thesis_id)
        with thesis_lock(thesis_dir):
            path = str(thesis_dir / "questions.yaml")
            raw = load_raw_yaml(thesis_dir / "questions.yaml")
            self._check_file_owner(raw, path, thesis_id)
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                base = self.load_state_as_of(thesis_id, eff)
                raw = {"schema_version": base.questions.get("schema_version", SCHEMA_VERSION),
                       "thesis_id": thesis_id,
                       "questions": [dict(q) for q in base.questions.get("questions", [])]}
                by_id = {q.get("question_id"): q for q in raw.get("questions", [])}
                for a in answered:
                    target = by_id.get(a["question_id"])
                    if target is None:
                        raise ValueError(f"{path}: question {a['question_id']!r} vanished mid-run")
                    target["status"] = QuestionStatus.ANSWERED.value
                    target["answer"] = a["answer"]
                for q in raw.get("questions", []):
                    ThesisQuestion.from_dict(q, path)
                self._write_snapshot_from_dicts_locked(
                    thesis_dir, thesis_id, effective_at=eff, reason="questions_answered",
                    thesis=dict(base.thesis), state=dict(base.state),
                    questions=raw, watch=dict(base.watch), memory=dict(base.memory))
                return
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
            self._snapshot_state_locked(thesis_dir, thesis_id, effective_at=eff, reason="questions_answered")

    def normalize_watch(self, thesis_id: str, *, effective_at: str | None = None) -> None:
        """Heal watch.yaml: rules without a deterministic backing stay disabled/unsupported."""
        from app.thesis.monitor import SUPPORTED_HANDLERS, _as_dt  # local: monitor owns the handler table + clock

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<watch>: bad effective_at {effective_at!r}")
        thesis_dir = self.dir_for_thesis(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            path = thesis_dir / "watch.yaml"
            raw = load_raw_yaml(path)
            self._check_file_owner(raw, str(path), thesis_id)
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                base = self.load_state_as_of(thesis_id, eff)
                raw = {"schema_version": base.watch.get("schema_version", SCHEMA_VERSION),
                       "thesis_id": thesis_id,
                       "rules": [dict(r) for r in base.watch.get("rules", [])]}
                changed = False
                for r in raw.get("rules", []):
                    if r.get("rule_type") in SUPPORTED_HANDLERS:
                        continue
                    if not r.get("enabled") and r.get("support_status") == "unsupported":
                        continue
                    r["enabled"] = False
                    r["support_status"] = "unsupported"
                    if not r.get("support_reason"):
                        if r.get("rule_type") == "new_external_evidence":
                            r["support_reason"] = (
                                "no production source for 'new_external_evidence'; never queried")
                        else:
                            r["support_reason"] = (
                                f"no deterministic monitor backing for {r.get('rule_type')!r}; never queried")
                    changed = True
                if changed:
                    self._write_snapshot_from_dicts_locked(
                        thesis_dir, thesis_id, effective_at=eff, reason="watch_normalized",
                        thesis=dict(base.thesis), state=dict(base.state),
                        questions=dict(base.questions), watch=raw, memory=dict(base.memory))
                return
            changed = False
            for r in raw.get("rules", []):
                if r.get("rule_type") in SUPPORTED_HANDLERS:
                    continue
                if not r.get("enabled") and r.get("support_status") == "unsupported":
                    continue
                r["enabled"] = False
                r["support_status"] = "unsupported"
                if not r.get("support_reason"):
                    if r.get("rule_type") == "new_external_evidence":
                        r["support_reason"] = (
                            "no production source for 'new_external_evidence'; never queried")
                    else:
                        r["support_reason"] = (
                            f"no deterministic monitor backing for {r.get('rule_type')!r}; never queried")
                changed = True
            if changed:
                atomic_write_yaml(path, raw, self.root)
                self._snapshot_state_locked(thesis_dir, thesis_id, effective_at=eff, reason="watch_normalized")

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
        claims: Sequence[str | dict[str, Any] | ThesisClaim] = (),
        assumptions: Sequence[str] = (),
        invalidators: Sequence[str] = (),
        unknowns: Sequence[str] = (),
        expressions: Sequence[dict[str, Any] | TradeExpression] = (),
        requirements: Sequence[dict[str, Any] | ExpressionRequirement] = (),
        watch_rules: Sequence[dict[str, Any]] = (),
        *,
        effective_at: str | None = None,
    ) -> Thesis:
        if not isinstance(user_thesis, str) or not user_thesis.strip():
            raise ValueError("<create>: 'user_thesis' must be a non-empty string")
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        if _as_dt(eff) is None:
            raise ValueError(f"<create>: bad effective_at {effective_at!r}")
        thesis_id = new_thesis_id()
        now = eff
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
        # Initial watch rules persist in the same commit; cross-refs must name
        # claims/expressions created above (never invented thresholds).
        claim_ids = {c.claim_id for c in thesis.claims}
        expr_ids = {e.expression_id for e in thesis.expressions}
        rule_objs = []
        for r in watch_rules or []:
            robj = WatchRule.from_dict(dict(r), "<create>")
            for cid in robj.claim_ids:
                if cid not in claim_ids:
                    raise ValueError(f"<create>: rule references absent claim {cid!r}")
            for eid in robj.expression_ids:
                if eid not in expr_ids:
                    raise ValueError(f"<create>: rule references absent expression {eid!r}")
            rule_objs.append(robj)

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
                {"schema_version": SCHEMA_VERSION, "thesis_id": thesis_id,
                 "rules": [r.to_dict() for r in rule_objs]}, self.root,
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
            self._snapshot_state_locked(
                thesis_dir, thesis_id, effective_at=thesis.created_at, reason="thesis_created")
        return thesis

    # -- mutations (all hold the thesis lock + validate candidate first) ---

    def update_thesis(self, id_or_slug: str, *, effective_at: str | None = None, **patch: Any) -> Thesis:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<update>: bad effective_at {effective_at!r}")
        thesis_id = self._resolve_id(id_or_slug)
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            if "thesis_id" in patch and patch["thesis_id"] != thesis_id:
                raise ValueError(f"{thesis_dir}/thesis.yaml: 'thesis_id' is immutable")
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                base = self.load_state_as_of(thesis_id, eff)
                if isinstance(patch.get("claims"), list):
                    base_claim_ids = {c.get("claim_id") for c in base.thesis.get("claims", []) if isinstance(c, dict)} - {None}
                    patch_claim_ids = {c.get("claim_id") for c in patch["claims"] if isinstance(c, dict)}
                    missing = base_claim_ids - patch_claim_ids
                    if missing:
                        cid = sorted(missing, key=str)[0]
                        raise ValueError(
                            f"{thesis_dir}/thesis.yaml: claim {cid!r} does not belong to thesis {thesis_id!r}")
                if isinstance(patch.get("expressions"), list):
                    base_expr_ids = {e.get("expression_id") for e in base.thesis.get("expressions", []) if isinstance(e, dict)} - {None}
                    patch_expr_ids = {e.get("expression_id") for e in patch["expressions"] if isinstance(e, dict)}
                    missing = base_expr_ids - patch_expr_ids
                    if missing:
                        eid = sorted(missing, key=str)[0]
                        raise ValueError(
                            f"{thesis_dir}/thesis.yaml: expression {eid!r} does not belong to thesis {thesis_id!r}")
                data = dict(base.thesis)
                data.update(patch)
                data["thesis_id"] = thesis_id
                data["updated_at"] = eff
                candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
                self._write_snapshot_from_dicts_locked(
                    thesis_dir, thesis_id, effective_at=eff, reason="thesis_updated",
                    thesis=candidate.to_dict(), state=dict(base.state),
                    questions=dict(base.questions), watch=dict(base.watch), memory=dict(base.memory))
                return candidate
            data = thesis.to_dict()
            data.update(patch)
            data["thesis_id"] = thesis_id
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            self._snapshot_state_locked(thesis_dir, thesis_id, effective_at=eff, reason="thesis_updated")
            return candidate

    def _set_status(self, thesis_id: str, status: str, *, effective_at: str | None = None) -> Thesis:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<status>: bad effective_at {effective_at!r}")
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                base = self.load_state_as_of(thesis_id, eff)
                if base.thesis.get("status") == models.ThesisStatus.CLOSED.value:
                    raise ValueError(f"{thesis_dir}/thesis.yaml: thesis {thesis_id!r} is closed; cannot change status")
                data = dict(base.thesis)
                data["status"] = status
                data["updated_at"] = eff
                data["thesis_id"] = thesis_id
                candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
                self._write_snapshot_from_dicts_locked(
                    thesis_dir, thesis_id, effective_at=eff, reason="status_changed",
                    thesis=candidate.to_dict(), state=dict(base.state),
                    questions=dict(base.questions), watch=dict(base.watch), memory=dict(base.memory))
                return candidate
            if thesis.status == models.ThesisStatus.CLOSED.value:
                raise ValueError(f"{thesis_dir}/thesis.yaml: thesis {thesis_id!r} is closed; cannot change status")
            data = thesis.to_dict()
            data["status"] = status
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            self._snapshot_state_locked(thesis_dir, thesis_id, effective_at=eff, reason="status_changed")
            return candidate

    def pause_thesis(self, thesis_id: str, *, effective_at: str | None = None) -> Thesis:
        return self._set_status(thesis_id, models.ThesisStatus.PAUSED.value, effective_at=effective_at)

    def resume_thesis(self, thesis_id: str, *, effective_at: str | None = None) -> Thesis:
        return self._set_status(thesis_id, models.ThesisStatus.ACTIVE.value, effective_at=effective_at)

    def close_thesis(self, thesis_id: str, *, effective_at: str | None = None) -> Thesis:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<status>: bad effective_at {effective_at!r}")
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                base = self.load_state_as_of(thesis_id, eff)
                data = dict(base.thesis)
                data["status"] = models.ThesisStatus.CLOSED.value
                data["updated_at"] = eff
                data["thesis_id"] = thesis_id
                candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
                self._write_snapshot_from_dicts_locked(
                    thesis_dir, thesis_id, effective_at=eff, reason="status_changed",
                    thesis=candidate.to_dict(), state=dict(base.state),
                    questions=dict(base.questions), watch=dict(base.watch), memory=dict(base.memory))
                return candidate
            data = thesis.to_dict()
            data["status"] = models.ThesisStatus.CLOSED.value
            data["updated_at"] = _utcnow()
            candidate = Thesis.from_dict(data, str(thesis_dir / "thesis.yaml"))
            atomic_write_yaml(thesis_dir / "thesis.yaml", candidate.to_dict(), self.root)
            self._snapshot_state_locked(thesis_dir, thesis_id, effective_at=eff, reason="status_changed")
            return candidate

    def append_journal_entry(self, thesis_id: str, entry: dict[str, Any]) -> Path:
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
            atomic_write_text(
                dest,
                _journal_front_matter(entry_id, thesis_id, created, d)
                + f"# {title}\n\n{body}\n",
                self.root,
            )
            return dest

    def has_journal_for_trigger(
        self, thesis_id: str, trigger_id: str, *, known_at: str | None = None, run_id: str | None = None
    ) -> bool:
        """True when a durable journal entry names this thesis and trigger."""
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers
        want = _as_dt(known_at) if known_at is not None else None
        thesis_dir = self._dir_for(thesis_id)
        journal_dir = thesis_dir / "journal"
        if not journal_dir.is_dir():
            return False
        for f in journal_dir.glob("*.md"):
            try:
                head = f.read_text(encoding="utf-8").split("---")
                fm = head[1] if len(head) >= 3 else ""
            except OSError:
                continue
            lines = {ln.split(":", 1)[0].strip(): ln.split(":", 1)[1].strip()
                     for ln in fm.splitlines() if ":" in ln}
            if lines.get("thesis_id") == thesis_id and lines.get("trigger_id") == trigger_id:
                if known_at is not None:
                    if want is None:
                        continue
                    got = _as_dt(lines.get("known_at", "")) if lines.get("known_at") else None
                    if got is None or got != want:
                        continue
                if run_id is not None and lines.get("run_id", "") != run_id:
                    continue
                return True
        return False

    def create_trigger(
        self,
        thesis_id: str,
        trigger_type: str = "new_external_evidence",
        importance: str = "medium",
        claim_ids: Sequence[str] = (),
        expression_ids: Sequence[str] = (),
        canonical_refs: Sequence[str] = (),
        summary: str = "",
        metadata: dict[str, Any] | None = None,
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

    def mark_trigger_processed(self, thesis_id: str, trigger_id: str, run_id: str = "", metadata: dict[str, Any] | None = None) -> Trigger:
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

    def _load_trigger_raw(self, thesis_dir: Path, thesis_id: str, trigger_id: str) -> tuple[Path, dict[str, Any]]:
        """Locate one trigger's raw mapping (direct name, else inbox scan)."""
        direct = thesis_dir / "inbox" / f"{_safe_name(trigger_id)}.yaml"
        if direct.is_file():
            raw = load_raw_yaml(direct)
            self._check_file_owner(raw, str(direct), thesis_id)
            return direct, raw
        for f in sorted((thesis_dir / "inbox").glob("*.yaml")):
            try:
                raw = load_raw_yaml(f)
            except Exception:
                continue
            if raw.get("trigger_id") == trigger_id:
                self._check_file_owner(raw, str(f), thesis_id)
                return f, raw
        raise KeyError(f"unknown trigger: {trigger_id!r}")

    def evidence_canonical_refs(self, thesis_id: str) -> set[str]:
        """Canonical refs of stored evidence files (provenance pool for writeback)."""
        thesis_dir = self._dir_for(thesis_id)
        out: set[str] = set()
        evdir = thesis_dir / "evidence"
        if evdir.is_dir():
            for f in sorted(evdir.glob("*.yaml")):
                try:
                    raw = load_raw_yaml(f)
                except Exception:
                    continue
                if not isinstance(raw, dict) or raw.get("thesis_id") != thesis_id:
                    continue
                ref = raw.get("canonical_ref")
                if isinstance(ref, str) and ref:
                    out.add(ref)
        return out

    def _pending_path(self, thesis_dir: Path, trigger_id: str) -> Path:
        return thesis_dir / "inbox" / f"{_safe_name(trigger_id)}.pending.json"

    def read_pending_result(self, thesis_id: str, trigger_id: str) -> dict[str, Any] | None:
        """Durable research-result intent for crash replay (None when absent)."""
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            p = self._pending_path(thesis_dir, trigger_id)
            if not p.is_file():
                return None
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"{p}: unreadable pending result intent: {exc}") from exc
            if (
                not isinstance(raw, dict)
                or raw.get("thesis_id") != thesis_id
                or raw.get("trigger_id") != trigger_id
                or not isinstance(raw.get("payload"), dict)
                or not isinstance(raw.get("run_id"), str)
                or not raw["run_id"]
            ):
                raise ValueError(f"{p}: foreign or corrupt pending result intent; operator review required")
            return raw

    def write_pending_result(self, thesis_id: str, trigger_id: str, intent: dict[str, Any]) -> Path:
        """Persist the validated result before mutating (replay skips the model call)."""
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            body = dict(intent)
            body["thesis_id"] = thesis_id
            body["trigger_id"] = trigger_id
            return atomic_write_json(self._pending_path(thesis_dir, trigger_id), body, self.root)

    def clear_pending_result(self, thesis_id: str, trigger_id: str) -> None:
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            try:
                self._pending_path(thesis_dir, trigger_id).unlink()
            except FileNotFoundError:
                pass


    def load_checkpoint(self, id_or_slug: str) -> Checkpoint:
        """Read validated checkpoint.yaml (lock-held; KeyError on unknown thesis)."""
        thesis_id = self._resolve_id(id_or_slug)
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            raw = load_raw_yaml(thesis_dir / "checkpoint.yaml")
            self._check_file_owner(raw, str(thesis_dir / "checkpoint.yaml"), thesis_id)
            return Checkpoint.from_dict(raw, str(thesis_dir / "checkpoint.yaml"))

    def save_checkpoint(self, thesis_id: str, checkpoint: Checkpoint | dict[str, Any]) -> Checkpoint:
        """Validate-then-replace checkpoint.yaml (lock-held, atomic)."""
        thesis_dir = self._dir_for(thesis_id)
        with thesis_lock(thesis_dir):
            self._check_owner(thesis_dir, thesis_id)
            data = checkpoint.to_dict() if isinstance(checkpoint, Checkpoint) else dict(checkpoint)
            data["thesis_id"] = thesis_id
            candidate = Checkpoint.from_dict(data, str(thesis_dir / "checkpoint.yaml"))
            atomic_write_yaml(thesis_dir / "checkpoint.yaml", candidate.to_dict(), self.root)
            return candidate


    def apply_research_result(
        self, thesis_id: str, result: dict[str, Any], run_id: str = "", *, allowed_refs: set[str] | None = None,
        effective_at: str | None = None,
    ) -> dict[str, Any]:
        """Single replayable research commit; de-dup additions by ID (crash-retry safe).

        One lock, fixed order: claim/expression status -> evidence refs -> state
        -> questions -> memory -> watch -> journal -> trigger processed metadata.
        Evidence provenance is validated before any write: refs carrying bodies,
        URLs, or publication/retrieval metadata are rejected, and (when a
        trigger_id is present) every canonical ref must name the run's allowed
        set. The runner threads its visible-run set through ``allowed_refs``;
        without it the allowed set is the trigger's refs plus stored refs whose
        known_at is chronologically at or before the payload time. Never
        deletes triggers.
        """
        if not isinstance(result, dict):
            raise ValueError(f"<apply>: result must be a mapping, got {type(result).__name__}")
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        eff = effective_at or _utcnow()
        eff_dt = _as_dt(eff)
        if eff_dt is None:
            raise ValueError(f"<apply>: bad effective_at {effective_at!r}")
        thesis_dir = self._dir_for(thesis_id)
        outcome: dict[str, Any] = {}
        with thesis_lock(thesis_dir):
            thesis = self._check_owner(thesis_dir, thesis_id)
            latest = self._latest_effective_locked(thesis_dir)
            if latest is not None and eff_dt < latest[0]:
                # Backdated commit: patch copies of the as-of snapshot, never live files.
                base = self.load_state_as_of(thesis_id, eff)
                if base.thesis.get("status") == models.ThesisStatus.CLOSED.value:
                    raise ValueError(
                        f"{thesis_dir}: thesis {thesis_id!r} is closed; refusing research writeback")
                base_claim_ids = {c.get("claim_id") for c in base.thesis.get("claims", [])
                                  if isinstance(c, dict)}
                base_expr_ids = {e.get("expression_id") for e in base.thesis.get("expressions", [])
                                 if isinstance(e, dict)}
                # 0a. claim/expression updates: IDs must belong to the as-of state.
                claim_patch = {}
                for c in result.get("claim_updates", []) or []:
                    if not isinstance(c, dict) or not c.get("claim_id"):
                        raise ValueError(f"{thesis_dir}/thesis.yaml: bad claim update {c!r}")
                    if c["claim_id"] not in base_claim_ids:
                        raise ValueError(
                            f"{thesis_dir}/thesis.yaml: claim {c['claim_id']!r} does not belong to thesis {thesis_id!r}")
                    if c.get("status") is not None and c["status"] not in {e.value for e in models.ClaimStatus}:
                        raise ValueError(f"{thesis_dir}/thesis.yaml: bad claim status {c.get('status')!r}")
                    claim_patch[c["claim_id"]] = c
                expr_patch = {}
                for e in result.get("expression_updates", []) or []:
                    if not isinstance(e, dict) or not e.get("expression_id"):
                        raise ValueError(f"{thesis_dir}/thesis.yaml: bad expression update {e!r}")
                    if e["expression_id"] not in base_expr_ids:
                        raise ValueError(
                            f"{thesis_dir}/thesis.yaml: expression {e['expression_id']!r}"
                            f" does not belong to thesis {thesis_id!r}")
                    if e.get("status") is not None and e["status"] not in {e2.value for e2 in models.ExpressionStatus}:
                        raise ValueError(f"{thesis_dir}/thesis.yaml: bad expression status {e.get('status')!r}")
                    expr_patch[e["expression_id"]] = e
                # 0b. evidence provenance before any write (same gate as the live path).
                tpath = None
                raw_trigger = None
                if result.get("trigger_id"):
                    tpath, raw_trigger = self._load_trigger_raw(thesis_dir, thesis_id, result["trigger_id"])
                    if allowed_refs is not None:
                        allowed = set(allowed_refs)
                    else:
                        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers
                        journal = result.get("journal_entry")
                        payload_known = journal.get("known_at") if isinstance(journal, dict) else None
                        if not payload_known:
                            payload_known = raw_trigger.get("created_at")
                        dt_cut = _as_dt(payload_known) if payload_known else None
                        pit_refs = set()
                        if dt_cut is not None:
                            evdir = thesis_dir / "evidence"
                            if evdir.is_dir():
                                for f in sorted(evdir.glob("*.yaml")):
                                    try:
                                        raw = load_raw_yaml(f)
                                    except Exception:
                                        continue
                                    if not isinstance(raw, dict) or raw.get("thesis_id") != thesis_id:
                                        continue
                                    ref, known = raw.get("canonical_ref"), raw.get("known_at")
                                    if not isinstance(ref, str) or not ref:
                                        continue
                                    dt_known = _as_dt(known) if known else None
                                    if dt_known is not None and dt_known <= dt_cut:
                                        pit_refs.add(ref)
                        allowed = set(raw_trigger.get("canonical_refs", [])) | pit_refs
                else:
                    allowed = None
                for ref in result.get("evidence_refs", []) or []:
                    if not isinstance(ref, dict):
                        raise ValueError(f"{thesis_dir}/evidence: evidence ref must be a mapping, got {type(ref).__name__}")
                    smuggled = [k for k in _FORBIDDEN_EVIDENCE_KEYS if ref.get(k)]
                    if smuggled:
                        raise ValueError(
                            f"{thesis_dir}/evidence: evidence refs must not carry bodies/provenance (got {smuggled})")
                    if allowed is not None and ref.get("canonical_ref") not in allowed:
                        raise ValueError(
                            f"{thesis_dir}/evidence: foreign canonical_ref {ref.get('canonical_ref')!r}"
                            f" (trigger {result['trigger_id']!r}); refusing writeback")
                # Working copies of the as-of state; only the snapshot persists them.
                thesis_d = dict(base.thesis)
                state_d = dict(base.state)
                questions_raw = {"schema_version": base.questions.get("schema_version", SCHEMA_VERSION),
                                 "thesis_id": thesis_id,
                                 "questions": [dict(q) for q in base.questions.get("questions", [])]}
                memory_raw = {"schema_version": base.memory.get("schema_version", SCHEMA_VERSION),
                              "thesis_id": thesis_id,
                              "memories": [dict(m) for m in base.memory.get("memories", [])]}
                watch_raw = {"schema_version": base.watch.get("schema_version", SCHEMA_VERSION),
                             "thesis_id": thesis_id,
                             "rules": [dict(r) for r in base.watch.get("rules", [])]}
                claim_or_expr = bool(claim_patch or expr_patch)
                state_changed = result.get("state") is not None
                questions_added = False
                memories_added = False
                watch_added = False
                questions_answered_updated = False
                # 0c. fold the old standalone thesis status mutation into this commit.
                if claim_patch or expr_patch:
                    patched = dict(base.thesis)
                    patched["claims"] = [
                        {**c, **claim_patch[c["claim_id"]]} if isinstance(c, dict) and c.get("claim_id") in claim_patch else c
                        for c in patched.get("claims", [])
                    ]
                    patched["expressions"] = [
                        {**e, **expr_patch[e["expression_id"]]} if isinstance(e, dict) and e.get("expression_id") in expr_patch else e
                        for e in patched.get("expressions", [])
                    ]
                    patched["thesis_id"] = thesis_id
                    patched["updated_at"] = eff
                    thesis_d = Thesis.from_dict(patched, str(thesis_dir / "thesis.yaml")).to_dict()
                # 1. evidence refs (live side effect: one file per ref, skip existing IDs)
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
                # 2. state candidate, validated (as-of copy only, never live state.yaml)
                if result.get("state") is not None:
                    sdata = result["state"] if isinstance(result["state"], dict) else result["state"].to_dict()
                    sdata = dict(sdata)
                    sdata["thesis_id"] = thesis_id
                    state_d = ThesisState.from_dict(sdata, str(thesis_dir / "state.yaml")).to_dict()
                # 3. questions (append by ID, skip known)
                if result.get("questions_add"):
                    known = {q.get("question_id") for q in questions_raw.get("questions", [])}
                    for q in result["questions_add"]:
                        d = dict(q)
                        d.setdefault("question_id", new_question_id())
                        qobj = ThesisQuestion.from_dict(d, str(thesis_dir / "questions.yaml"))
                        if qobj.question_id in known:
                            continue
                        questions_raw.setdefault("questions", []).append(qobj.to_dict())
                        known.add(qobj.question_id)
                        questions_added = True
                # 4. memory (append by ID, skip known)
                if result.get("memories_add"):
                    known = {m.get("memory_id") for m in memory_raw.get("memories", [])}
                    for m in result["memories_add"]:
                        d = dict(m)
                        d.setdefault("memory_id", new_memory_id())
                        d.setdefault("created_at", eff)
                        mobj = ThesisMemory.from_dict(d, str(thesis_dir / "memory.yaml"))
                        if mobj.memory_id in known:
                            continue
                        memory_raw.setdefault("memories", []).append(mobj.to_dict())
                        known.add(mobj.memory_id)
                        memories_added = True
                # 5. watch (append rules by ID with cross-ref checks against as-of state)
                if result.get("watch_add"):
                    known = {r.get("rule_id") for r in watch_raw.get("rules", [])}
                    claim_ids = {c.get("claim_id") for c in thesis_d.get("claims", []) if isinstance(c, dict)}
                    expr_ids = {e.get("expression_id") for e in thesis_d.get("expressions", []) if isinstance(e, dict)}
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
                        require_watch_targets(robj, str(thesis_dir / "watch.yaml"))
                        if robj.rule_id in known:
                            continue
                        watch_raw.setdefault("rules", []).append(robj.to_dict())
                        known.add(robj.rule_id)
                        watch_added = True
                # 5b. questions answered (as-of copy only, validated inline)
                if result.get("questions_answered"):
                    qpath = thesis_dir / "questions.yaml"
                    by_id = {q.get("question_id"): q for q in questions_raw.get("questions", [])}
                    for a in result["questions_answered"]:
                        if (not isinstance(a, dict) or not isinstance(a.get("question_id"), str)
                                or a["question_id"] not in by_id):
                            raise ValueError(f"{qpath}: answer names absent question {a!r}")
                        if not isinstance(a.get("answer"), str) or not a["answer"]:
                            raise ValueError(f"{qpath}: answer for {a['question_id']!r} must be a non-empty string")
                        by_id[a["question_id"]]["status"] = QuestionStatus.ANSWERED.value
                        by_id[a["question_id"]]["answer"] = a["answer"]
                    for q in questions_raw.get("questions", []):
                        ThesisQuestion.from_dict(q, str(qpath))
                    questions_answered_updated = True
                # 6. journal (live side effect, idempotent by entry id)
                if result.get("journal_entry") is not None:
                    jd = dict(result["journal_entry"])
                    jd.setdefault("run_id", run_id)
                    if result.get("trigger_id"):
                        jd.setdefault("trigger_id", result["trigger_id"])
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
                        dest = thesis_dir / "journal" / f"{_safe_name(entry_id)}.md"
                        atomic_write_text(
                            dest,
                            _journal_front_matter(entry_id, thesis_id, _utcnow(), jd)
                            + f"# {jd.get('title', 'Research run')}\n\n{jd.get('body', jd.get('summary', ''))}\n",
                            self.root,
                        )
                        outcome["journal"] = str(dest)
                    else:
                        outcome["journal"] = str(existing)
                # 7. trigger processed metadata (live side effect, never delete)
                if result.get("trigger_id"):
                    if raw_trigger is None or tpath is None:
                        tpath, raw_trigger = self._load_trigger_raw(thesis_dir, thesis_id, result["trigger_id"])
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
                mutable_changed = (claim_or_expr or state_changed or questions_added
                                   or memories_added or watch_added or questions_answered_updated)
                if mutable_changed:
                    self._write_snapshot_from_dicts_locked(
                        thesis_dir, thesis_id, effective_at=eff, reason="research_result",
                        run_id=run_id, trigger_id=result.get("trigger_id") or "",
                        thesis=thesis_d, state=state_d, questions=questions_raw,
                        watch=watch_raw, memory=memory_raw)
                return outcome
            if thesis.status == models.ThesisStatus.CLOSED.value:
                raise ValueError(f"{thesis_dir}: thesis {thesis_id!r} is closed; refusing research writeback")

            # 0a. claim/expression updates: IDs must belong here, statuses known.
            claim_patch: dict[str, dict[str, Any]] = {}
            for c in result.get("claim_updates", []) or []:
                if not isinstance(c, dict) or not c.get("claim_id"):
                    raise ValueError(f"{thesis_dir}/thesis.yaml: bad claim update {c!r}")
                if c["claim_id"] not in {x.claim_id for x in thesis.claims}:
                    raise ValueError(
                        f"{thesis_dir}/thesis.yaml: claim {c['claim_id']!r} does not belong to thesis {thesis_id!r}")
                if c.get("status") is not None and c["status"] not in {e.value for e in models.ClaimStatus}:
                    raise ValueError(f"{thesis_dir}/thesis.yaml: bad claim status {c.get('status')!r}")
                claim_patch[c["claim_id"]] = c
            expr_patch: dict[str, dict[str, Any]] = {}
            for e in result.get("expression_updates", []) or []:
                if not isinstance(e, dict) or not e.get("expression_id"):
                    raise ValueError(f"{thesis_dir}/thesis.yaml: bad expression update {e!r}")
                if e["expression_id"] not in {x.expression_id for x in thesis.expressions}:
                    raise ValueError(
                        f"{thesis_dir}/thesis.yaml: expression {e['expression_id']!r}"
                        f" does not belong to thesis {thesis_id!r}")
                if e.get("status") is not None and e["status"] not in {e2.value for e2 in models.ExpressionStatus}:
                    raise ValueError(f"{thesis_dir}/thesis.yaml: bad expression status {e.get('status')!r}")
                expr_patch[e["expression_id"]] = e

            # 0b. evidence provenance before any write (foreign/invalid changes nothing).
            tpath: Path | None = None
            raw_trigger: dict[str, Any] | None = None
            if result.get("trigger_id"):
                tpath, raw_trigger = self._load_trigger_raw(thesis_dir, thesis_id, result["trigger_id"])
                if allowed_refs is not None:
                    allowed = set(allowed_refs)
                else:
                    from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

                    journal = result.get("journal_entry")
                    payload_known = journal.get("known_at") if isinstance(journal, dict) else None
                    if not payload_known:
                        payload_known = raw_trigger.get("created_at")
                    dt_cut = _as_dt(payload_known) if payload_known else None
                    pit_refs: set[str] = set()
                    if dt_cut is not None:
                        evdir = thesis_dir / "evidence"
                        if evdir.is_dir():
                            for f in sorted(evdir.glob("*.yaml")):
                                try:
                                    raw = load_raw_yaml(f)
                                except Exception:
                                    continue
                                if not isinstance(raw, dict) or raw.get("thesis_id") != thesis_id:
                                    continue
                                ref, known = raw.get("canonical_ref"), raw.get("known_at")
                                if not isinstance(ref, str) or not ref:
                                    continue
                                dt_known = _as_dt(known) if known else None
                                if dt_known is not None and dt_known <= dt_cut:
                                    pit_refs.add(ref)
                    allowed = set(raw_trigger.get("canonical_refs", [])) | pit_refs
            else:
                allowed = None  # seed path without a trigger: membership unchecked
            for ref in result.get("evidence_refs", []) or []:
                if not isinstance(ref, dict):
                    raise ValueError(f"{thesis_dir}/evidence: evidence ref must be a mapping, got {type(ref).__name__}")
                smuggled = [k for k in _FORBIDDEN_EVIDENCE_KEYS if ref.get(k)]
                if smuggled:
                    raise ValueError(
                        f"{thesis_dir}/evidence: evidence refs must not carry bodies/provenance (got {smuggled})")
                if allowed is not None and ref.get("canonical_ref") not in allowed:
                    raise ValueError(
                        f"{thesis_dir}/evidence: foreign canonical_ref {ref.get('canonical_ref')!r}"
                        f" (trigger {result['trigger_id']!r}); refusing writeback")
            claim_or_expr = bool(claim_patch or expr_patch)
            state_changed = result.get("state") is not None
            questions_added = False
            memories_added = False
            watch_added = False
            questions_answered_updated = False
            # 0c. fold the old standalone thesis status mutation into this commit.
            if claim_patch or expr_patch:
                patched = thesis.to_dict()
                patched["claims"] = [
                    {**c, **claim_patch[c["claim_id"]]} if c["claim_id"] in claim_patch else c
                    for c in patched["claims"]
                ]
                patched["expressions"] = [
                    {**e, **expr_patch[e["expression_id"]]} if e["expression_id"] in expr_patch else e
                    for e in patched["expressions"]
                ]
                patched["thesis_id"] = thesis_id
                patched["updated_at"] = _utcnow()
                thesis = Thesis.from_dict(patched, str(thesis_dir / "thesis.yaml"))
                atomic_write_yaml(thesis_dir / "thesis.yaml", thesis.to_dict(), self.root)

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
                    questions_added = True
                atomic_write_yaml(thesis_dir / "questions.yaml", raw, self.root)

            # 4. memory (append by ID, skip known)
            if result.get("memories_add"):
                raw = load_raw_yaml(thesis_dir / "memory.yaml")
                self._check_file_owner(raw, str(thesis_dir / "memory.yaml"), thesis_id)
                known = {m.get("memory_id") for m in raw.get("memories", [])}
                for m in result["memories_add"]:
                    d = dict(m)
                    d.setdefault("memory_id", new_memory_id())
                    d.setdefault("created_at", eff)
                    mobj = ThesisMemory.from_dict(d, str(thesis_dir / "memory.yaml"))
                    if mobj.memory_id in known:
                        continue
                    raw.setdefault("memories", []).append(mobj.to_dict())
                    known.add(mobj.memory_id)
                    memories_added = True
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
                    require_watch_targets(robj, str(thesis_dir / "watch.yaml"))
                    if robj.rule_id in known:
                        continue
                    raw.setdefault("rules", []).append(robj.to_dict())
                    known.add(robj.rule_id)
                    watch_added = True
                atomic_write_yaml(thesis_dir / "watch.yaml", raw, self.root)

            # 5b. questions answered (folded; same lock, validated inline).
            if result.get("questions_answered"):
                qpath = thesis_dir / "questions.yaml"
                qraw = load_raw_yaml(qpath)
                self._check_file_owner(qraw, str(qpath), thesis_id)
                by_id = {q.get("question_id"): q for q in qraw.get("questions", [])}
                for a in result["questions_answered"]:
                    if (
                        not isinstance(a, dict)
                        or not isinstance(a.get("question_id"), str)
                        or a["question_id"] not in by_id
                    ):
                        raise ValueError(f"{qpath}: answer names absent question {a!r}")
                    if not isinstance(a.get("answer"), str) or not a["answer"]:
                        raise ValueError(f"{qpath}: answer for {a['question_id']!r} must be a non-empty string")
                    by_id[a["question_id"]]["status"] = QuestionStatus.ANSWERED.value
                    by_id[a["question_id"]]["answer"] = a["answer"]
                for q in qraw.get("questions", []):
                    ThesisQuestion.from_dict(q, str(qpath))
                atomic_write_yaml(qpath, qraw, self.root)
                questions_answered_updated = True

            # 6. journal (idempotent by entry id; atomic replace keeps Markdown intact)
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
                    dest = thesis_dir / "journal" / f"{_safe_name(entry_id)}.md"
                    atomic_write_text(
                        dest,
                        _journal_front_matter(entry_id, thesis_id, _utcnow(), jd)
                        + f"# {jd.get('title', 'Research run')}\n\n{jd.get('body', jd.get('summary', ''))}\n",
                        self.root,
                    )
                    outcome["journal"] = str(dest)
                else:
                    outcome["journal"] = str(existing)

            # 7. trigger processed metadata (never delete; last write of the commit)
            if result.get("trigger_id"):
                if raw_trigger is None or tpath is None:  # trigger_id validated in 0b; reload only if unset
                    tpath, raw_trigger = self._load_trigger_raw(thesis_dir, thesis_id, result["trigger_id"])
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
            mutable_changed = (claim_or_expr or state_changed or questions_added
                               or memories_added or watch_added or questions_answered_updated)
            if mutable_changed:
                self._snapshot_state_locked(
                    thesis_dir, thesis_id, effective_at=eff, reason="research_result",
                    run_id=run_id, trigger_id=result.get("trigger_id") or "")
        return outcome
