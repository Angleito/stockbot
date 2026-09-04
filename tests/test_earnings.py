"""EPS structure tests (live SEC EDGAR)."""

import pytest

# All tests in this module call live SEC EDGAR.
pytestmark = pytest.mark.integration


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
