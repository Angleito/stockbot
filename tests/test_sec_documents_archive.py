"""Archive-first bounded SEC documents (offline; edgar faked via monkeypatch)."""

import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

import app.sec.documents as documents
from app import tools
from app.policy import Capability, RequestContext

ACC = "0000000000-26-000001"
DOC = "primary.htm"
BIG = "0123456789abcdef" * 3125
assert len(BIG) == 50_000

PDF_BYTES = b"%PDF-1.4\nfake pdf bytes\n%%EOF\n"
PDF_TEXT = "PAGE\n%PDF-1.4\nfake pdf bytes\n%%EOF"


def _fake_pdftotext(tmp_path: Path, *, exit_code: int = 0) -> Callable[[str], str]:
    """Stand-in ``shutil.which``: an extractor that emits NUL/BEL/FF noise, then the file it was handed."""
    script = tmp_path / "pdftotext"
    script.write_text(f'#!/bin/sh\nprintf "PAGE\\000\\007\\f\\n"\n[ {exit_code} -eq 0 ] && cat "$3"\n')
    script.chmod(0o755)

    def _which(name: str) -> str:
        del name
        return str(script)

    return _which


def _no_pdftotext(name: str) -> None:
    """Stand-in ``shutil.which`` reporting no extractor installed."""
    del name


class _FakeAttachment:
    def __init__(self, text: str) -> None:
        self.document = DOC
        self.description = "10-K primary"
        self.url = f"https://sec/{ACC}/{DOC}"
        self.content = text


class _BytesAttachment:
    document = DOC
    description: str | None = None
    url = "https://sec/bytes"

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def download(self) -> bytes:
        return self._payload


class _FakeFiling:
    def __init__(self, text: str | _BytesAttachment, accession: str = ACC) -> None:
        self.form = "10-K"
        self.filing_date = "2026-01-15"
        self.acceptance_datetime: str | None = None
        self.accession_no = accession
        self.company = "Fake Corp"
        self.cik = 123
        self.homepage_url = f"https://sec/{accession}"
        self.period_of_report = "2025-12-31"
        self._att = _FakeAttachment(text) if isinstance(text, str) else text

    @property
    def document(self) -> _FakeAttachment | _BytesAttachment:
        return self._att

    @property
    def attachments(self) -> list[_FakeAttachment | _BytesAttachment]:
        return [self._att]


def _ctx() -> RequestContext:
    return RequestContext("research", frozenset({Capability.RESEARCH}))


def test_archive_first_bounded_windows_and_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCKBOT_DATA_DIR", str(tmp_path))

    def _fake_get(accession_no: str) -> _FakeFiling:
        return _FakeFiling(BIG)

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)

    first = tools.execute_tool(
        "get_sec_document", {"accession_no": ACC}, "test", context=_ctx())
    assert first["text"] == BIG[:12000]
    assert (first["offset"], first["end_offset"], first["total_chars"]) == (0, 12000, 50000)
    assert first["more_available"] is True
    assert first["cache_hit"] is False
    assert first["source_representation"] == "normalized_text"
    digest = hashlib.sha256(BIG.encode("utf-8")).hexdigest()
    assert first["content_hash"] == digest
    assert first["source_content_hash"] == digest
    assert first["raw_archive_path"]
    assert first["known_at"] and first["retrieved_at"]
    assert first["source_url"]

    def _boom(accession_no: str) -> NoReturn:
        raise RuntimeError("network down")

    monkeypatch.setattr(documents, "get_by_accession_number", _boom)
    second = tools.execute_tool(
        "get_sec_document", {"accession_no": ACC, "offset": 12000},
        "test", context=_ctx())
    assert second["text"] == BIG[12000:24000]
    assert (second["offset"], second["end_offset"]) == (12000, 24000)
    assert second["more_available"] is True
    assert second["cache_hit"] is True
    assert second["content_hash"] == first["content_hash"]
    assert second["source_content_hash"] == first["source_content_hash"]
    assert second["known_at"] == first["known_at"]
    assert second["raw_archive_path"] == first["raw_archive_path"]

    assert documents.get_sec_filing_text(ACC) == BIG

    for bad in ({"offset": -1}, {"max_chars": 0}, {"max_chars": 32001},
                {"offset": 60000}):
        rejected = tools.execute_tool(
            "get_sec_document", {"accession_no": ACC, **bad},
            "test", context=_ctx())
        assert rejected["error_type"] == "invalid_tool_arguments"


def test_conflicting_revisions_latest_warns_historical_conflicts(tmp_path: Path) -> None:
    from app.sec import store as _store

    acc = "0000000000-26-000002"
    _store.store_document_text(
        "doc:a", "aaa revision text", accession=acc, document_name=DOC,
        source_url="https://sec/x", known_at="2026-02-01T00:00:00Z",
        retrieved_at="2026-02-01T00:00:00Z", root=tmp_path)
    _store.store_document_text(
        "doc:z", "zzz revision text", accession=acc, document_name=DOC,
        source_url="https://sec/x", known_at="2026-02-01T00:00:00Z",
        retrieved_at="2026-02-01T00:00:00Z", root=tmp_path)

    latest = documents.get_sec_document(acc, data_root=tmp_path)
    hashes = {t: hashlib.sha256(t.encode()).hexdigest()
              for t in ("aaa revision text", "zzz revision text")}

    def _by_hash(revision_text: str) -> str:
        return hashes[revision_text]

    winner = max(hashes, key=_by_hash)
    assert latest["text"] == winner
    assert latest["cache_hit"] is True
    warnings = latest.get("warnings", [])
    assert isinstance(warnings, list)
    assert any(isinstance(w, str) and "conflicting revisions" in w for w in warnings)

    conflict = documents.get_sec_document(
        acc, as_of="2026-03-01", data_root=tmp_path)
    assert conflict["error_type"] == "pit_revision_conflict"


def test_byte_attachment_archives_source_bytes(tmp_path: Path) -> None:
    payload = b"\xff\xfe binary \x00\x01 document bytes"

    acc = "0000000000-26-000003"
    monkeypatch_filing = _FakeFiling(_BytesAttachment(payload), accession=acc)

    def _fake_get(accession_no: str) -> _FakeFiling:
        return monkeypatch_filing

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(documents, "get_by_accession_number", _fake_get)
        out = documents.get_sec_document(acc, data_root=tmp_path)
    assert out["source_content_hash"] == hashlib.sha256(payload).hexdigest()
    assert out["content_hash"] == hashlib.sha256(
        payload.decode("utf-8", "replace").encode("utf-8")).hexdigest()
    assert out["source_content_hash"] != out["content_hash"]
    assert Path(str(out["raw_archive_path"])).read_bytes() == payload
    # a binary payload never leaves as text: NUL-free empty text plus an explicit marker
    assert out["source_representation"] == "binary_unsupported"
    assert out["text"] == ""
    assert out["total_chars"] == 0
    warnings = out["warnings"]
    assert isinstance(warnings, list)
    assert any("has no text representation" in str(w) for w in warnings)


def test_archived_bounded_uses_row_name_and_slice() -> None:
    row: dict[str, object] = {
        "document_name": "row-doc.htm",
        "text": "x" * 100,
        "source_url": "https://sec/row",
        "content_hash": "ch",
        "source_content_hash": "sch",
        "source_representation": "normalized_text",
        "raw_archive_path": "/tmp/raw",
        "filed_at": "2024-01-01",
        "known_at": "2024-01-02",
        "retrieved_at": "2024-01-03",
    }
    out = documents._archived_bounded(
        "0000000000-24-000001", "arg-doc.htm", row, "0123456789", "https://sec/row",
        2, 4, ["w1"])
    assert out["document_name"] == "row-doc.htm"
    assert out["text"] == "2345"
    assert out["cache_hit"] is True and out["cache_type"] == "stockbot_archive"
    assert out["warnings"] == ["w1"]


def test_archived_bounded_falls_back_to_arg_name() -> None:
    out = documents._archived_bounded(
        "0000000000-24-000002", "arg-doc.htm", {}, "", None, 0, None, None)
    assert out["document_name"] == "arg-doc.htm"
    assert "warnings" not in out


def test_pdf_payload_without_pdftotext_is_binary_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = "0000000000-26-000004"
    filing = _FakeFiling(_BytesAttachment(PDF_BYTES), accession=acc)

    def _fake_get(accession_no: str) -> _FakeFiling:
        return filing

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)
    monkeypatch.setattr(shutil, "which", _no_pdftotext)
    out = documents.get_sec_document(acc, data_root=tmp_path)

    assert out["text"] == ""
    assert out["source_representation"] == "binary_unsupported"
    assert (out["offset"], out["end_offset"], out["total_chars"], out["more_available"]) == (0, 0, 0, False)
    assert out["source_content_hash"] == hashlib.sha256(PDF_BYTES).hexdigest()
    assert out["content_hash"] == hashlib.sha256(
        PDF_BYTES.decode("utf-8", "replace").encode("utf-8")).hexdigest()
    assert Path(str(out["raw_archive_path"])).read_bytes() == PDF_BYTES
    warnings = out["warnings"]
    assert isinstance(warnings, list)
    assert any("pdftotext unavailable" in str(w) for w in warnings)


def test_live_pdf_bytes_extract_through_pdftotext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = "0000000000-26-000005"
    filing = _FakeFiling(_BytesAttachment(PDF_BYTES), accession=acc)

    def _fake_get(accession_no: str) -> _FakeFiling:
        return filing

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)
    monkeypatch.setattr(shutil, "which", _fake_pdftotext(tmp_path))
    out = documents.get_sec_document(acc, data_root=tmp_path)

    assert out["source_representation"] == "pdf_extracted"
    # the extractor echoed the temp file it was handed: the exact live payload bytes
    assert out["text"] == PDF_TEXT
    assert out["total_chars"] == len(PDF_TEXT)
    assert out["warnings"] == ["pdf text extracted via pdftotext"]
    assert out["source_content_hash"] == hashlib.sha256(PDF_BYTES).hexdigest()


def test_pdf_extraction_failure_stays_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acc = "0000000000-26-000006"
    filing = _FakeFiling(_BytesAttachment(PDF_BYTES), accession=acc)

    def _fake_get(accession_no: str) -> _FakeFiling:
        return filing

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)
    monkeypatch.setattr(shutil, "which", _fake_pdftotext(tmp_path, exit_code=1))
    out = documents.get_sec_document(acc, data_root=tmp_path)

    assert out["text"] == ""
    assert out["source_representation"] == "binary_unsupported"
    warnings = out["warnings"]
    assert isinstance(warnings, list)
    assert any("pdf text extraction failed" in str(w) for w in warnings)


def test_archived_pdf_row_extracts_text_and_drops_control_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.sec import store as _store

    acc = "0000000000-26-000007"
    payload_path = tmp_path / "form8k2.pdf.json"
    payload_path.write_bytes(PDF_BYTES)
    _store.store_document_text(
        "doc:pdf", PDF_BYTES.decode("utf-8", "replace"), accession=acc,
        document_name="form8k2.pdf", source_url="https://sec/form8k2.pdf",
        raw_archive_path=str(payload_path), source_representation="source_bytes",
        source_content_hash=hashlib.sha256(PDF_BYTES).hexdigest(),
        filed_at="2015-04-29", known_at="2015-04-29",
        retrieved_at="2015-04-29T00:00:00Z", root=tmp_path)
    monkeypatch.setattr(shutil, "which", _fake_pdftotext(tmp_path))

    out = documents.get_sec_document(acc, "form8k2.pdf", data_root=tmp_path)
    assert out["source_representation"] == "pdf_extracted"
    assert out["text"] == PDF_TEXT
    assert not any(char in PDF_TEXT for char in "\x00\x07\x0c")
    assert out["total_chars"] == len(PDF_TEXT)
    assert out["raw_archive_path"] == str(payload_path)
    assert out["warnings"] == ["pdf text extracted via pdftotext"]

    raw_out = documents.get_sec_document(acc, "form8k2.pdf", data_root=tmp_path, raw=True)
    assert raw_out["view"] == "raw"
    assert raw_out["text"] == PDF_TEXT + "\n"
