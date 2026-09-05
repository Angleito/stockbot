"""Step-9 registry cutover: exact 16-tool SEC inventory + search_tools discovery.

Offline: app.sec seams are monkeypatched at the tools.sec boundary; no
network, no edgar import.
"""

from types import SimpleNamespace

import pytest

from app import tools
from app.policy import Capability, RequestContext

SEC_SUITE = [
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
        "list_sec_filings", {"ticker": "FAKE"}, "test", context=_research_context()
    )
    assert result["count"] == 1
    assert result["filings"][0]["accession_no"] == "0000000001-26-000001"
    assert result["source"] == "SEC EDGAR"


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
