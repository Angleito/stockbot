"""Immutable raw archive for SEC filings, keyed by accession number.
Thin wrapper over :mod:`app.storage.raw_archive`: each payload kind
(``submission`` JSON, ``primary`` document bytes, ``xml``, ``index``)
is archived under ``source='sec'`` with the accession number as key.
Content-hash dedup and never-overwrite come from the raw archive;
amendments coexist as separate accession records. This module never
fetches anything — callers supply the bytes.
"""

from __future__ import annotations

import warnings

from pathlib import Path
from typing import Optional

from ..storage import raw_archive
from .models import Filing

DOCUMENT_KIND = "document"


def _document_key(accession_no: str, document_name: str) -> str:
    return f"{accession_no}/{document_name}"

def archive_sec_filing(
    filing: Filing,
    payloads: dict[str, bytes],
    *,
    url: str,
    retrieved_at: Optional[str] = None,
    root: Optional[Path] = None,
) -> dict[str, raw_archive.ArchiveRecord]:
    return {
        kind: raw_archive.archive(
            source="sec",
            kind=kind,
            key=filing.accession_no,
            payload=payload,
            url=url,
            retrieved_at=retrieved_at,
            metadata={"form": filing.form, "cik": str(filing.filer_cik)},
            root=root,
        )
        for kind, payload in payloads.items()
    }


def find_archived(
    accession_no: str,
    kind: str = "primary",
    root: Optional[Path] = None,
) -> Optional[raw_archive.ArchiveRecord]:
    """Return the archived record for an accession/kind, or None."""
    return raw_archive.find("sec", kind, accession_no, root=root)

def archive_sec_document(
    accession_no: str,
    document_name: str,
    payload: bytes,
    *,
    url: str,
    retrieved_at: Optional[str] = None,
    metadata: Optional[dict] = None,
    root: Optional[Path] = None,
) -> raw_archive.ArchiveRecord:
    """Archive one exact filing document under key ``(accession, document)``.

    Immutable: conflicting bytes create another revision and warn; nothing is
    ever overwritten. Re-archiving identical bytes is idempotent (no warning).
    """
    key = _document_key(accession_no, document_name)
    digest = raw_archive.content_hash(payload)
    existed = raw_archive.has_payload(
        "sec", DOCUMENT_KIND, key, sha256=digest, root=root)
    meta = {"accession_no": accession_no, "document_name": document_name}
    meta.update(metadata or {})
    record = raw_archive.archive(
        source="sec",
        kind=DOCUMENT_KIND,
        key=key,
        payload=payload,
        url=url,
        retrieved_at=retrieved_at,
        metadata=meta,
        root=root,
    )
    if not existed:
        others = [r for r in raw_archive.iter_archive(
            "sec", DOCUMENT_KIND, key, root=root) if r.sha256 != digest]
        if others:
            warnings.warn(
                f"new immutable revision for {accession_no}/{document_name}: "
                f"{len(others)} prior revision(s) retained, none overwritten",
                stacklevel=2,
            )
    return record


def find_archived_document(
    accession_no: str,
    document_name: str,
    *,
    sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> Optional[raw_archive.ArchiveRecord]:
    """Return the archived record for one accession/document, or None."""
    return raw_archive.find(
        "sec", DOCUMENT_KIND, _document_key(accession_no, document_name),
        sha256=sha256, root=root)


def iter_archived_documents(
    accession_no: str,
    document_name: str,
    *,
    root: Optional[Path] = None,
):
    """All byte revisions for one accession/document, oldest first."""
    yield from raw_archive.iter_archive(
        "sec", DOCUMENT_KIND, _document_key(accession_no, document_name),
        root=root)
