"""Document-level retrieval off a filing accession."""

from dataclasses import replace

from .models import pit_of
from .normalization import document_from_attachment, filing_from_edgar


def get_by_accession_number(accession_no: str):
    """Seam for tests: monkeypatch this name, never `edgar` itself."""
    from edgar import get_by_accession_number as _get

    return _get(accession_no)


def _filing(accession_no: str):
    try:
        filing = get_by_accession_number(accession_no)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid accession number: {accession_no!r}") from exc
    if filing is None:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    return filing


def _meta(accession_no: str):
    """Hydrate filing metadata first so exact lookups cannot bypass PIT."""
    return filing_from_edgar(_filing(accession_no))


def _require_known(accession_no: str, as_of):
    """Validate as_of and reject accessions unknown at that date."""
    from .filings import _check_as_of

    as_of = _check_as_of(as_of)
    if as_of is None:
        return _meta(accession_no)
    meta = _meta(accession_no)
    value, _basis = pit_of(meta)
    if value is None or value[:10] > as_of:
        raise ValueError(
            f"filing {accession_no!r} not known as of {as_of!r}")
    return meta


def list_sec_documents(accession_no: str, as_of=None):
    meta = _require_known(accession_no, as_of)
    filing = _filing(accession_no)
    out = []
    for a in filing.attachments:
        doc = document_from_attachment(accession_no, a)
        out.append(replace(
            doc,
            filed_at=meta.filed_at or None,
            accepted_at=meta.accepted_at,
            known_at=meta.known_at or None,
            source_url=meta.source or None,
            is_primary=(doc.document_name is not None
                        and doc.document_name == meta.primary_document),
        ))
    return out


def _resolve_in(filing, accession_no: str, document_name=None):
    if document_name is None:
        try:
            attachment = filing.document
        except Exception as exc:
            raise ValueError(
                f"no primary document for accession: {accession_no!r}"
            ) from exc
        if attachment is None:
            raise ValueError(f"no primary document for accession: {accession_no!r}")
        return attachment
    try:
        attachments = filing.attachments
    except Exception as exc:
        raise ValueError(f"no documents for accession: {accession_no!r}") from exc
    for attachment in attachments:
        try:
            name = attachment.document
        except Exception:
            continue
        if name == document_name:
            return attachment
    raise ValueError(f"document not found: {document_name!r}")


def _resolve(accession_no: str, document_name=None):
    return _resolve_in(_filing(accession_no), accession_no, document_name)


def _text_of(attachment) -> str:
    for attr in ("content", "text"):
        try:
            value = getattr(attachment, attr)
        except Exception:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        if isinstance(value, str):
            return value
    return ""


def get_sec_document(accession_no: str, document_name=None, as_of=None) -> dict:
    """Exact document retrieval; EFTS callers pass the matched document name.
    Primary-document fallback applies only when document_name is None."""
    if as_of is not None:
        _require_known(accession_no, as_of)
    attachment = _resolve(accession_no, document_name)
    if isinstance(attachment, str):
        return {
            "accession_no": accession_no,
            "document_name": attachment,
            "description": None,
            "url": "",
            "text": attachment,
        }
    try:
        name = attachment.document
    except Exception:
        name = None
    try:
        description = attachment.description
    except Exception:
        description = None
    try:
        url = attachment.url or ""
    except Exception:
        url = ""
    return {
        "accession_no": accession_no,
        "document_name": name if isinstance(name, str) else document_name,
        "description": description,
        "url": url,
        "text": _text_of(attachment),
    }


def get_sec_filing_text(accession_no: str, document_name=None, as_of=None) -> str:
    return get_sec_document(accession_no, document_name, as_of=as_of)["text"]


def _exhibit_dict(accession_no: str, attachment) -> dict:
    def _get(name):
        try:
            return getattr(attachment, name)
        except Exception:
            return None

    url = _get("url") or ""
    return {
        "accession_no": accession_no,
        "exhibit": _get("document_type"),
        "description": _get("description"),
        "document": _get("document"),
        "url": url if isinstance(url, str) else "",
    }


def get_filing_exhibits(accession_no: str) -> list:
    filing = _filing(accession_no)
    exhibits = getattr(filing, "exhibits", None)
    if exhibits is None:
        exhibits = filing.attachments
    return [_exhibit_dict(accession_no, a) for a in exhibits]


def get_filing_exhibit(accession_no: str, exhibit: str) -> dict:
    want = exhibit.upper()
    for row in get_filing_exhibits(accession_no):
        if (row["exhibit"] or "").upper() == want:
            return row
    raise ValueError(f"exhibit not found: {exhibit!r}")
