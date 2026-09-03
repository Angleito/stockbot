"""Offline tests for the store-first SEC fact service (app/services/sec_facts.py).

Seed a tmp parquet store through the real normalizers, then exercise
get_fundamentals/get_xbrl_facts envelopes, the NVDA store/live parity
(ttm 6.53), true-filed_at restatement ordering, as_of gating, alias and
dispatch behavior.  Live paths are monkeypatched at the edgar_client seam.
"""

from datetime import date

import pytest

from app.normalization import normalize_sec_company_facts, normalize_sec_tickers
from app.policy import LOCAL_CONTEXT
from app.services import sec_facts
from app.storage import parquet
from app.tools import TOOLS, execute_tool
from app.tool_render import render_tool_result

NVDA_CIK = 1045810
RETRIEVED_AT = "2026-08-01T00:00:00Z"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated data root for every service query."""
    monkeypatch.setattr(sec_facts, "DEFAULT_DATA_ROOT", tmp_path)
    return tmp_path


def _seed_ticker(tmp_path, cik, ticker, retrieved_at=RETRIEVED_AT):
    datasets = normalize_sec_tickers(
        {"0": {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}},
        retrieved_at=retrieved_at, content_hash=f"tickers-{cik}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _seed_facts(tmp_path, cik, diluted=(), basic=(), shares=()):
    payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {}}
    if shares:
        payload["facts"]["dei"] = {
            "EntityCommonStockSharesOutstanding": {"units": {"shares": list(shares)}},
        }
    us_gaap = {}
    if diluted:
        us_gaap["EarningsPerShareDiluted"] = {"units": {"USD/shares": list(diluted)}}
    if basic:
        us_gaap["EarningsPerShareBasic"] = {"units": {"USD/shares": list(basic)}}
    if us_gaap:
        payload["facts"]["us-gaap"] = us_gaap
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash=f"facts-{cik}",
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        source_record_id=f"cik{cik:010d}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _eps_fact(val, start, end, fy, fp, filed, accn):
    return {"start": start, "end": end, "val": val, "accn": accn,
            "fy": fy, "fp": fp, "filed": filed}


def _shares_fact(val, end, filed, accn):
    return {"end": end, "val": val, "accn": accn, "filed": filed}


# NVDA fact set mirroring tests/test_edgar_metrics.py::_EPS_ROWS (the live
# algorithm's fixture): quarterly, YTD, FY-only Q4, restated Q3.
_NVDA_FACTS = [
    _eps_fact(0.76, "2025-01-27", "2025-04-27", 2026, "Q1", "2025-05-28", "a1"),
    _eps_fact(0.77, "2025-01-27", "2025-04-27", 2026, "Q1", "2025-05-28", "a2"),
    _eps_fact(1.84, "2025-01-27", "2025-07-27", 2026, "Q2", "2025-08-27", "a3"),  # 6-month YTD
    _eps_fact(1.08, "2025-04-28", "2025-07-27", 2026, "Q2", "2025-08-27", "a4"),
    _eps_fact(3.14, "2025-01-27", "2025-10-26", 2026, "Q3", "2025-11-19", "a5"),  # 9-month YTD
    _eps_fact(1.30, "2025-07-28", "2025-10-26", 2026, "Q3", "2025-11-19", "a6"),
    _eps_fact(4.90, "2025-01-27", "2026-01-25", 2026, "FY", "2026-02-25", "a7"),   # Q4 only as FY
    _eps_fact(2.39, "2026-01-26", "2026-04-26", 2027, "Q1", "2026-05-27", "a8"),
    _eps_fact(2.40, "2026-01-26", "2026-04-26", 2027, "Q1", "2026-05-27", "a9"),
]
_NVDA_BASIC = (0.77, 2.40)


def _seed_nvda(tmp_path):
    _seed_ticker(tmp_path, NVDA_CIK, "NVDA")
    diluted = [f for f in _NVDA_FACTS if f["val"] not in _NVDA_BASIC]
    basic = [f for f in _NVDA_FACTS if f["val"] in _NVDA_BASIC]
    _seed_facts(tmp_path, NVDA_CIK, diluted=diluted, basic=basic)


def _live_eps(ticker="NVDA"):
    return {
        "ticker": ticker,
        "quarterly_eps": [
            {"fiscal_year": "2026", "fiscal_period": "Q1", "eps_diluted": 0.76,
             "period_end": "2025-04-27"},
        ],
        "ttm_eps_diluted": 0.76,
        "source": "SEC EDGAR company facts (Basic & Diluted EPS)",
    }


def _live_shares(ticker="NVDA"):
    return {
        "ticker": ticker, "shares_outstanding": 999.0, "as_of": "2026-01-01",
        "source": "SEC EDGAR company facts",
        "note": "SEC-reported shares outstanding, not public float",
    }


def test_eps_store_parity_with_live_algorithm(store):
    _seed_nvda(store)
    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2026-08-10")
    assert result["data_source"] == "store"
    assert result["as_of_date"] == "2026-08-10"
    assert "requested_as_of" not in result
    assert result["row_count"] == 4  # tail(4): Q1'26 drops, mirroring live
    by_end = {q["period_end"]: q for q in result["quarterly_eps"]}
    assert set(by_end) == {"2025-07-27", "2025-10-26", "2026-01-25", "2026-04-26"}
    assert by_end["2025-07-27"]["eps_diluted"] == 1.08
    assert by_end["2025-10-26"]["eps_diluted"] == 1.30
    assert by_end["2026-01-25"]["eps_diluted"] == 1.76  # derived: 4.90 - 3.14
    assert by_end["2026-04-26"]["eps_diluted"] == 2.39
    assert by_end["2026-04-26"]["eps_basic"] == 2.40
    assert result["ttm_eps_diluted"] == 6.53  # matches the live fixture exactly
    assert "ttm_eps_basic" not in result
    assert result["source"] == "sec"
    assert result["source_label"] == "SEC EDGAR company facts (Basic & Diluted EPS)"


def test_eps_store_restatement_uses_true_filed_at_not_fiscal_year_proxy(store):
    """The fiscal-year proxy (live _dedup_latest) would pick the fy2027
    version; the store must pick the true latest filed_at (fy2026, 1.30)."""
    _seed_ticker(store, NVDA_CIK, "NVDA")
    diluted = [f for f in _NVDA_FACTS if f["val"] not in _NVDA_BASIC and f["val"] != 1.30]
    diluted.append(_eps_fact(1.35, "2025-07-28", "2025-10-26", 2027, "Q3", "2025-11-19", "a6"))
    diluted.append(_eps_fact(1.30, "2025-07-28", "2025-10-26", 2026, "Q3", "2026-03-10", "a10"))
    basic = [f for f in _NVDA_FACTS if f["val"] in _NVDA_BASIC]
    _seed_facts(store, NVDA_CIK, diluted=diluted, basic=basic)

    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2026-08-10")
    by_end = {q["period_end"]: q for q in result["quarterly_eps"]}
    assert by_end["2025-10-26"]["eps_diluted"] == 1.30  # latest filed_at wins
    assert result["ttm_eps_diluted"] == 6.53


def test_eps_store_as_of_gating_excludes_later_restatement(store):
    """A restatement filed after as_of must not change an earlier as_of."""
    _seed_ticker(store, NVDA_CIK, "NVDA")
    diluted = [
        _eps_fact(1.00, "2026-01-01", "2026-03-31", 2026, "Q1", "2026-05-01", "b1"),
        _eps_fact(1.10, "2026-04-01", "2026-06-30", 2026, "Q2", "2026-08-01", "b2"),
        _eps_fact(1.20, "2026-07-01", "2026-09-30", 2026, "Q3", "2026-11-01", "b3"),
        _eps_fact(1.30, "2026-10-01", "2026-12-31", 2026, "Q4", "2026-12-15", "b4"),
        _eps_fact(1.25, "2026-07-01", "2026-09-30", 2026, "Q3", "2027-01-10", "b5"),
    ]
    _seed_facts(store, NVDA_CIK, diluted=diluted)

    earlier = sec_facts.get_fundamentals("NVDA", "eps", as_of="2026-12-20")
    by_end = {q["period_end"]: q for q in earlier["quarterly_eps"]}
    assert by_end["2026-09-30"]["eps_diluted"] == 1.20
    assert earlier["ttm_eps_diluted"] == 4.60

    later = sec_facts.get_fundamentals("NVDA", "eps", as_of="2027-02-01")
    by_end = {q["period_end"]: q for q in later["quarterly_eps"]}
    assert by_end["2026-09-30"]["eps_diluted"] == 1.25
    assert later["ttm_eps_diluted"] == 4.65


# ---------------------------------------------------------------------------
# Live fallback path
# ---------------------------------------------------------------------------


def test_eps_live_fallback_when_store_empty(store, monkeypatch):
    _seed_ticker(store, NVDA_CIK, "NVDA")  # resolved entity, no facts
    calls = []
    monkeypatch.setattr(
        sec_facts.edgar_client, "get_fundamentals",
        lambda ticker, metric: calls.append((ticker, metric)) or _live_eps(),
    )
    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2025-01-15")
    assert result["data_source"] == "live"
    assert result["as_of_date"] == date.today().isoformat()
    assert result["requested_as_of"] == "2025-01-15"  # echo, never a false claim
    assert result["ttm_eps_diluted"] == 0.76
    assert result["quarterly_eps"][0]["eps_diluted"] == 0.76
    assert result["source"] == "sec"
    assert result["source_label"] == "SEC EDGAR company facts (Basic & Diluted EPS)"
    assert "source" not in result["quarterly_eps"][0]
    assert calls == [("NVDA", "eps")]


def test_eps_omitted_as_of_defaults_to_today_live(store, monkeypatch):
    _seed_ticker(store, NVDA_CIK, "NVDA")
    monkeypatch.setattr(
        sec_facts.edgar_client, "get_fundamentals",
        lambda ticker, metric: _live_eps(ticker),
    )
    result = sec_facts.get_fundamentals("NVDA", "eps")
    assert result["data_source"] == "live"
    assert result["as_of_date"] == date.today().isoformat()
    assert "requested_as_of" not in result


def test_ambiguous_ticker_skips_store_never_guesses(store, monkeypatch):
    _seed_ticker(store, 1, "NVDA")
    _seed_ticker(store, 2, "NVDA")
    _seed_facts(store, 1, diluted=[_eps_fact(1.0, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-05-01", "c1")])
    calls = []
    monkeypatch.setattr(
        sec_facts.edgar_client, "get_fundamentals",
        lambda ticker, metric: calls.append(metric) or _live_eps(),
    )
    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2026-08-10")
    assert result["data_source"] == "live"
    assert calls == ["eps"]


def test_invalid_as_of_returns_tool_argument_error(store, monkeypatch):
    monkeypatch.setattr(
        sec_facts.edgar_client, "get_fundamentals",
        lambda ticker, metric: _live_eps(ticker),
    )
    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2025/01/15")
    assert result["error"] == "as_of must be a date in YYYY-MM-DD format"
    assert result["error_type"] == "invalid_tool_arguments"


# ---------------------------------------------------------------------------
# Shares outstanding store path
# ---------------------------------------------------------------------------


def test_shares_outstanding_store_latest_period_wins(store):
    _seed_ticker(store, NVDA_CIK, "NVDA")
    _seed_facts(store, NVDA_CIK, shares=[
        _shares_fact(1000, "2025-10-26", "2025-11-19", "s1"),
        _shares_fact(1050, "2025-10-26", "2026-03-10", "s2"),  # restated
        _shares_fact(1100, "2026-01-25", "2026-02-25", "s3"),
    ])
    result = sec_facts.get_fundamentals("NVDA", "shares_outstanding", as_of="2026-08-10")
    assert result["data_source"] == "store"
    assert result["shares_outstanding"] == 1100  # newest period_end; filed_at breaks restatement ties
    assert result["as_of"] == "2026-01-25"
    assert result["accession"] == "s3"
    assert result["filed_at"] == "2026-02-25"
    assert result["known_at"] == "2026-02-25"
    assert "not public float" in result["note"]
    assert result["source_label"] == "SEC EDGAR company facts"


def test_shares_float_alias_preserved(store):
    _seed_ticker(store, NVDA_CIK, "NVDA")
    _seed_facts(store, NVDA_CIK, shares=[_shares_fact(1000, "2025-10-26", "2025-11-19", "s1")])
    result = sec_facts.get_fundamentals("NVDA", "shares_float", as_of="2026-08-10")
    assert result["metric"] == "shares_outstanding"
    assert result["shares_outstanding"] == 1000
    assert result["data_source"] == "store"


def test_shares_live_fallback_empty_store(store, monkeypatch):
    _seed_ticker(store, NVDA_CIK, "NVDA")
    monkeypatch.setattr(
        sec_facts.edgar_client, "get_fundamentals",
        lambda ticker, metric: _live_shares(ticker),
    )
    result = sec_facts.get_fundamentals("NVDA", "shares_outstanding", as_of="2026-08-10")
    assert result["data_source"] == "live"
    assert result["shares_outstanding"] == 999.0
    assert result["source_label"] == "SEC EDGAR company facts"


# ---------------------------------------------------------------------------
# Always-live metrics + XBRL envelope
# ---------------------------------------------------------------------------


def test_balance_sheet_and_overview_are_live_with_requested_as_of_echo(store, monkeypatch):
    monkeypatch.setattr(sec_facts.edgar_client, "get_fundamentals", lambda ticker, metric: {
        "ticker": ticker, "balance_sheet": {"totalAssets": 1.0},
        "source": "SEC EDGAR financials",
    } if metric == "balance_sheet" else {
        "ticker": ticker, "name": "NVDA", "cik": "0001045810",
        "industry": "Semiconductors", "source": "SEC EDGAR",
    })
    bs = sec_facts.get_fundamentals("NVDA", "balance_sheet", as_of="2025-01-15")
    assert bs["data_source"] == "live"
    assert bs["balance_sheet"] == {"totalAssets": 1.0}
    assert bs["requested_as_of"] == "2025-01-15"
    assert bs["source_label"] == "SEC EDGAR financials"
    ov = sec_facts.get_fundamentals("NVDA", "overview", as_of="2025-01-15")
    assert ov["data_source"] == "live"
    assert ov["name"] == "NVDA"
    assert ov["source_label"] == "SEC EDGAR"


def test_get_xbrl_facts_always_live_enveloped(store, monkeypatch):
    monkeypatch.setattr(sec_facts.edgar_client, "get_xbrl_facts", lambda ticker, concept: {
        "ticker": ticker, "concept_searched": concept,
        "matching_concepts": [{"concept": "Revenue", "value": 1.0, "period_end": "2026-01-25",
                               "fiscal_period": "FY"}],
        "count": 1, "source": "SEC EDGAR XBRL facts",
    })
    result = sec_facts.get_xbrl_facts("NVDA", "Revenue")
    assert result["source"] == "sec"
    assert result["metric"] == "concept"
    assert result["data_source"] == "live"
    assert result["row_count"] == 1
    assert result["returned_count"] == 1
    assert result["truncated"] is False
    assert result["matching_concepts"][0]["concept"] == "Revenue"
    assert result["source_label"] == "SEC EDGAR XBRL facts"
    assert "source" not in result["matching_concepts"][0]


# ---------------------------------------------------------------------------
# Tool schema + dispatch + render
# ---------------------------------------------------------------------------


def test_tool_schema_and_dispatch(store, monkeypatch):
    schema = next(item for item in TOOLS if item["function"]["name"] == "get_fundamentals")
    properties = schema["function"]["parameters"]["properties"]
    assert properties["as_of"]["type"] == "string"
    assert "store-backed for eps/shares_outstanding" in properties["as_of"]["description"]

    _seed_nvda(store)
    result = execute_tool(
        "get_fundamentals", {"ticker": "NVDA", "metric": "eps", "as_of": "2026-08-10"},
        model="test", context=LOCAL_CONTEXT,
    )
    assert result["source"] == "sec"
    assert result["data_source"] == "store"
    assert result["ttm_eps_diluted"] == 6.53


def test_render_sec_facts_envelope(store):
    _seed_nvda(store)
    result = sec_facts.get_fundamentals("NVDA", "eps", as_of="2026-08-10")
    text = render_tool_result(result)
    assert text.startswith("NVDA eps [store] as of 2026-08-10")
    assert "rows: 4" in text
    assert "ttm_eps_diluted: 6.53" in text
    assert "2026 Q2 (period end 2025-07-27): diluted 1.08" in text
    assert "2027 Q1 (period end 2026-04-26): diluted 2.39 | basic 2.4" in text
    assert "source_label: SEC EDGAR company facts (Basic & Diluted EPS)" in text
