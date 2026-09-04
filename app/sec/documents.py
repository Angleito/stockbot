"""Document-level retrieval off a filing accession."""

from .normalization import document_from_attachment


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


def list_sec_documents(accession_no: str):
    filing = _filing(accession_no)
    return [document_from_attachment(accession_no, a) for a in filing.attachments]


def _resolve(accession_no: str, document_name=None):
    filing = _filing(accession_no)
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


def get_sec_document(accession_no: str, document_name=None) -> dict:
    attachment = _resolve(accession_no, document_name)
    if isinstance(attachment, str):
        return {
            "accession_no": accession_no,
            "document": attachment,
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
        "document": name if isinstance(name, str) else document_name,
        "description": description,
        "url": url,
        "text": _text_of(attachment),
    }


def get_sec_filing_text(accession_no: str, document_name=None) -> str:
    return get_sec_document(accession_no, document_name)["text"]


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
