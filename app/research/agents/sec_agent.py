"""SEC source agent: bounded scout fan-out -> validated dossier.

Fake-model sketch (no live calls): inject ``spawn`` returning canned
``ScoutResult``s, ``dispatch`` returning ``{"evidence_ids": [...]}`` for the
known-set probe, ``model`` returning canned text; assert the dossier holds
only known evidence ids and carries session/wave/as_of labels.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import import_module

from .scout import DispatchFn, ModelFn, ScoutAssignment, ScoutResult, run_scout
from .source_agent import SourceDossier, assemble_dossier, decompose_question
def _coerce_wave(wave_id: int | str) -> int:
    """Accept int>=1 or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"sec assignment: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"sec assignment: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"sec assignment: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if wave < 1:
        raise ValueError(f"sec assignment: 'wave_id' must be >= 1, got {wave_id!r}")
    return wave


def _coerce_dossier(fallback: SourceDossier) -> object:
    """Prefer canonical dossiers/sec SECDossier, preserving every scout field."""
    try:
        dossiers_sec = import_module("app.research.dossiers.sec")
    except ImportError:
        return fallback
    factory = getattr(dossiers_sec, "create_dossier", None)
    if not callable(factory):
        return fallback
    try:
        entities: list[str] = []
        forms: list[str] = []
        supporting = list(fallback.evidence_ids)
        coverage: dict[str, object] = {
            "entities": entities,
            "forms": forms,
            "time_range": {"start": None, "end": None},
            "sources_examined": list(fallback.coverage_notes),
            "complete": False,
            "exclusions": [],
        }
        findings: list[dict[str, object]] = [
            {"finding_id": fid, "evidence_ids": supporting}
            for fid in fallback.finding_ids
        ]
        dossier = factory(
            dossier_id=fallback.dossier_id,
            session_id=fallback.session_id,
            wave_id=fallback.wave_id,
            as_of=fallback.as_of,
            coverage=coverage,
            findings=findings,
            supporting_evidence_ids=supporting,
            unknowns=list(fallback.unknowns),
            limitations=list(fallback.limitations),
        )
        validator = getattr(dossiers_sec, "validate_dossier", None)
        if callable(validator):
            validator(dossier, set(supporting))
        return dossier
    except Exception:
        return fallback


def run_sec_assignment(
    question: str,
    *,
    session_id: str,
    wave_id: int | str,
    as_of: str,
    tickers: Sequence[str],
    dispatch: DispatchFn,
    model: ModelFn,
    spawn: Callable[[ScoutAssignment], ScoutResult] | None = None,
    journal: Callable[[str, dict[str, object]], None] | None = None,
    known_evidence_ids: Sequence[str] | None = None,
) -> object:
    """Decompose via catalog discovery, run 3 bounded scouts, validate refs.

    ``spawn`` defaults to inline ``run_scout`` (jobs.py ``create_job`` path
    plugs in here when available). Returns canonical ``SECDossier`` when the
    dossiers module is importable, else the local ``SourceDossier``.
    ``wave_id`` accepts int>=1 or a numeric str and is stored as int.
    """
    wave = _coerce_wave(wave_id)

    def run_inline(assignment: ScoutAssignment) -> ScoutResult:
        return run_scout(assignment, dispatch=dispatch, model=model, journal=journal)
    run_one = spawn if spawn is not None else run_inline
    # Serial: Pi providers serve parallel=1, so concurrent scout model calls
    # contend on one slot and all hit the call timeout together.
    results = [run_one(assignment) for assignment in assignments_for(question, session_id, as_of, tickers, dispatch)]
    known: Sequence[str]
    if known_evidence_ids is not None:
        known = known_evidence_ids
    else:
        seen: list[str] = []
        for result in results:
            for eid in result.evidence_ids:
                if eid not in seen:
                    seen.append(eid)
        known = seen
    dossier = assemble_dossier(
        dossier_id=f"{session_id}:{wave}:sec",
        session_id=session_id,
        wave_id=wave,
        as_of=as_of,
        results=results,
        known_evidence_ids=known,
        journal=journal,
    )
    return _coerce_dossier(dossier)


def assignments_for(
    question: str,
    session_id: str,
    as_of: str,
    tickers: Sequence[str],
    dispatch: DispatchFn,
) -> list[ScoutAssignment]:
    """Catalog-driven decomposition (split for testability)."""
    return decompose_question(
        question, session_id=session_id, as_of=as_of, tickers=tickers, dispatch=dispatch
    )


__all__ = ["assignments_for", "run_sec_assignment"]
