"""Canonical SEC dossier: validated findings over frozen evidence. stdlib only."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

__all__ = [
    "COVERAGE_KEYS",
    "DossierIntegrityError",
    "SECDossier",
    "create_dossier",
    "default_coverage",
    "dossier_to_dict",
    "validate_dossier",
]

COVERAGE_KEYS = ("entities", "forms", "time_range", "sources_examined", "complete", "exclusions")


def dossier_to_dict(dossier: SECDossier) -> dict[str, object]:
    """Immutable payload for the dossier table; datetimes as ISO, created_at stamped."""
    return {
        "dossier_id": dossier.dossier_id,
        "session_id": dossier.session_id,
        "wave_id": dossier.wave_id,
        "subject": dossier.subject,
        "coverage": deepcopy(dossier.coverage),
        "findings": [dict(finding) for finding in dossier.findings],
        "supporting_evidence_ids": list(dossier.supporting_evidence_ids),
        "contradicting_evidence_ids": list(dossier.contradicting_evidence_ids),
        "unknowns": list(dossier.unknowns),
        "limitations": list(dossier.limitations),
        "open_questions": list(dossier.open_questions),
        "as_of": dossier.as_of.isoformat() if dossier.as_of else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class DossierIntegrityError(ValueError):
    """Dangling evidence ref, broken coverage contract, or bad wave id."""


def default_coverage() -> dict[str, object]:
    """Empty coverage: explicitly incomplete until a scout fills it.

    ``resolved``/``partially_resolved``/``unresolved`` track SEC-answerable
    question state (SEC vs overall sufficiency stays separate: a dossier can
    resolve its SEC slice while the overall question needs non-SEC sources);
    ``source_limitations`` records source-scoped gaps; unknowns survive to the
    freeze via ``unknowns``/``open_questions``. Negative-evidence keys
    (forms/dates/partitions/docs/gaps + complete) scope every no-hit claim:
    a non-exhaustive negative stays scoped, never universal.
    """
    return {
        "entities": [],
        "forms": [],
        "time_range": {"start": None, "end": None},
        "sources_examined": [],
        "complete": False,
        "exclusions": [],
        "resolved": [],
        "partially_resolved": [],
        "unresolved": [],
        "source_limitations": [],
        "dates": [],
        "partitions": [],
        "docs": [],
        "gaps": [],
    }


@dataclass(frozen=True)
class SECDossier:
    """One validated SEC slice; refs must resolve against the ledger at validate time."""

    dossier_id: str
    session_id: str
    wave_id: int
    subject: str = ""
    coverage: dict[str, object] = field(default_factory=default_coverage)
    findings: list[dict[str, object]] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.wave_id, bool) or not isinstance(self.wave_id, int):
            raise DossierIntegrityError(f"dossier {self.dossier_id}: 'wave_id' must be an int")


def _coerce_wave(wave_id: int | str) -> int:
    if isinstance(wave_id, bool):
        raise DossierIntegrityError(f"dossier: 'wave_id' must be an int, got {wave_id!r}")
    if isinstance(wave_id, int):
        return wave_id
    text = wave_id.strip()
    if text.isdigit():
        return int(text)
    raise DossierIntegrityError(f"dossier: 'wave_id' must be an int, got {wave_id!r}")


def _coerce_as_of(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DossierIntegrityError(f"dossier: 'as_of' must be ISO-8601, got {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _validate_finding_shape(finding: Mapping[str, object], dossier_id: str) -> tuple[str, list[str]]:
    """Single finding text + deduped evidence ids (raises on free-text)."""
    text = finding.get("text")
    ids = finding.get("evidence_ids")
    if not isinstance(text, str) or not text.strip():
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'text' must be a non-empty string")
    if not isinstance(ids, list) or not ids or any(not isinstance(e, str) or not e for e in ids):
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'evidence_ids' must be a non-empty list of strings")
    uniq = list(dict.fromkeys(ids))
    return text, uniq


def _ground_findings(
    findings: Sequence[Mapping[str, object]], dossier_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validated findings + derived supporting set (order-stable, deduped)."""
    grounded: list[dict[str, object]] = []
    supporting: list[str] = []
    for finding in findings:
        text, uniq = _validate_finding_shape(finding, dossier_id)
        grounded.append({"text": text, "evidence_ids": uniq})
        for eid in uniq:
            if eid not in supporting:
                supporting.append(eid)
    return grounded, supporting


def create_dossier(
    *,
    dossier_id: str,
    session_id: str,
    wave_id: int | str,
    subject: str = "",
    as_of: datetime | str | None = None,
    coverage: Mapping[str, object] | None = None,
    findings: Sequence[Mapping[str, object]] = (),
    contradicting_evidence_ids: Sequence[str] = (),
    unknowns: Sequence[str] = (),
    limitations: Sequence[str] = (),
    open_questions: Sequence[str] = (),
) -> SECDossier:
    """Pure constructor, no I/O. Supporting set derives from findings only.

    Each finding must be ``{"text": str, "evidence_ids": [str, ...]}`` with a
    non-empty citation set; free-text or whole-freeze citations are rejected.
    Inputs are copied so later caller mutation cannot leak in. ``coverage``
    may carry ``resolved``/``partially_resolved``/``unresolved`` plus
    ``source_limitations``; ``unknowns`` and ``open_questions`` are preserved
    verbatim so unknowns survive to the freeze.
    """
    grounded, supporting = _ground_findings(findings, dossier_id)
    return SECDossier(
        dossier_id=dossier_id,
        session_id=session_id,
        wave_id=_coerce_wave(wave_id),
        subject=subject,
        coverage=deepcopy(dict(coverage)) if coverage is not None else default_coverage(),
        findings=grounded,
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=list(dict.fromkeys(contradicting_evidence_ids)),
        unknowns=list(unknowns),
        limitations=list(limitations),
        open_questions=list(open_questions),
        as_of=_coerce_as_of(as_of),
    )


def _require_str_list(coverage: Mapping[str, object], key: str, dossier_id: str) -> None:
    value = coverage.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage[{key!r}] must be a list of strings")


def _check_dossier_identity(dossier: SECDossier) -> None:
    """Non-empty dossier/session ids (first gate, no coverage touch)."""
    if not dossier.dossier_id:
        raise DossierIntegrityError("dossier: 'dossier_id' must be non-empty")
    if not dossier.session_id:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: 'session_id' must be non-empty")


def _check_coverage_time_range(coverage: Mapping[str, object], dossier_id: str) -> None:
    """time_range must be {start: str|None, end: str|None}."""
    time_range = coverage.get("time_range")
    bad = (
        not isinstance(time_range, dict)
        or "start" not in time_range
        or "end" not in time_range
        or not (time_range["start"] is None or isinstance(time_range["start"], str))
        or not (time_range["end"] is None or isinstance(time_range["end"], str))
    )
    if bad:
        raise DossierIntegrityError(
            f"dossier {dossier_id}: coverage['time_range'] must be {{start: str|None, end: str|None}}"
        )


def _check_coverage_contract(coverage: Mapping[str, object], dossier_id: str) -> None:
    """Keys present + list types + time_range shape + complete flag.

    Resolution keys (resolved/partially_resolved/unresolved/source_limitations)
    are optional so older callers still validate; when present they must be
    string lists. Negative-evidence keys (dates/partitions/docs/gaps) are
    optional but, when present, must be string lists so no-hit claims carry
    their {forms,dates,partitions,docs,gaps,complete} scope.
    """
    missing = [key for key in COVERAGE_KEYS if key not in coverage]
    if missing:
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage missing keys {missing}")
    for key in ("entities", "forms", "sources_examined", "exclusions"):
        _require_str_list(coverage, key, dossier_id)
    for key in ("resolved", "partially_resolved", "unresolved", "source_limitations",
                "dates", "partitions", "docs", "gaps"):
        if key in coverage:
            _require_str_list(coverage, key, dossier_id)
    _check_coverage_time_range(coverage, dossier_id)
    if not isinstance(coverage.get("complete"), bool):
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage['complete'] must be a bool")



def _check_finding_ids_shape(ids: object, dossier_id: str) -> list[str]:
    """Finding citation list shape (non-empty strings); returns the ids."""
    if not isinstance(ids, list) or not ids or any(not isinstance(e, str) for e in ids):
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'evidence_ids' must be a non-empty list of strings")
    return list(ids)


def _check_finding_membership(ids: list[str], known: set[str], supporting_set: set[str], dossier_id: str) -> None:
    """Every cited id resolves to the ledger and the supporting set."""
    for eid in ids:
        if eid not in known:
            raise DossierIntegrityError(f"dossier {dossier_id}: unknown evidence ids {[eid][:5]}")
        if eid not in supporting_set:
            raise DossierIntegrityError(f"dossier {dossier_id}: finding cites id outside supporting set: {eid!r}")


def _check_single_finding_ref(
    finding: object, known: set[str], supporting_set: set[str], dossier_id: str,
) -> None:
    """One finding mapping + membership (shape then ledger gates)."""
    if not isinstance(finding, dict):
        raise DossierIntegrityError(f"dossier {dossier_id}: finding must be a mapping")
    ids = _check_finding_ids_shape(finding.get("evidence_ids"), dossier_id)
    _check_finding_membership(ids, known, supporting_set, dossier_id)


def _check_dossier_refs(dossier: SECDossier, known: set[str]) -> None:
    """Supporting/contradicting sets resolve; each finding cites supporting."""
    cited = set(dossier.supporting_evidence_ids) | set(dossier.contradicting_evidence_ids)
    dangling = sorted(cited - known)
    if dangling:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: unknown evidence ids {dangling[:5]}")
    supporting_set = set(dossier.supporting_evidence_ids)
    for finding in dossier.findings:
        _check_single_finding_ref(finding, known, supporting_set, dossier.dossier_id)


def validate_dossier(dossier: SECDossier, ledger_ids: Collection[str]) -> None:
    """Coverage contract + every supporting/contradicting/finding id must exist in the ledger."""
    _check_dossier_identity(dossier)
    _check_coverage_contract(dossier.coverage, dossier.dossier_id)
    _check_dossier_refs(dossier, set(ledger_ids))
