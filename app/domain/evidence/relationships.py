"""Open-vocabulary relationship evidence, validation, and promotion.

Provider-independent: SEC accessions/documents appear only as plain
provenance strings. No SEC, storage, DuckDB, broker, or LLM imports.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

STATUSES = frozenset({
    "observed", "candidate", "verified", "rejected",
    "expired", "superseded", "unknown",
})

ACTORS = frozenset({"deterministic", "extractor", "human", "evaluation"})

#: Conservative auto-verify floor: aggregate confidence is the *minimum*
#: contributing confidence, and it must reach this.
AUTO_VERIFY_MIN_CONFIDENCE = 0.95

#: Deterministic source-encoded roles verify directly, bypassing the
#: two-source semantic rule. Everything else uses the conservative rule.
DETERMINISTIC_TYPES = frozenset({
    "beneficial_owner",
    "insider_owner",
    "holding_manager",
    "transaction_party",
    "offering_party",
})


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_label(raw: object) -> str:
    """Open-vocabulary label -> lowercase snake case (raw is preserved)."""
    text = unicodedata.normalize("NFKD", str(raw or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return re.sub(r"_+", "_", text) or "unknown"


@dataclass(frozen=True)
class RelationshipEvidence:
    evidence_id: str
    relationship_id: str
    accession: str | None = None
    document_name: str | None = None
    source_span: str | None = None
    extraction_method: str | None = None
    confidence: float = 0.0
    is_counterevidence: bool = False
    known_at: str | None = None
    relationship_type: str | None = None
    from_entity_id: str | None = None
    to_entity_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type,
            "from_entity_id": self.from_entity_id,
            "to_entity_id": self.to_entity_id,
            "accession": self.accession,
            "document_name": self.document_name,
            "source_span": self.source_span,
            "extraction_method": self.extraction_method,
            "confidence": self.confidence,
            "is_counterevidence": self.is_counterevidence,
            "known_at": self.known_at,
        }
@dataclass(frozen=True)
class RelationshipRevision:
    revision_id: str
    relationship_id: str
    previous_status: str | None
    new_status: str
    actor: str
    reason: str
    recorded_at: str
    superseded_revision_id: str | None = None
    known_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "revision_id": self.revision_id,
            "relationship_id": self.relationship_id,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "actor": self.actor,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "superseded_revision_id": self.superseded_revision_id,
            "known_at": self.known_at,
        }


@dataclass
class Relationship:
    """One mutable instance; every operation appends to ``revisions``."""

    relationship_id: str
    relationship_type: str
    raw_label: str | None
    from_entity_id: str | None
    to_entity_id: str | None
    status: str = "unknown"
    known_at: str | None = None
    current_revision_id: str | None = None
    evidence: list = field(default_factory=list)
    counterevidence: list = field(default_factory=list)
    revisions: list = field(default_factory=list)

    def supporting(self) -> list:
        return [e for e in self.evidence if not e.is_counterevidence]


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def _record(rel: Relationship, prev: str | None, new: str, actor: str,
            reason: str, *, known_at: str | None = None,
            superseded: str | None = None,
            recorded_at: str | None = None) -> RelationshipRevision:
    if actor not in ACTORS:
        raise ValueError(f"actor must be one of {sorted(ACTORS)}, got {actor!r}")
    if new not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, got {new!r}")
    rev = RelationshipRevision(
        revision_id=f"{rel.relationship_id}:r{len(rel.revisions) + 1}",
        relationship_id=rel.relationship_id,
        previous_status=prev, new_status=new, actor=actor, reason=reason,
        recorded_at=recorded_at or _utcnow(),
        superseded_revision_id=superseded if superseded is not None
        else (rel.current_revision_id if prev != new else None),
        known_at=known_at or rel.known_at,
    )
    rel.status = new
    rel.current_revision_id = rev.revision_id
    rel.revisions.append(rev)
    return rev


def _check_as_of(as_of: str | None) -> str | None:
    if as_of is None:
        return None
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(as_of)):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    return str(as_of)


def validate_relationship(rel: Relationship, *, as_of: str | None = None,
                           endpoints_verified: dict | None = None) -> list[str]:
    """Generic validation -> error codes; empty means valid."""
    errors: list[str] = []
    if not rel.from_entity_id or not rel.to_entity_id:
        errors.append("unresolved-endpoint")
    elif rel.from_entity_id == rel.to_entity_id:
        errors.append("invalid-direction")
    for ev in rel.supporting():
        if not (ev.source_span or "").strip():
            errors.append(f"empty-span:{ev.evidence_id}")
            break
    if as_of is not None:
        for ev in rel.supporting():
            if not ev.known_at or ev.known_at[:10] > as_of:
                errors.append("pit-unsafe-evidence")
                break
    if endpoints_verified is not None:
        for eid in (rel.from_entity_id, rel.to_entity_id):
            if eid is not None and endpoints_verified.get(eid) is False:
                errors.append(f"endpoint-conflict:{eid}")
                break
    if rel.relationship_type in DETERMINISTIC_TYPES:
        for ev in rel.supporting():
            if not ev.accession or not ev.document_name:
                errors.append(f"deterministic-missing-provenance:{ev.evidence_id}")
                break
    return errors


def _auto_verify_errors(rel: Relationship, *, as_of: str | None,
                        endpoints_verified: dict | None) -> list[str]:
    """Conservative rule -> blocking reasons; empty means verify."""
    reasons = validate_relationship(
        rel, as_of=as_of, endpoints_verified=endpoints_verified)
    supporting = rel.supporting()
    sources = {(e.accession, e.document_name) for e in supporting
               if e.accession and e.document_name}
    if len(sources) < 2:
        reasons.append("needs-two-distinct-sources")
    for eid in (rel.from_entity_id, rel.to_entity_id):
        if eid is None:
            continue
        if endpoints_verified is None or endpoints_verified.get(eid) is not True:
            reasons.append(f"endpoint-unverified:{eid}")
    if supporting:
        floor = min(float(e.confidence or 0.0) for e in supporting)
        if floor < AUTO_VERIFY_MIN_CONFIDENCE:
            reasons.append(f"min-confidence-{floor:.2f}-below-0.95")
    else:
        reasons.append("no-supporting-evidence")
    if rel.counterevidence:
        reasons.append("unresolved-counterevidence")
    return reasons


def observe_relationship(from_entity_id: str | None, to_entity_id: str | None,
                         raw_label: object, *, span: object = None,
                         accession: object = None, document_name: object = None,
                         known_at: str | None = None,
                         relationship_id: str | None = None) -> Relationship:
    """Raw document/entity mention -> ``observed`` (never identity)."""
    rel = Relationship(
        relationship_id=relationship_id or _new_id("rel"),
        relationship_type=normalize_label(raw_label),
        raw_label=str(raw_label or ""),
        from_entity_id=from_entity_id, to_entity_id=to_entity_id,
        known_at=known_at or _utcnow(),
    )
    if span is not None or accession is not None:
        attach_relationship_evidence(
            rel, source_span=str(span or ""), accession=accession,
            document_name=document_name, extraction_method="mention",
            confidence=0.0, known_at=known_at, actor="extractor",
            reason="raw mention observed", initial_status="observed")
    else:
        _record(rel, None, "observed", "extractor", "raw mention observed",
                known_at=known_at)
    return rel


def propose_relationship(from_entity_id: str | None, to_entity_id: str | None,
                         raw_label: object, *, span: object = None,
                         accession: object = None, document_name: object = None,
                         extraction_method: str | None = None,
                         confidence: float = 0.0, known_at: str | None = None,
                         deterministic: bool = False,
                         relationship_id: str | None = None) -> Relationship:
    """Structured/LLM extraction -> ``candidate``; deterministic roles verify."""
    rel = Relationship(
        relationship_id=relationship_id or _new_id("rel"),
        relationship_type=normalize_label(raw_label),
        raw_label=str(raw_label or ""),
        from_entity_id=from_entity_id, to_entity_id=to_entity_id,
        known_at=known_at or _utcnow(),
    )
    if deterministic and rel.relationship_type not in DETERMINISTIC_TYPES:
        raise ValueError(
            f"deterministic verification needs a source-encoded role in "
            f"{sorted(DETERMINISTIC_TYPES)}, got {rel.relationship_type!r}")
    target = "verified" if deterministic else "candidate"
    actor = "deterministic" if deterministic else "extractor"
    if span is not None or accession is not None or deterministic:
        attach_relationship_evidence(
            rel, source_span=str(span or ""), accession=accession,
            document_name=document_name,
            extraction_method=extraction_method or "structured",
            confidence=confidence, known_at=known_at, actor=actor,
            reason="deterministic source role" if deterministic
            else "structured extraction proposed", initial_status=target)
    else:
        _record(rel, None, target, actor, "extraction proposed",
                known_at=known_at)
    return rel


def attach_relationship_evidence(
        rel: Relationship, *, source_span: object = None,
        accession: object = None, document_name: object = None,
        extraction_method: str | None = None, confidence: float = 0.0,
        known_at: str | None = None, actor: str = "extractor",
        reason: str = "evidence attached",
        initial_status: str | None = None,
        evidence_id: str | None = None) -> RelationshipEvidence:
    """Append supporting evidence; status unchanged (revision still written)."""
    ev = RelationshipEvidence(
        evidence_id=evidence_id or f"{rel.relationship_id}:e{len(rel.evidence) + 1}",
        relationship_id=rel.relationship_id,
        accession=str(accession) if accession is not None else None,
        document_name=str(document_name) if document_name is not None else None,
        source_span=str(source_span or "") or None,
        extraction_method=extraction_method,
        confidence=float(confidence or 0.0),
        known_at=known_at or _utcnow(),
        relationship_type=rel.relationship_type,
        from_entity_id=rel.from_entity_id,
        to_entity_id=rel.to_entity_id,
    )
    rel.evidence.append(ev)
    prev = rel.status
    _record(rel, prev, initial_status or prev, actor, reason,
            known_at=ev.known_at)
    return ev

def attach_relationship_counterevidence(
        rel: Relationship, *, source_span: object = None,
        accession: object = None, document_name: object = None,
        extraction_method: str | None = None, confidence: float = 0.0,
        known_at: str | None = None, actor: str = "extractor",
        reason: str = "counterevidence attached") -> RelationshipEvidence:
    """Counterevidence is its own record; it blocks auto-verify while present."""
    ev = RelationshipEvidence(
        evidence_id=f"{rel.relationship_id}:e{len(rel.evidence) + 1}",
        relationship_id=rel.relationship_id,
        accession=str(accession) if accession is not None else None,
        document_name=str(document_name) if document_name is not None else None,
        source_span=str(source_span or "") or None,
        extraction_method=extraction_method,
        is_counterevidence=True,
        known_at=known_at or _utcnow(),
        relationship_type=rel.relationship_type,
        from_entity_id=rel.from_entity_id,
        to_entity_id=rel.to_entity_id,
    )
    rel.evidence.append(ev)
    rel.counterevidence.append(ev)
    _record(rel, rel.status, rel.status, actor, reason, known_at=ev.known_at)
    return ev


def evaluate_relationship(rel: Relationship, *, as_of: str | None = None,
                          endpoints_verified: dict | None = None,
                          known_at: str | None = None) -> tuple[str, list[str]]:
    """Conservative evaluation -> (decision, reasons); writes a revision.

    Verified only on the full rule; any unresolved counterevidence
    rejects; otherwise the status stands. Later qualifying evidence may
    supersede even a human decision — the revision cites the window.
    """
    as_of = _check_as_of(as_of)
    if rel.counterevidence:
        prev = rel.status
        _record(rel, prev, "rejected", "evaluation",
                "unresolved counterevidence rejects", known_at=known_at)
        return "rejected", ["unresolved-counterevidence"]
    reasons = _auto_verify_errors(
        rel, as_of=as_of, endpoints_verified=endpoints_verified)
    if not reasons:
        prev = rel.status
        cite = as_of or (known_at or rel.known_at or "")[:10]
        _record(rel, prev, "verified", "evaluation",
                f"conservative auto-verify over {len(rel.supporting())} "
                f"evidence items window {cite}".strip(),
                known_at=known_at)
        return "verified", []
    return "no_change", reasons


def revise_relationship_status(rel: Relationship, new_status: str, *,
                               actor: str = "human", reason: str = "",
                               known_at: str | None = None) -> RelationshipRevision:
    """Explicit transition (human decisions audited, never locked)."""
    if not reason.strip():
        raise ValueError("revision reason is required")
    return _record(rel, rel.status, new_status, actor, reason.strip(),
                   known_at=known_at)


def supersede_relationship(rel: Relationship, *, new_status: str = "verified",
                           evidence: list | None = None, actor: str = "human",
                           reason: str = "",
                           known_at: str | None = None) -> RelationshipRevision:
    """Later qualifying evidence supersedes a prior (even human) decision.

    The reason must cite the new evidence: pass ``evidence`` items or name
    their IDs in ``reason``.
    """
    if not reason.strip():
        raise ValueError("supersession reason is required")
    cited = {e.evidence_id for e in (evidence or [])}
    if not cited and ":e" not in reason and "evidence" not in reason.lower():
        raise ValueError("supersession must cite the new evidence")
    seen = {e.evidence_id for e in rel.evidence}
    for ev in (evidence or []):
        if ev.evidence_id in seen:
            continue
        seen.add(ev.evidence_id)
        rel.evidence.append(ev)
        if ev.is_counterevidence and ev not in rel.counterevidence:
            rel.counterevidence.append(ev)
    prev_current = rel.current_revision_id
    rev = _record(rel, rel.status, new_status, actor,
                  f"supersedes {prev_current}: {reason.strip()}",
                  known_at=known_at, superseded=prev_current)
    return rev
