"""Tests for the tool-result rendering layer (app/tool_render.py).

Verifies that rendered tool-result text is compact Markdown within the
byte budget — never raw JSON or internal result structures — via direct
renderer unit tests. Offline and deterministic.
"""

import re
from datetime import datetime, timezone

from app.tool_render import (
    MAX_TOOL_MESSAGE_BYTES,
    TRUNCATED_MARKER,
    render_tool_result,
)


def _briefing_result(total_records: int | None = 12) -> dict:
    return {
        "dataset": "consolidatedShortInterest",
        "group": "otcMarket",
        "dataset_id": "otcMarket/consolidatedShortInterest",
        "source": "FINRA Query API otcMarket/consolidatedShortInterest",
        "query": {
            "ticker": "AAPL",
            "start_date": "2026-03-01",
            "end_date": "2026-08-14",
            "limit": 50,
            "offset": 0,
        },
        "coverage": {
            "rows_matched": 12,
            "rows_analyzed": 12,
            "complete": True,
            "page_complete": True,
            "query_complete": True if total_records else None,
            "analysis_complete": True if total_records else None,
            "cap": None,
            "first_date": "2026-03-01",
            "last_date": "2026-08-14",
        },
        "metrics": {
            "fields": {
                "currentShortPositionQuantity": {
                    "min": 100, "max": 200, "mean": 150, "median": 150, "sum": 1800
                }
            },
            "latest_vs_prior": [
                {
                    "field": "currentShortPositionQuantity",
                    "latest": 12400000,
                    "prior": 10800000,
                    "change": 1600000,
                    "change_percent": 14.81,
                    "latest_date": "2026-08-14",
                    "prior_date": "2026-08-01",
                }
            ],
            "categorical": {"symbolCode": {"AAPL": 12}},
        },
        "trends": [
            "currentShortPositionQuantity: 12400000 vs prior 10800000 "
            "(+1600000, +14.81%) — up"
        ],
        "warnings": [],
        "briefing": {
            "summary": "Short interest rose 14.8%.",
            "key_findings": ["Position up"],
            "caveats": [],
            "follow_up_suggestion": "",
        },
        "briefing_source": "analysis_model",
        "analysis_model": "mock/analysis-model",
        "returned_count": 12,
        "limit": 50,
        "offset": 0,
        "next_offset": 12,
        "may_have_more": False,
        "total_records": total_records,
        "pagination_source": "finra_header" if total_records else "estimate",
    }


def _datapoints_result(n_fields: int = 5, n_rows: int = 5, cell: str = "v") -> dict:
    fields = [f"field{i}" for i in range(n_fields)]
    return {
        "dataset": "consolidatedShortInterest",
        "group": "otcMarket",
        "dataset_id": "otcMarket/consolidatedShortInterest",
        "source": "FINRA Query API otcMarket/consolidatedShortInterest",
        "fields": fields,
        "records": [
            {f: f"{cell}{i}-{j}" for j, f in enumerate(fields)}
            for i in range(n_rows)
        ],
        "returned_count": n_rows,
        "limit": n_rows,
        "offset": 0,
        "next_offset": n_rows,
        "may_have_more": False,
        "total_records": n_rows,
        "pagination_source": "finra_header",
    }



def test_small_result_rendered_without_markers():
    result = {"a": 1, "b": "hello"}
    text = render_tool_result(result)
    assert text == "a: 1\nb: hello"
    assert TRUNCATED_MARKER not in text



def test_web_search_claims_shape_renders():
    result = {
        "result_type": "web_search",
        "query": "AMD news",
        "search_type": "auto",
        "evidence": [
            {
                "claim": "AMD shipped its MI400 accelerator.",
                "source_url": "https://example.com/amd-news",
                "published_at": "2026-08-01T10:00:00.000Z",
                "evidence_summary": "AMD announced its MI400 accelerator.",
            },
            {
                "claim": "AMD guided Q3 revenue above consensus.",
                "source_url": "https://example.com/amd-guidance",
                "published_at": None,
                "evidence_summary": "",
            },
        ],
        "omitted_count": 0,
        "row_count": 2,
        "source": "exa",
        "retrieved_at": "2026-08-02T00:00:00+00:00",
    }
    rendered = render_tool_result(result)
    assert "Claim: AMD shipped its MI400 accelerator." in rendered
    assert "Provenance: https://example.com/amd-news" in rendered
    assert "Published: 2026-08-01T10:00:00.000Z" in rendered
    assert "AMD announced its MI400 accelerator." in rendered
    assert "Claim: AMD guided Q3 revenue above consensus." in rendered
    assert "Provenance: https://example.com/amd-guidance" in rendered
    assert "No evidence" not in rendered


def test_web_search_mixed_claims_and_highlights_render():
    result = {
        "result_type": "web_search",
        "query": "AMD news",
        "search_type": "auto",
        "evidence": [
            {
                "claim": "AMD shipped its MI400 accelerator.",
                "source_url": "https://example.com/amd-news",
                "published_at": None,
                "evidence_summary": "",
            },
            {
                "title": "AMD MI400 Launch",
                "url": "https://example.com/legacy",
                "source_domain": "example.com",
                "published_at": "2026-08-01T10:00:00.000Z",
                "retrieved_at": "2026-08-02T00:00:00+00:00",
                "highlight": "AMD announced its MI400 accelerator.",
                "category": None,
            },
        ],
        "source": "exa",
        "retrieved_at": "2026-08-02T00:00:00+00:00",
    }
    rendered = render_tool_result(result)
    assert "Claim: AMD shipped its MI400 accelerator." in rendered
    assert "AMD MI400 Launch" in rendered
    assert "https://example.com/legacy" in rendered


# ---------------------------------------------------------------------------
# Direct renderer unit tests
# ---------------------------------------------------------------------------


def test_datapoints_table_escapes_pipe_characters():
    result = _datapoints_result(n_fields=1, n_rows=1)
    result["records"][0]["field0"] = "a|b\nc"
    text = render_tool_result(result)
    assert "a\\|b c" in text
    assert "\n\n" not in text


def test_briefing_completeness_statuses_rendered():
    result = _briefing_result(total_records=3)
    text = render_tool_result(result)
    assert "query complete: yes" in text
    assert "analysis complete: yes" in text

    incomplete = _briefing_result(total_records=99)
    incomplete["coverage"]["query_complete"] = False
    incomplete["coverage"]["analysis_complete"] = False
    text = render_tool_result(incomplete)
    assert "query complete: no" in text
    assert "analysis complete: no" in text

    unknown = _briefing_result(total_records=None)
    text = render_tool_result(unknown)
    assert "query complete: unknown" in text
    assert "analysis complete: unknown" in text
    assert "estimate" in text


def test_render_always_fits_budget():
    result = {
        "source": "S",
        "records": [
            {"a": "x" * 1_000_000} for _ in range(100)
        ],
        "fields": ["a"],
    }
    text = render_tool_result(result, max_bytes=2048)
    assert len(text.encode("utf-8")) <= 2048
    assert text  # non-empty


def test_non_dict_results_are_handled():
    text = render_tool_result("just a string")
    assert text == "result: just a string"


# ---------------------------------------------------------------------------
# Portfolio snapshot rendering
# ---------------------------------------------------------------------------


def _portfolio_snapshot_result(n_positions: int = 3, omitted: int = 0) -> dict:
    positions = []
    for i in range(n_positions):
        positions.append({
            "ticker": f"T{i}",
            "quantity": "10",
            "market_price": "100.00",
            "price_type": "last",
            "market_value": "1000.00",
            "portfolio_weight": "0.25",
            "unrealized_gain": "50.00",
            "security_id": f"sec:equity:{i}",
            "entity_id": f"sec:cik:{i}",
            "resolved": True,
            "sec": {
                "Revenue": {"value": "1000000", "period_end": "2026-06-30"},
                "NetIncomeLoss": {"value": "200000", "period_end": "2026-06-30"},
                "CashAndCashEquivalents": {"value": "300000", "period_end": "2026-06-30"},
                "LongTermDebt": {"value": "400000", "period_end": "2026-06-30"},
                "EntityCommonStockSharesOutstanding": {"value": "500000", "period_end": "2026-07-01"},
            },
            "finra": {
                "short_position": "100",
                "prev_position": "90",
                "change": "10",
                "change_pct": "0.1111111111",
                "days_to_cover": "1.5",
                "settlement_date": "2026-08-14",
            },
        })
    positions[0]["resolved"] = False
    return {
        "result_type": "portfolio_snapshot",
        "snapshot_id": "portfolio:robinhood:2026-08-25T12:00:00+00:00",
        "created_at": "2026-08-25T12:00:00+00:00",
        "broker": "robinhood",
        "account_ids": ["acc-1"],
        "total_value": "3234.56",
        "cash": "1234.56",
        "invested_value": "2000.00",
        "position_count": n_positions + omitted,
        "priced_position_count": n_positions - 1,
        "unresolved_position_count": 1,
        "concentration": "0.25",
        "positions": positions,
        "omitted_count": omitted,
        "largest_positions": [{"ticker": "T0", "market_value": "1000.00"}],
        "unresolved": ["T0"],
        "freshness": {
            "snapshot_created_at": "2026-08-25T12:00:00+00:00",
            "as_of": "2026-08-25",
            "sec_latest_filed_at": "2026-08-20",
            "finra_settlement_date": "2026-08-14",
            "finra_known_at": "2026-08-17T12:00:00Z",
        },
    }


def test_portfolio_snapshot_renders_markdown_within_budget():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result)
    assert text
    assert "portfolio" in text.lower()
    assert "T0" in text and "T2" in text
    assert "{" not in text  # no raw JSON reaches the main model
    assert len(text.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES


def test_portfolio_snapshot_reports_unresolved_and_research_freshness():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result)
    assert "[UNRESOLVED]" in text
    assert "Unresolved securities: T0" in text
    assert "SEC latest filing 2026-08-20" in text
    assert "FINRA settlement 2026-08-14" in text
    assert "Source: robinhood_mcp" in text
    expected_local = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).astimezone().isoformat()
    result["created_at_local"] = expected_local
    result["freshness"]["snapshot_created_at_local"] = expected_local
    text = render_tool_result(result)
    assert f"(local {expected_local})" in text


def test_large_portfolio_renders_omitted_count_within_budget():
    result = _portfolio_snapshot_result(n_positions=25, omitted=10)
    text = render_tool_result(result)
    assert len(text.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES
    assert "10 smaller positions omitted" in text
    assert "{" not in text


def test_portfolio_snapshot_truncated_when_budget_tiny():
    result = _portfolio_snapshot_result(n_positions=3)
    text = render_tool_result(result, max_bytes=128)
    assert len(text.encode("utf-8")) <= 128
    assert text  # non-empty

def test_mandate_evaluation_renders_breaches_and_exposures():
    result = {
        "result_type": "mandate_evaluation",
        "snapshot_id": "portfolio:robinhood:2026-08-25T12:00:00+00:00",
        "snapshot_created_at": "2026-08-25T12:00:00+00:00",
        "breaches": [
            {
                "metric": "sector_exposure",
                "target": "semiconductors",
                "severity": "critical",
                "actual": "0.75",
                "limit": "0.20",
                "excess": "0.55",
                "note": None,
            },
            {
                "metric": "prohibited_assets",
                "target": "GME",
                "severity": "warning",
                "actual": "GME",
                "limit": "GME",
                "excess": None,
                "note": "position GME (snap-1:acc-1:GME)",
            },
            {
                "metric": "minimum_cash",
                "target": None,
                "severity": "warning",
                "actual": "500",
                "limit": "1000",
                "excess": "500",
                "note": None,
                "unit": "dollars",
            },
        ],
        "sector_exposures": {"semiconductors": "0.75", "unknown_sector": "0.25"},
        "issues": [
            {
                "code": "position_weight_unavailable",
                "metric": "single_position_weight",
                "target": None,
                "position_id": None,
                "ticker": "ZZZZ",
            }
        ],
        "source": "mandate",
    }
    text = render_tool_result(result)
    assert "Mandate evaluation" in text
    assert "Snapshot created: 2026-08-25T12:00:00+00:00" in text
    expected_local = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc).astimezone().isoformat()
    result["snapshot_created_at_local"] = expected_local
    text = render_tool_result(result)
    assert f"(local {expected_local})" in text
    assert "[critical] sector_exposure semiconductors:" in text
    assert "actual 75.0%, limit 20.0%, excess 55.0%" in text
    # Prohibited-asset values render verbatim, not as percent.
    assert "actual GME, limit GME" in text
    # Dollars-unit breaches render currency, not percent.
    assert "actual $500.00, limit $1,000.00, excess $500.00" in text
    assert "Sector exposures: semiconductors 75.0%, unknown_sector 25.0%" in text
    assert "Not evaluable:" in text
    assert "- single_position_weight: ZZZZ (no weight)" in text
    assert "Source: mandate" in text
    assert "{" not in text
    assert len(text.encode("utf-8")) <= MAX_TOOL_MESSAGE_BYTES


def _valuation_result(**kwargs) -> dict:
    row = {
        "eps": 5.0,
        "pe": 20.0,
        "eps_after_contractual": 4.9,
        "pe_after_contractual": 19.6,
        "eps_after_all_obligations": 4.8,
        "pe_after_all_obligations": 19.2,
        "obligation_drag_per_share": 0.1,
        "contingent_drag_per_share": 0.1,
    }
    result = {
        "ticker": "AAPL",
        "price": {"last": 100.0},
        "as_of": "2026-09-01",
        "source": "test",
        "ttm_gaap_eps": 6.0,
        "trailing_pe": 16.7,
        "obligations": {},
        "forward_eps": {
            k: dict(row)
            for k in (
                "consensus",
                "adjusted",
                "scenario",
                "scenario_with_defaults",
                "worst_case",
                "consensus_next_fy",
                "adjusted_next_fy",
                "scenario_next_fy",
                "scenario_with_defaults_next_fy",
                "worst_case_next_fy",
            )
        },
    }
    result.update(kwargs)
    return result


def test_valuation_forward_eps_labels_follow_fiscal_year_metadata():
    text = render_tool_result(
        _valuation_result(fiscal_year_current="2026", fiscal_year_next="2027")
    )
    assert "Consensus FY2026" in text
    assert "Consensus FY2027" in text
    assert "Adjusted FY2026 (contractual incl.)" in text
    assert "WORST CASE FY2027 (all obligations incl. supply stranded)" in text
    assert "FY26" not in text and "FY27" not in text
    fallback = render_tool_result(_valuation_result())
    assert "Consensus (current FY)" in fallback
    assert "Consensus (next FY)" in fallback
    assert re.search(r"FY\d", fallback) is None


def test_obligations_ledger_dispatch_suppresses_schedule_components():
    """Live-shaped ledger (no `form` key) reaches the obligations renderer;
    headline + FY lines render once, components never standalone."""
    headline = {
        "type": "supply", "amount_billions": 13.3, "certainty": "contingent",
        "status": "future_cash_obligation", "revenue_matched": True,
        "filed": "2026-02-01", "accession": "0001",
        "excerpt": "Supply commitments were $13.3 billion.",
        "payment_horizon": {},
        "schedule": [{"fiscal_year": "2027", "amount_billions": 4.0}],
    }
    component = {
        "type": "supply", "amount_billions": 4.0, "schedule_component": True,
        "headline_type": "supply", "filed": "2026-02-01", "excerpt": "x",
    }
    result = {
        "ticker": "SYN", "filed": "2026-02-01", "source": "SEC EDGAR notes",
        "obligations": [headline, component], "current_snapshot": [headline],
    }
    assert "form" not in result
    text = render_tool_result(result)
    assert "contractual obligations" in text
    assert "- supply: $13.3B" in text
    assert "FY2027: $4.0B" in text
    assert "- supply: $4.0B" not in text
