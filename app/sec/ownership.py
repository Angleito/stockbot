"""Deterministic SC 13D/G beneficial-ownership normalization (no network)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .models import BeneficialOwnership, Filing, OwnershipChangeEvent

if TYPE_CHECKING:
    # Provider SDK schedules share no common base, and tests exercise this
    # boundary with SimpleNamespace doubles, so both shapes are named.
    from types import SimpleNamespace

    from edgar.beneficial_ownership import Schedule13D, Schedule13G

_FORMS_13D = ("SC 13D", "SC 13D/A")
_FORMS_13G = ("SC 13G", "SC 13G/A")
_DEFAULT_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")


def list_sec_filings(
    ticker_or_cik: str | int,
    forms: str | list[str] | tuple[str, ...] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    as_of: str | date | datetime | None = None,
    limit: int | None = 50,
) -> list[Filing]:
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(ticker_or_cik, forms=forms, start_date=start_date,
                 end_date=end_date, as_of=as_of, limit=limit)


def _safe_int(value: object) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return int(float(text)) if "." in text else int(text)
    except (ValueError, TypeError):
        return None


def _safe_float(value: object) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip().replace(",", "").rstrip("%")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return float(text)
    except (ValueError, TypeError):
        return None


def _first(obj: object, *names: str) -> object:
    """First present attribute; the SDK/test-double schedules probed here are
    dynamically shaped, so callers validate the result (`_safe_int`,
    `_safe_float`, `str`)."""
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _purpose_of(items: object) -> str | None:
    if items is None:
        return None
    for attr in ("purpose_of_transaction", "purpose"):
        try:
            value = getattr(items, attr)
        except Exception:
            continue
        if value:
            try:
                return str(value)
            except Exception:
                continue
    try:
        text = str(items)
    except Exception:
        return None
    if text and not text.startswith("<"):
        return text
    return None


def _subject_of(schedule: Schedule13D | Schedule13G | SimpleNamespace) -> tuple[str | None, str | None]:
    """Authoritative subject from structured issuer info; never the filer."""
    try:
        info = getattr(schedule, "issuer_info", None)
    except Exception:
        return None, None
    if info is None:
        return None, None
    cik = _first(info, "cik", "issuerCIK", "issuer_cik")
    name = _first(info, "name", "issuerName", "issuer_name")
    try:
        cik = str(cik).strip() if cik is not None else None
    except Exception:
        cik = None
    try:
        name = str(name).strip() if name is not None else None
    except Exception:
        name = None
    return (cik or None), (name or None)


def normalize_schedule(
    schedule: Schedule13D | Schedule13G | SimpleNamespace,
    *,
    issuer: str,
    form: str,
    filed_at: str | None,
    accession_no: str,
    subject_cik: str | int | None = None,
    subject_name: str | None = None,
    document_name: str | None = None,
    known_at: str | None = None,
    source_url: str | None = None,
) -> list[BeneficialOwnership]:
    """One BeneficialOwnership per reporting person; never raises.

    Subject identity comes only from explicit args or the schedule's
    structured ``issuer_info``. The filer is never copied into subject.
    """
    try:
        persons = getattr(schedule, "reporting_persons", []) or []
    except Exception:
        return []
    if isinstance(persons, (str, bytes)) or not isinstance(persons, (list, tuple)):
        return []
    try:
        items = getattr(schedule, "items", None)
    except Exception:
        items = None
    purpose = _purpose_of(items)
    info_cik, info_name = _subject_of(schedule)
    try:
        explicit_cik = str(subject_cik).strip() if subject_cik is not None else None
    except Exception:
        explicit_cik = None
    try:
        explicit_name = subject_name.strip() if subject_name is not None else None
    except Exception:
        explicit_name = None
    resolved_cik = explicit_cik or info_cik
    resolved_name = explicit_name or info_name
    try:
        is_amend = (form or "").strip().upper().endswith("/A")
    except Exception:
        is_amend = False
    try:
        known = known_at if known_at is not None else filed_at
    except Exception:
        known = filed_at
    out: list[BeneficialOwnership] = []
    iterator = persons
    for person in iterator:
        try:
            name = _first(person, "name", "filer_name", "reporting_person_name")
            cik = _first(person, "cik", "filer_cik", "reporting_person_cik")
            out.append(BeneficialOwnership(
                filer_name=str(name) if name is not None else "",
                filer_cik=str(cik) if cik is not None else None,
                issuer=issuer,
                form=form,
                filed_at=filed_at,
                accession_no=accession_no,
                shares=_safe_int(_first(person, "aggregate_amount", "shares",
                                              "beneficially_owned", "aggregate_shares")),
                percent=_safe_float(_first(person, "percent_of_class", "percent",
                                                  "ownership_percent")),
                sole_voting=_safe_int(_first(person, "sole_voting_power", "sole_voting")),
                shared_voting=_safe_int(_first(person, "shared_voting_power",
                                                      "shared_voting")),
                sole_dispositive=_safe_int(_first(person, "sole_dispositive_power",
                                                         "sole_dispositive")),
                shared_dispositive=_safe_int(_first(person, "shared_dispositive_power",
                                                           "shared_dispositive")),
                is_amendment=is_amend,
                purpose_text=purpose,
                subject_cik=resolved_cik,
                subject_name=resolved_name,
                document_name=document_name,
                known_at=known,
                source_url=source_url,
            ))
        except Exception:
            continue
    return out

def load_schedule(accession_no: str) -> Schedule13D | Schedule13G:
    """Live seam: edgar import stays here; raises on failure."""
    from .documents import get_by_accession_number

    filing = get_by_accession_number(accession_no)
    form = getattr(filing, "form", "") or ""
    if form in _FORMS_13D:
        from edgar.beneficial_ownership import Schedule13D

        schedule = Schedule13D.from_filing(filing)
        if schedule is None:
            raise ValueError(f"no schedule for accession {accession_no!r}")
        return schedule
    from edgar.beneficial_ownership import Schedule13G

    schedule = Schedule13G.from_filing(filing)
    if schedule is None:
        raise ValueError(f"no schedule for accession {accession_no!r}")
    return schedule


def get_beneficial_ownership(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 20,
    forms: tuple[str, ...] | list[str] = _DEFAULT_FORMS,
) -> list[BeneficialOwnership]:
    filings = list_sec_filings(ticker_or_cik, forms=list(forms),
                               as_of=as_of, limit=limit)
    out: list[BeneficialOwnership] = []
    for filing in filings:
        try:
            accession = getattr(filing, "accession_no", "")
            form = getattr(filing, "form", "")
            filed_at = getattr(filing, "filed_at", None)
            issuer = getattr(filing, "filer_name", None) or str(ticker_or_cik)
            schedule = load_schedule(accession)
            try:
                info = getattr(schedule, "issuer_info", None)
                subject_name = getattr(info, "name", None) if info is not None else None
                issuer = subject_name or issuer
            except Exception:
                pass
            out.extend(normalize_schedule(
                schedule, issuer=issuer, form=form, filed_at=filed_at,
                accession_no=accession,
                document_name=getattr(filing, "primary_document", None),
                known_at=getattr(filing, "known_at", None) or filed_at,
                source_url=getattr(filing, "source", None)))
        except Exception:
            continue
    if limit is not None:
        out = out[:limit]
    return out


def query_subject_owners(
    subject_cik: int | str,
    *,
    as_of: str | None = None,
    root: Path | str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Subject -> reporting owners over ``sec_beneficial_ownership`` (PIT)."""
    from . import store as _store

    return _store.query_beneficial_ownership(
        subject_cik=subject_cik, as_of=as_of, root=root, limit=limit)


def query_owner_subjects(
    owner_cik: int | str,
    *,
    as_of: str | None = None,
    root: Path | str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Owner/reporter -> subjects over ``sec_beneficial_ownership`` (PIT)."""
    from . import store as _store

    return _store.query_beneficial_ownership(
        owner_cik=owner_cik, as_of=as_of, root=root, limit=limit)


def _filer_key(record: BeneficialOwnership) -> str:
    return record.filer_cik or record.filer_name


def _record_order(record: BeneficialOwnership) -> tuple[str, str]:
    return (record.filed_at or "", record.accession_no)


def _event_order(event: OwnershipChangeEvent) -> tuple[str, str]:
    return (event.filed_at or "", event.current_accession)


def diff_ownership(previous: BeneficialOwnership, current: BeneficialOwnership) -> OwnershipChangeEvent:
    share_change = None
    if previous.shares is not None and current.shares is not None:
        share_change = current.shares - previous.shares
    percent_change = None
    if previous.percent is not None and current.percent is not None:
        percent_change = current.percent - previous.percent
    voting_changed = any(
        a is not None and b is not None and a != b
        for a, b in (
            (previous.sole_voting, current.sole_voting),
            (previous.shared_voting, current.shared_voting),
            (previous.sole_dispositive, current.sole_dispositive),
            (previous.shared_dispositive, current.shared_dispositive),
        )
    )
    text_changed = bool(previous.purpose_text and current.purpose_text
                        and previous.purpose_text != current.purpose_text)
    return OwnershipChangeEvent(
        filer_name=current.filer_name,
        filer_cik=current.filer_cik,
        issuer=current.issuer,
        previous_accession=previous.accession_no,
        current_accession=current.accession_no,
        filed_at=current.filed_at,
        prev_shares=previous.shares,
        curr_shares=current.shares,
        share_change=share_change,
        prev_percent=previous.percent,
        curr_percent=current.percent,
        percent_change=percent_change,
        voting_changed=voting_changed,
        text_changed=text_changed,
    )


def get_ownership_changes(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 20,
) -> list[OwnershipChangeEvent]:
    records = get_beneficial_ownership(ticker_or_cik, as_of=as_of, limit=None)
    groups: dict[str, list[BeneficialOwnership]] = {}
    for record in records:
        groups.setdefault(_filer_key(record), []).append(record)
    events: list[OwnershipChangeEvent] = []
    for filings in groups.values():
        filings.sort(key=_record_order)
        for prev, curr in zip(filings, filings[1:]):
            try:
                events.append(diff_ownership(prev, curr))
            except Exception:
                continue
    events.sort(key=_event_order, reverse=True)
    if limit is not None:
        events = events[:limit]
    return events
