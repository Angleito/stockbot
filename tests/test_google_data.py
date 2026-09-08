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


def test_national_rollup_emits_every_refresh(monkeypatch, tmp_path):
    _enable(monkeypatch)
    refreshes = ["2026-09-01", "2026-09-02"]
    seen = []

    def _rows(template, params):
        if template != "trends_us_top":
            return []
        refresh = params["start_date"]
        assert refresh in refreshes
        return [_dma_row("New York", refresh=refresh, rank=1, score=90),
                _dma_row("Los Angeles", refresh=refresh, rank=2, score=80)]

    result = trends.collect_trends(
        start_date="2026-09-01", end_date="2026-09-02", geos=["US"],
        limit=10, data_root=tmp_path, week_start="2026-08-01", week_end="2026-08-31",
        executor=_scoped_trend_executor(refreshes=refreshes, rows_for=_rows, seen=seen))
    assert result["status"] == "ok"
    by_refresh = {}
    for obs in result["observations"]:
        assert obs["geo"] == "US"
        by_refresh.setdefault(obs["metrics"]["refresh_date"], []).append(obs)
    assert sorted(by_refresh) == refreshes
    for refresh, obs_list in by_refresh.items():
        assert len(obs_list) == 1
        assert obs_list[0]["metrics"]["dma_count"] == 2
        assert refresh in obs_list[0]["source_record_id"]
    table = trends._template_table("trends_us_top")
    for refresh in refreshes:
        durable = trends._warehouse_rows(tmp_path, table, refresh)
        assert durable, f"expected durable rows for {refresh}"
        assert {str((r.get("metrics") or {}).get("refresh_date")) for r in durable} == {refresh}
        assert all((r.get("metrics") or {}).get("dma_count") == 2 for r in durable)


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
    assert data_calls and all(params["limit"] == 1000 for params in data_calls)
    assert result["count"] == 1 and len(result["observations"]) == 1
    assert result["continuation"] is True and "truncated" in result["warnings"]
    retained = signals.query_signals(data_root=tmp_path)
    assert sorted(s["term"] for s in retained) == ["alpha", "beta", "gamma"]


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
