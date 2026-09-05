"""Phase-10 tool cutover: bounded SEC inventory + search envelopes.

Offline: app.sec seams are monkeypatched at the tools.sec boundary; no
network, no edgar import.
"""

from types import SimpleNamespace

import pytest

from app import tools
from app.policy import Capability, RequestContext
from app.sec.models import (
    EntityCandidate,
    SECSearchRequest,
    SECSearchResult,
    SECTextHit,
    SearchAttempt,
    SearchCoverage,
)

SEC_SUITE = [
    "find_sec_entities",
    "search_sec_filings",
    "search_sec_relationships",
    "get_sec_search_coverage",
    "list_sec_filings",
    "get_sec_filing",
    "list_sec_documents",
    "get_sec_document",
    "diff_sec_filings",
    "get_material_events",
    "get_beneficial_ownership",
    "get_ownership_changes",
    "get_insider_activity",
    "get_planned_insider_sales",
    "get_offering_history",
    "get_dilution_profile",
    "get_governance_events",
    "get_transaction_status",
    "get_short_pressure_profile",
    "search_tools",
]


def _research_context():
    return RequestContext("research", frozenset({Capability.RESEARCH}))


def _result(**over):
    base = dict(
        search_id="s1",
        request=SECSearchRequest(query="Acme Labs"),
        coverage=SearchCoverage(
            status="partial", sources_attempted=("entity", "efts"),
            sources_completed=("entity",), sources_failed=(),
            results_reported=3, results_retrieved=2, pages=2,
            pending_backfill_jobs=("job-1",),
        ),
        attempts=(SearchAttempt(
            attempt_id="s1-entity-1", search_id="s1", backend="entity",
            query="Acme Labs", status="complete", results_reported=1,
            results_retrieved=1, pages_retrieved=1, pit_basis="known_at",
        ),),
        warnings=("1 partition queued",), errors=(),
        retrieval_order=("entity", "efts"),
        evidence_packet_ids=("entity:1234567",),
    )
    base.update(over)
    return SECSearchResult(**base)


def test_exact_inventory_registered():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    assert set(SEC_SUITE) <= names


def test_every_new_tool_has_handler_capability_domain_envelope():
    from app.security.action_policy import TOOL_DOMAINS
    from app.security.context_gateway import TOOL_ENVELOPES

    names = {entry["function"]["name"] for entry in tools.TOOLS}
    for name in SEC_SUITE:
        assert name in names
        assert name in tools._DIRECT_HANDLERS
        assert tools.TOOL_CAPABILITIES[name] is Capability.RESEARCH
        assert TOOL_DOMAINS[name] == "financial_research"
        assert name in TOOL_ENVELOPES


def test_get_filing_section_retired():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    assert "get_filing_section" not in names
    assert "get_filing_section" not in tools._DIRECT_HANDLERS
    assert "get_filing_section" not in tools.TOOL_CAPABILITIES
    result = tools.execute_tool("get_filing_section", {}, "test", context=_research_context())
    assert "error" in result


def test_find_sec_company_retired_no_alias():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    assert "find_sec_company" not in names
    assert "find_sec_company" not in tools._DIRECT_HANDLERS
    assert "find_sec_company" not in tools.TOOL_CAPABILITIES
    result = tools.execute_tool("find_sec_company", {"query": "Acme"}, "test", context=_research_context())
    assert "error" in result


def test_get_institutional_ownership_retired():
    names = {entry["function"]["name"] for entry in tools.TOOLS}
    assert "get_institutional_ownership" not in names
    assert "get_institutional_ownership" not in tools._DIRECT_HANDLERS
    assert "get_institutional_ownership" not in tools.TOOL_CAPABILITIES
    result = tools.execute_tool("get_institutional_ownership", {"ticker": "FAKE"}, "test", context=_research_context())
    assert "error" in result


def test_search_tools_insider_sale_returns_two_schemas_only():
    result = tools.execute_tool(
        "search_tools", {"query": "insider sale"}, "test", context=_research_context()
    )
    found = {schema["function"]["name"] for schema in result["schemas"]}
    assert found == {"get_insider_activity", "get_planned_insider_sales"}


def test_search_tools_domain_browse_returns_ownership_pack():
    result = tools.execute_tool(
        "search_tools", {"domain": "ownership"}, "test", context=_research_context()
    )
    found = {schema["function"]["name"] for schema in result["schemas"]}
    assert found == {"search_sec_relationships", "get_beneficial_ownership", "get_ownership_changes"}


def test_list_sec_filings_dispatch_wraps_records(monkeypatch):
    fake = SimpleNamespace(to_dict=lambda: {"accession_no": "0000000001-26-000001"})
    monkeypatch.setattr(tools.sec, "list_sec_filings", lambda *a, **k: [fake])
    result = tools.execute_tool(
        "list_sec_filings", {"identifier": "FAKE"}, "test", context=_research_context()
    )
    assert result["count"] == 1
    assert result["filings"][0]["accession_no"] == "0000000001-26-000001"
    assert result["source"] == "SEC EDGAR"


def test_list_sec_filings_rejects_old_ticker_key():
    result = tools.execute_tool(
        "list_sec_filings", {"ticker": "FAKE"}, "test", context=_research_context()
    )
    assert result["error_type"] == "invalid_tool_arguments"


def test_find_sec_entities_dispatch_carries_verification(monkeypatch):
    result_obj = _result(entities=(EntityCandidate(
        cik=1234567, name="Acme Labs Inc", tickers=(), exchange=None,
        match_source="cik-lookup", match_score=1.0, match_type="exact_name",
        verification_status="verified", entity_id="sec:cik:1234567",
    ),))
    monkeypatch.setattr(tools.sec, "find_sec_entities", lambda *a, **k: result_obj)
    result = tools.execute_tool(
        "find_sec_entities", {"query": "Acme Labs"}, "test", context=_research_context()
    )
    assert result["search_id"] == "s1"
    assert result["entities"][0]["verification_status"] == "verified"
    assert result["coverage"]["status"] == "partial"
    assert result["backfill_jobs"] == ["job-1"]
    assert list(result["evidence_packet_ids"]) == ["entity:1234567"]
    assert result["pit_basis"] == "known_at"
    assert result["attempts"][0]["backend"] == "entity"
    assert result["source"] == "SEC EDGAR"


def test_search_sec_filings_dispatch_full_packet(monkeypatch):
    result_obj = _result(text_hits=(SECTextHit(
        search_id="s1", attempt_id="s1-efts-1", query="Acme Labs",
        accession_no="0000000001-26-000001", form="D",
        filed_at="2026-01-01", filer_cik=1234567,
        filer_name="Acme Labs Inc", matched_document="primary.htm",
        file_type="D", score=5.5,
    ),))

    class _FakeService:
        seen = None

        def search(self, request):
            _FakeService.seen = request
            return result_obj

    monkeypatch.setattr(tools.sec, "SECDiscoveryService", _FakeService)
    result = tools.execute_tool(
        "search_sec_filings", {"query": "Acme Labs", "forms": ["D", "D/A"]},
        "test", context=_research_context(),
    )
    assert _FakeService.seen.query == "Acme Labs"
    assert list(_FakeService.seen.forms) == ["D", "D/A"]
    assert result["count"] == 1
    hit = result["hits"][0]
    assert hit["filer_cik"] == 1234567
    assert hit["match_role"] == "mention"
    assert hit["matched_document"] == "primary.htm"
    assert hit["accession_no"] == "0000000001-26-000001"
    assert result["coverage"]["status"] == "partial"
    assert result["backfill_jobs"] == ["job-1"]
    assert result["counts"]["results_reported"] == 3
    assert result["counts"]["pages"] == 2
    assert list(result["evidence_packet_ids"]) == ["entity:1234567"]
    assert list(result["warnings"]) == ["1 partition queued", "payload truncated to 20 context rows; coverage reports full retrieval"]
    assert result["source"] == "SEC EDGAR"


def test_search_sec_filings_accepts_person_domain_security(monkeypatch):
    class _FakeService:
        seen = None

        def search(self, request):
            _FakeService.seen = request
            return _result()

    monkeypatch.setattr(tools.sec, "SECDiscoveryService", _FakeService)
    result = tools.execute_tool(
        "search_sec_filings",
        {"person_name": "Jane Doe", "domain": "example.com",
         "security_identifier": "123456789"},
        "test", context=_research_context(),
    )
    assert _FakeService.seen.person_name == "Jane Doe"
    assert _FakeService.seen.domain == "example.com"
    assert _FakeService.seen.security_identifier == "123456789"
    assert result["search_id"] == "s1"


def test_search_sec_filings_rejects_empty_selectors():
    result = tools.execute_tool("search_sec_filings", {}, "test", context=_research_context())
    assert "error" in result


def test_search_sec_relationships_dispatch_groups(monkeypatch):
    payload = {
        "entity": "1234567", "ciks": ("1234567",),
        "groups": {"beneficial_owner": {"verified": [{"accession": "ACC-1"}]}},
        "typed": [{"relationship_type": "beneficial_owner", "status": "verified",
                   "accession": "ACC-1"}],
        "relationships": [], "mentions": [{"relationship_type": "mention"}],
        "attempts": [{"backend": "local-typed", "status": "complete"}],
        "warnings": [], "errors": [],
    }
    seen = {}
    def _fake(*a, **k):
        seen.update(k)
        seen["args"] = a
        return payload
    monkeypatch.setattr(tools.sec, "search_sec_relationships", _fake)
    result = tools.execute_tool(
        "search_sec_relationships", {"entity": "1234567"},
        "test", context=_research_context(),
    )
    assert result["ciks"] == ["1234567"]
    assert result["groups"]["beneficial_owner"]["verified"][0]["accession"] == "ACC-1"
    assert result["parties"][0]["relationship_type"] == "beneficial_owner"
    assert result["coverage"]["status"] == "complete"
    assert result["counts"] == {"typed": 1, "workflow": 0, "mentions": 1}
    assert result["attempts"][0]["backend"] == "local-typed"
    assert result["source"] == "SEC EDGAR"
    assert seen["limit"] == 50
    assert seen["exhaustive"] is False
    seen.clear()
    tools.execute_tool(
        "search_sec_relationships", {"entity": "1234567", "exhaustive": True},
        "test", context=_research_context(),
    )
    assert seen["exhaustive"] is False

def test_search_sec_relationships_partial_on_errors(monkeypatch):
    payload = {"entity": "X", "ciks": (), "groups": {}, "typed": [],
               "relationships": [], "mentions": [],
               "attempts": [{"backend": "local-typed", "status": "failed"}],
               "warnings": [], "errors": ["local-typed failed: boom"]}
    monkeypatch.setattr(tools.sec, "search_sec_relationships", lambda *a, **k: payload)
    result = tools.execute_tool(
        "search_sec_relationships", {"entity": "X"},
        "test", context=_research_context(),
    )
    assert result["coverage"]["status"] == "failed"
    assert result["errors"] == ["local-typed failed: boom"]


def test_get_sec_search_coverage_reads_persisted_only(monkeypatch):
    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return {"source": kwargs.get("source"), "form": kwargs.get("form"),
                "search_id": None, "search": None,
                "coverage": [{"form": "10-K", "status": "complete"}],
                "jobs": [{"id": "job-1", "status": "queued"}],
                "errors": [], "provenance": "persisted-ledgers-only"}

    monkeypatch.setattr(tools.sec, "get_sec_search_coverage", _fake)
    result = tools.execute_tool(
        "get_sec_search_coverage", {"source": "sec-global", "form": "10-K"},
        "test", context=_research_context(),
    )
    assert seen == {"source": "sec-global", "form": "10-K",
                    "search_id": None, "limit": 200}
    assert result["coverage"] == [{"form": "10-K", "status": "complete"}]
    assert result["jobs"] == [{"id": "job-1", "status": "queued"}]
    assert result["provenance"] == "persisted-ledgers-only"


def test_discovery_blank_and_bad_limit_surface_errors(monkeypatch):
    missing = tools.execute_tool("find_sec_entities", {}, "test", context=_research_context())
    assert missing["error_type"] == "invalid_tool_arguments"
    monkeypatch.setattr(
        tools.sec, "find_sec_entities",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid query")),
    )
    blanked = tools.execute_tool(
        "find_sec_entities", {"query": "  "}, "test", context=_research_context()
    )
    assert "error" in blanked
    missing_rel = tools.execute_tool("search_sec_relationships", {}, "test", context=_research_context())
    assert missing_rel["error_type"] == "invalid_tool_arguments"


def test_search_tools_discovery_queries_and_domain_order():
    found = tools.execute_tool(
        "search_tools", {"query": "private issuer CIK"}, "test", context=_research_context()
    )
    assert "find_sec_entities" in {s["function"]["name"] for s in found["schemas"]}
    assert "find_sec_company" not in {s["function"]["name"] for s in found["schemas"]}
    fts = tools.execute_tool(
        "search_tools", {"query": "founder filing full text"}, "test", context=_research_context()
    )
    assert {s["function"]["name"] for s in fts["schemas"]} == {"search_sec_filings"}
    pack = tools.execute_tool(
        "search_tools", {"domain": "filings"}, "test", context=_research_context()
    )
    names = [s["function"]["name"] for s in pack["schemas"]]
    assert names[:3] == ["find_sec_entities", "search_sec_filings", "get_sec_search_coverage"]
    listed = next(s for s in pack["schemas"] if s["function"]["name"] == "list_sec_filings")
    assert "identifier" in listed["function"]["parameters"]["properties"]
    assert "Does NOT search company names" in listed["function"]["description"]
    rel = tools.execute_tool(
        "search_tools", {"query": "inverse 13F manager holdings"}, "test", context=_research_context()
    )
    assert "search_sec_relationships" in {s["function"]["name"] for s in rel["schemas"]}


def test_get_sec_document_dispatch_passes_through(monkeypatch):
    monkeypatch.setattr(
        tools.sec, "get_sec_document",
        lambda acc, name=None, as_of=None: {"accession_no": acc, "text": "hi"}
    )
    result = tools.execute_tool(
        "get_sec_document", {"accession_no": "0000000001-26-000001"},
        "test", context=_research_context(),
    )
    assert result["text"] == "hi"


def test_missing_required_argument_is_tool_argument_error():
    result = tools.execute_tool(
        "get_sec_document", {}, "test", context=_research_context()
    )
    assert result["error_type"] == "invalid_tool_arguments"


def test_get_material_events_dispatch_carries_accession_citations(monkeypatch):
    fake = SimpleNamespace(to_dict=lambda: {
        "event_id": "0000000001-26-000001:1.03",
        "event_type": "bankruptcy",
        "known_at": "2026-01-15",
        "source_accessions": ["0000000001-26-000001"],
    })
    monkeypatch.setattr(tools.sec, "get_material_events", lambda *a, **k: [fake])
    result = tools.execute_tool(
        "get_material_events", {"ticker": "FAKE", "since": "2026-01-01"},
        "test", context=_research_context(),
    )
    assert result["count"] == 1
    assert result["events"][0]["source_accessions"] == ["0000000001-26-000001"]


def test_research_projection_includes_suite_excludes_broker():
    names = {
        entry["function"]["name"]
        for entry in tools.tools_for_capabilities(frozenset({Capability.RESEARCH}))
    }
    assert set(SEC_SUITE) <= names
    assert "get_portfolio_snapshot" not in names


def test_get_governance_events_dispatch_wraps_structured(monkeypatch):
    fake = SimpleNamespace(to_dict=lambda: {"event_id": "ACC:gov", "contested": True})
    monkeypatch.setattr(tools.sec, "get_governance_events", lambda *a, **k: [fake])
    result = tools.execute_tool(
        "get_governance_events", {"ticker": "FAKE"},
        "test", context=_research_context(),
    )
    assert result["count"] == 1
    assert result["events"][0]["contested"] is True


def test_get_transaction_status_dispatch_wraps_structured(monkeypatch):
    fake = SimpleNamespace(to_dict=lambda: {"event_id": "FAKE:merger:ACC", "status": "unknown"})
    monkeypatch.setattr(tools.sec, "get_transaction_status", lambda *a, **k: [fake])
    result = tools.execute_tool(
        "get_transaction_status", {"ticker": "FAKE"},
        "test", context=_research_context(),
    )
    assert result["count"] == 1
    assert result["transactions"][0]["status"] == "unknown"
