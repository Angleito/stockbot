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
    """Empty coverage: explicitly incomplete until a scout fills it."""
    return {
        "entities": [],
        "forms": [],
        "time_range": {"start": None, "end": None},
        "sources_examined": [],
        "complete": False,
        "exclusions": [],
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


def create_dossier(
    *,
    dossier_id: str,
    session_id: str,
    wave_id: int | str,
    subject: str = "",
    as_of: datetime | str | None = None,
    coverage: Mapping[str, object] | None = None,
    findings: Sequence[Mapping[str, object]] = (),
    evidence_ids: Sequence[str] = (),
    supporting_evidence_ids: Sequence[str] = (),
    contradicting_evidence_ids: Sequence[str] = (),
    unknowns: Sequence[str] = (),
    limitations: Sequence[str] = (),
    open_questions: Sequence[str] = (),
) -> SECDossier:
    """Pure constructor, no I/O. ``evidence_ids`` aliases ``supporting_evidence_ids``.

    Accepts the source-agent call shape (``as_of``/``evidence_ids``) and the full
    canonical shape; inputs are copied so later caller mutation cannot leak in.
    """
    supporting = list(dict.fromkeys(supporting_evidence_ids)) or list(dict.fromkeys(evidence_ids))
    return SECDossier(
        dossier_id=dossier_id,
        session_id=session_id,
        wave_id=_coerce_wave(wave_id),
        subject=subject,
        coverage=deepcopy(dict(coverage)) if coverage is not None else default_coverage(),
        findings=[dict(finding) for finding in findings],
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


def validate_dossier(dossier: SECDossier, ledger_ids: Collection[str]) -> None:
    """Coverage contract + every supporting/contradicting id must exist in the ledger."""
    if not dossier.dossier_id:
        raise DossierIntegrityError("dossier: 'dossier_id' must be non-empty")
    if not dossier.session_id:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: 'session_id' must be non-empty")
    missing = [key for key in COVERAGE_KEYS if key not in dossier.coverage]
    if missing:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: coverage missing keys {missing}")
    for key in ("entities", "forms", "sources_examined", "exclusions"):
        _require_str_list(dossier.coverage, key, dossier.dossier_id)
    time_range = dossier.coverage.get("time_range")
    if (
        not isinstance(time_range, dict)
        or "start" not in time_range
        or "end" not in time_range
        or not (time_range["start"] is None or isinstance(time_range["start"], str))
        or not (time_range["end"] is None or isinstance(time_range["end"], str))
    ):
        raise DossierIntegrityError(
            f"dossier {dossier.dossier_id}: coverage['time_range'] must be {{start: str|None, end: str|None}}"
        )
    if not isinstance(dossier.coverage.get("complete"), bool):
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: coverage['complete'] must be a bool")
    known = set(ledger_ids)
    cited = set(dossier.supporting_evidence_ids) | set(dossier.contradicting_evidence_ids)
    dangling = sorted(cited - known)
    if dangling:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: unknown evidence ids {dangling[:5]}")
