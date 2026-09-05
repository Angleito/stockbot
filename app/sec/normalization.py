"""edgartools objects -> domain models. Every optional metadata access is
best-effort: any failure yields None, never an invented value."""

from .models import Filing, FilingDocument


def _best(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _str_or_none(value):
    if value is None:
        return None
    try:
        text = str(value)
    except Exception:
        return None
    return text or None


def _accepted_at(filing):
    for attr in ("acceptance_datetime", "accepted_at"):
        value = _best(lambda a=attr: getattr(filing, a))
        if value is not None:
            return _str_or_none(value)
    header = _best(lambda: filing.header)
    if header is not None:
        for attr in ("acceptance_datetime", "accepted_at", "acceptance_time"):
            value = _best(lambda a=attr: getattr(header, a))
            if value is not None:
                return _str_or_none(value)
    sgml = _best(lambda: filing.sgml())
    if sgml is not None:
        for attr in ("acceptance_datetime", "accepted_at"):
            value = _best(lambda a=attr: getattr(sgml, a))
            if value is not None:
                return _str_or_none(value)
    return None


_ISSUER_SUBJECT_FORMS = frozenset({"10-K", "10-Q", "8-K", "S-1", "S-3", "DEF 14A"})


def _subject_of(form: str, filer_cik: int, filer_name: str) -> tuple:
    """Subject equals filer only for issuer periodic/current/registration/proxy
    forms; third-party filings (13D/G, 3/4/5/144, 13F, tender/merger, …) leave
    subject unknown until a structured parser supplies it."""
    base = (form or "").split("/")[0].strip().upper()
    if base in _ISSUER_SUBJECT_FORMS:
        return filer_cik, filer_name
    return None, None


def filing_from_edgar(filing) -> Filing:
    form = _best(lambda: filing.form, "") or ""
    filed_at = str(_best(lambda: filing.filing_date, "") or "")
    accepted_at = _accepted_at(filing)
    accession_no = _best(lambda: filing.accession_no)
    if accession_no is None:
        accession_no = _best(lambda: filing.accession_number, "") or ""
    doc = _best(lambda: filing.document)
    if isinstance(doc, str):
        primary_document = doc or None
    elif doc is None:
        primary_document = None
    else:
        name = _best(lambda: doc.document)
        primary_document = name if isinstance(name, str) and name else None
    filer_cik = _best(lambda: int(filing.cik), 0) or 0
    filer_name = _best(lambda: str(filing.company), "") or ""
    subject_cik, subject_name = _subject_of(form, filer_cik, filer_name)
    return Filing(
        accession_no=accession_no,
        form=form,
        filer_cik=filer_cik,
        filer_name=filer_name,
        filed_at=filed_at,
        accepted_at=accepted_at,
        known_at=accepted_at or filed_at,
        report_period=_str_or_none(_best(lambda: filing.period_of_report)),
        primary_document=primary_document,
        is_amendment=form.endswith("/A"),
        amendment_of=None,
        source=_best(lambda: str(filing.homepage_url), "") or "",
        subject_cik=subject_cik,
        subject_name=subject_name,
        accepted_at_missing=accepted_at is None,
    )


def document_from_attachment(accession_no: str, attachment) -> FilingDocument:
    return FilingDocument(
        accession_no=accession_no,
        document_name=_best(lambda: attachment.document),
        description=_best(lambda: attachment.description),
        size=_best(lambda: attachment.size),
        url=_best(lambda: attachment.url, "") or "",
        document_type=_best(lambda: attachment.document_type),
    )
