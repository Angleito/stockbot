"""Deterministic SC 13D/G beneficial-ownership normalization (no network)."""

from .models import BeneficialOwnership, OwnershipChangeEvent

_FORMS_13D = ("SC 13D", "SC 13D/A")
_FORMS_13G = ("SC 13G", "SC 13G/A")
_DEFAULT_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def _safe_int(value):
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


def _safe_float(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip().replace(",", "").rstrip("%")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return float(text)
    except (ValueError, TypeError):
        return None


def _first(obj, *names):
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _purpose_of(items) -> "str | None":
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
def _subject_of(schedule) -> "tuple[str | None, str | None]":
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


def normalize_schedule(schedule, *, issuer, form, filed_at, accession_no,
                       subject_cik=None, subject_name=None,
                       document_name=None, known_at=None, source_url=None):
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
        explicit_name = str(subject_name).strip() if subject_name is not None else None
    except Exception:
        explicit_name = None
    resolved_cik = explicit_cik or info_cik
    resolved_name = explicit_name or info_name
    try:
        is_amend = str(form or "").strip().upper().endswith("/A")
    except Exception:
        is_amend = False
    try:
        known = str(known_at) if known_at is not None else filed_at
    except Exception:
        known = filed_at
    out = []
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


def load_schedule(accession_no: str):
    """Live seam: edgar import stays here; raises on failure."""
    from .documents import get_by_accession_number

    filing = get_by_accession_number(accession_no)
    form = getattr(filing, "form", "") or ""
    if form in _FORMS_13D:
        from edgar.beneficial_ownership import Schedule13D

        return Schedule13D.from_filing(filing)
    from edgar.beneficial_ownership import Schedule13G

    return Schedule13G.from_filing(filing)


def get_beneficial_ownership(ticker_or_cik, *, as_of=None, limit=20,
                             forms=_DEFAULT_FORMS):
    filings = list_sec_filings(ticker_or_cik, forms=list(forms),
                               as_of=as_of, limit=limit)
    out = []
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


def query_subject_owners(subject_cik, *, as_of=None, root=None, limit=200):
    """Subject -> reporting owners over ``sec_beneficial_ownership`` (PIT)."""
    from . import store as _store

    return _store.query_beneficial_ownership(
        subject_cik=subject_cik, as_of=as_of, root=root, limit=limit)


def query_owner_subjects(owner_cik, *, as_of=None, root=None, limit=200):
    """Owner/reporter -> subjects over ``sec_beneficial_ownership`` (PIT)."""
    from . import store as _store

    return _store.query_beneficial_ownership(
        owner_cik=owner_cik, as_of=as_of, root=root, limit=limit)


def diff_ownership(previous: BeneficialOwnership, current: BeneficialOwnership):
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


def get_ownership_changes(ticker_or_cik, *, as_of=None, limit=20):
    records = get_beneficial_ownership(ticker_or_cik, as_of=as_of, limit=None)
    groups: dict = {}
    for record in records:
        groups.setdefault(record.filer_cik or record.filer_name, []).append(record)
    events = []
    for filings in groups.values():
        filings.sort(key=lambda r: (r.filed_at or "", r.accession_no))
        for prev, curr in zip(filings, filings[1:]):
            try:
                events.append(diff_ownership(prev, curr))
            except Exception:
                continue
    events.sort(key=lambda e: (e.filed_at or "", e.current_accession),
                reverse=True)
    if limit is not None:
        events = events[:limit]
    return events
