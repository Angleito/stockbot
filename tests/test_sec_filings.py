"""Offline tests for app/sec/ (no network; edgar faked via monkeypatch)."""

from pathlib import Path
from typing import NoReturn

import pytest

import app.sec.documents as documents
import app.sec.filings as filings
from app.sec.models import EntityCandidate
from types import SimpleNamespace


class _FakeAttachment:
    def __init__(self, document: str, description: str = "desc", size: int = 10,
                 url: str = "https://x/y", document_type: str = "10-K",
                 text: str = "body") -> None:
        self.document = document
        self.description = description
        self.size = size
        self.url = url
        self.document_type = document_type
        self.content = text


class _FakeFiling:
    def __init__(self, form: str = "10-K", filed: str = "2024-01-15",
                 accepted: str | None = None, accession: str = "0001",
                 attachments: list[_FakeAttachment] | None = None) -> None:
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
    seen: dict[str, object] | None = None

    def __init__(self, filings: list[_FakeFiling]) -> None:
        self._filings = filings

    def get_filings(self, **kwargs: object) -> list[_FakeFiling]:
        _FakeCompany.seen = kwargs
        return self._filings


def _patch_company(monkeypatch: pytest.MonkeyPatch, fake_filings: list[_FakeFiling]) -> None:
    def _get_company(ticker_or_cik: str) -> _FakeCompany:
        return _FakeCompany(fake_filings)

    monkeypatch.setattr(filings, "get_company", _get_company)

def _stub_find_sec_entities(query: str, **kwargs: object) -> SimpleNamespace:
    """Verified single-candidate entity packet for discovery tests."""
    return SimpleNamespace(
        entities=(EntityCandidate(cik=123, name="Acme", tickers=(),
                                  exchange=None, match_source="exact-cik",
                                  match_score=1.0, match_type="exact_cik",
                                  verification_status="verified",
                                  entity_id="sec:cik:0000000123"),),
        filings=(), documents=(), relationships=(), text_hits=(),
        coverage=SimpleNamespace(status="complete", source_limits=()),
        attempts=(), warnings=(), errors=(),
        retrieval_order=(), evidence_packet_ids=())



def test_arbitrary_form_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_company(monkeypatch, [])
    filings.list_sec_filings("AAPL", forms="13F-HR")
    assert _FakeCompany.seen is not None and _FakeCompany.seen.get("form") == "13F-HR"


def test_as_of_excludes_later_known_at(monkeypatch: pytest.MonkeyPatch) -> None:
    old = _FakeFiling(filed="2024-01-10", accession="old")
    new = _FakeFiling(filed="2024-06-10", accession="new")
    _patch_company(monkeypatch, [new, old])
    out = filings.list_sec_filings("AAPL", as_of="2024-03-01")
    assert [f.accession_no for f in out] == ["old"]


def test_missing_acceptance_falls_back_to_filed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_company(monkeypatch, [_FakeFiling(accepted=None, filed="2024-01-15")])
    (filing,) = filings.list_sec_filings("AAPL")
    assert filing.accepted_at is None
    assert filing.known_at == filing.filed_at == "2024-01-15"
    assert filing.accepted_at_missing is True


def test_get_sec_filing_amendment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeFiling(form="10-K/A", filed="2024-02-01",
                       accepted="2024-02-01 10:00:00", accession="0002")
    def _fake_get(accession_no: str) -> _FakeFiling:
        return fake

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)
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


def test_get_sec_filing_invalid_accession(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(acc: str) -> NoReturn:
        raise RuntimeError("not found")

    monkeypatch.setattr(documents, "get_by_accession_number", boom)
    with pytest.raises(ValueError):
        filings.get_sec_filing("nope")


def test_documents_list_get_text_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    atts = [_FakeAttachment("primary.htm", text="hello"),
            _FakeAttachment("ex-99.htm", text="exhibit")]
    fake = _FakeFiling(accession="0003", attachments=atts)
    def _fake_get(accession_no: str) -> _FakeFiling:
        return fake

    monkeypatch.setattr(documents, "get_by_accession_number", _fake_get)

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


def test_find_sec_company_normalizes_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.entity.search as company_search

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)
    frame = pd.DataFrame([
        {"cik": "1234567", "ticker": "", "company": "Acme Labs Inc", "score": 99},
        {"cik": "not-a-cik", "ticker": "X", "company": "Skip Me", "score": 50},
        {"cik": 320193, "ticker": "AAPL", "company": "AAPL Inc", "score": 90},
    ])
    def _find_company(query: str, top_n: int = 10) -> SimpleNamespace:
        return SimpleNamespace(results=frame, empty=False)

    monkeypatch.setattr(company_search, "find_company", _find_company)
    out = client.find_sec_company("Acme Labs", limit=2)
    assert out == [
        {"name": "Acme Labs Inc", "cik": 1234567, "tickers": [], "exchange": None},
        {"name": "AAPL Inc", "cik": 320193, "tickers": ["AAPL"], "exchange": None},
    ]


def test_search_sec_filings_normalizes_cik_accession(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)

    class _Hit:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    def _search(query: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(results=[
            _Hit(accession_number="0000000001-26-000001", form="D",
                 filed="2026-01-01", company="Acme Labs Inc",
                 cik="1234567", period=None, score=5.5),
            _Hit(accession_number="0000000002-26-000001", form="D/A",
                 filed="2026-02-01", company=None,
                 cik="bad", period="2025-12-31", score=3.0),
        ])

    monkeypatch.setattr(efts, "search_filings", _search)
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


def test_efts_page_two_failure_is_partial_with_page_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)

    class _Hit:
        def __init__(self, **kwargs: object) -> None:
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
    def _search_page1(query: str, **k: object) -> SimpleNamespace:
        return page1

    monkeypatch.setattr(efts, "search_filings", _search_page1)
    result = client.search_sec_filings("Acme Labs", limit=10)
    assert result.coverage.status == "partial"
    assert [h.accession_no for h in result.text_hits] == ["0000000001-26-000001"]
    assert result.coverage.results_reported == 3
    assert result.coverage.results_retrieved == 1
    assert any(a.status == "failed" for a in result.attempts)
    assert result.errors and "page 2" in result.errors[0]


def test_efts_preserves_matched_document_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)

    class _Hit:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    def _search(query: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(total=1, results=[
            _Hit(accession_number="0000000001-26-000001", form="10-K",
                 filed="2026-01-01", company="Acme Labs Inc", cik="1234567",
                 period=None, score=5.0, document_id="acme-10k.htm",
                 file_type="10-K", file_description="ANNUAL REPORT",
                 items=["1A", "7"], sic="3571", location="CA",
                 state="CA", inc_state="DE"),
        ])

    monkeypatch.setattr(efts, "search_filings", _search)
    (hit,) = client.search_sec_filings("Acme Labs").text_hits
    assert hit.matched_document == "acme-10k.htm"
    assert hit.file_type == "10-K"
    assert hit.file_description == "ANNUAL REPORT"
    assert "1A" in hit.items
    assert hit.sic == "3571"
    assert hit.filer_name == "Acme Labs Inc"  # mention query never rewrites filer
    assert hit.page == 1


def test_efts_dedups_repeated_accession_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)

    class _Hit:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    def _hit():
        return _Hit(accession_number="0000000001-26-000001", form="10-K",
                    filed="2026-01-01", company="Acme Labs Inc",
                    cik="1234567", period=None, score=5.0)

    page2 = SimpleNamespace(total=2, results=[_hit()])
    page1 = SimpleNamespace(total=2, results=[_hit()], next=lambda: page2)
    def _search_page1(query: str, **k: object) -> SimpleNamespace:
        return page1

    monkeypatch.setattr(efts, "search_filings", _search_page1)
    result = client.search_sec_filings("Acme Labs", limit=10)
    assert len(result.text_hits) == 1  # same query/accession/document deduped
    assert result.coverage.results_reported == 2
    assert result.coverage.pages == 2
    assert len(result.attempts) == 2  # every producing attempt retained


def test_efts_as_of_excludes_future_filed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import app.sec.client as client
    import edgar.search.efts as efts

    def _ensure() -> None:
        return None

    monkeypatch.setattr(client, "ensure_identity", _ensure)

    class _Hit:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    def _search(query: str, **k: object) -> SimpleNamespace:
        return SimpleNamespace(total=1, results=[
            _Hit(accession_number="0000000009-26-000001", form="10-K",
                 filed="2026-09-01", company="Acme Labs Inc",
                 cik="1234567", period=None, score=5.0),
        ])

    monkeypatch.setattr(efts, "search_filings", _search)
    result = client.search_sec_filings("Acme Labs", as_of="2026-01-01")
    assert result.text_hits == ()
    assert any("as_of" in w for w in result.warnings)


def test_discovery_current_feed_page_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import Filing, SECSearchRequest
    import app.sec.client as client

    seen: dict[str, int] = {}

    def _filing(accession: str) -> Filing:
        return Filing(accession_no=accession, form="8-K", filer_cik=1234567,
                      filer_name="Acme Inc", filed_at="2026-08-01",
                      accepted_at="2026-08-01T10:00:00Z",
                      known_at="2026-08-01T10:00:00Z",
                      report_period=None, primary_document="primary.htm",
                      is_amendment=False, amendment_of=None,
                      source=f"https://sec/{accession}")

    def _feed(form: str, page_size: int = 40, owner: str = "include") -> list[Filing]:
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


def test_discovery_filer_submissions_probe_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import Filing, SECSearchRequest
    import app.sec.filings as _filings

    seen: dict[str, int | None] = {}

    def _fake_list(cik: int | str, forms: list[str] | None = None, start_date: str | None = None, end_date: str | None = None, as_of: str | None = None, limit: int | None = 50) -> list[Filing]:
        seen["limit"] = limit
        return [Filing(accession_no=f"ACC-{i:03d}", form="10-K", filer_cik=int(str(cik)),
                       filer_name="Acme", filed_at="2024-01-15", accepted_at=None,
                       known_at="2024-01-15T00:00:00Z", report_period=None,
                       primary_document="p.htm", is_amendment=False, amendment_of=None,
                       source="http://x") for i in range(25)]

    monkeypatch.setattr(_filings, "list_sec_filings", _fake_list)
    monkeypatch.setattr("app.sec.discovery.service.find_sec_entities", _stub_find_sec_entities)
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(query="123", forms=("10-K",), max_results=20,
                                        search_relationships=False))
    assert seen["limit"] is not None and seen["limit"] <= 21
    assert len(result.filings) <= 20
    sub = [a for a in result.attempts if a.backend == "filer-submissions"]
    assert sub and all(a.status == "partial" for a in sub)
    assert all(a.truncated and a.source_limit for a in sub)
    assert result.coverage.status == "partial"


def test_bounded_entity_discovery_probes_limit_plus_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.discovery.service as _svc
    seen: dict[str, int] = {}
    rows: list[dict[str, int | str | list[str]]] = [{"cik": 1000000 + i, "name": f"Test Co {i}", "tickers": []} for i in range(25)]

    def _lookup(query: str, limit: int = 50) -> list[dict[str, int | str | list[str]]]:
        seen["cik_lookup_limit"] = limit
        return list(rows)

    def _company(query: str, limit: int = 50) -> list[dict[str, object]]:
        seen["company_limit"] = limit
        return []

    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", _lookup)
    monkeypatch.setattr("app.sec.client.find_sec_company", _company)
    def _metadata(cik: int | str) -> dict[str, object]:
        return {"cik": cik, "name": "Test Co", "tickers": [],
                "exchanges": [], "sic": None, "former_names": []}

    monkeypatch.setattr("app.sec.client.get_submissions_metadata", _metadata)
    out = _svc.find_sec_entities("Test Co", max_results=20, data_root=tmp_path)
    assert seen["cik_lookup_limit"] <= 21
    assert seen["company_limit"] <= 21
    assert len(out.entities) <= 20
    assert out.coverage.status == "partial"
    assert any(a.status == "partial" and a.truncated and a.source_limit for a in out.attempts)
    assert "results capped at 20; rerun with a higher limit or exhaustive=true" in list(out.warnings)
    assert out.request.exhaustive is False
    assert out.request.max_results == 20


def test_entity_writes_land_only_in_explicit_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.discovery.service as _svc

    explicit = tmp_path / "explicit"
    other = tmp_path / "other"
    explicit.mkdir()
    other.mkdir()
    def _resolve(query: str) -> int:
        return 1234567

    def _no_candidates(query: str, limit: int = 50) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr("app.sec.client.resolve_cik", _resolve)
    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", _no_candidates)
    monkeypatch.setattr("app.sec.client.find_sec_company", _no_candidates)
    def _metadata(cik: int | str) -> dict[str, object]:
        return {"cik": 1234567, "name": "Acme Inc", "tickers": ["ACME"],
                "exchanges": ["Nasdaq"], "sic": "1234", "former_names": []}

    monkeypatch.setattr("app.sec.client.get_submissions_metadata", _metadata)
    out = _svc.find_sec_entities("ACME", max_results=20, data_root=explicit)
    assert [e for e in out.entities if e.verification_status == "verified"]
    assert (explicit / "parquet").exists()
    assert not (other / "parquet").exists()


def test_exhaustive_filer_and_current_pass_none_and_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.discovery.service import SECDiscoveryService
    from app.sec.models import Filing, SECSearchRequest
    import app.sec.filings as _filings
    import app.sec.client as client
    seen: dict[str, int | None] = {}
    def _fake_list(cik: int | str, forms: list[str] | None = None, start_date: str | None = None, end_date: str | None = None, as_of: str | None = None, limit: int | None = 50) -> list[Filing]:
        seen["limit"] = limit
        return [Filing(accession_no=f"ACC-{i:03d}", form="10-K", filer_cik=int(str(cik)),
                       filer_name="Acme", filed_at="2024-01-15", accepted_at=None,
                       known_at="2024-01-15T00:00:00Z", report_period=None,
                       primary_document="p.htm", is_amendment=False, amendment_of=None,
                       source="http://x") for i in range(75)]
    def _fake_current(form: str, page_size: int = 40, owner: str = "include") -> list[Filing]:
        seen["page_size"] = page_size
        return [Filing(accession_no=f"CUR-{i:03d}", form=form, filer_cik=123,
                       filer_name="Acme", filed_at="2026-08-01",
                       accepted_at="2026-08-01T10:00:00Z", known_at="2026-08-01T10:00:00Z",
                       report_period=None, primary_document="p.htm",
                       is_amendment=False, amendment_of=None, source="http://x")
                for i in range(75)]
    monkeypatch.setattr(_filings, "list_sec_filings", _fake_list)
    monkeypatch.setattr(client, "get_current_filings", _fake_current)
    monkeypatch.setattr("app.sec.discovery.service.find_sec_entities", _stub_find_sec_entities)
    svc = SECDiscoveryService(data_root=tmp_path)
    result = svc.search(SECSearchRequest(query="123", forms=("10-K",), exhaustive=True,
                                        max_results=None, search_relationships=False))
    assert seen["limit"] is None and seen["page_size"] is None
    assert len(result.filings) >= 75
    cur = [a for a in result.attempts if a.backend == "current-filings"]
    assert cur and all(a.status == "complete" for a in cur)


def test_entity_51_row_probe_marks_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sec.discovery.service as _svc
    rows: list[dict[str, int | str | list[str]]] = [{"cik": 1000000 + i, "name": f"Test Co {i}", "tickers": []} for i in range(51)]

    def _lookup(query: str, limit: int = 50) -> list[dict[str, int | str | list[str]]]:
        return list(rows)

    def _no_company(query: str, limit: int = 50) -> list[dict[str, object]]:
        return []

    def _metadata(cik: int | str) -> dict[str, object]:
        return {"cik": cik, "name": "Test Co", "tickers": [],
                "exchanges": [], "sic": None, "former_names": []}

    monkeypatch.setattr("app.sec.client.get_cik_lookup_candidates", _lookup)
    monkeypatch.setattr("app.sec.client.find_sec_company", _no_company)
    monkeypatch.setattr("app.sec.client.get_submissions_metadata", _metadata)
    out = _svc.find_sec_entities("Test Co", exhaustive=True, max_results=None)
    assert out.coverage.status == "partial"
    assert any(a.backend == "cik-lookup" and a.status == "partial" and a.truncated
               and a.source_limit == "50 candidates" for a in out.attempts)


def test_list_sec_filings_stops_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    sgml_calls: list[int] = []

    class _Bomb(_FakeFiling):
        def sgml(self) -> NoReturn:
            sgml_calls.append(1)
            raise RuntimeError("unbounded sgml download")

    # Each normalization calls sgml exactly once (accepted=None falls
    # through to the sgml fallback, swallowed by _best), so sgml_calls
    # counts normalizations. Pre-fix normalizes all 100; post-fix 10.
    _patch_company(monkeypatch, [_Bomb(filed="2024-01-15", accession=f"{i:04d}") for i in range(100)])
    out = filings.list_sec_filings("AAPL", limit=10)
    assert [f.accession_no for f in out] == [f"{i:04d}" for i in range(10)]
    assert len(sgml_calls) <= 10


def test_list_sec_filings_as_of_keeps_lazy_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.sec.models import pit_of

    sgml_calls: list[int] = []

    class _Bomb(_FakeFiling):
        def sgml(self) -> NoReturn:
            sgml_calls.append(1)
            raise RuntimeError("unbounded sgml download")

    fakes: list[_FakeFiling] = [
        _Bomb(
            filed="2024-01-10" if i % 2 == 0 else "2024-06-10",
            accession=f"{i:04d}",
        )
        for i in range(100)
    ]
    as_of = "2024-03-01"
    _patch_company(monkeypatch, fakes)
    # Unlimited call normalizes everything: the old normalize-all-then-
    # filter-then-slice semantics. The limited call must match its prefix.
    full = filings.list_sec_filings("AAPL", as_of=as_of, limit=None)
    assert [f.accession_no for f in full] == [f"{i:04d}" for i in range(0, 100, 2)]
    expected = full[:10]
    raw_tenth = next(i for i, f in enumerate(fakes) if f.accession_no == expected[9].accession_no)
    bound = raw_tenth + 1
    sgml_calls.clear()
    out = filings.list_sec_filings("AAPL", as_of=as_of, limit=10)
    assert out == expected
    for x in out:
        value, _basis = pit_of(x)
        assert value is not None and value[:10] <= as_of
    assert len(sgml_calls) <= bound
