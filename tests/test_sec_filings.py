"""Offline tests for app/sec/ (no network; edgar faked via monkeypatch)."""

import pytest

import app.sec.documents as documents
import app.sec.filings as filings


class _FakeAttachment:
    def __init__(self, document, description="desc", size=10, url="https://x/y",
                 document_type="10-K", text="body"):
        self.document = document
        self.description = description
        self.size = size
        self.url = url
        self.document_type = document_type
        self.content = text


class _FakeFiling:
    def __init__(self, form="10-K", filed="2024-01-15", accepted=None,
                 accession="0001", attachments=None):
        self.cik = 123
        self.company = "Fake Corp"
        self.form = form
        self.filing_date = filed
        self.acceptance_datetime = accepted
        self.accession_no = accession
        self.homepage_url = f"https://sec/{accession}"
        self.period_of_report = "2023-12-31"
        self._attachments = attachments or [_FakeAttachment("primary.htm")]

    @property
    def document(self):
        return self._attachments[0]

    @property
    def attachments(self):
        return self._attachments


class _FakeCompany:
    seen = None

    def __init__(self, filings):
        self._filings = filings

    def get_filings(self, **kwargs):
        _FakeCompany.seen = kwargs
        return self._filings


def _patch_company(monkeypatch, fake_filings):
    monkeypatch.setattr(
        filings, "get_company", lambda ticker_or_cik: _FakeCompany(fake_filings)
    )


def test_arbitrary_form_passes_through(monkeypatch):
    _patch_company(monkeypatch, [])
    filings.list_sec_filings("AAPL", forms="13F-HR")
    assert _FakeCompany.seen.get("form") == "13F-HR"


def test_as_of_excludes_later_known_at(monkeypatch):
    old = _FakeFiling(filed="2024-01-10", accession="old")
    new = _FakeFiling(filed="2024-06-10", accession="new")
    _patch_company(monkeypatch, [new, old])
    out = filings.list_sec_filings("AAPL", as_of="2024-03-01")
    assert [f.accession_no for f in out] == ["old"]


def test_missing_acceptance_falls_back_to_filed_at(monkeypatch):
    _patch_company(monkeypatch, [_FakeFiling(accepted=None, filed="2024-01-15")])
    (filing,) = filings.list_sec_filings("AAPL")
    assert filing.accepted_at is None
    assert filing.known_at == filing.filed_at == "2024-01-15"
    assert filing.accepted_at_missing is True


def test_get_sec_filing_amendment(monkeypatch):
    fake = _FakeFiling(form="10-K/A", filed="2024-02-01",
                       accepted="2024-02-01 10:00:00", accession="0002")
    monkeypatch.setattr(documents, "get_by_accession_number", lambda acc: fake)
    filing = filings.get_sec_filing("0002")
    assert filing.accession_no == "0002"
    assert filing.form == "10-K/A"
    assert filing.is_amendment is True
    assert filing.amendment_of is None
    assert filing.cik == filing.issuer_cik == 123
    assert filing.report_period == "2023-12-31"
    assert filing.primary_document == "primary.htm"
    assert filing.source == "https://sec/0002"
    assert filing.to_dict()["company"] == "Fake Corp"


def test_get_sec_filing_invalid_accession(monkeypatch):
    def boom(acc):
        raise RuntimeError("not found")

    monkeypatch.setattr(documents, "get_by_accession_number", boom)
    with pytest.raises(ValueError):
        filings.get_sec_filing("nope")


def test_documents_list_get_text_primary(monkeypatch):
    atts = [_FakeAttachment("primary.htm", text="hello"),
            _FakeAttachment("ex-99.htm", text="exhibit")]
    fake = _FakeFiling(accession="0003", attachments=atts)
    monkeypatch.setattr(documents, "get_by_accession_number", lambda acc: fake)

    listed = documents.list_sec_documents("0003")
    assert [d.document for d in listed] == ["primary.htm", "ex-99.htm"]
    assert listed[0].to_dict()["accession_no"] == "0003"

    doc = documents.get_sec_document("0003")
    assert doc["document"] == "primary.htm"
    assert doc["text"] == "hello"

    assert documents.get_sec_filing_text("0003") == "hello"
    assert documents.get_sec_document("0003", "ex-99.htm")["text"] == "exhibit"
    with pytest.raises(ValueError):
        documents.get_sec_document("0003", "missing.htm")
