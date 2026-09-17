"""Shared test seam for evidence admission: a fake SEC archive behind the kernel reload.

`record_evidence` admits an observed fact only by reloading the document window a
canonical ``source_handle`` names and slicing the cited passage out of those bytes.
Tests mint handles here and route the reload to this fake archive, so evidence
admission is exercised end to end without touching the network.
"""

from __future__ import annotations

from hashlib import sha256

import pytest

DEFAULT_ACCESSION = "0000320193-25-000079"
DEFAULT_DOCUMENT = "nvda-20250331.htm"

_DOC_WINDOWS: dict[tuple[str, str], str] = {}


def handle_for(
    passage: str,
    *,
    accession: str = DEFAULT_ACCESSION,
    document: str = DEFAULT_DOCUMENT,
) -> dict[str, object]:
    """Canonical source_handle for one cited passage.

    Passages accumulate under the document key and the window is the span this
    passage occupies, so reloading the same coordinates reproduces exactly these
    bytes and a different passage at the same coordinates fails the hash check.
    """
    window = _DOC_WINDOWS.get((accession, document), "")
    offset = len(window) + (1 if window else 0)
    _DOC_WINDOWS[(accession, document)] = f"{window}\n{passage}" if window else passage
    return {
        "accession_no": accession,
        "document_name": document,
        "basis": "rendered",
        "section": None,
        "query": None,
        "offset": offset,
        "max_chars": len(passage),
        "length": len(passage),
        "text_hash": sha256(passage.encode("utf-8")).hexdigest(),
        "content_hash": "0" * 64,
        "source_uri": f"source://sec/{accession}/{document}",
    }


def fake_sec_document(
    accession_no: str,
    document_name: str | None = None,
    as_of: str | None = None,
    *,
    offset: int = 0,
    max_chars: int | None = None,
    data_root: object = None,
    section: str | None = None,
    query: str | None = None,
    cursor: object = None,
    limit: object = None,
    raw: bool = False,
) -> dict[str, object]:
    """The archive reload: the registered window, sliced to the requested coordinates."""
    window = _DOC_WINDOWS.get((accession_no, document_name or ""))
    if window is None:
        raise ValueError(f"test seam: no registered document for {(accession_no, document_name)!r}")
    end = len(window) if max_chars is None else min(offset + max_chars, len(window))
    return {
        "accession_no": accession_no,
        "document_name": document_name,
        "text": window[offset:end],
        "content_hash": "0" * 64,
        "source_uri": f"source://sec/{accession_no}/{document_name}",
    }


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the fake archive and route evidence admission reloads to it."""
    _DOC_WINDOWS.clear()
    monkeypatch.setattr("app.sec.documents.get_sec_document", fake_sec_document)
