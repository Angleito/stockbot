"""Deterministic search_tools ranking for recent-move catalyst questions + guards."""

from app.tools import _search_tools


def _names(query: str, limit: int = 4) -> list[str]:
    result = _search_tools({"query": query}, "test")
    matches = result.get("matches")
    assert isinstance(matches, list)
    names = [m["name"] for m in matches if isinstance(m, dict) and isinstance(m.get("name"), str)]
    return names[:limit]


def test_gpro_catalyst_top_two() -> None:
    assert set(_names("why did GPRO shoot up the past 30 days?", 2)) == {"search_web", "get_material_events"}


def test_nvidia_fall_catalyst_top_two() -> None:
    assert set(_names("why has Nvidia fallen this week?", 2)) == {"search_web", "get_material_events"}


def test_post_earnings_rally_top_two() -> None:
    top2 = _names("what caused Apple's stock to rally after earnings?", 2)
    assert "search_web" in top2 and "get_material_events" in top2


def test_guards_preserved() -> None:
    assert _names("what is NVDA diluted EPS?", 1) == ["get_fundamentals"]
    assert _names("is AAPL expensive?", 1) == ["get_valuation_metrics"]
    assert _names("who owns more than 5% of XYZ?", 1) == ["get_beneficial_ownership"]
    assert _names("did insiders sell NVDA?", 1) == ["get_insider_activity"]
    assert _names("what changed in AMD's recent 8-Ks?", 1) == ["get_material_events"]
    assert _names("show me Apple's latest 10-K", 1) == ["list_sec_filings"]
