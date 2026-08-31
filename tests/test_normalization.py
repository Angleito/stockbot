"""Unit tests for the deterministic SEC/FINRA normalizers (app/normalization.py).

Fixture payloads mirror the shapes seeded in tests/test_analytics_screens.py;
no network access.
"""

from app.normalization import (
    COMPANY_TICKERS_PARSER_VERSION,
    COMPANY_FACTS_PARSER_VERSION,
    SHARES_OUTSTANDING_CONCEPT,
    SHORT_INTEREST_PARSER_VERSION,
    normalize_sec_tickers,
    normalize_sec_company_facts,
    normalize_finra_short_interest,
)

RETRIEVED_AT = "2026-08-10T12:00:00Z"


def _tickers_payload(cik=1):
    return {"0": {"cik_str": cik, "ticker": "AAA", "title": "Alpha Corp"}}


def _facts_payload(cik=1, facts=None):
    return {"cik": cik, "entityName": f"CIK{cik}", "facts": {"dei": {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": facts or []}},
    }}}


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
    payload["facts"]["dei"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
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
    rows = [{
        "symbolCode": " aaa ", "issueName": "  Alpha  ",
        "currentShortPositionQuantity": "-5", "settlementDate": "2026-08-14",
    }]
    datasets = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", known_at=RETRIEVED_AT, retrieved_at=RETRIEVED_AT,
        content_hash="h1", source_url="u", source_record_id="r",
    )
    (row,) = datasets["short_interest"]
    assert row["symbol_code"] == "AAA"
    assert row["issue_name"] == "Alpha"
    assert row["short_position"] is None  # negative position -> None
    assert row["days_to_cover"] is None   # missing -> None
    assert row["parser_version"] == SHORT_INTEREST_PARSER_VERSION
    assert row["row_id"] == "finra:row:2026-08-14:AAA:h1"


def test_short_interest_corrected_snapshot_is_new_version():
    rows = [{"symbolCode": "AAA", "currentShortPositionQuantity": 20, "settlementDate": "2026-08-14"}]
    v1 = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", known_at=RETRIEVED_AT, retrieved_at=RETRIEVED_AT,
        content_hash="v1-hash", source_url="u", source_record_id="r",
    )
    v2 = normalize_finra_short_interest(
        rows, settlement_date="2026-08-14", known_at=RETRIEVED_AT, retrieved_at=RETRIEVED_AT,
        content_hash="v2-hash", source_url="u", source_record_id="r",
    )
    assert v1["short_interest"][0]["row_id"] != v2["short_interest"][0]["row_id"]
