"""Unit tests for the deterministic SEC/FINRA normalizers (app/normalization.py).

Fixture payloads mirror the shapes seeded in tests/test_analytics_screens.py;
no network access.
"""

from pathlib import Path

from app.normalization import (
    COMPANY_TICKERS_PARSER_VERSION,
    COMPANY_FACTS_PARSER_VERSION,
    SHARES_OUTSTANDING_CONCEPT,
    SHORT_INTEREST_PARSER_VERSION,
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)
from app.storage import parquet

RETRIEVED_AT = "2026-08-10T12:00:00Z"


def _tickers_payload(cik: int = 1) -> dict[str, dict[str, str | int]]:
    return {"0": {"cik_str": cik, "ticker": "AAA", "title": "Alpha Corp"}}


def _facts_payload(cik: int = 1, facts: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": facts or []}},
    }}}

def _eps_payload(
    cik: int = 1,
    diluted: list[dict[str, object]] | None = None,
    basic: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Companyfacts payload with us-gaap EPS concepts (USD/shares units)."""
    us_gaap: dict[str, object] = {}
    if diluted is not None:
        us_gaap["EarningsPerShareDiluted"] = {"units": {"USD/shares": diluted}}
    if basic is not None:
        us_gaap["EarningsPerShareBasic"] = {"units": {"USD/shares": basic}}
    return {"cik": cik, "entityName": f"CIK{cik}", "facts": {"us-gaap": us_gaap}}


def _normalize(payload: object) -> dict[str, list[dict[str, object]]]:
    return normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="u", source_record_id="cik0000000001",
    )


def test_ticker_normalization():
    datasets = normalize_sec_tickers(_tickers_payload(cik=1), retrieved_at=RETRIEVED_AT, content_hash="h1")
    entities = datasets["entities"]
    assert len(entities) == 1
    assert entities[0]["entity_id"] == "sec:cik:0000000001"
    assert entities[0]["source"] == "sec:company_tickers"
    assert entities[0]["known_at"] == RETRIEVED_AT
    assert entities[0]["retrieved_at"] == RETRIEVED_AT
    assert entities[0]["content_hash"] == "h1"
    assert entities[0]["parser_version"] == COMPANY_TICKERS_PARSER_VERSION

    alias = datasets["entity_aliases"][0]
    assert alias["alias_type"] == "ticker"
    assert alias["alias_value"] == "AAA"
    assert alias["entity_id"] == "sec:cik:0000000001"
    assert alias["security_id"] == "sec:equity:0000000001"
    assert alias["source"] == "sec:company_tickers"
    assert alias["known_at"] == RETRIEVED_AT
    assert alias["retrieved_at"] == RETRIEVED_AT
    assert alias["content_hash"] == "h1"
    assert alias["parser_version"] == COMPANY_TICKERS_PARSER_VERSION


def test_ticker_normalization_skips_malformed_rows():
    raw = {
        "0": {"cik_str": "not-a-cik", "ticker": "BAD"},
        "1": {"cik_str": 2, "ticker": ""},
        "2": "junk",
    }
    datasets = normalize_sec_tickers(raw, retrieved_at=RETRIEVED_AT, content_hash="h1")
    assert datasets["entities"] == []
    assert datasets["entity_aliases"] == []


def test_facts_canonical_concepts():
    payload = _facts_payload(facts=[{"end": "2026-06-30", "val": 10, "accn": "a1", "filed": "2026-08-02"}])
    facts_env = payload["facts"]
    assert isinstance(facts_env, dict)
    dei_env = facts_env["dei"]
    assert isinstance(dei_env, dict)
    dei_env["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
        "units": {"USD": [{"end": "2026-06-30", "val": 123.5, "accn": "a2", "filed": "2026-08-02"}]},
    }
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        source_record_id="cik0000000001",
    )
    facts = {f["concept"]: f for f in datasets["financial_facts"]}
    assert facts["Revenue"]["original_concept"] == "dei:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert facts["Revenue"]["value"] == 123.5
    assert facts["Revenue"]["unit"] == "USD"
    assert facts[SHARES_OUTSTANDING_CONCEPT]["unit"] == "shares"


def test_fact_known_at_is_filed_at_not_retrieved_at():
    payload = _facts_payload(facts=[{"end": "2026-06-30", "val": 100, "accn": "a1", "filed": "2026-08-02"}])
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="u", source_record_id="cik0000000001",
    )
    fact = datasets["financial_facts"][0]
    assert fact["known_at"] == "2026-08-02"
    assert fact["retrieved_at"] == RETRIEVED_AT
    assert fact["known_at"] != fact["retrieved_at"]


def test_malformed_facts_are_skipped_without_crash():
    payload = _facts_payload(facts=[
        {"end": "2026-06-30", "val": "not-a-number", "accn": "a1", "filed": "2026-08-02"},
        {"end": "2026-06-30", "val": 1, "accn": "a2"},          # missing filed
        {"end": "2026-06-30", "val": 2, "filed": "2026-08-02"},  # missing accn
        {"val": 3, "accn": "a4", "filed": "2026-08-02"},        # missing end
    ])
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="u", source_record_id="cik0000000001",
    )
    assert datasets["financial_facts"] == []


def test_non_usd_units_are_ignored():
    payload = {"cik": 1, "facts": {"dei": {
        "Revenues": {"units": {"EUR": [{"end": "2026-06-30", "val": 10, "accn": "a1", "filed": "2026-08-02"}]}},
    }}}
    datasets = normalize_sec_company_facts(
        payload, retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="u", source_record_id="cik0000000001",
    )
    assert not any(f["concept"] == "Revenue" for f in datasets["financial_facts"])


def test_security_classification():
    with_facts = normalize_sec_company_facts(
        _facts_payload(facts=[{"end": "2026-06-30", "val": 100, "accn": "a1", "filed": "2026-08-02"}]),
        retrieved_at=RETRIEVED_AT, content_hash="h1", source_url="u", source_record_id="cik0000000001",
    )
    assert with_facts["securities"][0]["security_type"] == "equity-common"

    empty = normalize_sec_company_facts(
        _facts_payload(), retrieved_at=RETRIEVED_AT, content_hash="h1",
        source_url="u", source_record_id="cik0000000001",
    )
    assert empty["securities"][0]["security_type"] == "unknown"
    assert empty["securities"][0]["parser_version"] == COMPANY_FACTS_PARSER_VERSION


def test_short_interest_normalization():
    rows: list[dict[str, object]] = [{
        "symbolCode": " aaa ", "issueName": "  Alpha  ",
        "currentShortPositionQuantity": "-5", "settlementDate": "2026-08-14",
    }]
    datasets = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", retrieved_at=RETRIEVED_AT,
        content_hash="h1", source_url="u", source_record_id="r",
    )
    (row,) = datasets["short_interest"]
    assert row["symbol_code"] == "AAA"
    assert row["issue_name"] == "Alpha"
    assert row["short_position"] is None  # negative position -> None
    assert row["days_to_cover"] is None   # missing -> None
    assert row["parser_version"] == SHORT_INTEREST_PARSER_VERSION
    assert row["row_id"] == "finra:row:2026-08-14:AAA:h1"
    assert row["known_at"] == RETRIEVED_AT  # conservative: publication unknown, retrieval is known_at
    assert row["retrieved_at"] == RETRIEVED_AT


def test_short_interest_corrected_snapshot_is_new_version():
    rows: list[dict[str, object]] = [{"symbolCode": "AAA", "currentShortPositionQuantity": 20, "settlementDate": "2026-08-14"}]
    v1 = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", retrieved_at=RETRIEVED_AT,
        content_hash="v1-hash", source_url="u", source_record_id="r",
    )
    v2 = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", retrieved_at=RETRIEVED_AT,
        content_hash="v2-hash", source_url="u", source_record_id="r",
    )
    assert v1["short_interest"][0]["row_id"] != v2["short_interest"][0]["row_id"]

def test_short_interest_explicit_known_at_wins_with_bounds():
    datasets = normalize_finra_short_interest(
        [{"symbolCode": "AAA", "currentShortPositionQuantity": 20}],
        settlement_date="2025-12-15", known_at="2025-12-24T08:00:00Z",
        retrieved_at="2025-12-24T12:00:00Z",
        content_hash="h1", source_url="u", source_record_id="r",
    )
    (row,) = datasets["short_interest"]
    assert row["known_at"] == "2025-12-24T08:00:00Z"
    assert row["retrieved_at"] == "2025-12-24T12:00:00Z"


def test_short_interest_explicit_known_at_rejects_out_of_bounds():
    import pytest

    kw: dict[str, object] = {
        "settlement_date": "2025-12-15",
        "retrieved_at": "2025-12-24T12:00:00Z",
        "content_hash": "h1",
        "source_url": "u",
        "source_record_id": "r",
    }
    with pytest.raises(ValueError, match="precedes settlement_date"):
        normalize_finra_short_interest(
            [{"symbolCode": "AAA"}], known_at="2025-12-14T23:00:00Z", **kw,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="exceeds retrieved_at"):
        normalize_finra_short_interest(
            [{"symbolCode": "AAA"}], known_at="2025-12-25T00:00:00Z", **kw,  # type: ignore[arg-type]
        )

def test_eps_facts_normalized_with_period_metadata():
    diluted: list[dict[str, object]] = [{
        "start": "2025-05-01", "end": "2025-07-31", "val": 1.5, "accn": "a1",
        "fy": 2025, "fp": "Q2", "filed": "2025-08-28",
    }]
    basic: list[dict[str, object]] = [{
        "start": "2025-05-01", "end": "2025-07-31", "val": 1.52, "accn": "a2",
        "fy": 2025, "fp": "Q2", "filed": "2025-08-28",
    }]
    datasets = _normalize(_eps_payload(diluted=diluted, basic=basic))
    facts = {f["concept"]: f for f in datasets["financial_facts"]}
    assert set(facts) == {"EarningsPerShareDiluted", "EarningsPerShareBasic"}
    diluted_row = facts["EarningsPerShareDiluted"]
    assert diluted_row["original_concept"] == "us-gaap:EarningsPerShareDiluted"
    assert diluted_row["unit"] == "USD/shares"
    assert diluted_row["value"] == 1.5
    assert diluted_row["duration_type"] == "duration"
    assert diluted_row["period_start"] == "2025-05-01"
    assert diluted_row["period_end"] == "2025-07-31"
    assert diluted_row["fiscal_year"] == 2025
    assert diluted_row["fiscal_period"] == "Q2"
    assert diluted_row["known_at"] == "2025-08-28"
    assert diluted_row["parser_version"] == "sec-companyfacts-v6"
    assert diluted_row["parser_version"] == COMPANY_FACTS_PARSER_VERSION


def test_eps_instant_fact_without_period_metadata_is_nullable():
    datasets = _normalize(_eps_payload(diluted=[
        {"end": "2025-01-31", "val": 0.01, "accn": "a1", "filed": "2025-03-03"},
    ]))
    (row,) = datasets["financial_facts"]
    assert row["duration_type"] == "instant"
    assert row["period_start"] is None
    assert row["fiscal_year"] is None
    assert row["fiscal_period"] is None


def test_eps_non_usd_shares_units_are_ignored():
    payload = _eps_payload()
    facts_env = payload["facts"]
    assert isinstance(facts_env, dict)
    gaap_env = facts_env["us-gaap"]
    assert isinstance(gaap_env, dict)
    gaap_env["EarningsPerShareDiluted"] = {
        "units": {
            "USD": [{"end": "2025-07-31", "val": 1.5, "accn": "a1", "filed": "2025-08-28"}],
            "shares": [{"end": "2025-07-31", "val": 2, "accn": "a2", "filed": "2025-08-28"}],
        },
    }
    datasets = _normalize(payload)
    assert datasets["financial_facts"] == []


def test_malformed_eps_facts_are_skipped_without_crash():
    datasets = _normalize(_eps_payload(diluted=[
        {"end": "2025-07-31", "val": "not-a-number", "accn": "a1", "filed": "2025-08-28"},
        {"end": "2025-07-31", "val": 1.0, "accn": "a2"},            # missing filed
        {"end": "2025-07-31", "val": 1.0, "filed": "2025-08-28"},   # missing accn
        {"val": 1.0, "accn": "a4", "filed": "2025-08-28"},          # missing end
    ]))
    assert datasets["financial_facts"] == []


def test_eps_rows_dedup_on_rerun(tmp_path: Path):
    datasets = _normalize(_eps_payload(
        diluted=[{"end": "2025-07-31", "val": 1.5, "accn": "a1", "filed": "2025-08-28"}],
        basic=[{"end": "2025-07-31", "val": 1.52, "accn": "a2", "filed": "2025-08-28"}],
    ))
    root = tmp_path / "parquet"
    assert parquet.write_rows("financial_facts", datasets["financial_facts"], root=root) == 2
    assert parquet.write_rows("financial_facts", datasets["financial_facts"], root=root) == 0
    table = parquet.read_table("financial_facts", root=root)
    assert table.num_rows == 2
    assert set(table.column("concept").to_pylist()) == {"EarningsPerShareDiluted", "EarningsPerShareBasic"}

def test_ambiguous_xbrl_group_emits_undated_events():
    payload = {"cik": 21344, "entityName": "CIK21344", "facts": {"us-gaap": {
        "DividendsPayableAmountPerShare": {"units": {"USD/shares": [
            {"val": 0.50, "accn": "A1", "filed": "2026-08-01"},
            {"val": 0.54, "accn": "A1", "filed": "2026-08-01"},
        ]}},
        "DividendPayableDateToBePaidDayMonthAndYear": {"units": {"USD": [
            {"val": "2026-09-01", "accn": "A1", "filed": "2026-08-01"},
            {"val": "2026-10-01", "accn": "A1", "filed": "2026-08-01"},
        ]}},
    }}}
    events = _normalize(payload)["dividend_events"]
    assert len(events) == 2
    assert all(e["declaration_date"] is None and e["record_date"] is None
               and e["payment_date"] is None for e in events)
    assert len({e["dividend_event_id"] for e in events}) == 2


def test_single_amount_single_payment_stays_paired():
    payload = {"cik": 21344, "entityName": "CIK21344", "facts": {"us-gaap": {
        "DividendsPayableAmountPerShare": {"units": {"USD/shares": [
            {"val": 0.54, "accn": "A2", "filed": "2026-08-01"},
        ]}},
        "DividendPayableDateToBePaidDayMonthAndYear": {"units": {"USD": [
            {"val": "2026-09-15", "accn": "A2", "filed": "2026-08-01"},
        ]}},
        "DividendsPayableDateOfRecordDayMonthAndYear": {"units": {"USD": [
            {"val": "2026-08-29", "accn": "A2", "filed": "2026-08-01"},
        ]}},
    }}}
    (event,) = _normalize(payload)["dividend_events"]
    assert event["amount_per_share"] == 0.54
    assert event["payment_date"] == "2026-09-15"
    assert event["record_date"] == "2026-08-29"

def test_multiple_record_dates_emit_undated():
    payload = {"cik": 21344, "entityName": "CIK21344", "facts": {"us-gaap": {
        "DividendsPayableAmountPerShare": {"units": {"USD/shares": [
            {"val": 0.54, "accn": "A3", "filed": "2026-08-01"},
        ]}},
        "DividendPayableDateToBePaidDayMonthAndYear": {"units": {"USD": [
            {"val": "2026-09-15", "accn": "A3", "filed": "2026-08-01"},
        ]}},
        "DividendsPayableDateOfRecordDayMonthAndYear": {"units": {"USD": [
            {"val": "2026-08-29", "accn": "A3", "filed": "2026-08-01"},
            {"val": "2026-08-30", "accn": "A3", "filed": "2026-08-01"},
        ]}},
    }}}
    (event,) = _normalize(payload)["dividend_events"]
    assert event["declaration_date"] is None
    assert event["record_date"] is None
    assert event["payment_date"] is None


def test_store_document_text_extracts_filing_text_event(tmp_path: Path):
    from app.sec import store as sec_store
    from app.storage import duckdb
    parquet.write_rows("sec_filings", [{
        "accession": "0000999999", "form": "10-Q", "cik": "21344", "company": "KO",
        "filer_cik": "21344", "filer_name": "KO",
        "filed_at": "2026-08-01", "known_at": "2026-08-02T00:00:00Z",
        "retrieved_at": "2026-08-02T00:00:00Z",
    }], root=tmp_path / "parquet")
    prose = ("The board declared a quarterly cash dividend of $0.54 per share, "
             "payable October 1, 2026, to stockholders of record September 1, 2026.")
    assert sec_store.store_document_text(
        "d1", prose, accession="0000999999", source_url="u",
        filed_at="2026-08-01", known_at="2026-08-02T00:00:00Z", root=tmp_path) == 1
    rows = duckdb.query("SELECT * FROM dividend_events", [], data_root=tmp_path)
    assert len(rows) == 1
    assert rows[0]["source_type"] == "filing_text"
    assert rows[0]["amount_per_share"] == 0.54
    assert rows[0]["known_at"] == "2026-08-02T00:00:00Z"
    assert rows[0]["evidence_excerpt"] == prose
