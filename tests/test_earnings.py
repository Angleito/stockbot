"""Test earnings retrieval across multiple tickers."""

import pytest

from app.agent import run_chat
from app.config import get_default_model
from app.tools import get_earnings_summary


def test_earnings_retrieval_aapl():
    """Verify earnings retrieval works for AAPL and includes source attribution."""
    result = get_earnings_summary("AAPL", get_default_model())
    assert "error" not in result, f"AAPL earnings retrieval failed: {result}"
    assert "source" in result, "Missing source attribution in result"
    assert "text" in result or "summary" in result, "Missing earnings text/summary"
    # Source should indicate whether it's 8-K or 10-Q
    source_lower = result["source"].lower()
    assert "8-k" in source_lower or "10-q" in source_lower, (
        f"Source does not indicate filing type: {result['source']}"
    )


def test_earnings_retrieval_msft():
    """Verify earnings retrieval works for MSFT and includes source attribution."""
    result = get_earnings_summary("MSFT", get_default_model())
    assert "error" not in result, f"MSFT earnings retrieval failed: {result}"
    assert "source" in result, "Missing source attribution in result"
    assert "text" in result or "summary" in result, "Missing earnings text/summary"
    source_lower = result["source"].lower()
    assert "8-k" in source_lower or "10-q" in source_lower, (
        f"Source does not indicate filing type: {result['source']}"
    )


def test_earnings_retrieval_nvda():
    """Verify earnings retrieval works for NVDA (regression: was failing before fallback)."""
    result = get_earnings_summary("NVDA", get_default_model())
    assert "error" not in result, f"NVDA earnings retrieval failed: {result}"
    assert "source" in result, "Missing source attribution in result"
    assert "text" in result or "summary" in result, "Missing earnings text/summary"
    source_lower = result["source"].lower()
    assert "8-k" in source_lower or "10-q" in source_lower, (
        f"Source does not indicate filing type: {result['source']}"
    )


def test_agent_earnings_query_nvda():
    """Integration test: agent should retrieve and summarize NVDA earnings."""
    messages = [
        {"role": "user", "content": "What are NVDA's latest earnings?"}
    ]
    model = get_default_model()
    response, trace = run_chat(messages, model=model, return_trace=True)

    assert response, "Agent returned empty response"
    # Should have called earnings summary tool
    assert "get_earnings_summary" in trace, (
        f"Agent did not call get_earnings_summary. Tools called: {trace}"
    )
    # Response should not be error-like (no hallucination, no fake numbers)
    lower_resp = response.lower()
    assert "error" not in lower_resp, f"Agent response contains error: {response}"


def test_context_awareness_eps():
    """Test that agent maintains context: 'what is AAPL's EPS?' + 'what is undiluted?' works."""
    messages = [
        {"role": "user", "content": "What is AAPL's latest EPS?"}
    ]
    model = get_default_model()
    response1, trace1 = run_chat(messages, model=model, return_trace=True)

    assert response1, "First response was empty"
    assert "get_fundamentals" in trace1, "First query should call get_fundamentals"
    
    # Add response to messages, then ask follow-up about undiluted
    messages.append({"role": "assistant", "content": response1})
    messages.append({"role": "user", "content": "what is undiluted?"})
    
    response2, trace2 = run_chat(messages, model=model, return_trace=True)
    
    assert response2, "Follow-up response was empty"
    # Should recognize AAPL context and explain undiluted EPS
    lower_resp2 = response2.lower()
    assert "aapl" in lower_resp2 or "undiluted" in lower_resp2 or "basic" in lower_resp2, (
        f"Follow-up response did not maintain context: {response2}"
    )


def test_eps_data_structure():
    """Test that get_fundamentals returns eps_basic and eps_diluted in quarterly_eps."""
    from app.edgar_client import get_fundamentals
    
    result = get_fundamentals("AAPL", "eps")
    assert "error" not in result, f"EPS retrieval failed: {result}"
    assert "quarterly_eps" in result, "Missing quarterly_eps in result"
    assert len(result["quarterly_eps"]) > 0, "No quarterly data returned"
    
    # Check that each quarter has diluted EPS
    first_quarter = result["quarterly_eps"][0]
    assert "eps_diluted" in first_quarter, "Missing eps_diluted in quarterly data"
    
    # Check if basic EPS is available (optional, depends on company)
    if "eps_basic" in first_quarter:
        assert isinstance(first_quarter["eps_basic"], float), "eps_basic should be float"
    
    # Check TTM metrics
    assert "ttm_eps_diluted" in result, "Missing ttm_eps_diluted"
