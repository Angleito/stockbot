"""Past/present/future-declared dividend events (app/services/sec_facts.py).

Isolation reuses the _seed_ticker/parquet.write_rows pattern from
tests/test_dividends.py: every test seeds one ticker plus dividend facts and
dividend_events rows under tmp_path, then queries get_fundamentals as_of a
fixed date with Yahoo stubbed out.
"""

import pytest

import app.valuation as valuation
from app.domain.market import ids
from app.normalization import (
    DIVIDEND_PER_SHARE_CONCEPT,
    normalize_sec_company_facts,
    normalize_sec_tickers,
)
from app.services import sec_facts
from app.storage import parquet

KO_CIK = 21344
RETRIEVED_AT = "2026-08-01T00:00:00Z"
AS_OF = "2026-08-10"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated data root for every service query."""
    monkeypatch.setattr(sec_facts, "DEFAULT_DATA_ROOT", tmp_path)
    return tmp_path


def _seed_ticker(tmp_path, cik, ticker):
    datasets = normalize_sec_tickers(
        {"0": {"cik_str": cik, "ticker": ticker, "title": f"{ticker} Corp"}},
        retrieved_at=RETRIEVED_AT, content_hash=f"tickers-{cik}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _div_fact(val, start, end, fy, fp, filed, accn):
    return {"start": start, "end": end, "val": val, "accn": accn,
            "fy": fy, "fp": fp, "filed": filed}


def _seed_dividends(tmp_path, cik, facts):
    payload = {"cik": cik, "entityName": f"CIK{cik}", "facts": {
        "us-gaap": {DIVIDEND_PER_SHARE_CONCEPT: {"units": {"USD/shares": list(facts)}}},
    }}
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash=f"div-facts-{cik}",
        source_url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        source_record_id=f"cik{cik:010d}",
    )
    for name, rows in datasets.items():
        parquet.write_rows(name, rows, root=tmp_path / "parquet")


def _seed_quarters(tmp_path):
    """Four contiguous quarters (TTM 2.10) so the store path serves events."""
    _seed_ticker(tmp_path, KO_CIK, "KO")
    _seed_dividends(tmp_path, KO_CIK, [
        _div_fact(0.51, "2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-28", "q3"),
        _div_fact(0.51, "2025-10-01", "2025-12-31", 2025, "Q4", "2026-02-10", "q4"),
        _div_fact(0.54, "2026-01-01", "2026-03-31", 2026, "Q1", "2026-04-28", "q1"),
        _div_fact(0.54, "2026-04-01", "2026-06-30", 2026, "Q2", "2026-07-28", "q2"),
    ])


def _event(cik, event_id, amount, *, decl=None, record=None, pay=None,
           known="2026-08-01T00:00:00Z", filed=None, accn="0000123456",
           source_type="structured_xbrl", dtype="regular"):
    return {
        "dividend_event_id": event_id,
        "entity_id": ids.sec_entity_id(cik),
        "security_id": ids.sec_security_id(cik),
        "ticker": "KO",
        "amount_per_share": amount,
        "currency": "USD",
        "dividend_type": dtype,
        "declaration_date": decl,
        "record_date": record,
        "payment_date": pay,
        "ex_dividend_date": None,
        "ex_dividend_date_source": "unknown",
        "status": "unknown",
        "source_form": "10-Q",
        "accession": accn,
        "filed_at": filed or known,
        "known_at": known,
        "source_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}/doc.htm",
        "source_concept": "DividendsPayableAmountPerShare",
        "source_type": source_type,
        "evidence_excerpt": None,
        "content_hash": f"h-{event_id}",
        "parser_version": "sec-companyfacts-v5",
    }


def _seed_events(tmp_path, rows):
    parquet.write_rows("dividend_events", rows, root=tmp_path / "parquet")


def _fail_on_price(monkeypatch):
    def _boom(ticker):
        raise AssertionError("historical dividend query must not call Yahoo")
    monkeypatch.setattr(valuation, "get_live_quote", _boom)


def test_upcoming_vs_paid_split(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_quarters(store)
    _seed_events(store, [
        _event(KO_CIK, "ev-paid", 0.51, decl="2026-06-15", record="2026-06-30",
               pay="2026-07-01", accn="0000000001"),
        _event(KO_CIK, "ev-next", 0.54, decl="2026-08-01", record="2026-08-29",
               pay="2026-09-15", accn="0000000002"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["last_dividend"] == {
        "amount_per_share": 0.51, "payment_date": "2026-07-01", "type": "regular"}
    assert result["next_declared_dividend"] == {
        "amount_per_share": 0.54, "declaration_date": "2026-08-01",
        "record_date": "2026-08-29", "payment_date": "2026-09-15",
        "status": "upcoming",
        "source_url": result["next_declared_dividend"]["source_url"],
        "accession": "0000000002"}
    assert [e["payment_date"] for e in result["past_events"]] == ["2026-09-15", "2026-07-01"]
    assert result["events_coverage"] == "structured_and_text"
    assert result["row_count"] == len(result["annual_history"])


def test_future_declaration_invisible_at_earlier_as_of(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_quarters(store)
    _seed_events(store, [
        _event(KO_CIK, "ev-future", 0.54, decl="2026-08-20", record="2026-09-15",
               pay="2026-10-01", known="2026-09-01T00:00:00Z", accn="0000000003"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of="2026-08-15")
    assert result["next_declared_dividend"] is None
    assert result["last_dividend"] is None
    assert result["past_events"] == []
    assert result["events_coverage"] == "no_structured_events"


def test_duplicate_accessions_dedup_to_one(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_quarters(store)
    event_id = ids.sec_dividend_event_id(KO_CIK, 0.54, "2026-08-29", "2026-09-15", "regular")
    _seed_events(store, [
        _event(KO_CIK, event_id, 0.54, decl="2026-08-01", record="2026-08-29",
               pay="2026-09-15", known="2026-08-01T00:00:00Z", accn="0000000004"),
        _event(KO_CIK, event_id, 0.54, decl="2026-08-01", record="2026-08-29",
               pay="2026-09-15", known="2026-08-02T00:00:00Z", accn="0000000005"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert len(result["past_events"]) == 1
    assert result["next_declared_dividend"]["amount_per_share"] == 0.54


def test_amended_amount_supersedes_via_latest_known_at(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_quarters(store)
    old_id = ids.sec_dividend_event_id(KO_CIK, 0.50, "2026-08-29", "2026-09-15", "regular")
    new_id = ids.sec_dividend_event_id(KO_CIK, 0.54, "2026-08-29", "2026-09-15", "regular")
    assert old_id != new_id
    _seed_events(store, [
        _event(KO_CIK, old_id, 0.50, decl="2026-07-15", record="2026-08-29",
               pay="2026-09-15", known="2026-07-20T00:00:00Z", accn="0000000006"),
        _event(KO_CIK, new_id, 0.54, decl="2026-08-01", record="2026-08-29",
               pay="2026-09-15", known="2026-08-02T00:00:00Z", accn="0000000007"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["next_declared_dividend"]["amount_per_share"] == 0.54
    assert result["next_declared_dividend"]["accession"] == "0000000007"


def test_incomplete_event_excluded_from_last_next(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_quarters(store)
    _seed_events(store, [
        _event(KO_CIK, "ev-full", 0.54, decl="2026-08-01", record="2026-08-29",
               pay="2026-09-15", accn="0000000008"),
        _event(KO_CIK, "ev-partial", 0.54, decl="2026-08-01", record=None,
               pay=None, accn="0000000009"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["next_declared_dividend"]["payment_date"] == "2026-09-15"
    assert result["last_dividend"] is None
    partial = next(e for e in result["past_events"] if e["accession"] == "0000000009")
    assert partial["payment_date"] is None
    assert partial["status"] == "unknown"
    # Null payment sorts last.
    assert result["past_events"][-1]["accession"] == "0000000009"


def test_restated_q1_still_wins_ttm_while_events_classify(store, monkeypatch):
    _fail_on_price(monkeypatch)
    _seed_ticker(store, KO_CIK, "KO")
    _seed_dividends(store, KO_CIK, [
        _div_fact(0.50, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-04-29", "k1"),
        _div_fact(0.51, "2025-01-01", "2025-03-31", 2025, "Q1", "2025-05-06", "k2"),
        _div_fact(0.51, "2025-04-01", "2025-06-30", 2025, "Q2", "2025-07-29", "k4"),
        _div_fact(0.51, "2025-07-01", "2025-09-30", 2025, "Q3", "2025-10-28", "k6"),
        _div_fact(0.51, "2025-10-01", "2025-12-31", 2025, "Q4", "2026-02-10", "k7"),
        _div_fact(0.54, "2026-01-01", "2026-03-31", 2026, "Q1", "2026-04-28", "k8"),
    ])
    _seed_events(store, [
        _event(KO_CIK, "ev-ko-next", 0.53, decl="2026-07-20", record="2026-08-29",
               pay="2026-09-15", accn="0000000010"),
    ])
    result = sec_facts.get_fundamentals("KO", "dividends", as_of=AS_OF)
    assert result["ttm_dividend_per_share"] == 2.07
    assert result["dividend_status"] == "paying"
    assert result["next_declared_dividend"]["amount_per_share"] == 0.53
    assert result["last_dividend"] is None


# --- Phase 4: pure lifecycle analysis (app/services/dividend_analysis.py) ---
# These tests call the pure module directly with plain dicts: no store fixture,
# no network, no Yahoo. Running fixture-free IS the purity proof.

import datetime as _dt

from app.services import dividend_analysis as _lifecycle


def _paid(payment_date, amount, dtype="regular"):
    return {"payment_date": payment_date, "amount_per_share": amount,
            "dividend_type": dtype}


def _series(amounts, start, step_days):
    day = _dt.date.fromisoformat(start)
    return [_paid((day + _dt.timedelta(days=i * step_days)).isoformat(), amount)
            for i, amount in enumerate(amounts)]


def test_lifecycle_quarterly_cadence_high_confidence():
    result = _lifecycle.analyze_dividends(
        paid_events=_series([0.50] * 5, "2025-01-15", 91), as_of="2026-02-01")
    assert result["payment_cadence"] == "quarterly"
    assert result["cadence_confidence"] == "high"
    assert result["cadence_basis"] == "payment_dates"


def test_lifecycle_monthly_cadence_high_confidence():
    result = _lifecycle.analyze_dividends(
        paid_events=_series([0.10] * 5, "2026-01-15", 30), as_of="2026-06-01")
    assert result["payment_cadence"] == "monthly"
    assert result["cadence_confidence"] == "high"

def test_lifecycle_two_intervals_is_medium_confidence():
    result = _lifecycle.analyze_dividends(
        paid_events=_series([0.50] * 3, "2025-01-15", 91), as_of="2025-08-01")
    assert result["payment_cadence"] == "quarterly"
    assert result["cadence_confidence"] == "medium"


def test_lifecycle_sparse_payments_are_unknown_cadence():
    one = _lifecycle.analyze_dividends(paid_events=[_paid("2025-01-15", 0.50)])
    assert one["payment_cadence"] == "unknown"
    assert one["cadence_confidence"] is None
    two = _lifecycle.analyze_dividends(
        paid_events=[_paid("2025-01-15", 0.50), _paid("2025-04-16", 0.50)])
    assert two["payment_cadence"] == "unknown"
    assert two["cadence_confidence"] is None


def test_lifecycle_increase():
    paid = _series([0.50, 0.50, 0.50, 0.54], "2025-01-15", 91)
    result = _lifecycle.analyze_dividends(paid_events=paid, as_of="2025-11-01")
    assert result["increase"] == {"pct": 0.08, "amount": 0.54, "date": paid[-1]["payment_date"]}
    assert result["cut"] is None
    assert result["freeze"] is None


def test_lifecycle_cut():
    paid = _series([0.54, 0.54, 0.54, 0.40], "2025-01-15", 91)
    result = _lifecycle.analyze_dividends(paid_events=paid, as_of="2025-11-01")
    assert result["cut"] == {"pct": round((0.40 - 0.54) / 0.54, 4), "prior": 0.54,
                             "new": 0.40, "date": paid[-1]["payment_date"]}
    assert result["increase"] is None


def test_lifecycle_freeze_quarterly():
    result = _lifecycle.analyze_dividends(
        paid_events=_series([0.54] * 4, "2025-01-15", 91), as_of="2025-11-01")
    assert result["freeze"] == {"amount": 0.54, "count": 4}
    assert result["increase"] is None and result["cut"] is None


def test_lifecycle_freeze_semiannual_needs_two():
    result = _lifecycle.analyze_dividends(
        paid_events=_series([0.80] * 3, "2025-01-15", 182), as_of="2026-07-20")
    assert result["payment_cadence"] == "semiannual"
    assert result["cadence_confidence"] == "medium"
    assert result["freeze"] == {"amount": 0.80, "count": 3}


def test_lifecycle_specials_listed_separately_with_splits():
    paid = _series([0.50] * 4, "2025-01-15", 91)
    paid += [_paid("2025-12-15", 2.00, "special"), _paid("2025-12-15", 0.25, "supplemental")]
    result = _lifecycle.analyze_dividends(paid_events=paid, as_of="2026-01-10")
    assert result["specials"] == [
        {"amount": 2.00, "payment_date": "2025-12-15"},
        {"amount": 0.25, "payment_date": "2025-12-15"},
    ]
    assert result["regular_paid_per_share"] == 2.00
    assert result["special_paid_per_share"] == 2.25
    assert result["total_paid_per_share"] == 4.25
    assert result["increase"] is None and result["cut"] is None


def test_lifecycle_possible_suspension_is_flag_not_status():
    paid = _series([0.50] * 4, "2025-01-15", 91)
    stale = _lifecycle.analyze_dividends(paid_events=paid, as_of="2026-08-10")
    assert stale["possible_suspension"] is True
    assert "dividend_status" not in stale
    fresh = _lifecycle.analyze_dividends(paid_events=paid, as_of="2025-12-01")
    assert fresh["possible_suspension"] is False


def test_lifecycle_reinstatement_after_gap():
    paid = _series([0.50] * 4, "2023-01-15", 91)
    paid.append(_paid("2025-06-15", 0.50))
    result = _lifecycle.analyze_dividends(paid_events=paid, as_of="2025-07-01")
    assert result["reinstatement"] == {"date": "2025-06-15"}
    assert result["possible_suspension"] is False


def test_lifecycle_growth_trend():
    paid = [_paid("2025-01-15", 0.50)]
    assert _lifecycle.analyze_dividends(
        paid_events=paid, growth={"growth_1y": 0.02, "growth_5y_cagr": 0.08}
    )["growth_trend"] == "decelerating"
    assert _lifecycle.analyze_dividends(
        paid_events=paid, growth={"growth_1y": 0.12, "growth_5y_cagr": 0.08}
    )["growth_trend"] == "accelerating"
    assert _lifecycle.analyze_dividends(
        paid_events=paid, growth={"growth_1y": 0.08, "growth_5y_cagr": 0.08}
    )["growth_trend"] == "stable_or_unknown"
    assert _lifecycle.analyze_dividends(paid_events=paid)["growth_trend"] == "stable_or_unknown"
    assert _lifecycle.analyze_dividends(paid_events=paid)["growth_basis"] == "total_aggregates"


def test_lifecycle_regular_basis_growth_needs_five_consecutive_years():
    five_years = [p for year in range(2021, 2026)
                  for p in _series([0.50] * 4, f"{year}-01-15", 91)]
    full = _lifecycle.analyze_dividends(paid_events=five_years, as_of="2026-02-01")
    assert full["growth_basis"] == "total_aggregates"
    assert full["regular_basis_growth"] is not None
    assert full["regular_basis_growth"]["growth_1y"] == 0.0
    four_years = [p for year in range(2022, 2026)
                  for p in _series([0.50] * 4, f"{year}-01-15", 91)]
    short = _lifecycle.analyze_dividends(paid_events=four_years, as_of="2026-02-01")
    assert short["regular_basis_growth"] is None
    assert short["growth_basis"] == "total_aggregates"
