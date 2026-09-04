"""Immutable raw archive for SEC filings, keyed by accession number.

Thin wrapper over :mod:`app.storage.raw_archive`: each payload kind
(``submission`` JSON, ``primary`` document bytes, ``xml``, ``index``)
is archived under ``source='sec'`` with the accession number as key.
Content-hash dedup and never-overwrite come from the raw archive;
amendments coexist as separate accession records. This module never
fetches anything — callers supply the bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..storage import raw_archive
from .models import Filing


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
            metadata={"form": filing.form, "cik": filing.cik},
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
