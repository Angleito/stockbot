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
def normalize_schedule(schedule, *, issuer, form, filed_at, accession_no):
    """One BeneficialOwnership per reporting person; never raises."""
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
    try:
        is_amend = str(form or "").strip().upper().endswith("/A")
    except Exception:
        is_amend = False
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
        from edgar import Schedule13D

        return Schedule13D.from_filing(filing)
    from edgar import Schedule13G

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
            issuer = getattr(filing, "company", None) or str(ticker_or_cik)
            schedule = load_schedule(accession)
            out.extend(normalize_schedule(schedule, issuer=issuer, form=form,
                                          filed_at=filed_at,
                                          accession_no=accession))
        except Exception:
            continue
    if limit is not None:
        out = out[:limit]
    return out


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
