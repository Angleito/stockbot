"""Archive-first bounded SEC documents (offline; edgar faked via monkeypatch)."""

import hashlib

from app import tools
from app.policy import Capability, RequestContext
import app.sec.documents as documents

ACC = "0000000000-26-000001"
DOC = "primary.htm"
BIG = "0123456789abcdef" * 3125
assert len(BIG) == 50_000


class _FakeAttachment:
    def __init__(self, text):
        self.document = DOC
        self.description = "10-K primary"
        self.url = f"https://sec/{ACC}/{DOC}"
        self.content = text


class _FakeFiling:
    def __init__(self, text, accession=ACC):
        self.form = "10-K"
        self.filing_date = "2026-01-15"
        self.acceptance_datetime = None
        self.accession_no = accession
        self.company = "Fake Corp"
        self.cik = 123
        self.homepage_url = f"https://sec/{accession}"
        self.period_of_report = "2025-12-31"
        self._att = _FakeAttachment(text) if isinstance(text, str) else text

    @property
    def document(self):
        return self._att

    @property
    def attachments(self):
        return [self._att]


def _ctx():
    return RequestContext("research", frozenset({Capability.RESEARCH}))


def test_archive_first_bounded_windows_and_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        documents, "get_by_accession_number", lambda acc: _FakeFiling(BIG))

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

    def _boom(acc):
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


def test_conflicting_revisions_latest_warns_historical_conflicts(tmp_path):
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
    winner = max(hashes, key=hashes.get)
    assert latest["text"] == winner
    assert latest["cache_hit"] is True
    assert any("conflicting revisions" in w for w in latest.get("warnings", []))

    conflict = documents.get_sec_document(
        acc, as_of="2026-03-01", data_root=tmp_path)
    assert conflict["error_type"] == "pit_revision_conflict"


def test_byte_attachment_archives_source_bytes(tmp_path):
    payload = b"\xff\xfe binary \x00\x01 document bytes"

    class _BytesAttachment:
        document = DOC
        description = None
        url = "https://sec/bytes"

        def download(self):
            return payload

    acc = "0000000000-26-000003"
    monkeypatch_filing = _FakeFiling(_BytesAttachment(), accession=acc)
    import pytest

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(documents, "get_by_accession_number",
                   lambda got: monkeypatch_filing)
        out = documents.get_sec_document(acc, data_root=tmp_path)
    assert out["source_representation"] == "source_bytes"
    assert out["source_content_hash"] == hashlib.sha256(payload).hexdigest()
    assert out["content_hash"] == hashlib.sha256(
        payload.decode("utf-8", "replace").encode("utf-8")).hexdigest()
    assert out["source_content_hash"] != out["content_hash"]
    assert out["text"] == payload.decode("utf-8", "replace")
