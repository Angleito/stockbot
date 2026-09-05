"""Step-9 registry cutover: exact 16-tool SEC inventory + search_tools discovery.

Offline: app.sec seams are monkeypatched at the tools.sec boundary; no
network, no edgar import.
"""

from types import SimpleNamespace

import pytest

from app import tools
from app.policy import Capability, RequestContext

SEC_SUITE = [
    "find_sec_company",
    "search_sec_filings",
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
    assert found == {"get_beneficial_ownership", "get_ownership_changes"}


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


def test_find_sec_company_dispatch_permits_empty_tickers(monkeypatch):
    rows = [
        {"name": "Acme Labs Inc", "cik": 1234567, "tickers": [], "exchange": None},
        {"name": "AAPL Inc", "cik": 320193, "tickers": ["AAPL"], "exchange": None},
    ]
    monkeypatch.setattr(tools.sec, "find_sec_company", lambda *a, **k: rows)
    result = tools.execute_tool(
        "find_sec_company", {"query": "Acme Labs"}, "test", context=_research_context()
    )
    assert result["count"] == 2
    assert result["matches"][0]["tickers"] == []
    assert result["matches"][0]["exchange"] is None
    assert result["matches"][1]["tickers"] == ["AAPL"]


def test_search_sec_filings_dispatch_exposes_cik_accession(monkeypatch):
    rows = [{
        "company": "Acme Labs Inc", "cik": 1234567, "form": "D",
        "filed": "2026-01-01", "accession_no": "0000000001-26-000001",
        "source_url": None, "score": 5.5, "period": None,
    }]
    monkeypatch.setattr(tools.sec, "search_sec_filings", lambda *a, **k: rows)
    result = tools.execute_tool(
        "search_sec_filings", {"query": "Acme Labs", "forms": ["D", "D/A"]},
        "test", context=_research_context(),
    )
    assert result["count"] == 1
    assert result["hits"][0]["cik"] == 1234567
    assert result["hits"][0]["accession_no"] == "0000000001-26-000001"


def test_discovery_blank_and_bad_limit_surface_errors(monkeypatch):
    missing = tools.execute_tool("find_sec_company", {}, "test", context=_research_context())
    assert missing["error_type"] == "invalid_tool_arguments"
    monkeypatch.setattr(
        tools.sec, "find_sec_company",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid query")),
    )
    blanked = tools.execute_tool(
        "find_sec_company", {"query": "  "}, "test", context=_research_context()
    )
    assert "error" in blanked
    monkeypatch.setattr(
        tools.sec, "search_sec_filings",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("invalid limit")),
    )
    bad_limit = tools.execute_tool(
        "search_sec_filings", {"query": "Acme", "limit": 0}, "test", context=_research_context()
    )
    assert "error" in bad_limit


def test_search_tools_discovery_queries_and_domain_order():
    found = tools.execute_tool(
        "search_tools", {"query": "private issuer CIK"}, "test", context=_research_context()
    )
    assert "find_sec_company" in {s["function"]["name"] for s in found["schemas"]}
    fts = tools.execute_tool(
        "search_tools", {"query": "founder filing full text"}, "test", context=_research_context()
    )
    assert {s["function"]["name"] for s in fts["schemas"]} == {"search_sec_filings"}
    pack = tools.execute_tool(
        "search_tools", {"domain": "filings"}, "test", context=_research_context()
    )
    names = [s["function"]["name"] for s in pack["schemas"]]
    assert names[:3] == ["find_sec_company", "search_sec_filings", "list_sec_filings"]
    listed = next(s for s in pack["schemas"] if s["function"]["name"] == "list_sec_filings")
    assert "identifier" in listed["function"]["parameters"]["properties"]
    assert "Does NOT search company names" in listed["function"]["description"]


def test_get_sec_document_dispatch_passes_through(monkeypatch):
    monkeypatch.setattr(
        tools.sec, "get_sec_document", lambda acc, name=None: {"accession_no": acc, "text": "hi"}
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
    fake = SimpleNamespace(to_dict=lambda: {"event_id": "FAKE:merger:ACC", "status": "announced"})
    monkeypatch.setattr(tools.sec, "get_transaction_status", lambda *a, **k: [fake])
    result = tools.execute_tool(
        "get_transaction_status", {"ticker": "FAKE"},
        "test", context=_research_context(),
    )
    assert result["count"] == 1
    assert result["transactions"][0]["status"] == "announced"
