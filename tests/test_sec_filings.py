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

    seen = {}

    def _filing(accession):
        return Filing(accession_no=accession, form="8-K", filer_cik=1234567,
                      filer_name="Acme Inc", filed_at="2026-08-01",
                      accepted_at="2026-08-01T10:00:00Z",
                      known_at="2026-08-01T10:00:00Z",
                      report_period=None, primary_document="primary.htm",
                      is_amendment=False, amendment_of=None,
                      source=f"https://sec/{accession}")

    def _feed(form, page_size=40, owner="include"):
        seen["page_size"] = page_size
        return [_filing(f"a{i:03d}") for i in range(25)]

    monkeypatch.setattr(client, "get_current_filings", _feed)
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(forms=("8-K",), max_results=20,
                                        search_entities=False,
                                        search_relationships=False))
    assert seen["page_size"] is not None and seen["page_size"] <= 21
    assert len(result.filings) <= 20
    current = [a for a in result.attempts if a.backend == "current-filings"]
    assert current and all(a.status == "partial" for a in current)
    assert all(a.truncated and a.source_limit for a in current)
    assert result.coverage.status == "partial"
    assert "results capped at 20; rerun with a higher limit or exhaustive=true" in list(result.warnings)


def test_discovery_filer_submissions_probe_is_bounded(tmp_path, monkeypatch):
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import EntityCandidate, Filing, SECSearchRequest
    import app.sec.filings as _filings

    seen = {}

    def _fake_list(cik, forms=None, start_date=None, end_date=None, as_of=None, limit=50):
        seen["limit"] = limit
        return [Filing(accession_no=f"ACC-{i:03d}", form="10-K", filer_cik=int(str(cik)),
                       filer_name="Acme", filed_at="2024-01-15", accepted_at=None,
                       known_at="2024-01-15T00:00:00Z", report_period=None,
                       primary_document="p.htm", is_amendment=False, amendment_of=None,
                       source="http://x") for i in range(25)]

    monkeypatch.setattr(_filings, "list_sec_filings", _fake_list)
    monkeypatch.setattr("app.sec.discovery.service.find_sec_entities",
                        lambda q, **k: __import__("types").SimpleNamespace(
                            entities=(EntityCandidate(cik=123, name="Acme", tickers=(),
                                                      exchange=None, match_source="exact-cik",
                                                      match_score=1.0, match_type="exact_cik",
                                                      verification_status="verified",
                                                      entity_id="sec:cik:0000000123"),),
                            filings=(), documents=(), relationships=(), text_hits=(),
                            coverage=__import__("types").SimpleNamespace(status="complete",
                                                                          source_limits=()),
                            attempts=(), warnings=(), errors=(),
                            retrieval_order=(), evidence_packet_ids=()))
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(query="123", forms=("10-K",), max_results=20,
                                        search_relationships=False))
    assert seen["limit"] is not None and seen["limit"] <= 21
    assert len(result.filings) <= 20
    sub = [a for a in result.attempts if a.backend == "filer-submissions"]
    assert sub and all(a.status == "partial" for a in sub)
    assert all(a.truncated and a.source_limit for a in sub)
    assert result.coverage.status == "partial"


def test_bounded_entity_discovery_probes_limit_plus_one(tmp_path, monkeypatch):
    import app.sec.discovery.service as _svc
    seen = {}
    rows = [{"cik": 1000000 + i, "name": f"Test Co {i}", "tickers": []} for i in range(25)]

    def _lookup(query, limit=50):
        seen["cik_lookup_limit"] = limit
        return list(rows)

    def _company(query, limit=50):
        seen["company_limit"] = limit
        return []

    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", _lookup)
    monkeypatch.setattr("app.sec.client.find_sec_company", _company)
    monkeypatch.setattr("app.sec.client.get_submissions_metadata",
                        lambda cik: {"cik": cik, "name": "Test Co", "tickers": [],
                                     "exchanges": [], "sic": None, "former_names": []})
    out = _svc.find_sec_entities("Test Co", max_results=20, data_root=tmp_path)
    assert seen["cik_lookup_limit"] <= 21
    assert seen["company_limit"] <= 21
    assert len(out.entities) <= 20
    assert out.coverage.status == "partial"
    assert any(a.status == "partial" and a.truncated and a.source_limit for a in out.attempts)
    assert "results capped at 20; rerun with a higher limit or exhaustive=true" in list(out.warnings)
    assert out.request.exhaustive is False
    assert out.request.max_results == 20


def test_entity_writes_land_only_in_explicit_root(tmp_path, monkeypatch):
    import app.sec.discovery.service as _svc

    explicit = tmp_path / "explicit"
    other = tmp_path / "other"
    explicit.mkdir()
    other.mkdir()
    monkeypatch.setattr("app.sec.client.resolve_cik", lambda q: 1234567)
    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", lambda q, limit=50: [])
    monkeypatch.setattr("app.sec.client.find_sec_company", lambda q, limit=50: [])
    monkeypatch.setattr("app.sec.client.get_submissions_metadata",
                        lambda cik: {"cik": 1234567, "name": "Acme Inc", "tickers": ["ACME"],
                                     "exchanges": ["Nasdaq"], "sic": "1234", "former_names": []})
    out = _svc.find_sec_entities("ACME", max_results=20, data_root=explicit)
    assert [e for e in out.entities if e.verification_status == "verified"]
    assert (explicit / "parquet").exists()
    assert not (other / "parquet").exists()


def test_exhaustive_filer_and_current_pass_none_and_complete(tmp_path, monkeypatch):
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import EntityCandidate, Filing, SECSearchRequest
    import app.sec.filings as _filings
    import app.sec.client as client
    seen = {}
    def _fake_list(cik, forms=None, start_date=None, end_date=None, as_of=None, limit=50):
        seen["limit"] = limit
        return [Filing(accession_no=f"ACC-{i:03d}", form="10-K", filer_cik=int(str(cik)),
                       filer_name="Acme", filed_at="2024-01-15", accepted_at=None,
                       known_at="2024-01-15T00:00:00Z", report_period=None,
                       primary_document="p.htm", is_amendment=False, amendment_of=None,
                       source="http://x") for i in range(75)]
    def _fake_current(form, page_size=40, owner="include"):
        seen["page_size"] = page_size
        return [Filing(accession_no=f"CUR-{i:03d}", form=form, filer_cik=123,
                       filer_name="Acme", filed_at="2026-08-01",
                       accepted_at="2026-08-01T10:00:00Z", known_at="2026-08-01T10:00:00Z",
                       report_period=None, primary_document="p.htm",
                       is_amendment=False, amendment_of=None, source="http://x")
                for i in range(75)]
    monkeypatch.setattr(_filings, "list_sec_filings", _fake_list)
    monkeypatch.setattr(client, "get_current_filings", _fake_current)
    monkeypatch.setattr("app.sec.discovery.service.find_sec_entities",
                        lambda q, **k: __import__("types").SimpleNamespace(
                            entities=(EntityCandidate(cik=123, name="Acme", tickers=(),
                                                      exchange=None, match_source="exact-cik",
                                                      match_score=1.0, match_type="exact_cik",
                                                      verification_status="verified",
                                                      entity_id="sec:cik:0000000123"),),
                            filings=(), documents=(), relationships=(), text_hits=(),
                            coverage=__import__("types").SimpleNamespace(status="complete",
                                                                          source_limits=()),
                            attempts=(), warnings=(), errors=(),
                            retrieval_order=(), evidence_packet_ids=()))
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(query="123", forms=("10-K",), exhaustive=True,
                                        max_results=None, search_relationships=False))
    assert seen["limit"] is None and seen["page_size"] is None
    assert len(result.filings) >= 75
    cur = [a for a in result.attempts if a.backend == "current-filings"]
    assert cur and all(a.status == "complete" for a in cur)


def test_entity_51_row_probe_marks_partial(monkeypatch):
    import app.sec.discovery.service as _svc
    rows = [{"cik": 1000000 + i, "name": f"Test Co {i}", "tickers": []} for i in range(51)]
    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", lambda q, limit=50: list(rows))
    monkeypatch.setattr("app.sec.client.find_sec_company", lambda q, limit=50: [])
    monkeypatch.setattr("app.sec.client.get_submissions_metadata",
                        lambda cik: {"cik": cik, "name": f"Test Co", "tickers": [],
                                     "exchanges": [], "sic": None, "former_names": []})
    out = _svc.find_sec_entities("Test Co", exhaustive=True, max_results=None)
    assert out.coverage.status == "partial"
    assert any(a.backend == "cik-lookup" and a.status == "partial" and a.truncated
               and a.source_limit == "50 candidates" for a in out.attempts)
