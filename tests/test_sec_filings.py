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
    assert filing.filer_cik == 123
    assert filing.filer_name == "Fake Corp"
    assert filing.subject_cik == filing.filer_cik == 123
    assert filing.subject_name == "Fake Corp"
    assert filing.report_period == "2023-12-31"
    assert filing.primary_document == "primary.htm"
    assert filing.source == "https://sec/0002"
    assert filing.to_dict()["filer_name"] == "Fake Corp"


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
    assert [d.document_name for d in listed] == ["primary.htm", "ex-99.htm"]
    assert listed[0].to_dict()["accession_no"] == "0003"

    doc = documents.get_sec_document("0003")
    assert doc["document_name"] == "primary.htm"
    assert doc["text"] == "hello"

    assert documents.get_sec_filing_text("0003") == "hello"
    assert documents.get_sec_document("0003", "ex-99.htm")["text"] == "exhibit"
    with pytest.raises(ValueError):
        documents.get_sec_document("0003", "missing.htm")


def test_find_sec_company_normalizes_and_preserves_order(monkeypatch):
    import pandas as pd
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.entity.search as company_search

    monkeypatch.setattr(client, "ensure_identity", lambda: None)
    frame = pd.DataFrame([
        {"cik": "1234567", "ticker": "", "company": "Acme Labs Inc", "score": 99},
        {"cik": "not-a-cik", "ticker": "X", "company": "Skip Me", "score": 50},
        {"cik": 320193, "ticker": "AAPL", "company": "AAPL Inc", "score": 90},
    ])
    monkeypatch.setattr(
        company_search, "find_company",
        lambda query, top_n=10: SimpleNamespace(results=frame, empty=False),
    )
    out = client.find_sec_company("Acme Labs", limit=2)
    assert out == [
        {"name": "Acme Labs Inc", "cik": 1234567, "tickers": [], "exchange": None},
        {"name": "AAPL Inc", "cik": 320193, "tickers": ["AAPL"], "exchange": None},
    ]


def test_search_sec_filings_normalizes_cik_accession(monkeypatch):
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    monkeypatch.setattr(client, "ensure_identity", lambda: None)

    class _Hit:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(
        efts, "search_filings",
        lambda query, **kwargs: SimpleNamespace(results=[
            _Hit(accession_number="0000000001-26-000001", form="D",
                 filed="2026-01-01", company="Acme Labs Inc",
                 cik="1234567", period=None, score=5.5),
            _Hit(accession_number="0000000002-26-000001", form="D/A",
                 filed="2026-02-01", company=None,
                 cik="bad", period="2025-12-31", score=3.0),
        ]),
    )
    result = client.search_sec_filings("Acme Labs", forms=["D", "D/A"], limit=2)
    (first, second) = result.text_hits
    assert first.filer_cik == 1234567
    assert first.filer_name == "Acme Labs Inc"
    assert first.accession_no == "0000000001-26-000001"
    assert first.source_url is None
    assert second.filer_cik is None
    assert second.filer_name is None
    assert second.filed_at == "2026-02-01"
    assert result.coverage.status == "complete"
    assert result.coverage.results_reported == 2


def test_discovery_adapters_reject_blank_and_bad_limit():
    import app.sec.client as client

    with pytest.raises(ValueError):
        client.find_sec_company("   ")
    with pytest.raises(ValueError):
        client.find_sec_company("Acme", limit=0)
    with pytest.raises(ValueError):
        client.search_sec_filings("")
    with pytest.raises(ValueError):
        client.search_sec_filings("Acme", limit=0)


def test_efts_page_two_failure_is_partial_with_page_one(monkeypatch):
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    monkeypatch.setattr(client, "ensure_identity", lambda: None)

    class _Hit:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _boom():
        raise ConnectionError("efts reset")

    page2 = SimpleNamespace(
        total=3,
        results=[_Hit(accession_number="0000000003-26-000001", form="10-K",
                      filed="2026-03-01", company="Acme Labs Inc",
                      cik="1234567", period=None, score=1.0)],
    )
    page1 = SimpleNamespace(
        total=3,
        results=[_Hit(accession_number="0000000001-26-000001", form="10-K",
                      filed="2026-01-01", company="Acme Labs Inc",
                      cik="1234567", period=None, score=5.0)],
        next=_boom,
    )
    monkeypatch.setattr(efts, "search_filings", lambda query, **k: page1)
    result = client.search_sec_filings("Acme Labs", limit=10)
    assert result.coverage.status == "partial"
    assert [h.accession_no for h in result.text_hits] == ["0000000001-26-000001"]
    assert result.coverage.results_reported == 3
    assert result.coverage.results_retrieved == 1
    assert any(a.status == "failed" for a in result.attempts)
    assert result.errors and "page 2" in result.errors[0]


def test_efts_preserves_matched_document_metadata(monkeypatch):
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    monkeypatch.setattr(client, "ensure_identity", lambda: None)

    class _Hit:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(
        efts, "search_filings",
        lambda query, **kwargs: SimpleNamespace(total=1, results=[
            _Hit(accession_number="0000000001-26-000001", form="10-K",
                 filed="2026-01-01", company="Acme Labs Inc", cik="1234567",
                 period=None, score=5.0, document_id="acme-10k.htm",
                 file_type="10-K", file_description="ANNUAL REPORT",
                 items=["1A", "7"], sic="3571", location="CA",
                 state="CA", inc_state="DE"),
        ]),
    )
    (hit,) = client.search_sec_filings("Acme Labs").text_hits
    assert hit.matched_document == "acme-10k.htm"
    assert hit.file_type == "10-K"
    assert hit.file_description == "ANNUAL REPORT"
    assert "1A" in hit.items
    assert hit.sic == "3571"
    assert hit.filer_name == "Acme Labs Inc"  # mention query never rewrites filer
    assert hit.page == 1


def test_efts_dedups_repeated_accession_document(monkeypatch):
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    monkeypatch.setattr(client, "ensure_identity", lambda: None)

    class _Hit:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _hit():
        return _Hit(accession_number="0000000001-26-000001", form="10-K",
                    filed="2026-01-01", company="Acme Labs Inc",
                    cik="1234567", period=None, score=5.0)

    page2 = SimpleNamespace(total=2, results=[_hit()])
    page1 = SimpleNamespace(total=2, results=[_hit()], next=lambda: page2)
    monkeypatch.setattr(efts, "search_filings", lambda query, **k: page1)
    result = client.search_sec_filings("Acme Labs", limit=10)
    assert len(result.text_hits) == 1  # same query/accession/document deduped
    assert result.coverage.results_reported == 2
    assert result.coverage.pages == 2
    assert len(result.attempts) == 2  # every producing attempt retained


def test_efts_as_of_excludes_future_filed_at(monkeypatch):
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    monkeypatch.setattr(client, "ensure_identity", lambda: None)

    class _Hit:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr(
        efts, "search_filings",
        lambda query, **k: SimpleNamespace(total=1, results=[
            _Hit(accession_number="0000000009-26-000001", form="10-K",
                 filed="2026-09-01", company="Acme Labs Inc",
                 cik="1234567", period=None, score=5.0),
        ]),
    )
    result = client.search_sec_filings("Acme Labs", as_of="2026-01-01")
    assert result.text_hits == ()
    assert any("as_of" in w for w in result.warnings)


def test_discovery_current_feed_page_is_partial(tmp_path, monkeypatch):
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import Filing, SECSearchRequest
    import app.sec.client as client

    def _filing(accession):
        return Filing(accession_no=accession, form="8-K", filer_cik=1234567,
                      filer_name="Acme Inc", filed_at="2026-08-01",
                      accepted_at="2026-08-01T10:00:00Z",
                      known_at="2026-08-01T10:00:00Z",
                      report_period=None, primary_document="primary.htm",
                      is_amendment=False, amendment_of=None,
                      source=f"https://sec/{accession}")

    monkeypatch.setattr(client, "get_current_filings",
                        lambda form, page_size=50: [_filing("a1"), _filing("a2")])
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(forms=("8-K",), max_results=2,
                                        search_entities=False,
                                        search_relationships=False))
    current = [a for a in result.attempts if a.backend == "current-filings"]
    assert current and all(a.status == "partial" for a in current)
    assert result.coverage.status == "partial"
