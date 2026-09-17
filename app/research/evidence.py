"""Append-only evidence ledger with provenance + PIT ingest gate. stdlib only."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256

from .models import JSONValue, pit_unverified, pit_violated, validate_json_mapping

__all__ = [
    "ACCESSION_RE",
    "CLAIM_KINDS",
    "PROVENANCE_KINDS",
    "RECORD_KINDS",
    "DiscoveryRecord",
    "Evidence",
    "EvidenceIntegrityError",
    "EvidenceLedger",
    "EvidenceNotFoundError",
    "EvidenceRecord",
    "EvidenceRejectedError",
    "discovery_only",
    "evidence_content_hash",
    "evidence_from_dict",
    "evidence_to_dict",
    "ingest_evidence",
    "normalize_accession",
    "search_run_ref",
    "sec_source_ref",
    "substantive_records",
    "validate_provenance",
]

ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
"""Canonical SEC accession; a bare 18-digit run normalizes into it (``normalize_accession``)."""

CLAIM_KINDS = ("observed_fact",)
"""Closed claim vocabulary: what the record asserts, stated by the caller, never inferred from wording.

Only raw-document claims are evidence. A search-derived absence observation is a
session coverage artifact (``ResearchRepository.save_coverage_artifact``), never a
ledger row: evidence is what a human/source document states, coverage is what a
search did or did not reach.
"""

PROVENANCE_KINDS = ("sec_source", "search_run", "none")
"""Closed provenance vocabulary: a raw document passage, the executed search, or nothing recorded."""


class EvidenceIntegrityError(ValueError):
    """Hash mismatch, bad confidence, duplicate id, or broken supersede link."""


class EvidenceRejectedError(ValueError):
    """Ingest refusal; carries the journal payload reason."""

    def __init__(self, evidence_id: str, reason: str, detail: str = "") -> None:
        self.evidence_id = evidence_id
        self.reason = reason
        self.detail = detail
        message = f"evidence {evidence_id} rejected: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


class EvidenceNotFoundError(KeyError):
    """Ledger holds no record for the id."""


def evidence_content_hash(content: str) -> str:
    """Canonical content hash (sha256 hex over utf-8)."""
    return sha256(content.encode("utf-8")).hexdigest()


RECORD_KINDS = frozenset({"discovery", "evidence"})


def normalize_accession(value: object) -> str:
    """Canonical dashed accession; a bare 18-digit run takes the 10-2-6 dashes. ValueError otherwise."""
    text = value.strip() if isinstance(value, str) else ""
    if text.isdigit() and len(text) == 18:
        text = f"{text[:10]}-{text[10:12]}-{text[12:]}"
    if not ACCESSION_RE.match(text):
        raise ValueError(f"invalid accession number: {value!r}")
    return text


BASIS_KINDS = ("raw", "rendered")
"""Closed basis vocabulary for a materialized passage: the stored document text, or its rendered view."""


def _canonical_offset(value: object, key: str) -> int:
    """Non-negative int offset/end of a materialized passage."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"sec_source_ref: {key} must be an int >= 0, got {value!r}")
    return value


def _canonical_hash(value: object) -> str:
    """Non-empty sha256 hex of the window the passage was sliced from."""
    text = value.strip() if isinstance(value, str) else ""
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError(f"sec_source_ref: text_hash must be a sha256 hex digest, got {value!r}")
    return text


def sec_source_ref(
    *,
    accession_no: object,
    document_name: object,
    passage: object,
    source_uri: object = None,
    offset: object = None,
    end: object = None,
    basis: object = None,
    text_hash: object = None,
) -> dict[str, JSONValue]:
    """SECSourceRef: the filing document + the kernel-materialized passage an observed fact is read off.

    ``offset``/``end`` are the passage's coordinates in the read ``basis`` (``raw``
    stored text or ``rendered`` view), and ``text_hash`` pins the window the
    kernel reloaded from the archive. They are required: the kernel, never the
    model, materializes the source text.
    """
    document, quoted = _ref_texts(document_name, passage)
    if basis not in BASIS_KINDS:
        raise ValueError(f"sec_source_ref: basis must be one of {list(BASIS_KINDS)}, got {basis!r}")
    start, stop = _ref_window(offset, end)
    return {
        "kind": "sec_source",
        "accession_no": normalize_accession(accession_no),
        "document_name": document,
        "passage": quoted,
        "offset": start,
        "end": stop,
        "basis": basis,
        "text_hash": _canonical_hash(text_hash),
        "source_uri": source_uri.strip() if isinstance(source_uri, str) and source_uri.strip() else None,
    }


def _ref_texts(document_name: object, passage: object) -> tuple[str, str]:
    """(document, quoted passage) of one source ref; both must be non-empty strings."""
    document = document_name.strip() if isinstance(document_name, str) else ""
    quoted = passage.strip() if isinstance(passage, str) else ""
    if not document or not quoted:
        raise ValueError("sec_source_ref: document_name and passage must be non-empty strings")
    return document, quoted


def _ref_window(offset: object, end: object) -> tuple[int, int]:
    """(offset, end) of the passage inside the read window; the window must be non-empty."""
    start = _canonical_offset(offset, "offset")
    stop = _canonical_offset(end, "end")
    if stop <= start:
        raise ValueError(f"sec_source_ref: end ({stop}) must be greater than offset ({start})")
    return start, stop


def search_run_ref(*, search_id: object, query: object) -> dict[str, JSONValue]:
    """SearchRunRef: the executed search a navigation (discovery) row records; never evidence of a claim."""
    sid = search_id.strip() if isinstance(search_id, str) else ""
    text = query.strip() if isinstance(query, str) else ""
    if not sid or not text:
        raise ValueError("search_run_ref: search_id and query must be non-empty strings")
    return {"kind": "search_run", "search_id": sid, "query": text}


def _provenance_str(prov: Mapping[str, object], key: str, where: str) -> str:
    value = prov.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvidenceIntegrityError(f"{where}: provenance[{key!r}] must be a non-empty string")
    return value


def _uncanonical_sec_source_ref(value: Mapping[str, object], where: str) -> dict[str, JSONValue]:
    """Pre-canonical SECSourceRef (no window coordinates): read tolerance for rows persisted earlier.

    It preserves exactly what the row recorded — no revision can be re-verified
    for such a row, so it is never written by this version.
    """
    return {
        "kind": "sec_source",
        "accession_no": normalize_accession(value.get("accession_no")),
        "document_name": _provenance_str(value, "document_name", where),
        "passage": _provenance_str(value, "passage", where),
        "source_uri": str(value["source_uri"]) if isinstance(value.get("source_uri"), str) else None,
    }


def validate_provenance(value: object, where: str = "<evidence>: 'provenance'") -> dict[str, JSONValue]:
    """Validate one persisted provenance mapping; empty means 'not recorded' (legacy rows)."""
    if not isinstance(value, Mapping):
        raise EvidenceIntegrityError(f"{where} must be an object")
    if not value:
        return {}
    kind = value.get("kind")
    if kind not in PROVENANCE_KINDS:
        raise EvidenceIntegrityError(f"{where}: 'kind' must be one of {list(PROVENANCE_KINDS)}, got {kind!r}")
    if kind == "none":
        return {"kind": "none"}
    if kind == "search_run":
        return search_run_ref(
            search_id=_provenance_str(value, "search_id", where),
            query=_provenance_str(value, "query", where),
        )
    return _sec_source_provenance(value, where)


def _sec_source_provenance(value: Mapping[str, object], where: str) -> dict[str, JSONValue]:
    """Canonical SEC ref when the row holds kernel coordinates, else the legacy un-canonical ref."""
    if "text_hash" in value or "offset" in value or "basis" in value:
        try:
            return sec_source_ref(
                accession_no=value.get("accession_no"),
                document_name=_provenance_str(value, "document_name", where),
                passage=_provenance_str(value, "passage", where),
                source_uri=value.get("source_uri"),
                offset=value.get("offset"),
                end=value.get("end"),
                basis=value.get("basis"),
                text_hash=value.get("text_hash"),
            )
        except ValueError as exc:
            raise EvidenceIntegrityError(f"{where}: {exc}") from None
    try:
        return _uncanonical_sec_source_ref(value, where)
    except ValueError as exc:
        raise EvidenceIntegrityError(f"{where}: {exc}") from None


@dataclass(frozen=True)
class DiscoveryRecord:
    """One catalog/tool-discovery hit: provenance of the search, never substantive coverage."""

    record_id: str
    session_id: str
    tool: str
    query: str
    search_id: str | None = None
    retrieved_at: datetime | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    """One substantive sourced claim over frozen SEC evidence (the coverage unit)."""

    record_id: str
    session_id: str
    evidence_id: str
    claim_text: str


@dataclass(frozen=True)
class Evidence:
    """One sourced claim. Missing timestamps stay None; never invented."""

    evidence_id: str
    session_id: str
    wave_id: int
    source_type: str
    source_name: str
    subject: str
    claim_text: str
    content: str
    content_hash: str
    retrieved_at: datetime
    source_uri: str | None = None
    source_record_id: str | None = None
    published_at: datetime | None = None
    known_at: datetime | None = None
    effective_at: datetime | None = None
    job_id: str | None = None
    agent_id: str | None = None
    supports: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()
    confidence: float | None = None
    quality: str | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)
    # Correction chain: id of the prior record this record corrects; None for originals.
    superseded_by: str | None = None
    # discovery = catalog hit (never substantive coverage alone); evidence = sourced claim.
    record_kind: str = "evidence"
    # What the record asserts (never inferred from wording) and what backs it.
    claim_kind: str = "observed_fact"
    provenance: dict[str, JSONValue] = field(default_factory=dict)

    def _check_record_kind(self) -> None:
        if self.record_kind not in RECORD_KINDS:
            raise EvidenceIntegrityError(
                f"evidence {self.evidence_id}: 'record_kind' must be discovery|evidence, got {self.record_kind!r}"
            )

    def _check_claim_kind(self) -> None:
        if self.claim_kind not in CLAIM_KINDS:
            raise EvidenceIntegrityError(
                f"evidence {self.evidence_id}: 'claim_kind' must be one of {list(CLAIM_KINDS)}, got {self.claim_kind!r}"
            )

    def _check_wave_hash(self) -> None:
        if isinstance(self.wave_id, bool) or not isinstance(self.wave_id, int) or self.wave_id < 1:
            raise EvidenceIntegrityError(f"evidence {self.evidence_id}: 'wave_id' must be an int >= 1")
        if evidence_content_hash(self.content) != self.content_hash:
            raise EvidenceIntegrityError(f"evidence {self.evidence_id}: content_hash mismatch")

    def __post_init__(self) -> None:
        self._check_record_kind()
        self._check_claim_kind()
        self._check_wave_hash()
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise EvidenceIntegrityError(f"evidence {self.evidence_id}: 'confidence' must be within 0..1")
        object.__setattr__(self, "supports", tuple(self.supports))
        object.__setattr__(self, "contradicts", tuple(self.contradicts))
        object.__setattr__(
            self,
            "provenance",
            validate_provenance(self.provenance, f"evidence {self.evidence_id}: 'provenance'"),
        )


class EvidenceLedger:
    """In-memory append-only ledger; the only evidence holder, never mutated."""

    def __init__(self) -> None:
        self._records: dict[str, Evidence] = {}

    def append(self, evidence: Evidence) -> Evidence:
        """Append one record; duplicates and dangling supersede links fail."""
        if evidence.evidence_id in self._records:
            raise EvidenceIntegrityError(f"duplicate evidence_id {evidence.evidence_id!r}")
        if evidence.superseded_by is not None and evidence.superseded_by not in self._records:
            raise EvidenceIntegrityError(
                f"evidence {evidence.evidence_id}: superseded_by {evidence.superseded_by!r} not in ledger"
            )
        self._records[evidence.evidence_id] = evidence
        return evidence

    def supersede(self, corrected: Evidence) -> Evidence:
        """Append a correction record; the original stays untouched."""
        if not corrected.superseded_by:
            raise EvidenceIntegrityError("supersede needs corrected.superseded_by set to the prior id")
        return self.append(corrected)

    def get(self, evidence_id: str) -> Evidence:
        """Return the record; raise EvidenceNotFoundError when absent."""
        try:
            return self._records[evidence_id]
        except KeyError:
            raise EvidenceNotFoundError(evidence_id) from None

    def current(self, evidence_id: str) -> Evidence:
        """Follow the supersede chain to the latest correction."""
        # ponytail: O(n) forward-index rebuild per call; index if ledgers grow large.
        forward = {e.superseded_by: e.evidence_id for e in self._records.values() if e.superseded_by is not None}
        seen = self.get(evidence_id)
        while seen.evidence_id in forward:
            seen = self.get(forward[seen.evidence_id])
        return seen

    def list_session(self, session_id: str) -> list[Evidence]:
        """Records for one session, in append order."""
        return [e for e in self._records.values() if e.session_id == session_id]

    def ids(self) -> tuple[str, ...]:
        """All record ids, in append order."""
        return tuple(self._records)

    def __contains__(self, evidence_id: object) -> bool:
        return evidence_id in self._records

    def __len__(self) -> int:
        return len(self._records)


def _record_kind_of(item: object) -> str:
    """Kind tag for one record: attr wins, then mapping keys, absent means evidence."""
    kind = getattr(item, "record_kind", None)
    if kind is None and isinstance(item, Mapping):
        meta = item.get("metadata")
        meta_kind = meta.get("record_kind") if isinstance(meta, dict) else None
        kind = item.get("record_kind", meta_kind)
    return str(kind) if kind is not None else "evidence"


def _record_items(records: object) -> list[object]:
    """Coerce list/tuple input to a list (anything else means no records)."""
    return list(records) if isinstance(records, (list, tuple)) else []


def discovery_only(records: object) -> bool:
    """True when every record is discovery-kind (no substantive evidence)."""
    items = _record_items(records)
    return bool(items) and all(_record_kind_of(item) == "discovery" for item in items)


def substantive_records(records: object) -> list[object]:
    """Filter to evidence-kind records (discovery never satisfies coverage alone)."""
    return [item for item in _record_items(records) if _record_kind_of(item) == "evidence"]


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else value


def _ingest_required(evidence: Evidence) -> tuple[tuple[str, object], ...]:
    """Required fields. A discovery row is a navigation artifact: it carries no source document.

    Only evidential rows need a source document/ref; a search run is never evidence,
    so a SearchRunRef can satisfy neither ingest nor a citation.
    """
    if evidence.record_kind == "discovery":
        return (
            ("session_id", evidence.session_id),
            ("retrieved_at", evidence.retrieved_at),
            ("lineage", evidence.job_id or evidence.agent_id),
        )
    return (
        ("session_id", evidence.session_id),
        ("source_name", evidence.source_name),
        ("source_ref", evidence.source_uri or evidence.source_record_id),
        ("retrieved_at", evidence.retrieved_at),
        ("lineage", evidence.job_id or evidence.agent_id),
    )


def _ingest_missing(evidence: Evidence) -> list[str]:
    missing = [key for key, value in _ingest_required(evidence) if not value]
    if isinstance(evidence.wave_id, bool) or not isinstance(evidence.wave_id, int):
        missing.append("wave_id")
    return missing


def _ingest_pit_gate(evidence: Evidence, as_of: datetime | str | None) -> tuple[str, str]:
    """PIT eligibility for an evidential row; a navigation artifact has no known_at to check."""
    if evidence.record_kind == "discovery":
        return "", ""
    try:
        unverified = pit_unverified(as_of, evidence.known_at)
        violated = False if unverified else pit_violated(as_of, evidence.known_at)
    except ValueError as exc:
        return "PROVENANCE_FAILURE", f"bad timestamp: {exc}"
    if unverified:
        return "PIT_UNVERIFIED", f"known_at unknown for historical as_of {_iso(as_of)}"
    if violated:
        return "PIT_VIOLATION", f"known_at {_iso(evidence.known_at)} > as_of {_iso(as_of)}"
    return "", ""


def _ingest_reject(
    evidence: Evidence,
    as_of: datetime | str | None,
    reason: str,
    detail: str,
    on_reject: Callable[[str, dict[str, object]], None] | None,
) -> None:
    payload: dict[str, object] = {
        "evidence_id": evidence.evidence_id,
        "reason": reason,
        "known_at": _iso(evidence.known_at),
        "as_of": _iso(as_of),
    }
    if detail:
        payload["detail"] = detail
    if on_reject is not None:
        try:
            on_reject("evidence.rejected", payload)
        except Exception as exc:
            raise EvidenceRejectedError(evidence.evidence_id, reason, detail) from exc
    raise EvidenceRejectedError(evidence.evidence_id, reason, detail)


def ingest_evidence(
    ledger: EvidenceLedger,
    evidence: Evidence,
    *,
    as_of: datetime | str | None,
    on_reject: Callable[[str, dict[str, object]], None] | None = None,
) -> Evidence:
    """Provenance + PIT gate: source/ref/retrieved_at/session/wave/lineage present, known_at <= as_of.

    Discovery rows are navigation artifacts (search/navigation tool results): they
    carry no source document, can never enter a freeze, and are never citable, so
    only their session/retrieval/lineage fields are required and PIT eligibility
    does not apply. Every evidential row keeps the full gate.

    Refusals journal ``evidence.rejected`` ({evidence_id, reason, known_at, as_of})
    via ``on_reject`` (wire KernelCore's append_event with functools.partial) then raise.
    """
    missing = _ingest_missing(evidence)
    if missing:
        reason, detail = "PROVENANCE_FAILURE", f"missing: {', '.join(missing)}"
    else:
        reason, detail = _ingest_pit_gate(evidence, as_of)
    if reason:
        _ingest_reject(evidence, as_of, reason, detail, on_reject)
    return ledger.append(evidence)


def evidence_to_dict(evidence: Evidence) -> dict[str, JSONValue]:
    """Evidence -> JSON-able dict (datetimes as ISO); the JSON blob is source of truth."""
    return {
        "evidence_id": evidence.evidence_id,
        "session_id": evidence.session_id,
        "wave_id": evidence.wave_id,
        "source_type": evidence.source_type,
        "source_name": evidence.source_name,
        "source_uri": evidence.source_uri,
        "source_record_id": evidence.source_record_id,
        "subject": evidence.subject,
        "claim_text": evidence.claim_text,
        "content": evidence.content,
        "content_hash": evidence.content_hash,
        "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
        "known_at": evidence.known_at.isoformat() if evidence.known_at else None,
        "effective_at": evidence.effective_at.isoformat() if evidence.effective_at else None,
        "retrieved_at": evidence.retrieved_at.isoformat(),
        "job_id": evidence.job_id,
        "agent_id": evidence.agent_id,
        "supports": _json_str_list(list(evidence.supports)),
        "contradicts": _json_str_list(list(evidence.contradicts)),
        "confidence": evidence.confidence,
        "quality": evidence.quality,
        "metadata": dict(evidence.metadata),
        "superseded_by": evidence.superseded_by,
        "record_kind": evidence.record_kind,
        "claim_kind": evidence.claim_kind,
        "provenance": dict(evidence.provenance),
    }


def _req_str(d: dict[str, object], key: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value:
        raise EvidenceIntegrityError(f"evidence: '{key}' must be a non-empty string")
    return value


def _opt_str(d: dict[str, object], key: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceIntegrityError(f"evidence: '{key}' must be a string or null")
    return value or None


def _req_dt(d: dict[str, object], key: str) -> datetime:
    value = d.get(key)
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            pass
        else:
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    raise EvidenceIntegrityError(f"evidence: '{key}' must be an ISO-8601 datetime")


def _opt_dt(d: dict[str, object], key: str) -> datetime | None:
    if d.get(key) is None:
        return None
    return _req_dt(d, key)


def _req_int(d: dict[str, object], key: str) -> int:
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceIntegrityError(f"evidence: '{key}' must be an int")
    return value


def _str_list(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
        raise EvidenceIntegrityError(f"evidence: '{key}' must be a list of strings")
    return tuple(value)


def _json_str_list(values: list[str]) -> list[JSONValue]:
    out: list[JSONValue] = []
    out.extend(values)
    return out


def _evidence_confidence(d: dict[str, object]) -> float | None:
    confidence = d.get("confidence")
    if confidence is None:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise EvidenceIntegrityError("evidence: 'confidence' must be a number or null")
    return float(confidence)


def _evidence_metadata(d: dict[str, object]) -> dict[str, object]:
    metadata = d.get("metadata", {})
    if not isinstance(metadata, dict):
        raise EvidenceIntegrityError("evidence: 'metadata' must be an object")
    return dict(metadata)


def _evidence_record_kind(d: dict[str, object]) -> str:
    """discovery|evidence; metadata.record_kind fallback; absent means evidence (back-compat)."""
    raw = d.get("record_kind")
    if raw is None:
        meta = d.get("metadata")
        raw = meta.get("record_kind") if isinstance(meta, dict) else None
    if raw is None:
        return "evidence"
    if isinstance(raw, str) and raw in RECORD_KINDS:
        return raw
    raise EvidenceIntegrityError(f"evidence: 'record_kind' must be discovery|evidence, got {raw!r}")


def _evidence_claim_kind(d: dict[str, object]) -> str:
    """claim_kind; absent means observed_fact so persisted history stays readable."""
    raw = d.get("claim_kind")
    if raw is None:
        return "observed_fact"
    if isinstance(raw, str) and raw in CLAIM_KINDS:
        return raw
    raise EvidenceIntegrityError(f"evidence: 'claim_kind' must be one of {list(CLAIM_KINDS)}, got {raw!r}")


def evidence_from_dict(data: Mapping[str, object]) -> Evidence:
    """Rebuild validated Evidence (constructor re-checks hash/confidence)."""
    d = dict(data)
    return Evidence(
        evidence_id=_req_str(d, "evidence_id"),
        session_id=_req_str(d, "session_id"),
        wave_id=_req_int(d, "wave_id"),
        source_type=_req_str(d, "source_type"),
        source_name=_req_str(d, "source_name"),
        subject=_req_str(d, "subject"),
        claim_text=_req_str(d, "claim_text"),
        content=_req_str(d, "content"),
        content_hash=_req_str(d, "content_hash"),
        retrieved_at=_req_dt(d, "retrieved_at"),
        source_uri=_opt_str(d, "source_uri"),
        source_record_id=_opt_str(d, "source_record_id"),
        published_at=_opt_dt(d, "published_at"),
        known_at=_opt_dt(d, "known_at"),
        effective_at=_opt_dt(d, "effective_at"),
        job_id=_opt_str(d, "job_id"),
        agent_id=_opt_str(d, "agent_id"),
        supports=_str_list(d.get("supports", []), "supports"),
        contradicts=_str_list(d.get("contradicts", []), "contradicts"),
        confidence=_evidence_confidence(d),
        metadata=validate_json_mapping(_evidence_metadata(d), "<evidence>: 'metadata'"),
        superseded_by=_opt_str(d, "superseded_by"),
        record_kind=_evidence_record_kind(d),
        claim_kind=_evidence_claim_kind(d),
        provenance=validate_provenance(d.get("provenance", {}), "<evidence>: 'provenance'"),
    )
