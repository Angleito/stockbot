"""Tests for the SEC and FINRA ingestion pipelines.

Everything is offline: connector fetches are replaced with in-memory
payloads, and FINRA discovery/query calls are replaced with fakes that
return fixture-shaped data.  The acceptance criteria under test:

- one ingestion run can be rerun without duplicate normalized facts;
- a missing FINRA pagination header aborts the snapshot with nothing
  normalized;
- SEC/FINRA failures raise and never leave partial checkpoints.
"""

import json

import pytest

from app import finra_client
from app.ingestion import base, finra, sec
from app.storage import ids, parquet

# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _tickers_payload():
    return json.dumps({
        "0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Corp"},
        "1": {"cik_str": 2, "ticker": "BBB", "title": "Beta Corp"},
    }).encode()


def _facts_payload(cik=1, value=100, end="2026-08-01", filed="2026-08-02", accession="0000000001-26-000001"):
    return json.dumps({
        "cik": cik,
        "entityName": "Alpha Corp",
        "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": end, "val": value, "accn": accession, "fy": 2026, "fp": "Q3",
             "form": "10-Q", "filed": filed, "frame": "CY2026Q3I"}
        ]}}}},
    }).encode()


def _canonical_facts_payload(cik=1):
    """Companyfacts payload: shares concept plus all four canonical concepts."""
    return json.dumps({
        "cik": cik,
        "entityName": "Alpha Corp",
        "facts": {
            "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                {"end": "2026-08-01", "val": 100, "accn": "0000000001-26-000001", "fy": 2026,
                 "fp": "Q3", "form": "10-Q", "filed": "2026-08-02", "frame": "CY2026Q3I"}
            ]}}},
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
                    {"start": "2026-01-01", "end": "2026-06-30", "val": 5000000, "accn": "0000000001-26-000001",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-02", "frame": "CY2026Q2"}
                ]}},
                "NetIncomeLoss": {"units": {"USD": [
                    {"start": "2026-01-01", "end": "2026-06-30", "val": 750000, "accn": "0000000001-26-000001",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-02", "frame": "CY2026Q2"}
                ]}},
                "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
                    {"end": "2026-06-30", "val": 2000000, "accn": "0000000001-26-000001",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-02", "frame": "CY2026Q2I"}
                ]}},
                "LongTermDebtCurrentAndNoncurrent": {"units": {"USD": [
                    {"end": "2026-06-30", "val": 3000000, "accn": "0000000001-26-000001",
                     "fy": 2026, "fp": "Q2", "form": "10-Q", "filed": "2026-08-02", "frame": "CY2026Q2I"}
                ]}},
            },
        },
    }).encode()


def _fetch_result(key, payload, url):
    return sec.FetchResult(key=key, payload=payload, url=url, kind="x",
                           metadata={"retrieved_at": "2026-08-21T12:00:00Z"})


# ---------------------------------------------------------------------------
# SEC: company tickers
# ---------------------------------------------------------------------------


def test_sec_tickers_pipeline_writes_entities_and_aliases(data_root, monkeypatch):
    monkeypatch.setattr(sec.SecConnector, "fetch_tickers", lambda self: _fetch_result("company_tickers", _tickers_payload(), sec.SEC_TICKERS_URL))

    summary = sec.ingest_company_tickers(data_root)
    assert summary["payloads_written"] == 1

    entities = parquet.read_table("entities", root=data_root / "parquet").to_pylist()
    assert {e["entity_id"] for e in entities} == {"sec:cik:0000000001", "sec:cik:0000000002"}
    aliases = parquet.read_table("entity_aliases", root=data_root / "parquet").to_pylist()
    assert {a["alias_value"] for a in aliases} == {"AAA", "BBB"}
    assert all(a["alias_type"] == "ticker" for a in aliases)


def test_sec_tickers_rerun_is_a_noop(data_root, monkeypatch):
    calls = []
    monkeypatch.setattr(sec.SecConnector, "fetch_tickers", lambda self: calls.append(1) or _fetch_result("company_tickers", _tickers_payload(), sec.SEC_TICKERS_URL))

    sec.ingest_company_tickers(data_root)
    sec.ingest_company_tickers(data_root)
    sec.ingest_company_tickers(data_root)

    assert len(calls) == 1
    assert parquet.count_rows("entities", root=data_root / "parquet") == 2
    assert parquet.count_rows("entity_aliases", root=data_root / "parquet") == 2


# ---------------------------------------------------------------------------
# SEC: company facts
# ---------------------------------------------------------------------------


def test_sec_facts_pipeline_writes_facts_documents_and_securities(data_root, monkeypatch):
    payload = _facts_payload()
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: _fetch_result(f"cik{cik:010d}", payload, sec.SEC_FACTS_URL.format(cik=cik)))

    summary = sec.ingest_company_facts(1, data_root)
    assert summary["payloads_written"] == 1

    facts = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()
    assert len(facts) == 1
    fact = facts[0]
    assert fact["entity_id"] == "sec:cik:0000000001"
    assert fact["concept"] == "EntityCommonStockSharesOutstanding"
    assert fact["value"] == 100.0
    assert fact["period_end"] == "2026-08-01"
    assert fact["known_at"] == "2026-08-02"  # filed date: knowable on filing
    assert fact["accession"] == "0000000001-26-000001"

    documents = parquet.read_table("documents", root=data_root / "parquet").to_pylist()
    assert len(documents) == 1
    securities = parquet.read_table("securities", root=data_root / "parquet").to_pylist()
    assert securities[0]["security_type"] == "equity-common"


def test_sec_facts_rerun_within_ttl_is_a_noop(data_root, monkeypatch):
    calls = []
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: calls.append(cik) or _fetch_result(f"cik{cik:010d}", _facts_payload(), sec.SEC_FACTS_URL.format(cik=cik)))

    sec.ingest_company_facts(1, data_root)
    sec.ingest_company_facts(1, data_root)

    assert len(calls) == 1
    assert parquet.count_rows("financial_facts", root=data_root / "parquet") == 1


def test_sec_facts_older_checkpoint_is_refetched(data_root, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq

    calls = []
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: calls.append(cik) or _fetch_result(f"cik{cik:010d}", _facts_payload(), sec.SEC_FACTS_URL.format(cik=cik)))
    sec.ingest_company_facts(1, data_root)
    # Age every checkpoint past the TTL by rewriting the parquet files.
    for path in (data_root / "parquet" / "ingestion_checkpoints").rglob("*.parquet"):
        table = pq.read_table(path)
        aged = pa.array(["2020-01-01T00:00:00Z"] * table.num_rows)
        table = table.set_column(
            table.schema.get_field_index("finished_at"),
            pa.field("finished_at", pa.string()), aged,
        )
        pq.write_table(table, path)
    sec.ingest_company_facts(1, data_root)
    assert len(calls) == 2


def test_sec_facts_canonical_concepts_are_normalized(data_root, monkeypatch):
    payload = _canonical_facts_payload()
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: _fetch_result(f"cik{cik:010d}", payload, sec.SEC_FACTS_URL.format(cik=cik)))

    sec.ingest_company_facts(1, data_root)

    facts = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()
    by_concept = {f["concept"]: f for f in facts}
    assert set(by_concept) == {
        "EntityCommonStockSharesOutstanding", "Revenue", "NetIncomeLoss",
        "CashAndCashEquivalents", "LongTermDebt",
    }

    revenue = by_concept["Revenue"]
    assert revenue["original_concept"] == "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
    assert revenue["value"] == 5000000.0
    assert revenue["unit"] == "USD"
    assert revenue["duration_type"] == "duration"
    assert revenue["period_end"] == "2026-06-30"
    assert revenue["fact_id"] == ids.sec_fact_id(1, revenue["accession"], "Revenue", revenue["period_end"], revenue["value"])

    net_income = by_concept["NetIncomeLoss"]
    assert net_income["original_concept"] == "us-gaap:NetIncomeLoss"
    assert net_income["value"] == 750000.0
    assert net_income["duration_type"] == "duration"
    assert net_income["fact_id"] == ids.sec_fact_id(1, net_income["accession"], "NetIncomeLoss", net_income["period_end"], net_income["value"])

    cash = by_concept["CashAndCashEquivalents"]
    assert cash["original_concept"] == "us-gaap:CashAndCashEquivalentsAtCarryingValue"
    assert cash["value"] == 2000000.0
    assert cash["duration_type"] == "instant"
    assert cash["fact_id"] == ids.sec_fact_id(1, cash["accession"], "CashAndCashEquivalents", cash["period_end"], cash["value"])

    debt = by_concept["LongTermDebt"]
    assert debt["original_concept"] == "us-gaap:LongTermDebtCurrentAndNoncurrent"
    assert debt["value"] == 3000000.0
    assert debt["duration_type"] == "instant"
    assert debt["fact_id"] == ids.sec_fact_id(1, debt["accession"], "LongTermDebt", debt["period_end"], debt["value"])

    assert all(f["parser_version"] == "sec-companyfacts-v2" for f in facts)


def test_sec_facts_canonical_alias_fallback_and_nonmatching_ignored(data_root, monkeypatch):
    payload = json.dumps({
        "cik": 1,
        "entityName": "Alpha Corp",
        "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [
                {"end": "2026-08-01", "val": 1000, "accn": "0000000001-26-000002",
                 "filed": "2026-08-02", "frame": "CY2026Q3"}
            ]}},
            "Assets": {"units": {"USD": [
                {"end": "2026-08-01", "val": 999, "accn": "0000000001-26-000002",
                 "filed": "2026-08-02", "frame": "CY2026Q3"}
            ]}},
        }},
    }).encode()
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: _fetch_result(f"cik{cik:010d}", payload, sec.SEC_FACTS_URL.format(cik=cik)))

    sec.ingest_company_facts(1, data_root)

    facts = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()
    assert len(facts) == 1
    assert facts[0]["concept"] == "Revenue"
    assert facts[0]["original_concept"] == "us-gaap:Revenues"
    assert facts[0]["value"] == 1000.0


def test_sec_facts_canonical_concept_ignores_non_usd_units(data_root, monkeypatch):
    payload = json.dumps({
        "cik": 1,
        "entityName": "Alpha Corp",
        "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [
                {"end": "2026-08-01", "val": 1000, "accn": "0000000001-26-000003", "filed": "2026-08-02"}
            ], "shares": [
                {"end": "2026-08-01", "val": 5, "accn": "0000000001-26-000003", "filed": "2026-08-02"}
            ]}},
        }},
    }).encode()
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: _fetch_result(f"cik{cik:010d}", payload, sec.SEC_FACTS_URL.format(cik=cik)))

    sec.ingest_company_facts(1, data_root)

    facts = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()
    assert len(facts) == 1
    assert facts[0]["value"] == 1000.0


def test_sec_shares_fact_row_shape_regression(data_root, monkeypatch):
    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", lambda self, cik: _fetch_result(f"cik{cik:010d}", _facts_payload(), sec.SEC_FACTS_URL.format(cik=cik)))

    sec.ingest_company_facts(1, data_root)

    fact = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()[0]
    assert fact["concept"] == "EntityCommonStockSharesOutstanding"
    assert fact["original_concept"] == "dei:EntityCommonStockSharesOutstanding"
    assert fact["unit"] == "shares"
    assert fact["value"] == 100.0
    assert fact["duration_type"] == "instant"
    assert fact["period_end"] == "2026-08-01"
    assert fact["known_at"] == "2026-08-02"
    assert fact["frame"] == "CY2026Q3I"
    assert fact["parser_version"] == "sec-companyfacts-v2"
    assert fact["fact_id"] == ids.sec_fact_id(1, fact["accession"], "EntityCommonStockSharesOutstanding", fact["period_end"], fact["value"])


def test_sec_normalize_company_facts_shape_and_securities_classification():
    from app.normalization import sec as sec_norm

    out = sec_norm.normalize_company_facts(
        json.loads(_canonical_facts_payload()),
        retrieved_at="2026-08-21T12:00:00Z",
        content_hash="hash",
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
        source_record_id="cik0000000001",
    )
    assert set(out) == {"documents", "financial_facts", "securities"}
    assert len(out["financial_facts"]) == 5
    assert out["securities"][0]["security_type"] == "equity-common"
    assert out["securities"][0]["security_id"] == "sec:equity:0000000001"
    assert out["documents"][0]["parser_version"] == "sec-companyfacts-v2"
    assert out["securities"][0]["parser_version"] == "sec-companyfacts-v2"
    assert all(f["parser_version"] == "sec-companyfacts-v2" for f in out["financial_facts"])


# ---------------------------------------------------------------------------
# FINRA: short interest snapshot
# ---------------------------------------------------------------------------


def _finra_short_interest_rows():
    return [
        {"symbolCode": "AAA", "issueName": "Alpha", "settlementDate": "2026-08-14",
         "currentShortPositionQuantity": 20, "previousShortPositionQuantity": 10,
         "averageDailyVolumeQuantity": 1000, "daysToCoverQuantity": 0.5},
        {"symbolCode": "BBB", "issueName": "Beta", "settlementDate": "2026-08-14",
         "currentShortPositionQuantity": 20, "previousShortPositionQuantity": 20,
         "averageDailyVolumeQuantity": 2000, "daysToCoverQuantity": None},
        {"symbolCode": "CCC", "issueName": "Gamma", "settlementDate": "2026-08-14",
         "currentShortPositionQuantity": 5},
    ]


def _finra_spec(extra_fields=True):
    fields = ["symbolCode", "issueName", "settlementDate", "currentShortPositionQuantity",
              "previousShortPositionQuantity", "averageDailyVolumeQuantity", "daysToCoverQuantity"]
    return finra_client.DatasetSpec(
        "otcMarket", "consolidatedShortInterest", "",
        fields=tuple({"name": n} for n in fields),
        partition_fields=("settlementDate",), date_field="settlementDate",
    )


@pytest.fixture
def finra_fakes(monkeypatch):
    entry = finra_client.CatalogEntry("otcMarket", "consolidatedShortInterest", "", supports_record_offset=True)
    spec = _finra_spec()
    monkeypatch.setattr(finra_client, "_resolve_dataset", lambda _: entry)
    monkeypatch.setattr(finra_client, "_get_dataset_spec", lambda _: spec)
    monkeypatch.setattr(finra_client, "_dataset_path_name", lambda _: spec.name)
    monkeypatch.setattr(finra_client, "_build_payload", lambda *a, **k: {"offset": k.get("offset")})

    def _query(_group, _name, payload):
        if payload["offset"] == 0:
            return (json.dumps({"data": _finra_short_interest_rows()}).encode(),
                    _finra_short_interest_rows(), {"record-total": "3"})
        return (b"[]", [], {"record-total": "3"})

    monkeypatch.setattr(finra_client, "ingestion_post_query", _query)
    return entry, spec


def test_finra_snapshot_writes_normalized_short_interest(data_root, finra_fakes):
    summary = finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    assert summary["payloads_written"] == 1

    rows = parquet.read_table("short_interest", root=data_root / "parquet").to_pylist()
    assert len(rows) == 3
    by_symbol = {r["symbol_code"]: r for r in rows}
    assert by_symbol["AAA"]["short_position"] == 20.0
    assert by_symbol["AAA"]["prev_position"] == 10.0
    assert by_symbol["AAA"]["known_at"] == by_symbol["CCC"]["known_at"]
    assert by_symbol["AAA"]["entity_id"] == "finra:symbol:AAA"
    assert by_symbol["AAA"]["security_id"] is None


def test_finra_snapshot_rerun_is_a_noop(data_root, finra_fakes, monkeypatch):
    calls = []

    def _query(_group, _name, payload):
        calls.append(payload)
        return (json.dumps({"data": _finra_short_interest_rows()}).encode(),
                _finra_short_interest_rows(), {"record-total": "3"})

    monkeypatch.setattr(finra_client, "ingestion_post_query", _query)
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)

    assert len(calls) == 1
    assert parquet.count_rows("short_interest", root=data_root / "parquet") == 3


def test_finra_snapshot_requires_record_total_header(data_root, finra_fakes, monkeypatch):
    monkeypatch.setattr(
        finra_client, "ingestion_post_query",
        lambda _g, _n, _p: (b"[]", [], {}),
    )
    with pytest.raises(ValueError, match="Record-Total"):
        finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    assert parquet.count_rows("short_interest", root=data_root / "parquet") == 0


def test_finra_snapshot_failure_leaves_no_checkpoint(data_root, finra_fakes, monkeypatch):
    monkeypatch.setattr(
        finra_client, "ingestion_post_query",
        lambda _g, _n, _p: (_ for _ in ()).throw(RuntimeError("FINRA down")),
    )
    with pytest.raises(RuntimeError, match="FINRA down"):
        finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    assert parquet.count_rows("ingestion_checkpoints", root=data_root / "parquet") == 0
    assert parquet.count_rows("short_interest", root=data_root / "parquet") == 0


def test_finra_rerun_creates_no_duplicate_normalized_facts(data_root, finra_fakes):
    """Acceptance: one ingestion run can be rerun without duplicates."""
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    summary = finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    assert summary["payloads_skipped"] == 1
    rows = parquet.read_table("short_interest", root=data_root / "parquet").to_pylist()
    assert len(rows) == 3


def test_finra_corrected_snapshot_is_ingested_as_new_version(data_root, finra_fakes, monkeypatch):
    """A corrected payload after the refresh TTL becomes a new source version:
    new rows (distinct row IDs, new known_at) and a new checkpoint, while the
    original version remains point-in-time valid."""
    monkeypatch.setattr(finra, "FINRA_REFRESH_TTL_SECONDS", 0)
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    corrected = [
        {**row, "currentShortPositionQuantity": 25} if row["symbolCode"] == "AAA" else row
        for row in _finra_short_interest_rows()
    ]
    monkeypatch.setattr(
        finra_client, "ingestion_post_query",
        lambda _g, _n, _p: (json.dumps({"data": corrected}).encode(), corrected, {"record-total": "3"}),
    )
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)

    rows = parquet.read_table("short_interest", root=data_root / "parquet").to_pylist()
    assert len(rows) == 6
    aaa_versions = sorted(
        (r for r in rows if r["symbol_code"] == "AAA"),
        key=lambda r: r["known_at"],
    )
    assert {r["short_position"] for r in aaa_versions} == {20.0, 25.0}
    assert len({r["row_id"] for r in aaa_versions}) == 2
    assert aaa_versions[0]["known_at"] != aaa_versions[1]["known_at"]
    checkpoints = parquet.read_table("ingestion_checkpoints", root=data_root / "parquet").to_pylist()
    assert len(checkpoints) == 2


def test_finra_identical_payload_after_ttl_still_deduplicates(data_root, finra_fakes, monkeypatch):
    """Even after the refresh TTL forces a refetch, an identical payload is
    normalized only once (no duplicate facts, no second checkpoint)."""
    monkeypatch.setattr(finra, "FINRA_REFRESH_TTL_SECONDS", 0)
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)

    rows = parquet.read_table("short_interest", root=data_root / "parquet").to_pylist()
    assert len(rows) == 3
    checkpoints = parquet.read_table("ingestion_checkpoints", root=data_root / "parquet").to_pylist()
    assert len(checkpoints) == 1


def test_finra_pages_archived_under_custom_data_root(data_root, finra_fakes):
    """Page archives must live under the pipeline's data root, keeping raw
    provenance and normalized data together."""
    finra.ingest_short_interest_snapshot("2026-08-14", data_root)
    page_files = list((data_root / "raw" / "finra" / "data_page").rglob("*.json"))
    assert page_files, "expected archived FINRA page payloads under the data root"
    assert any(p.suffix == ".json" for p in page_files)
    assert all("consolidatedShortInterest" in str(p) or p.suffix == ".json" for p in page_files)
    manifests = list((data_root / "raw" / "finra" / "data_page").rglob("*.manifest.json"))
    assert manifests


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def test_resolve_entity_and_security_ids(data_root, monkeypatch):
    monkeypatch.setattr(sec.SecConnector, "fetch_tickers", lambda self: _fetch_result("company_tickers", _tickers_payload(), sec.SEC_TICKERS_URL))
    sec.ingest_company_tickers(data_root)
    assert sec.resolve_entity_id("AAA", data_root) == "sec:cik:0000000001"
    assert sec.resolve_entity_id("aaa", data_root) == "sec:cik:0000000001"
    assert sec.resolve_entity_id("ZZZ", data_root) is None
    assert sec.resolve_security_id("AAA", data_root) == "sec:equity:0000000001"


# ---------------------------------------------------------------------------
# Connector resilience
# ---------------------------------------------------------------------------


def test_sec_connector_retries_on_rate_limit_then_succeeds(monkeypatch):
    import requests

    class _Response:
        status_code = 200
        content = b"{}"

    calls = []

    def _fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            response = requests.Response()
            response.status_code = 429
            response.url = url
            raise requests.HTTPError(response=response)
        return _Response()

    connector = sec.SecConnector()
    monkeypatch.setattr(connector.pacing, "wait", lambda: None)
    monkeypatch.setattr("app.ingestion.sec.requests.get", _fake_get)
    monkeypatch.setattr(sec.retry_policy, "sleep", lambda _s: None)

    result = connector._get_with_retry("https://example.test/x")
    assert result.content == b"{}"
    assert len(calls) == 2


def test_sec_connector_gives_up_after_retries(monkeypatch):
    import requests

    def _fake_get(url, headers=None, timeout=None):
        response = requests.Response()
        response.status_code = 429
        response.url = url
        raise requests.HTTPError(response=response)

    connector = sec.SecConnector()
    monkeypatch.setattr(connector.pacing, "wait", lambda: None)
    monkeypatch.setattr("app.ingestion.sec.requests.get", _fake_get)
    monkeypatch.setattr(sec.retry_policy, "sleep", lambda _s: None)

    with pytest.raises(requests.HTTPError):
        connector._get_with_retry("https://example.test/x")


def _no_facts_result(cik):
    return sec.FetchResult(
        key=f"cik{cik:010d}",
        payload=b"",
        url=sec.SEC_FACTS_URL.format(cik=cik),
        kind="companyfacts",
        metadata={
            "retrieved_at": "2026-08-21T12:00:00Z",
            "cik": cik,
            "status": 404,
            "no_companyfacts": True,
        },
    )


def test_sec_connector_404_is_explicit_no_facts_result(monkeypatch):
    import requests

    def _fake_get(url, headers=None, timeout=None):
        response = requests.Response()
        response.status_code = 404
        response.url = url
        raise requests.HTTPError(response=response)

    connector = sec.SecConnector()
    monkeypatch.setattr(connector.pacing, "wait", lambda: None)
    monkeypatch.setattr("app.ingestion.sec.requests.get", _fake_get)
    monkeypatch.setattr(sec.retry_policy, "sleep", lambda _s: None)

    result = connector.fetch_company_facts(123)
    assert result.metadata["no_companyfacts"] is True
    assert result.metadata["status"] == 404
    assert result.payload == b""

    def _fake_500(url, headers=None, timeout=None):
        response = requests.Response()
        response.status_code = 500
        response.url = url
        raise requests.HTTPError(response=response)

    monkeypatch.setattr("app.ingestion.sec.requests.get", _fake_500)
    with pytest.raises(requests.HTTPError):
        connector.fetch_company_facts(123)


def test_ingest_company_facts_404_checkpoints_negative_and_rerun_skips(data_root, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sec.SecConnector, "fetch_company_facts",
        lambda self, cik: calls.append(cik) or _no_facts_result(cik),
    )

    summary = sec.ingest_company_facts(2, data_root)
    assert summary["no_companyfacts"] is True
    assert summary["payloads_written"] == 0

    assert parquet.count_rows("financial_facts", root=data_root / "parquet") == 0
    assert parquet.count_rows("documents", root=data_root / "parquet") == 0
    assert parquet.count_rows("securities", root=data_root / "parquet") == 0
    checkpoints = parquet.read_table("ingestion_checkpoints", root=data_root / "parquet").to_pylist()
    assert len(checkpoints) == 1
    assert checkpoints[0]["status"] == "complete"
    assert checkpoints[0]["record_count"] == 0

    sec.ingest_company_facts(2, data_root)
    assert len(calls) == 1


def test_ingest_shares_facts_continues_past_404_and_reports_negatives(data_root, monkeypatch):
    monkeypatch.setattr(
        sec.SecConnector, "fetch_tickers",
        lambda self: _fetch_result("company_tickers", _tickers_payload(), sec.SEC_TICKERS_URL),
    )
    sec.ingest_company_tickers(data_root)

    def _fetch(self, cik):
        if cik == 2:
            return _no_facts_result(cik)
        return _fetch_result(f"cik{cik:010d}", _facts_payload(cik=cik), sec.SEC_FACTS_URL.format(cik=cik))

    monkeypatch.setattr(sec.SecConnector, "fetch_company_facts", _fetch)

    summary = sec.ingest_shares_facts_for_tickers(["AAA", "BBB"], data_root)

    assert summary["ciks_requested"] == 2
    assert summary["ciks_written"] == 1
    assert summary["ciks_no_companyfacts"] == 1
    facts = parquet.read_table("financial_facts", root=data_root / "parquet").to_pylist()
    assert len(facts) == 1
    assert facts[0]["entity_id"] == "sec:cik:0000000001"