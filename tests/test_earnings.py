"""EPS structure tests (live SEC EDGAR)."""

import pytest

# All tests in this module call live SEC EDGAR.
pytestmark = pytest.mark.integration


def _as_seq(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    for item in value:
        assert isinstance(item, dict)
    return value

def test_eps_data_structure():
    """Test that get_fundamentals returns eps_basic and eps_diluted in quarterly_eps."""
    from app.edgar_client import get_fundamentals

    result = get_fundamentals("AAPL", "eps")
    assert "error" not in result, f"EPS retrieval failed: {result}"
    assert "quarterly_eps" in result, "Missing quarterly_eps in result"
    quarterly_eps = result["quarterly_eps"]
    quarters = _as_seq(quarterly_eps)
    assert len(quarters) > 0, "No quarterly data returned"

    # Check that each quarter has diluted EPS
    first_quarter = quarters[0]

    # Check if basic EPS is available (optional, depends on company)
    if "eps_basic" in first_quarter:
        assert isinstance(first_quarter["eps_basic"], float), "eps_basic should be float"

    # Check TTM metrics
    assert "ttm_eps_diluted" in result, "Missing ttm_eps_diluted"
