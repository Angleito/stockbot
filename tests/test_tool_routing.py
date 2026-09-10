"""Deterministic search_tools ranking over the discovery catalog."""

from app.tools import _search_tools

SHORT_INTEREST_FAMILY = frozenset({
    "query_finra",
    "get_short_interest",
    "get_reg_sho_volume",
    "get_short_pressure_profile",
    "get_short_interest_leaderboard",
    "get_finra_datapoints",
})


def _names(query: str, limit: int = 4) -> list[str]:
    result = _search_tools({"query": query}, "test")
    matches = result.get("matches")
    assert isinstance(matches, list)
    names = [m["name"] for m in matches if isinstance(m, dict) and isinstance(m.get("name"), str)]
    return names[:limit]


def test_eps_routes_to_fundamentals() -> None:
    assert _names("What does Apple earn per share?", 1) == ["get_fundamentals"]
    assert _names("What is Apple's EPS, basic and diluted, including trailing twelve months?", 1) == ["get_fundamentals"]


def test_short_interest_change_routes_to_finra_family() -> None:
    top4 = _names("How has Apple's short interest changed over time?")
    assert top4[0] in SHORT_INTEREST_FAMILY
    assert "query_finra" in top4 or "get_short_interest" in top4


def test_large_owner_routes_to_beneficial_ownership() -> None:
    assert _names("Who owns more than 5% of Apple?", 1) == ["get_beneficial_ownership"]


def test_analyst_expectations_routes_to_analyst_estimates() -> None:
    assert _names("What do analysts expect from Apple going forward?", 1) == ["get_analyst_estimates"]
    assert _names("What are analysts estimating for Apple?", 1) == ["get_analyst_estimates"]


def test_unemployment_routes_to_macro() -> None:
    assert _names("What is the unemployment rate in California?", 1) == ["get_macro_context"]


def test_ambiguous_short_interest_accepts_any_family_member() -> None:
    assert any(n in SHORT_INTEREST_FAMILY for n in _names("What is Apple's current short interest?", 2))


def test_short_interest_move_ranks_finra_above_web() -> None:
    top4 = _names("why did short interest move up?")
    assert "search_web" in top4
    assert min(top4.index(n) for n in top4 if n in SHORT_INTEREST_FAMILY) < top4.index("search_web")


def test_falling_eps_estimates_routes_to_analyst() -> None:
    assert _names("EPS estimates fell for Apple", 1) == ["get_analyst_estimates"]


def test_insider_selling_jump_routes_to_insider() -> None:
    assert _names("insider selling jump at Apple", 1) == ["get_insider_activity"]


def test_unemployment_move_routes_to_macro() -> None:
    assert _names("unemployment moved up last month", 1) == ["get_macro_context"]


def test_stock_price_jump_routes_to_web_or_events() -> None:
    assert _names("why did Apple stock jump today?", 1)[0] in {"search_web", "get_material_events"}
