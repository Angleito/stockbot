"""Focused behavioral contracts for Google public-data (no live network).

Provider fakes sit at the network boundary only: BigQuery via
client_factory fakes (dry_run/submit protocol), HTTP via monkeypatched
requests/session objects. Nothing here invents sibling behavior; every
assertion follows the batch contract.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
import requests

from app.google_data import bigquery_client as bq
from app.google_data import (
    datacommons,
    geo_context,
    patents,
    signals,
    stackoverflow,
    trends,
    youtube,
)

GOOGLE_ENV = (
    "GOOGLE_DATA_ENABLED", "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "DATACOMMONS_API_KEY", "GOOGLE_CLOUD_API_KEY",
    "BIGQUERY_MAX_BYTES_PER_QUERY", "BIGQUERY_MONTHLY_BYTES_LIMIT",
    "BIGQUERY_DAILY_BYTES_LIMIT", "GOOGLE_TRENDS_API_ENABLED",
    "YOUTUBE_SEARCH_DAILY_LIMIT",
)


@pytest.fixture(autouse=True)
def _clean_google_env(monkeypatch):
    for key in GOOGLE_ENV:
        monkeypatch.delenv(key, raising=False)


def _enable(monkeypatch, project="test-proj"):
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project)


class FakeBQ:
    """Boundary fake: dry_run(params)->bytes, submit(params, max_bytes, job_id)."""

    def __init__(self, total_bytes=10, rows=None, state="DONE",
                 billed=None, billing_enabled=False):
        self.total_bytes = total_bytes
        self._rows = rows if rows is not None else [{"n": 1}]
        self.state = state
        self.billed = total_bytes if billed is None else billed
        self.billing_enabled = billing_enabled
        self.submits: list = []
        self.dry_runs: list = []

    def dry_run(self, params):
        self.dry_runs.append(dict(params))
        return self.total_bytes

    def submit(self, params, max_bytes, job_id):
        self.submits.append(
            {"params": dict(params), "max_bytes": max_bytes, "job_id": job_id})
        return {"job_id": job_id, "total_bytes_billed": self.billed,
                "rows": list(self._rows), "state": self.state}


def _factory(fake):
    def _make(*args, **kwargs):
        return fake
    return _make


def _ledger(tmp_path):
    return json.loads((tmp_path / "google_data" / "bq_ledger.json").read_text())


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


# BigQuery executor ------------------------------------------------------

def test_billing_enabled_refuses_submit(monkeypatch, tmp_path):
    _enable(monkeypatch)
    fake = FakeBQ(billing_enabled=True)
    result = bq.submit_template("trends_top", {"limit": 5},
                                client_factory=_factory(fake), data_root=tmp_path)
    assert result["error_type"] == "billing_enabled"
    assert result["source"] == "bigquery"
    assert fake.submits == [] and fake.dry_runs == []


def test_billing_unknown_refuses_submit(monkeypatch, tmp_path):
    _enable(monkeypatch)
    fake = FakeBQ(billing_enabled=None)
    result = bq.submit_template("trends_top", {"limit": 5},
                                client_factory=_factory(fake), data_root=tmp_path)
    assert result["error_type"] == "billing_unknown"
    assert fake.submits == []


def test_check_billing_disabled_states():
    assert bq.check_billing_disabled({"billingEnabled": False}) is True
    assert bq.check_billing_disabled({"billingEnabled": True}) is False
    assert bq.check_billing_disabled({}) is False
    assert bq.check_billing_disabled(FakeBQ(billing_enabled=False)) is True
    assert bq.check_billing_disabled(FakeBQ(billing_enabled=True)) is False


def test_dry_run_above_cap_refuses(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("BIGQUERY_MAX_BYTES_PER_QUERY", "1000")
    fake = FakeBQ(total_bytes=5000)
    result = bq.submit_template("trends_top", {"limit": 5},
                                client_factory=_factory(fake), data_root=tmp_path)
    assert result["error_type"] == "cost_limit_exceeded"
    assert fake.submits == []


def test_monthly_ceiling_includes_pending(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("BIGQUERY_MONTHLY_BYTES_LIMIT", "3000")
    fake = FakeBQ(total_bytes=2000)
    first = bq.submit_template("trends_top", {"k": 1},
                               client_factory=_factory(fake), data_root=tmp_path)
    assert first["status"] == "ok"
    second = bq.submit_template("trends_top", {"k": 2},
                                client_factory=_factory(fake), data_root=tmp_path)
    assert second["error_type"] == "monthly_limit_exceeded"
    assert len(fake.submits) == 1


def test_retry_reuses_job_id(monkeypatch, tmp_path):
    _enable(monkeypatch)
    fake = FakeBQ(total_bytes=100)
    first = bq.submit_template("trends_top", {"k": 1},
                               client_factory=_factory(fake), data_root=tmp_path)
    second = bq.submit_template("trends_top", {"k": 1},
                                client_factory=_factory(fake), data_root=tmp_path)
    assert first["job_id"] == second["job_id"]
    assert len(fake.submits) == 1
    assert len(_ledger(tmp_path)["jobs"]) == 1


def test_corrupt_ledger_refuses(monkeypatch, tmp_path):
    _enable(monkeypatch)
    root = tmp_path / "google_data"
    root.mkdir(parents=True, exist_ok=True)
    (root / "bq_ledger.json").write_text("not json{{{")
    with pytest.raises(bq.LedgerCorrupt):
        bq.submit_template("trends_top", {"k": 1},
                           client_factory=_factory(FakeBQ()), data_root=tmp_path)
    (root / "bq_ledger.json").write_text(json.dumps({"jobs": [], "months": {}}))
    with pytest.raises(bq.LedgerCorrupt):
        bq.submit_template("trends_top", {"k": 1},
                           client_factory=_factory(FakeBQ()), data_root=tmp_path)


def test_cross_month_retains_unresolved(monkeypatch, tmp_path):
    _enable(monkeypatch)
    fake = FakeBQ(total_bytes=100, state="RUNNING")
    bq.submit_template("trends_top", {"k": "old"},
                       client_factory=_factory(fake), data_root=tmp_path)
    path = tmp_path / "google_data" / "bq_ledger.json"
    ledger = json.loads(path.read_text())
    assert len(ledger["jobs"]) == 1
    old_id = next(iter(ledger["jobs"]))
    ledger["jobs"][old_id]["month"] = "2000-01"
    path.write_text(json.dumps(ledger))
    done_fake = FakeBQ(total_bytes=50)
    second = bq.submit_template("trends_top", {"k": "new"},
                                client_factory=_factory(done_fake), data_root=tmp_path)
    assert second["status"] == "ok"
    kept = json.loads(path.read_text())["jobs"]
    assert kept[old_id]["month"] == "2000-01"
    assert len(kept) == 2


def test_allowlist_rejects_unknown_template(tmp_path):
    result = bq.submit_template("definitely_not_a_template", {},
                                client_factory=_factory(FakeBQ()), data_root=tmp_path)
    assert "error" in result
    assert result["source"] == "bigquery"


def test_patent_template_over_cap(monkeypatch, tmp_path):
    _enable(monkeypatch)
    monkeypatch.setenv("BIGQUERY_MAX_BYTES_PER_QUERY", "1000")
    fake = FakeBQ(total_bytes=10 ** 9)
    result = bq.submit_template(
        "patents_assignee",
        {"assignees": ["Acme"], "start_date": "2020-01-01",
         "end_date": "2026-01-01", "limit": 5},
        client_factory=_factory(fake), data_root=tmp_path)
    assert result["error_type"] == "cost_limit_exceeded"
    assert fake.submits == []


# Signals math + identity -------------------------------------------------

PERIODS = ["2026-W01", "2026-W02", "2026-W03", "2026-W04"]
GEOS = ["US", "GB", "DE", "FR"]


def _math_batch():
    rows = []
    for period in PERIODS:
        for geo in GEOS:
            hit = period in PERIODS[:3] and geo in GEOS[:2]
            rows.append({"table": "trends_top", "period": period, "geo": geo,
                         "term": "alpha" if hit else None, "list_kind": "rising",
                         "rank": 5 if hit else None, "present": hit})
    rows.append({"table": "trends_top", "period": "2026-W01", "geo": "US",
                 "term": "alpha", "list_kind": "rising", "rank": 5,
                 "present": True})  # duplicate: must not inflate
    return rows


def test_candidate_math_persistence_diffusion():
    features = signals.compute_candidate_features(_math_batch())
    assert features["persistence"] == pytest.approx(0.75)
    assert features["diffusion"] == pytest.approx(0.5)


def test_rank_improvement_within_same_table_list_geo():
    rows = [
        {"table": "t", "period": "2026-W01", "geo": "US",
         "term": "alpha", "list_kind": "rising", "rank": 9},
        {"table": "t", "period": "2026-W02", "geo": "US",
         "term": "alpha", "list_kind": "rising", "rank": 4},
    ]
    assert signals.compute_candidate_features(rows)["rank_improvement"] == 5


def test_normalize_signal_id_and_first_known_at():
    record = {"table": "t", "period": "2026-W01", "geo": "US",
              "term": "alpha", "list_kind": "rising", "rank": 3}
    first = signals.normalize_candidate(record, retrieved_at="2026-09-01T00:00:00+00:00",
                                        persist=False)
    assert first["signal_id"] == hashlib.sha256(
        "t|2026-W01|US|alpha|rising".encode()).hexdigest()
    assert first["status"] == "candidate"
    recollected = signals.normalize_candidate(
        record, retrieved_at="2026-09-02T00:00:00+00:00",
        known_at=first["known_at"], persist=False)
    assert recollected["signal_id"] == first["signal_id"]
    assert recollected["known_at"] == first["known_at"]


def _trend_rows():
    return [
        {"table": "trends_top", "period": "2026-W01", "geo": "US",
         "term": "alpha", "list_kind": "top", "rank": 3},
        {"table": "trends_rising", "period": "2026-W01", "geo": "US",
         "term": "beta", "list_kind": "rising", "rank": 1},
    ]


def _trend_executor(rows):
    def _run(template, params):
        return {"status": "ok", "rows": [dict(r) for r in rows], "source": "bigquery"}
    return _run


def test_trends_usable_without_other_keys(monkeypatch, tmp_path):
    _enable(monkeypatch)
    result = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["US"],
        limit=10, data_root=tmp_path, executor=_trend_executor(_trend_rows()))
    assert result["status"] == "ok"
    assert result["source"] == "trends"
    assert "alpha" in json.dumps(result)


def test_asof_excludes_future_evidence_and_recollect_idempotent(monkeypatch, tmp_path):
    _enable(monkeypatch)
    run = lambda: trends.collect_trends(  # noqa: E731
        start_date="2026-09-01", end_date="2026-09-02", geos=["US"],
        limit=10, data_root=tmp_path, executor=_trend_executor(_trend_rows()))
    first, second = run(), run()
    assert first["status"] == second["status"] == "ok"
    before = signals.query_signals(as_of="2000-01-01", data_root=tmp_path)
    assert before == []
    after = signals.query_signals(data_root=tmp_path)
    assert after, "expected retained evidence"
    first_ids = sorted(s["signal_id"] for s in signals.query_signals(data_root=tmp_path))
    assert [s["signal_id"] for s in after] == first_ids
    known = {s["signal_id"]: s["known_at"] for s in after}
    rerun = {s["signal_id"]: s["known_at"]
             for s in signals.query_signals(data_root=tmp_path)}
    assert rerun == known
    assert len(first_ids) == len(set(first_ids))


# Trends durability regressions -------------------------------------------------

def _scoped_trend_executor(*, refreshes, rows_for, seen):
    """Params-aware executor: partitions for trends_refreshes, per-refresh rows otherwise."""
    def _run(template, params):
        seen.append((template, dict(params)))
        if template == "trends_refreshes":
            return {"status": "ok", "rows": [{"refresh_date": r} for r in refreshes],
                    "source": "bigquery"}
        return {"status": "ok", "rows": [dict(r) for r in rows_for(template, dict(params))],
                "source": "bigquery"}
    return _run


def _dma_row(dma, term="alpha", week="2026-08-24", refresh="2026-09-02",
             rank=1, score=90, kind="top"):
    return {"term": term, "week": week, "refresh_date": refresh,
            "dma_name": dma, "rank": rank, "score": score, "list_kind": kind}


def _three_dma_rows():
    return [_dma_row(dma, term, refresh="2026-09-02", rank=i + 1, score=90 - 10 * i)
            for i, (dma, term) in enumerate(
                [("Chicago", "alpha"), ("Los Angeles", "beta"), ("New York", "gamma")])]


def _national_row(term="alpha", week="2026-08-24", refresh="2026-09-02",
                 dma_count=2, score=85.0, kind="top"):
    return {"term": term, "week": week, "refresh_date": refresh,
            "dma_count": dma_count, "score": score, "list_kind": kind}


def _intl_national_row(term="alpha", week="2026-08-24", refresh="2026-09-02",
                      country="GB", region_count=5, score=80.0, kind="top"):
    return {"term": term, "week": week, "refresh_date": refresh,
            "country_code": country, "region_count": region_count,
            "score": score, "list_kind": kind}

def test_plan_groups_keeps_national_us_with_other_geos():
    assert trends._plan_groups(["US", "GB"]) == [
        {"kind": "us", "national": True, "dmas": []},
        {"kind": "intl", "country": "GB"},
    ]
    assert trends._plan_groups(["US", "New York"]) == [
        {"kind": "us", "national": True, "dmas": []},
        {"kind": "us", "national": False, "dmas": ["New York"]},
    ]
    assert trends._plan_groups(["US", "GB", "New York"]) == [
        {"kind": "us", "national": True, "dmas": []},
        {"kind": "us", "national": False, "dmas": ["New York"]},
        {"kind": "intl", "country": "GB"},
    ]



def test_national_rollup_emits_every_refresh(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-01", "2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top_national":
            return []
        refresh = params["start_date"]
        assert refresh in refreshes
        assert "dmas" not in params and "all_dmas" not in params
        return [_national_row("alpha", refresh=refresh, dma_count=2, score=85.0),
                _national_row("beta", refresh=refresh, dma_count=3, score=70.0)]

    result = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["US"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    assert {t for t, _ in seen if t != "trends_refreshes"} <= {
        "trends_us_top_national", "trends_us_rising_national"}
    by_refresh = {}
    for obs in result["observations"]:
        assert obs["geo"] == "US"
        assert obs.get("rank") is None
        assert (obs.get("metrics") or {}).get("rank") is None
        by_refresh.setdefault(obs["metrics"]["refresh_date"], []).append(obs)
    assert sorted(by_refresh) == refreshes
    for refresh, obs_list in by_refresh.items():
        by_term = {o["term"]: o for o in obs_list}
        assert by_term["alpha"]["metrics"]["dma_count"] == 2
        assert by_term["alpha"]["metrics"]["score"] == pytest.approx(85.0)
        assert by_term["beta"]["metrics"]["dma_count"] == 3
        assert by_term["beta"]["metrics"]["score"] == pytest.approx(70.0)
        assert all(refresh in o["source_record_id"] for o in obs_list)
        for obs in obs_list:
            assert obs["metrics"]["score_basis"] == "mean_list_score_where_listed"
            assert obs["features"]["diffusion"] is None
            assert obs["features"]["coverage"]["missing"]["diffusion"] == "subregion rows aggregated"
            assert obs["features"]["rules"]["diffusion"]["value"] is None
    table = trends._template_table("trends_us_top_national")
    for refresh in refreshes:
        durable = trends._warehouse_rows(tmp_path, table, refresh)
        assert durable, f"expected durable rows for {refresh}"
        assert {str((r.get("metrics") or {}).get("refresh_date")) for r in durable} == {refresh}
        assert {(r.get("metrics") or {}).get("dma_count") for r in durable} == {2, 3}

def test_intl_national_nulls_diffusion_and_marks_score_basis(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_intl_top_national":
            return []
        return [_intl_national_row("alpha", refresh="2026-09-02",
                                   country="GB", region_count=5, score=80.0)]

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["GB"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    assert result["observations"]
    for obs in result["observations"]:
        assert obs["metrics"]["region_count"] == 5
        assert obs["metrics"]["score_basis"] == "mean_list_score_where_listed"
        assert obs["features"]["diffusion"] is None
        assert obs["features"]["coverage"]["missing"]["diffusion"] == "subregion rows aggregated"
        assert obs["features"]["rules"]["diffusion"]["value"] is None


def test_explicit_dma_keeps_numeric_diffusion(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return [_dma_row("New York", refresh=params["start_date"], rank=1)]

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    assert result["observations"]
    for obs in result["observations"]:
        assert obs["features"]["diffusion"] == pytest.approx(1.0)
        assert "score_basis" not in (obs.get("metrics") or {})


def test_national_default_scope_is_bounded(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    result = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["US"],
        limit=10, data_root=tmp_path,
        executor=_scoped_trend_executor(
            refreshes=refreshes, rows_for=lambda template, params: [], seen=seen))
    assert result["status"] == "ok"
    data_calls = [(t, p) for t, p in seen if t != "trends_refreshes"]
    assert data_calls
    assert {t for t, _ in data_calls} <= {
        "trends_us_top_national", "trends_us_rising_national"}
    for _, params in data_calls:
        assert params["limit"] == 1001
        assert "dmas" not in params and "all_dmas" not in params
        assert params["week_end"] == "2026-09-02"
        assert params["week_start"] == "2026-08-20"


def test_national_sentinel_fires_post_aggregation(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    big_us = [_national_row(term=f"term-{i}", refresh="2026-09-02",
                            dma_count=210, score=50.0) for i in range(1001)]
    big_gb = [_intl_national_row(term=f"term-{i}", refresh="2026-09-02",
                                 country="GB", region_count=12, score=50.0)
              for i in range(1001)]

    def _collect(geos, big, template):
        seen: list = []

        def _rows(t, params):
            if t == template:
                return [dict(r) for r in big]
            return []

        return trends.collect_trends(
            start_date="2026-09-02", end_date="2026-09-02", geos=geos,
            limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(
                refreshes=refreshes, rows_for=_rows, seen=seen)), seen

    for geos, big, template in ((["US"], big_us, "trends_us_top_national"),
                                (["GB"], big_gb, "trends_intl_top_national")):
        result, seen = _collect(geos, big, template)
        assert result["status"] == "unavailable"
        assert result["reason"] == "query_scope_too_large"
        assert result["error_type"] == "missing_coverage"
        assert template in {t for t, _ in seen}
    try:
        observations = trends._parquet.read_table(
            "google_observations", tmp_path / "parquet").to_pylist()
    except Exception:
        observations = []
    assert observations == []
    try:
        stored = trends._parquet.read_table(
            "ingestion_checkpoints", tmp_path / "parquet").to_pylist()
    except Exception:
        stored = []
    assert [r for r in stored if r.get("status") == "complete"] == []


def test_national_ok_below_sentinel_preserves_counts(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template == "trends_us_top_national":
            return [_national_row(term=f"term-{i}", refresh="2026-09-02",
                                  dma_count=210, score=50.0) for i in range(1000)]
        return []

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["US"],
        limit=1000, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    assert result["count"] == 1000
    assert all((o.get("metrics") or {}).get("dma_count") == 210
               for o in result["observations"])


def test_explicit_dma_keeps_row_level_sql(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return [_dma_row("New York", refresh=params["start_date"], rank=1)]

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    data_calls = [(t, p) for t, p in seen if t != "trends_refreshes"]
    assert data_calls
    assert {t for t, _ in data_calls} <= {"trends_us_top", "trends_us_rising"}
    top_calls = [p for t, p in data_calls if t == "trends_us_top"]
    assert top_calls and all(p["all_dmas"] is False for p in top_calls)
    assert all(p["week_start"] == "2026-08-01" and p["week_end"] == "2026-08-31"
               for p in top_calls)
    assert {o["geo"] for o in result["observations"]} == {"New York"}



def test_limit_truncates_response_not_warehouse(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return _three_dma_rows()

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02",
        geos=["Chicago", "Los Angeles", "New York"],
        limit=1, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    data_calls = [params for template, params in seen if template != "trends_refreshes"]
    assert data_calls and all(params["limit"] == 1001 for params in data_calls)
    assert result["count"] == 1 and len(result["observations"]) == 1
    assert result["continuation"] is True and "truncated" in result["warnings"]
    retained = signals.query_signals(data_root=tmp_path)
    assert sorted(s["term"] for s in retained) == ["alpha", "beta", "gamma"]


def test_term_filter_applies_before_limit_slice(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []
    dmas = ["Chicago", "Los Angeles", "New York"]

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        fillers = [_dma_row(dmas[i % 3], term=f"filler-{i:03d}", rank=i + 1,
                            score=90, refresh="2026-09-02")
                   for i in range(100)]
        return fillers + [_dma_row("New York", term="Stanley Cup target", rank=101,
                                   score=90, refresh="2026-09-02")]

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02",
        geos=["Chicago", "Los Angeles", "New York"],
        limit=10, term="Stanley", data_root=tmp_path,
        week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    assert result["count"] == 1 and len(result["observations"]) == 1
    assert "Stanley" in result["observations"][0]["term"]
    assert result["continuation"] is False
    retained = signals.query_signals(data_root=tmp_path)
    assert len(retained) == 101
    assert any("Stanley" in s["term"] for s in retained)

    unfiltered = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02",
        geos=["Chicago", "Los Angeles", "New York"],
        limit=10, data_root=tmp_path,
        week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=[]))
    assert unfiltered["count"] == 10
    assert unfiltered["continuation"] is True


def test_partial_write_does_not_checkpoint(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return _three_dma_rows()

    orig_write = trends._parquet.write_rows

    def _drop_one(name, rows, root=None, **kwargs):
        if name == "google_observations" and len(rows) > 1:
            rows = list(rows)[:-1]
        return orig_write(name, rows, root=root, **kwargs)

    monkeypatch.setattr(trends._parquet, "write_rows", _drop_one)
    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02",
        geos=["Chicago", "Los Angeles", "New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] in ("unavailable", "error")
    assert "warehouse verify failed" in result.get("error", "")
    try:
        stored = trends._parquet.read_table(
            "ingestion_checkpoints", tmp_path / "parquet").to_pylist()
    except Exception:
        stored = []
    complete = {row.get("key") for row in stored if row.get("status") == "complete"}
    scoped = {trends._checkpoint_key(template, params["start_date"], params)
              for template, params in seen if template != "trends_refreshes"}
    assert scoped
    assert not (scoped & complete)


def test_checkpoint_scope_distinguishes_dmas(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return [_dma_row(dma, refresh=params["start_date"], rank=1)
                for dma in params.get("dmas", [])]

    def _collect(geos):
        return trends.collect_trends(
            start_date="2026-09-01", end_date="2026-09-02", geos=geos,
            limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(
                refreshes=refreshes, rows_for=_rows, seen=seen))

    first = _collect(["New York"])
    assert first["status"] == "ok"
    assert {obs["geo"] for obs in first["observations"]} == {"New York"}
    first_seen = len(seen)
    second = _collect(["Los Angeles"])
    assert second["status"] == "ok"
    assert {obs["geo"] for obs in second["observations"]} == {"Los Angeles"}
    assert [t for t, _ in seen[first_seen:] if t != "trends_refreshes"], \
        "scoped miss must re-execute data templates"
    scoped = lambda calls: {trends._checkpoint_key(t, p["start_date"], p)
                            for t, p in calls if t != "trends_refreshes"}
    ny_keys, la_keys = scoped(seen[:first_seen]), scoped(seen[first_seen:])
    assert ny_keys and la_keys and not (ny_keys & la_keys)


def test_partial_write_retry_never_checkpoints_incomplete_batch(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen: list = []
    calls: dict = {}

    def _run(template, params):
        seen.append((template, dict(params)))
        if template == "trends_refreshes":
            return {"status": "ok", "rows": [{"refresh_date": r} for r in refreshes],
                    "source": "bigquery"}
        key = (template, json.dumps(params, sort_keys=True, default=str))
        n = calls.get(key, 0)
        calls[key] = n + 1
        job_id = f"job-{template}-{params['start_date']}"
        if n == 0:
            rows = _three_dma_rows() if template == "trends_us_top" else []
            return {"status": "ok", "rows": [dict(r) for r in rows],
                    "source": "bigquery", "job_id": job_id}
        return {"status": "ok", "cached": True, "job_id": job_id, "rows": [],
                "source": "bigquery"}

    orig_write = trends._parquet.write_rows

    def _drop_one(name, rows, root=None, **kwargs):
        if name == "google_observations" and len(rows) > 1:
            rows = list(rows)[:-1]
        return orig_write(name, rows, root=root, **kwargs)

    monkeypatch.setattr(trends._parquet, "write_rows", _drop_one)

    def _collect():
        return trends.collect_trends(
            start_date="2026-09-02", end_date="2026-09-02",
            geos=["Chicago", "Los Angeles", "New York"],
            limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_run)

    first, second = _collect(), _collect()
    for result in (first, second):
        assert result["status"] in ("unavailable", "error")
        assert "warehouse verify failed" in result.get("error", "")
    try:
        stored = trends._parquet.read_table(
            "ingestion_checkpoints", tmp_path / "parquet").to_pylist()
    except Exception:
        stored = []
    complete = {row.get("key") for row in stored if row.get("status") == "complete"}
    scoped = {trends._checkpoint_key(template, params["start_date"], params)
              for template, params in seen if template != "trends_refreshes"}
    assert scoped
    assert not (scoped & complete)


def test_query_scope_over_1000_never_checkpoints(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    seen: list = []
    calls: dict = {}
    big = [_dma_row("New York", term=f"term-{i}", week="2026-08-24",
                    refresh="2026-09-02", rank=i + 1, score=90)
           for i in range(1001)]

    def _run(template, params):
        seen.append((template, dict(params)))
        if template == "trends_refreshes":
            return {"status": "ok", "rows": [{"refresh_date": r} for r in refreshes],
                    "source": "bigquery"}
        key = (template, json.dumps(params, sort_keys=True, default=str))
        n = calls.get(key, 0)
        calls[key] = n + 1
        job_id = f"job-{template}-{params['start_date']}"
        if n == 0:
            rows = big if template == "trends_us_top" else []
            return {"status": "ok", "rows": [dict(r) for r in rows],
                    "source": "bigquery", "job_id": job_id}
        return {"status": "ok", "cached": True, "job_id": job_id, "rows": [],
                "source": "bigquery"}

    def _collect():
        return trends.collect_trends(
            start_date="2026-09-02", end_date="2026-09-02", geos=["New York"],
            limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_run)

    first, second = _collect(), _collect()
    for result in (first, second):
        assert result["status"] == "unavailable"
        assert result["error_type"] == "missing_coverage"
        assert result["reason"] == "query_scope_too_large"
    try:
        observations = trends._parquet.read_table(
            "google_observations", tmp_path / "parquet").to_pylist()
    except Exception:
        observations = []
    assert observations == []
    try:
        stored = trends._parquet.read_table(
            "ingestion_checkpoints", tmp_path / "parquet").to_pylist()
    except Exception:
        stored = []
    assert [r for r in stored if r.get("status") == "complete"] == []


def test_feature_calculation_uses_latest_refresh_per_week(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-01", "2026-09-02"]
    seen: list = []
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"

    def _rows(template, params):
        if template == "trends_us_top":
            refresh = params["start_date"]
            if refresh == "2026-09-01":
                return [_dma_row("New York", term="alpha", week=w, refresh=refresh,
                                 rank=r, score=s)
                        for w, r, s in [(w1, 4, 10), (w2, 3, 20), (w3, 2, 30), (w4, 1, 100)]]
            if refresh == "2026-09-02":
                return [_dma_row("New York", term="alpha", week=w4, refresh=refresh,
                                 rank=5, score=40)]
            return []
        if template == "trends_us_rising":
            refresh = params["start_date"]
            score = 70 if refresh == "2026-09-01" else 71
            return [_dma_row("New York", term="control", week=w4, refresh=refresh,
                             rank=1, score=score, kind="rising")]
        return []

    executor = _scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen)
    result = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=executor)
    assert result["status"] == "ok"
    alpha = [o for o in result["observations"] if o.get("term") == "alpha"]
    assert alpha
    features = alpha[0].get("features") or {}
    assert features["velocity"] == pytest.approx(7.5)
    assert features["acceleration"] == pytest.approx(0.0)
    assert features["rank_improvement"] == -3
    assert features["percentile"] == pytest.approx(1.0)
    table = trends._template_table("trends_us_top")
    old = trends._warehouse_rows(tmp_path, table, "2026-09-01")
    new = trends._warehouse_rows(tmp_path, table, "2026-09-02")
    assert {r.get("week") for r in old} >= {w4}
    assert {r.get("week") for r in new} == {w4}
    assert {(r.get("metrics") or {}).get("score") for r in old if r.get("week") == w4} == {100}
    assert {(r.get("metrics") or {}).get("score") for r in new if r.get("week") == w4} == {40}
    queried = signals.query_signals(query="alpha", geo="New York", data_root=tmp_path)
    week4 = [s for s in queried if s.get("period") == w4]
    assert len(week4) == 1
    latest = week4[0]
    assert (latest.get("metrics") or {}).get("refresh_date") == "2026-09-02"
    assert (latest.get("metrics") or {}).get("score") == 40
    assert latest.get("features") == features
    assert len(latest.get("available_feature_scopes") or []) == 1
    seen.clear()
    replayed = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=executor)
    assert replayed["status"] == "ok"
    strip = lambda o: {k: v for k, v in o.items() if k not in ("known_at", "retrieved_at")}
    skey = lambda o: (o.get("signal_id"), o.get("period"),
                       str((o.get("metrics") or {}).get("refresh_date")), o.get("term"))
    assert sorted((strip(o) for o in replayed["observations"]), key=skey) == \
        sorted((strip(o) for o in result["observations"]), key=skey)
    replayed_alpha = [o for o in replayed["observations"] if o.get("term") == "alpha"]
    assert replayed_alpha and (replayed_alpha[0].get("features") or {}) == features
    assert seen and all(t == "trends_refreshes" for t, _ in seen)


def test_trend_features_persist_through_query_signals(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        return [_dma_row("New York", term="alpha", week=w, refresh=params["start_date"],
                         rank=r, score=s)
                for w, r, s in [(w1, 4, 10), (w2, 3, 20), (w3, 2, 30), (w4, 1, 50)]]

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["New York"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=[]))
    assert result["status"] == "ok"
    assert result["observations"]
    collected = {o["term"]: o["features"] for o in result["observations"]}
    assert collected["alpha"]
    queried = {s["term"]: s for s in signals.query_signals(data_root=tmp_path)}
    assert queried["alpha"]["features"] == collected["alpha"]
    from app.storage import parquet as _pq
    raw = _pq.read_table("google_observations", tmp_path / "parquet").to_pylist()
    assert raw
    for row in raw:
        assert row.get("features_json") is None
        assert row.get("calc_version") == "1"
    for name, rule in collected["alpha"]["rules"].items():
        assert rule["rule"] == f"trend_{name}_v2"
        assert rule["calc_version"] == "2"
    legacy = {
        "observation_id": "obs-legacy-no-features", "source": "trends", "table": "trends_top",
        "term": "legacy-term", "geo": "US", "list_kind": "top", "period": "2026-W99",
        "observed_at": "2026-W99", "known_at": "2026-09-02T00:00:00+00:00",
        "retrieved_at": "2026-09-02T00:00:00+00:00",
        "source_record_id": "trends_top|2026-09-02|US|legacy-term|top",
        "content_hash": "hash-legacy-no-features", "collector_version": "1",
        "calc_version": "1",
        "metrics_json": json.dumps({"rank": 1}, sort_keys=True),
        "evidence_json": "[]", "source_url": "bq://trends_top",
    }
    _pq.write_rows("google_observations", [legacy], root=tmp_path / "parquet")
    by_term = {s["term"]: s for s in signals.query_signals(data_root=tmp_path)}
    assert by_term["legacy-term"]["features"] is None


def test_feature_calculation_isolates_mixed_geographies(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"
    weeks = [w1, w2, w3, w4]
    us_scores = [10, 20, 30, 50]
    ny_scores, ny_ranks = [100, 70, 60, 40], [1, 2, 3, 4]
    gb_scores = [5, 5, 10, 30]

    def _rows(template, params):
        refresh = params["start_date"]
        if template == "trends_us_top_national":
            return [_national_row("alpha", week=w, refresh=refresh,
                                  dma_count=2, score=s)
                    for w, s in zip(weeks, us_scores)]
        if template == "trends_us_top":
            return [_dma_row("New York", term="alpha", week=w, refresh=refresh,
                             rank=r, score=s)
                    for w, r, s in zip(weeks, ny_ranks, ny_scores)]
        if template == "trends_intl_top_national":
            return [_intl_national_row("alpha", week=w, refresh=refresh,
                                        country="GB", region_count=5, score=s)
                    for w, s in zip(weeks, gb_scores)]
        return []

    result = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["US", "New York", "GB"],
        limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=[]))
    assert result["status"] == "ok"
    by_geo: dict = {}
    for obs in result["observations"]:
        by_geo.setdefault(obs["geo"], []).append(obs)
    assert set(by_geo) == {"US", "New York", "GB"}
    us_features = by_geo["US"][0]["features"]
    ny_features = by_geo["New York"][0]["features"]
    gb_features = by_geo["GB"][0]["features"]
    assert us_features["velocity"] == pytest.approx(10.0)
    assert ny_features["velocity"] == pytest.approx(-15.0)
    assert gb_features["velocity"] == pytest.approx(6.25)
    assert us_features["acceleration"] == pytest.approx(5.0)
    assert ny_features["acceleration"] == pytest.approx(5.0)
    assert gb_features["acceleration"] == pytest.approx(10.0)
    assert us_features["percentile"] == pytest.approx(1.0)
    assert ny_features["percentile"] == pytest.approx(0.25)
    assert gb_features["percentile"] == pytest.approx(1.0)
    assert us_features["rank_improvement"] is None
    assert ny_features["rank_improvement"] == -1
    assert gb_features["rank_improvement"] is None
    assert us_features["diffusion"] is None
    assert us_features["rules"]["diffusion"]["value"] is None
    assert us_features["coverage"]["missing"]["diffusion"] == "subregion rows aggregated"
    assert by_geo["US"][0]["metrics"]["dma_count"] == 2
    assert gb_features["diffusion"] is None
    assert gb_features["rules"]["diffusion"]["value"] is None
    assert gb_features["coverage"]["missing"]["diffusion"] == "subregion rows aggregated"
    assert by_geo["GB"][0]["metrics"]["region_count"] == 5
    assert ny_features["diffusion"] == pytest.approx(1.0)
    assert ny_features["coverage"]["geos_covered"] == ["New York"]


def test_scope_change_does_not_duplicate_source_observations(monkeypatch, tmp_path):
    _enable(monkeypatch)
    from app.storage import parquet as _pq
    refreshes = ["2026-09-02"]
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"
    weeks = [w1, w2, w3, w4]

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        return [_dma_row("New York", term="alpha", week=w, refresh=refresh,
                         rank=r, score=s)
                for w, r, s in zip(weeks, [4, 3, 2, 1], [10, 20, 30, 50])]

    first = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02", geos=["New York"],
        limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=[]))
    assert first["status"] == "ok"
    second = trends.collect_trends(
        start_date="2026-09-02", end_date="2026-09-02",
        geos=["New York", "Los Angeles"],
        limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=[]))
    assert second["status"] == "ok"
    table = trends._template_table("trends_us_top")
    obs_rows = [r for r in _pq.read_table(
        "google_observations", tmp_path / "parquet").to_pylist()
        if r.get("table") == table and r.get("geo") == "New York"
        and r.get("term") == "alpha"]
    assert len(obs_rows) == 4
    for row in obs_rows:
        assert row.get("features_json") is None
    by_week = {r.get("period"): r for r in obs_rows}
    assert set(by_week) == set(weeks)
    target_oid = by_week[w4]["observation_id"]
    feat_rows = [r for r in _pq.read_table(
        "google_signal_features", tmp_path / "parquet").to_pylist()
        if str(r.get("observation_id") or "") == str(target_oid)]
    assert len(feat_rows) == 2
    hashes = {r.get("feature_scope_hash") for r in feat_rows}
    assert len(hashes) == 2
    diffusions = sorted(json.loads(r.get("features_json") or "{}").get("diffusion")
                        for r in feat_rows)
    assert diffusions == pytest.approx([0.5, 1.0])


def test_checkpoint_replay_is_scope_exact(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-02"]
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"
    weeks = [w1, w2, w3, w4]

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        return [_dma_row("New York", term="alpha", week=w, refresh=refresh,
                         rank=r, score=s)
                for w, r, s in zip(weeks, [4, 3, 2, 1], [10, 20, 30, 50])]

    def _collect(geos):
        return trends.collect_trends(
            start_date="2026-09-02", end_date="2026-09-02", geos=geos,
            limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(
                refreshes=refreshes, rows_for=_rows, seen=[]))

    assert _collect(["New York"])["status"] == "ok"
    assert _collect(["New York", "Los Angeles"])["status"] == "ok"
    replayed = _collect(["New York"])
    assert replayed["status"] == "ok"
    ny_obs = [o for o in replayed["observations"] if o.get("geo") == "New York"]
    assert ny_obs
    for obs in ny_obs:
        assert obs["features"]["diffusion"] == pytest.approx(1.0)
        assert obs["features"]["coverage"]["geos_covered"] == ["New York"]


def test_query_signals_prefers_newer_refresh_over_later_backfill(tmp_path):
    from app.storage import parquet as _pq
    table, period, geo, term, kind = "trends_top", "2026-W01", "US", "alpha", "top"

    def _rev(refresh, known, marker, content_hash, tag):
        return {
            "observation_id": "obs-alpha", "source": "trends", "table": table,
            "term": term, "geo": geo, "list_kind": kind, "period": period,
            "observed_at": period, "known_at": known, "retrieved_at": known,
            "source_record_id": f"{table}|{refresh}|{geo}|{term}|{kind}#{tag}",
            "content_hash": content_hash, "collector_version": "1", "calc_version": "1",
            "metrics_json": json.dumps(
                {"rank": 1, "refresh_date": refresh, "marker": marker}, sort_keys=True),
            "evidence_json": "[]", "source_url": f"bq://{table}",
        }

    newer = _rev("2026-09-02", "2026-09-02", "new", "hash-new", "new")
    backfill = _rev("2026-09-01", "2026-09-03", "backfill", "hash-old", "old")
    for rev in (newer, backfill):
        _pq.write_rows("google_observations", [rev], root=tmp_path / "parquet")
    at_cutoff = signals.query_signals(data_root=tmp_path, as_of="2026-09-03")
    assert len(at_cutoff) == 1
    assert at_cutoff[0]["metrics"]["refresh_date"] == "2026-09-02"
    assert at_cutoff[0]["metrics"]["marker"] == "new"
    uncut = signals.query_signals(data_root=tmp_path)
    assert len(uncut) == 1
    assert uncut[0]["metrics"]["refresh_date"] == "2026-09-02"
    assert signals.query_signals(data_root=tmp_path, as_of="2026-09-01") == []


def test_query_signals_tie_breaks_on_refresh_date(tmp_path):
    from app.storage import parquet as _pq
    table, period, geo, term, kind = "trends_top", "2026-W01", "US", "alpha", "top"
    known_at = "2026-09-02T00:00:00+00:00"

    def _rev(refresh, marker, content_hash, tag):
        return {
            "observation_id": "obs-alpha", "source": "trends", "table": table,
            "term": term, "geo": geo, "list_kind": kind, "period": period,
            "observed_at": period, "known_at": known_at,
            "retrieved_at": f"2026-09-02T00:00:0{'1' if tag == 'old' else '2'}+00:00",
            "source_record_id": f"{table}|{refresh}|{geo}|{term}|{kind}#{tag}",
            "content_hash": content_hash, "collector_version": "1", "calc_version": "1",
            "metrics_json": json.dumps(
                {"rank": 1, "refresh_date": refresh, "marker": marker}, sort_keys=True),
            "evidence_json": "[]", "source_url": f"bq://{table}",
        }

    old = _rev("2026-09-01", "old", "zzz-old-hash", "old")
    new = _rev("2026-09-02", "new", "aaa-new-hash", "new")
    for name, order in (("a", (old, new)), ("b", (new, old))):
        for rev in order:
            _pq.write_rows("google_observations", [rev], root=tmp_path / name / "parquet")
    for name in ("a", "b"):
        found = signals.query_signals(data_root=tmp_path / name)
        assert len(found) == 1
        assert found[0]["metrics"]["refresh_date"] == "2026-09-02"
        assert found[0]["metrics"]["marker"] == "new"

# Entity + macro ------------------------------------------------------------
# Migration note: Knowledge Graph entity resolution was removed from the
# research path (no GOOGLE_KG_API_KEY; investigation uses SEC-confirmed
# entity mappings). app/google_data/knowledge_graph.py was deleted; no KG
# behavior is pinned here.


def _dc_payload():
    return {"byVariable": {"Count_Person": {"byEntity": {"geoId/06": {"orderedFacets": [
        {"facetId": "f1", "observations": [{"date": "2020", "value": 1.0}]},
        {"facetId": "f2", "observations": [{"date": "2020", "value": 2.0}]},
    ]}}}},
        "facets": {"f1": {"unit": "people", "importName": "US Census"},
                   "f2": {"unit": "USD", "importName": "BEA"}}}


def test_datacommons_two_facets_stay_distinct(monkeypatch):
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    monkeypatch.setenv("DATACOMMONS_API_KEY", "dc-test")
    seen = {}

    def _post(url, **kwargs):
        seen.update(kwargs)
        seen["url"] = url
        return _Resp(_dc_payload())

    monkeypatch.setattr(requests, "post", _post)
    result = datacommons.get_macro_context(["geoId/06"], ["Count_Person"])
    assert result["status"] == "ok"
    assert len(result["series"]) == 2
    by_facet = {s["facet"]: s for s in result["series"]}
    assert by_facet["f1"]["unit"] == "people"
    assert by_facet["f2"]["unit"] == "USD"
    assert by_facet["f1"]["provider"] == "US Census"
    assert by_facet["f2"]["provider"] == "BEA"
    assert seen["url"] == "https://api.datacommons.org/v2/observation"
    assert seen["headers"] == {"X-API-Key": "dc-test"}
    assert "params" not in seen


def test_datacommons_missing_key_makes_no_call(monkeypatch):
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    calls = []
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: (calls.append(a), _Resp({}))[1])
    result = datacommons.get_macro_context(["geoId/06"], ["Count_Person"])
    assert result["status"] == "unavailable"
    assert result["error"] == "DATACOMMONS_AUTH_REQUIRED"
    assert result["error_type"] == "auth_required"
    assert calls == []


# YouTube analytics (memory-only, thesis-bound) ---------------------------------

def _yt_enable(monkeypatch):
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", "test-key")


def _yt_thesis(tmp_path):
    from app.thesis.repository import ThesisRepository
    repo = ThesisRepository(tmp_path / "thesis")
    return repo.create_thesis("NVDA demand stays strong", scope="NVDA", claims=["demand holds"])


class _YtPipe:
    """Streaming HTTP fake: status_code + iter_content + close."""

    def __init__(self, payload=None, status=200, raw=None):
        self.status_code = status
        self._raw = raw if raw is not None else json.dumps(
            payload if payload is not None else {}).encode()
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._raw), chunk_size):
            yield self._raw[i:i + chunk_size]

    def close(self):
        self.closed = True


def _yt_search_item(video_id, title, channel, live="none"):
    return {"id": {"videoId": video_id},
            "snippet": {"title": title, "channelId": "chan-" + video_id,
                        "channelTitle": channel, "publishedAt": "2026-01-01T00:00:00Z",
                        "liveBroadcastContent": live}}


def _yt_chart_item(video_id, title, channel, live="none"):
    snip = _yt_search_item(video_id, title, channel, live)["snippet"]
    return {"id": video_id, "snippet": snip}


def test_youtube_disabled_before_network_or_files(monkeypatch, tmp_path):
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        return _YtPipe()

    monkeypatch.setattr(requests, "get", _get)
    thesis = _yt_thesis(tmp_path)
    before = {p for p in tmp_path.rglob("*")}
    for kwargs in ({"mode": "topic", "query": "nvda"},
                   {"mode": "popular"}):
        result = youtube.get_youtube_analytics(
            thesis_id=thesis.thesis_id, data_root=tmp_path, **kwargs)
        assert result == {"status": "disabled", "source": "youtube",
                          "reason": "google_disabled"}
    assert calls == []
    assert {p for p in tmp_path.rglob("*")} == before
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    missing = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, mode="popular", data_root=tmp_path)
    assert missing == {"status": "disabled", "source": "youtube", "reason": "missing_key"}
    assert calls == []


def test_youtube_topic_ok_preserves_order_and_counts(monkeypatch, tmp_path):
    from datetime import datetime, timedelta
    _yt_enable(monkeypatch)
    thesis = _yt_thesis(tmp_path)
    search = {"regionCode": "US", "items": [
        _yt_search_item("vidA", "Alpha Marker", "Chan Alpha"),
        _yt_search_item("vidB", "Beta Marker", "Chan Beta", live="live"),
    ]}
    details = {"items": [
        {"id": "vidB", "statistics": {"viewCount": "9007199254740993",
                                      "commentCount": "7"}},
        {"id": "vidA", "statistics": {"viewCount": "12", "likeCount": "3",
                                      "commentCount": "1"}},
    ]}
    seen = {}

    def _get(url, **kwargs):
        params = kwargs.get("params", {})
        if "search" in url:
            seen["search"] = params
            assert params["order"] == "viewCount" and params["q"] == "nvda gpus"
            return _YtPipe(search)
        seen["videos"] = params
        return _YtPipe(details)

    monkeypatch.setattr(requests, "get", _get)
    result = youtube.get_youtube_analytics(
        thesis_id=thesis.slug, mode="topic", query="nvda gpus", region="us",
        days=7, limit=2, data_root=tmp_path)
    assert result["status"] == "ok"
    assert result["order"] == "viewCount" and result["query"] == "nvda gpus"
    assert result["days"] == 7 and result["region"] == "US" and result["mode"] == "topic"
    assert result["thesis"] == {"thesis_id": thesis.thesis_id, "slug": thesis.slug}
    assert [v["video_id"] for v in result["videos"]] == ["vidA", "vidB"]
    assert result["videos"][1]["view_count"] == "9007199254740993"
    assert result["videos"][1]["like_count"] is None
    assert result["videos"][1]["live_broadcast_content"] == "live"
    assert result["warnings"] == ["live_ordering"]
    assert seen["search"]["publishedAfter"] < seen["search"]["publishedBefore"]
    retrieved = datetime.fromisoformat(result["retrieved_at"])
    expires = datetime.fromisoformat(result["expires_at"])
    assert expires - retrieved == timedelta(minutes=15)


def test_youtube_no_content_on_disk_and_expiry(monkeypatch, tmp_path):
    _yt_enable(monkeypatch)
    thesis = _yt_thesis(tmp_path)
    title, channel = "DISK-MARKER-TITLE-9Z3", "DISK-MARKER-CHANNEL-8Y2"

    def _get(url, **kwargs):
        if "search" in url:
            return _YtPipe({"items": [_yt_search_item("v1", title, channel)]})
        return _YtPipe({"items": [{"id": "v1", "statistics": {"viewCount": "5"}}]})

    monkeypatch.setattr(requests, "get", _get)
    result = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, query="public term", limit=1, data_root=tmp_path)
    assert result["status"] == "ok"
    assert not (tmp_path / "google_data" / "youtube_cache.json").exists()
    assert not (tmp_path / "google_data" / "youtube_cache.json.tmp").exists()
    blob = "".join(p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file())
    assert title not in blob and channel not in blob


def test_youtube_popular_uses_chart_without_query(monkeypatch, tmp_path):
    _yt_enable(monkeypatch)
    thesis = _yt_thesis(tmp_path)
    seen = {}

    def _get(url, **kwargs):
        seen.update(kwargs.get("params", {}))
        return _YtPipe({"items": [_yt_chart_item("p1", "Pop Title", "Pop Chan")]})

    monkeypatch.setattr(requests, "get", _get)
    result = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, mode="popular", region="GB", limit=1,
        data_root=tmp_path)
    assert result["status"] == "ok" and result["order"] == "mostPopular"
    assert result["query"] is None and result["days"] is None
    assert seen.get("chart") == "mostPopular" and seen.get("regionCode") == "GB"
    assert "q" not in seen and "publishedAfter" not in seen


def test_youtube_legacy_cache_refuses_without_serving(monkeypatch, tmp_path):
    _yt_enable(monkeypatch)
    thesis = _yt_thesis(tmp_path)
    cache = tmp_path / "google_data" / "youtube_cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"seeded": "STALE-MARKER"}))
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        return _YtPipe()

    monkeypatch.setattr(requests, "get", _get)
    result = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, query="x", data_root=tmp_path)
    assert result["error_type"] == "legacy_cache_present"
    assert calls == [] and "STALE-MARKER" not in json.dumps(result)


def test_youtube_invalid_params_and_private_query_refuse(monkeypatch, tmp_path):
    _yt_enable(monkeypatch)
    thesis = _yt_thesis(tmp_path)
    calls = []

    def _get(url, **kwargs):
        calls.append(url)
        return _YtPipe()

    monkeypatch.setattr(requests, "get", _get)
    cases = [
        {"mode": "trending", "query": "x"},
        {"query": "x", "region": "USA"},
        {"query": "x", "limit": 0},
        {"query": "x", "limit": True},
        {"query": "   "},
        {"query": "x" * 201},
        {"query": "bad\x01query"},
        {"mode": "popular", "query": "x"},
        {"query": "x", "days": 91},
    ]
    for extra in cases:
        kwargs = {"thesis_id": thesis.thesis_id, "query": "x", "data_root": tmp_path}
        kwargs.update(extra)
        assert youtube.get_youtube_analytics(**kwargs)["error_type"] == "invalid_params"
    unknown = youtube.get_youtube_analytics(
        thesis_id="thesis:missing", query="x", data_root=tmp_path)
    assert unknown["error_type"] == "invalid_thesis"
    private = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id,
        query="my portfolio holdings are up $5000 today", data_root=tmp_path)
    assert private["error_type"] == "private_args_denied"
    assert calls == []
    assert not (tmp_path / "google_data" / "youtube_quota.json").exists()


def test_youtube_quota_exhaustion_and_failed_request_accounting(monkeypatch, tmp_path):
    _yt_enable(monkeypatch)
    monkeypatch.setenv("YOUTUBE_SEARCH_DAILY_LIMIT", "1")
    thesis = _yt_thesis(tmp_path)
    calls: list = []

    def _fail(url, **kwargs):
        calls.append(url)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", _fail)
    failed = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, query="x", limit=1, data_root=tmp_path)
    assert failed["error_type"] == "source_unavailable"
    blocked = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, query="y", limit=1, data_root=tmp_path)
    assert blocked["error_type"] == "quota_exhausted"
    assert len(calls) == 1, "failed request consumed the only quota unit; no second call"


# BigQuery-backed context -------------------------------------------------------

def test_patents_require_aliases_and_count_publications(monkeypatch):
    _enable(monkeypatch)
    calls: list = []

    def _run(template, params):
        calls.append((template, params))
        return {"status": "ok", "rows": [{
            "publication_id": "US-1", "publication_date": "2024-05-01",
            "assignees": ["Acme Corp"], "classes": ["G06F"], "title": "Widget"}],
            "source": "bigquery"}

    refused = patents.search_company_patents("Acme")
    assert refused["status"] == "unavailable"
    assert calls == []
    result = patents.search_company_patents(
        "Acme", assignees=["Acme Corp"], limit=5, executor=_run)
    assert result["status"] == "ok"
    assert [p["publication_id"] for p in result["publications"]] == ["US-1"]
    assert "invention" not in json.dumps(result).lower()
    assert "start_yyyymmdd" in calls[0][1] and "end_yyyymmdd" in calls[0][1]
    assert calls[0][1]["end_yyyymmdd"] >= calls[0][1]["start_yyyymmdd"]
    n_calls = len(calls)
    half_open = patents.search_company_patents(
        "Acme", assignees=["Acme Corp"], start_date="2024-01-01", executor=_run)
    assert half_open["error_type"] == "invalid_params"
    assert len(calls) == n_calls


def test_geo_mismatch_is_explicit(monkeypatch):
    _enable(monkeypatch)
    empty = geo_context.get_geo_context(
        ["__not_a_geo__"], executor=lambda t, p: {"status": "ok", "rows": []})
    assert empty["status"] in ("unavailable", "error", "disabled")
    if empty["status"] == "unavailable":
        assert "missing" in json.dumps(empty).lower()
    bad_rows = geo_context.get_geo_context(
        ["geoId/06"], executor=lambda t, p: {"status": "ok", "rows": [{"foo": 1}]})
    assert bad_rows["error_type"] == "unsupported_join"


def test_stackoverflow_stale_coverage(monkeypatch):
    _enable(monkeypatch)
    rows = [{"tag": "python", "period": "2020-01", "activity_count": 5},
            {"tag": "python", "period": "2021-01", "activity_count": 7}]
    result = stackoverflow.get_tag_activity(
        ["python"], executor=lambda t, p: {"status": "ok", "rows": rows})
    assert result["status"] == "ok"
    assert result["last_covered"] == "2021-01"


# Integration boundary ----------------------------------------------------------

def test_tool_schemas_bounded():
    from app import tools as tools_mod
    from app.security.action_policy import TOOL_DOMAINS
    from app.security.context_gateway import TOOL_ENVELOPES

    schemas = {t["function"]["name"]: t["function"] for t in tools_mod.TOOLS}
    expected_caps = {"find_alternative_signals": 100, "get_trend_evidence": 1000,
                     "investigate_social_arbitrage_candidate": 25,
                     "get_macro_context": 100, "search_company_patents": 20}
    for name, cap in expected_caps.items():
        params = schemas[name]["parameters"]
        assert params["properties"]["limit"]["maximum"] == cap
        assert tools_mod.TOOL_CAPABILITIES[name].name == "RESEARCH"
        assert TOOL_DOMAINS[name] == "financial_research"
        assert name in TOOL_ENVELOPES
    assert set(schemas["investigate_social_arbitrage_candidate"]["parameters"]["required"]) == {"term"}
    assert set(schemas["get_macro_context"]["parameters"]["required"]) == {"geos", "variables"}
    assert schemas["search_company_patents"]["parameters"]["required"] == ["company_id", "assignees"]


def test_handler_disabled_passthrough_makes_no_network_call():
    from app import tools as tools_mod

    handler = tools_mod._DIRECT_HANDLERS["get_trend_evidence"]
    result = handler({"start_date": "2026-09-01", "end_date": "2026-09-02",
                      "geos": ["US"], "limit": 5}, "test-model")
    assert isinstance(result, dict)
    assert result["status"] == "disabled"


def test_trend_evidence_term_passed_to_collect(monkeypatch, tmp_path):
    from app import tools as tools_mod
    from app.google_data import trends as trends_mod

    payload = {"status": "ok", "observations": [{"term": "Stanley Cup"}],
               "rows": [{"term": "Stanley Cup"}], "count": 1}
    seen = {}

    def _fake_collect(**kwargs):
        seen.update(kwargs)
        return payload

    monkeypatch.setattr(trends_mod, "collect_trends", _fake_collect)
    handler = tools_mod._DIRECT_HANDLERS["get_trend_evidence"]
    result = handler({"start_date": "2026-09-01", "end_date": "2026-09-02",
                      "geos": ["US"], "limit": 10, "term": "Stanley"}, "test-model")
    assert seen.get("term") == "Stanley"
    assert seen.get("limit") == 10
    assert result == payload


def test_investigate_returns_evidence_and_gaps():
    from app import tools as tools_mod

    handler = tools_mod._DIRECT_HANDLERS["investigate_social_arbitrage_candidate"]
    result = handler({"term": "Stanley"}, "test-model")
    assert result["term"] == "Stanley"
    assert "evidence" in result and "gaps" in result
def test_investigate_never_embeds_youtube(monkeypatch, tmp_path):
    import hashlib
    import sqlite3

    from app import tools as tools_mod
    from app.storage.runs import RunRecorder
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_API_KEY", "yt-key")
    monkeypatch.setenv("RUNS_DB_PATH", str(tmp_path / "runs.sqlite"))
    marker = "YT-MARKER-7Q2-never-persist"
    calls = []

    def _fake_fetch(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "videos": [{"title": marker}]}

    monkeypatch.setattr(youtube, "get_youtube_analytics", _fake_fetch)
    handler = tools_mod._DIRECT_HANDLERS["investigate_social_arbitrage_candidate"]
    recorder = RunRecorder(
        run_id="r-yt", request_id="q-yt", question="stanley", as_of=None,
        model="t", provider="t", model_parameters={}, agent_version="t",
        prompt_version="t", tool_registry_version="t", git_sha="t",
        data_root=tmp_path)
    with recorder:
        result = handler({"term": "Stanley"}, "test-model")
        rendered = json.dumps(result)
        recorder.record_evidence(
            evidence_id="r-yt:evid:0001", run_id="r-yt", tool_call_id="c1",
            round=0, tool_name="investigate_social_arbitrage_candidate",
            rendered_hash=hashlib.sha256(rendered.encode()).hexdigest(),
            rendered_bytes=len(rendered.encode()),
            estimated_tokens=len(rendered) // 4,
            source_names="[]", source_freshness="[]", as_of=None,
            rendered_text=rendered)
    assert calls == []
    assert marker not in rendered
    assert "youtube" not in json.dumps(result.get("evidence", {}))
    assert "youtube-analytics" in rendered
    rows = sqlite3.connect(tmp_path / "runs.sqlite").execute(
        "SELECT rendered_text FROM evidence").fetchall()
    assert rows and all(marker not in (row[0] or "") for row in rows)


def test_cli_google_data_parses_and_patents_needs_company(capsys):
    import argparse

    import cli

    args = cli._build_parser().parse_args(
        ["google-data", "collect", "--source", "trends",
         "--start-date", "2026-09-01", "--end-date", "2026-09-02",
         "--geo", "US", "--limit", "25"])
    assert args.source == "trends" and args.geo == ["US"] and args.limit == 25
    cli._cmd_google_data(argparse.Namespace(
        google_data_command="collect", source="patents", company=None,
        start_date=None, end_date=None, geo=None, variable=[], limit=25,
        data_root=None))
    out = json.loads(capsys.readouterr().out)
    assert out["sources"]["patents"]["status"] == "error"


# Live smoke (explicit markers only; offline suite skips) ----------------------

def test_smoke_bigquery_trends_partition(monkeypatch, tmp_path):
    if not os.environ.get("RUN_BIGQUERY_SMOKE"):
        pytest.skip("live validation unperformed (no RUN_BIGQUERY_SMOKE)")
    from datetime import date, timedelta
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=1)).isoformat()
    result = bq.submit_template(
        "trends_refreshes",
        {"table": "bigquery-public-data.google_trends.top_terms",
         "start_date": start, "end_date": end, "limit": 5},
        data_root=tmp_path)
    assert result["status"] == "ok"


def test_smoke_datacommons_california_population():
    if not os.environ.get("RUN_DATACOMMONS_SMOKE"):
        pytest.skip("live validation unperformed (no RUN_DATACOMMONS_SMOKE)")
    result = datacommons.get_macro_context(["geoId/06"], ["Count_Person"], limit=5)
    assert result["status"] == "ok"
    assert result["series"], "expected at least one California population series"


def test_smoke_youtube_search_and_statistics(monkeypatch, tmp_path):
    if not os.environ.get("RUN_YOUTUBE_SMOKE"):
        pytest.skip("live validation unperformed (no RUN_YOUTUBE_SMOKE)")
    monkeypatch.setenv("GOOGLE_DATA_ENABLED", "true")
    thesis = _yt_thesis(tmp_path)
    result = youtube.get_youtube_analytics(
        thesis_id=thesis.thesis_id, mode="topic", query="python programming",
        limit=1, data_root=tmp_path)
    assert result["status"] == "ok"
    assert result["videos"], "expected at least one video with batched statistics"


def test_smoke_trends_api_pending():
    if not os.environ.get("RUN_GOOGLE_TRENDS_API_SMOKE"):
        pytest.skip("live validation unperformed (no RUN_GOOGLE_TRENDS_API_SMOKE)")
    from app.google_data import trends_api
    result = trends_api.get_interest_over_time(terms=["python"], interval="weekly")
    if result.get("error") in ("GOOGLE_TRENDS_API_DISABLED",
                               "GOOGLE_TRENDS_API_PENDING_ACCESS"):
        pytest.skip("official Trends pending alpha access")
    assert result["status"] == "ok"


# Executor contract fidelity --------------------------------------------------

def test_render_filters_to_referenced_params_with_sdk_types():
    bq_sdk = pytest.importorskip("google.cloud.bigquery")
    spec = bq.TEMPLATES["trends_us_top"]
    sql, params = bq._render_sql(
        spec, {"start_date": "2026-09-01", "end_date": "2026-09-02",
               "dmas": ["New York NY"], "all_dmas": False,
               "week_start": "2020-01-01", "week_end": "2026-09-02",
               "limit": 10, "collector_version": "1", "sql_version": "1"},
        spec["table"])
    assert "DATE(@start_date)" in sql and "CURRENT_DATE" not in sql
    assert set(params) == {"start_date", "end_date", "dmas", "all_dmas",
                           "week_start", "week_end"}
    typed = {p.name: p for p in bq._query_parameters(bq_sdk, params)}
    assert typed["dmas"].array_type == "STRING"
    assert typed["start_date"].type_ == "STRING"
    assert typed["all_dmas"].type_ == "BOOL"


def test_youtube_default_search_ceiling_is_80(monkeypatch):
    from app import config as _config
    assert _config.get_youtube_search_daily_limit() == 80
    assert _config.get_bq_daily_bytes_limit() == 10737418240
    assert _config.get_bq_monthly_bytes_limit() == 536870912000


def test_feature_revisions_are_append_only_and_pit_exact(monkeypatch, tmp_path):
    _enable(monkeypatch)
    from app.storage import parquet as _pq
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"
    weeks = [w1, w2, w3, w4]

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        scores = [10, 20, 30, 50] if refresh == "2026-09-02" else [11, 21, 31, 99]
        return [_dma_row("New York", term="alpha", week=w, refresh=refresh,
                         rank=r, score=s)
                for w, r, s in zip(weeks, [4, 3, 2, 1], scores)]

    def _collect(refresh):
        return trends.collect_trends(
            start_date=refresh, end_date=refresh, geos=["New York"],
            limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(refreshes=[refresh], rows_for=_rows, seen=[]))

    assert _collect("2026-09-02")["status"] == "ok"
    assert _collect("2026-09-03")["status"] == "ok"
    table = trends._template_table("trends_us_top")
    stored = _pq.read_table("google_observations", tmp_path / "parquet").to_pylist()
    obs_rows = [r for r in stored
                if r.get("table") == table and r.get("geo") == "New York"
                and r.get("term") == "alpha" and r.get("period") == w4]
    assert len(obs_rows) == 2
    target_oid = obs_rows[0]["observation_id"]
    assert {r["observation_id"] for r in obs_rows} == {target_oid}
    kuts = sorted({r.get("known_at") for r in stored})
    assert len(kuts) == 2 and kuts[0] < kuts[1]
    feat_rows = [r for r in _pq.read_table(
        "google_signal_features", tmp_path / "parquet").to_pylist()
        if str(r.get("observation_id") or "") == str(target_oid)]
    assert len(feat_rows) == 2
    assert len({r.get("feature_scope_hash") for r in feat_rows}) == 1
    assert len({r.get("inputs_hash") for r in feat_rows}) == 2
    by_hash = {r.get("inputs_hash"): json.loads(r.get("features_json") or "{}")
               for r in feat_rows}
    assert len({json.dumps(v, sort_keys=True) for v in by_hash.values()}) == 2

    def _by_period(as_of):
        return {s["period"]: s for s in signals.query_signals(
            data_root=tmp_path, as_of=as_of)
            if s.get("term") == "alpha" and s.get("geo") == "New York"}
    old, new = _by_period(kuts[0]), _by_period(kuts[1])
    assert old[w4]["features"] == by_hash[old[w4]["available_feature_scopes"][0]["inputs_hash"]]
    assert new[w4]["features"] == by_hash[new[w4]["available_feature_scopes"][0]["inputs_hash"]]
    assert old[w4]["features"] != new[w4]["features"]

    v2 = [r for r in obs_rows if r.get("known_at") == kuts[1]][0]
    metrics = json.loads(v2["metrics_json"])
    metrics["score"] = 123.0
    evidence = json.loads(v2["evidence_json"])
    _, new_hash = trends._observation_identity(
        v2["table"], v2["period"], v2["geo"], v2["term"], v2["list_kind"],
        metrics, evidence)
    rev = dict(v2)
    rev["metrics_json"] = json.dumps(metrics, sort_keys=True, default=str)
    rev["content_hash"] = new_hash
    rev["known_at"] = "2099-01-01T00:00:00+00:00"
    rev["retrieved_at"] = "2099-01-01T00:00:00+00:00"
    _pq.write_rows("google_observations", [rev], root=tmp_path / "parquet")
    latest = _by_period(None)
    assert latest[w4]["features"] is None
    assert latest[w4]["feature_scope"] is None
    assert latest[w4]["feature_scope_hash"] is None
    assert latest[w4]["feature_calculated_at"] is None


def test_inputs_hash_includes_shared_period_coverage(monkeypatch, tmp_path):
    _enable(monkeypatch)
    from app.storage import parquet as _pq
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        if refresh == "2026-09-02":
            out = [_dma_row("New York", term="alpha", week=w4, refresh="2026-09-02",
                            rank=1, score=50)]
            out.extend(_dma_row("New York", term="beta", week=w, refresh="2026-09-02",
                                rank=r, score=s)
                       for w, r, s in zip([w2, w3, w4], [3, 2, 1], [20, 30, 50]))
            return out
        out = [_dma_row("New York", term="alpha", week=w4, refresh="2026-09-02",
                        rank=1, score=50)]
        out.extend(_dma_row("New York", term="beta", week=w, refresh="2026-09-03",
                            rank=r, score=s)
                   for w, r, s in zip([w1, w2, w3, w4], [4, 3, 2, 1], [10, 20, 30, 50]))
        return out

    def _collect(refresh):
        return trends.collect_trends(
            start_date=refresh, end_date=refresh, geos=["New York"],
            limit=20, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(refreshes=[refresh], rows_for=_rows, seen=[]))

    assert _collect("2026-09-02")["status"] == "ok"
    assert _collect("2026-09-03")["status"] == "ok"
    table = trends._template_table("trends_us_top")
    stored = _pq.read_table("google_observations", tmp_path / "parquet").to_pylist()
    obs_rows = [r for r in stored
                if r.get("table") == table and r.get("geo") == "New York"
                and r.get("term") == "alpha" and r.get("period") == w4]
    assert len(obs_rows) == 1
    target_oid = obs_rows[0]["observation_id"]
    assert len({r["content_hash"] for r in obs_rows}) == 1
    kuts = sorted({r.get("known_at") for r in stored})
    assert len(kuts) == 2 and kuts[0] < kuts[1]
    feat_rows = [r for r in _pq.read_table(
        "google_signal_features", tmp_path / "parquet").to_pylist()
        if str(r.get("observation_id") or "") == str(target_oid)]
    assert len(feat_rows) == 2
    assert len({r.get("feature_scope_hash") for r in feat_rows}) == 1
    assert len({r.get("inputs_hash") for r in feat_rows}) == 2
    by_hash = {r.get("inputs_hash"): json.loads(r.get("features_json") or "{}")
               for r in feat_rows}
    narrow = [v for v in by_hash.values()
              if v.get("coverage", {}).get("periods_covered") == [w2, w3, w4]]
    expanded = [v for v in by_hash.values()
                if v.get("coverage", {}).get("periods_covered") == [w1, w2, w3, w4]]
    assert len(narrow) == 1 and len(expanded) == 1
    assert narrow[0]["persistence"] == 1 / 3
    assert expanded[0]["persistence"] == 1 / 4

    def _alpha(as_of):
        rows = [s for s in signals.query_signals(data_root=tmp_path, as_of=as_of)
                if s.get("term") == "alpha" and s.get("geo") == "New York" and s.get("period") == w4]
        assert len(rows) == 1
        return rows[0]
    old, new = _alpha(kuts[0]), _alpha(kuts[1])
    assert old["features"] == narrow[0]
    assert new["features"] == expanded[0]
    assert _alpha(None)["features"] == expanded[0]


def test_unscoped_reads_are_order_independent(monkeypatch, tmp_path):
    _enable(monkeypatch)
    w1, w2, w3, w4 = "2026-08-03", "2026-08-10", "2026-08-17", "2026-08-24"
    weeks = [w1, w2, w3, w4]

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        out = []
        for dma, scores in (("New York", [10, 20, 30, 50]),
                            ("Los Angeles", [15, 25, 35, 55])):
            out.extend(_dma_row(dma, term="alpha", week=w, refresh=refresh,
                                rank=r, score=s)
                       for w, r, s in zip(weeks, [4, 3, 2, 1], scores))
        return out

    def _collect(root, geos):
        result = trends.collect_trends(
            start_date="2026-09-02", end_date="2026-09-02", geos=geos,
            limit=20, data_root=root, week_start="2026-08-01", week_end="2026-08-31",
            executor=_scoped_trend_executor(
                refreshes=["2026-09-02"], rows_for=_rows, seen=[]))
        assert result["status"] == "ok"

    def _scrubbed(root):
        rows = []
        for record in signals.query_signals(data_root=root):
            record = dict(record)
            for key in ("known_at", "retrieved_at", "feature_calculated_at"):
                if key in record:
                    record[key] = ""
            record["available_feature_scopes"] = [
                {**entry, "feature_calculated_at": ""}
                for entry in (record.get("available_feature_scopes") or [])]
            rows.append(record)
        rows.sort(key=lambda r: str(r.get("signal_id")))
        return rows

    _collect(tmp_path / "a", ["New York"])
    _collect(tmp_path / "a", ["New York", "Los Angeles"])
    _collect(tmp_path / "b", ["New York", "Los Angeles"])
    _collect(tmp_path / "b", ["New York"])
    a, b = _scrubbed(tmp_path / "a"), _scrubbed(tmp_path / "b")
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    by_key = {(s["term"], s["geo"], s["period"]): s for s in a}
    multi = by_key[("alpha", "New York", w4)]
    assert multi["features"] is None
    assert len(multi["available_feature_scopes"]) == 2
    assert [e["feature_scope_hash"] for e in multi["available_feature_scopes"]] == sorted(
        e["feature_scope_hash"] for e in multi["available_feature_scopes"])
    assert by_key[("alpha", "Los Angeles", w4)]["features"] is not None
    for root in (tmp_path / "a", tmp_path / "b"):
        unfiltered = {(s["term"], s["geo"], s["period"]): s
                      for s in signals.query_signals(data_root=root)}
        filtered = signals.query_signals(data_root=root, geo="New York")
        assert filtered and all(s.get("geo") == "New York" for s in filtered)
        ny_unfiltered = unfiltered[("alpha", "New York", w4)]
        ny_filtered = {(s["term"], s["geo"], s["period"]): s for s in filtered}[("alpha", "New York", w4)]
        assert ny_filtered["features"] is None
        assert ny_unfiltered["features"] is None
        assert ny_filtered["available_feature_scopes"] == ny_unfiltered["available_feature_scopes"]
        assert len(ny_filtered["available_feature_scopes"]) == 2
        assert {tuple(sorted(e["feature_scope"]["geos"])) for e in ny_filtered["available_feature_scopes"]} == {
            ("New York",), ("Los Angeles", "New York")}
