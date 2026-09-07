"""Google Trends discovery evidence via bounded BigQuery templates only.

Daily top/rising tables (US + international) through the shared bounded
executor: checked-in SELECTs with ``refresh_date`` bounds for partition
pruning and an optional ``week`` pushdown for long lookbacks. ``refresh_date``
is load provenance (retrieval/knowledge time); ``week`` is the observed
interest week (analysis time). Scores/ranks are comparable only within one
refresh/week/geo/granularity — analytics stays list-membership. No hourly
template exists; hourly requests fail closed. No scraping, no paid fallback.
Empty results are a valid empty batch; malformed rows fail that batch with an
actionable error instead of inventing observations.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

try:
    from . import signals as _signals
except ImportError:  # pragma: no cover
    from app.google_data import signals as _signals  # type: ignore

try:
    from ..storage import parquet as _parquet
except ImportError:  # pragma: no cover
    try:
        from app.storage import parquet as _parquet  # type: ignore
    except ImportError:
        _parquet = None  # type: ignore

try:
    from ..storage import raw_archive as _raw_archive
except ImportError:  # pragma: no cover
    try:
        from app.storage import raw_archive as _raw_archive  # type: ignore
    except ImportError:
        _raw_archive = None  # type: ignore

try:
    from . import bigquery_client as _bq
except ImportError:  # pragma: no cover
    try:
        from app.google_data import bigquery_client as _bq  # type: ignore
    except ImportError:
        _bq = None  # type: ignore

SOURCE = "trends"
_US_TEMPLATES = ("trends_us_top", "trends_us_rising")
_INTL_TEMPLATES = ("trends_intl_top", "trends_intl_rising")
_TEMPLATE_LIST_KIND = {
    "trends_us_top": "top", "trends_us_rising": "rising",
    "trends_intl_top": "top", "trends_intl_rising": "rising",
    "trends_top": "top", "trends_rising": "rising",
}
_FALLBACK_TABLES = {
    "trends_us_top": "bigquery-public-data.google_trends.top_terms",
    "trends_us_rising": "bigquery-public-data.google_trends.top_rising_terms",
    "trends_intl_top": "bigquery-public-data.google_trends.international_top_terms",
    "trends_intl_rising": "bigquery-public-data.google_trends.international_top_rising_terms",
}
_COLLECTOR_VERSION = "1"
_SQL_VERSION = "1"
_CHECKPOINT_PIPELINE = "google_trends"
_MAX_LIMIT = 1000
_MAX_GEOS = 50
_MAX_SPAN_DAYS = 31
_WIDE_WEEK_START = "1900-01-01"
_WIDE_WEEK_END = "2100-01-01"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def _data_enabled() -> bool:
    fn = getattr(_config, "google_data_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return _env_flag("GOOGLE_DATA_ENABLED")


def _setting(fn_name: str, env_name: str, default=None):
    fn = getattr(_config, fn_name, None)
    if callable(fn):
        try:
            value = fn()
            if value is not None:
                return value
        except Exception:
            pass
    value = (os.getenv(env_name) or "").strip()
    return value or default


def _bq_ready() -> bool:
    return bool(_data_enabled() and _setting("get_google_cloud_project", "GOOGLE_CLOUD_PROJECT"))


def _check_bq_limits():
    try:
        per_q = int(_setting("get_bq_max_bytes_per_query", "BIGQUERY_MAX_BYTES_PER_QUERY", 1073741824))
        per_m = int(_setting("get_bq_monthly_bytes_limit", "BIGQUERY_MONTHLY_BYTES_LIMIT", 536870912000))
        per_d = int(_setting("get_bq_daily_bytes_limit", "BIGQUERY_DAILY_BYTES_LIMIT", 10737418240))
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": "invalid BigQuery byte limit", "error_type": "invalid_config"}
    if per_q <= 0 or per_m <= 0 or per_d <= 0:
        return {"status": "error", "source": SOURCE,
                "error": "non-positive BigQuery byte limit", "error_type": "invalid_config"}
    return None


def _submit(template: str, params: dict, executor, data_root) -> dict:
    if executor is None:
        try:
            from . import bigquery_client as _client
        except ImportError:
            try:
                from app.google_data import bigquery_client as _client  # type: ignore
            except ImportError:
                return {"error": "bigquery client unavailable",
                        "error_type": "source_unavailable", "source": "bigquery"}
        try:
            return _client.submit_template(template, params, data_root=data_root)
        except Exception as exc:
            if type(exc).__name__ == "LedgerCorrupt":
                raise
            return {"status": "error", "source": SOURCE,
                    "error": f"{SOURCE} query failed: {exc}", "error_type": "executor_error"}
    try:
        if callable(executor):
            return executor(template, params)
        return executor.submit_template(template, params)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return {"status": "error", "source": SOURCE,
                "error": f"{SOURCE} query failed: {exc}", "error_type": "executor_error"}


_UNAVAILABLE = frozenset({"billing_enabled", "billing_unknown", "cost_limit_exceeded",
                          "monthly_limit_exceeded", "daily_limit_exceeded",
                          "source_unavailable", "ledger_corrupt"})


def _wrap_error(result: dict) -> dict:
    if "status" in result:
        return result
    out = dict(result)
    out["status"] = "unavailable" if out.get("error_type") in _UNAVAILABLE else "error"
    out["engine"] = out.get("source", "bigquery")
    out["source"] = SOURCE
    return out


def _template_table(template: str) -> str:
    if _bq is not None:
        try:
            spec = _bq.TEMPLATES.get(template, {})
            if spec.get("table"):
                return spec["table"]
        except Exception:
            pass
    return _FALLBACK_TABLES.get(template, "trends")


def _is_country_code(value: str) -> bool:
    return len(value) == 2 and value.isalpha() and value.isupper()


def _plan_groups(geos: list) -> list:
    """Split geos into a US group (national or DMA names) plus per-country groups."""
    national = geos == ["US"]
    dmas = [g for g in geos if g != "US" and not _is_country_code(g)]
    countries = [g for g in geos if g != "US" and _is_country_code(g)]
    groups = []
    if national:
        groups.append({"kind": "us", "national": True, "dmas": []})
    elif dmas:
        groups.append({"kind": "us", "national": False, "dmas": dmas})
    for country in countries:
        groups.append({"kind": "intl", "country": country})
    if not groups:  # e.g. geos == [] handled earlier; defensive: treat as national
        groups.append({"kind": "us", "national": True, "dmas": []})
    return groups


def _parquet_root(data_root) -> Path | None:
    if data_root is None or _parquet is None:
        return None
    return Path(data_root) / "parquet"


def _completed_refreshes(data_root, templates: list) -> set:
    """Checkpointed (template, refresh_date) pairs; missing warehouse reads as none."""
    done = set()
    proot = _parquet_root(data_root)
    if proot is None:
        return done
    try:
        table = _parquet.read_table("ingestion_checkpoints", proot)
    except Exception:
        return done
    try:
        rows = table.to_pylist()
    except Exception:
        return done
    wanted = set(templates)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("pipeline") != _CHECKPOINT_PIPELINE or row.get("source") != "bigquery":
            continue
        if row.get("status") != "complete":
            continue
        key = str(row.get("key") or "")
        template, _, refresh = key.partition("|")
        if template in wanted and refresh:
            done.add((template, refresh))
    return done


def _warehouse_rows(data_root, table: str, refresh: str) -> list:
    """Normalized observations already stored for one table/refresh partition."""
    proot = _parquet_root(data_root)
    if proot is None:
        return []
    try:
        rows = _parquet.read_table("google_observations", proot).to_pylist()
    except Exception:
        return []
    prefix = f"{table}|{refresh}|"
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not str(row.get("source_record_id") or "").startswith(prefix):
            continue
        try:
            metrics = json.loads(row.get("metrics_json") or "{}")
            evidence = json.loads(row.get("evidence_json") or "[]")
        except ValueError:
            continue
        out.append({
            "table": row.get("table") or table,
            "period": row.get("period"), "week": row.get("period"),
            "geo": row.get("geo"), "term": row.get("term"),
            "list_kind": row.get("list_kind"), "rank": metrics.get("rank"),
            "source_record_id": row.get("source_record_id"),
            "observed_at": row.get("observed_at"),
            "known_at": row.get("known_at"), "retrieved_at": row.get("retrieved_at"),
            "metrics": metrics, "evidence": evidence,
        })
    return out


def _store_observations(data_root, observations: list, retrieved_at: str) -> None:
    """Write normalized observations to the warehouse, preserving first known_at."""
    proot = _parquet_root(data_root)
    if proot is None:
        return
    try:
        stored = _parquet.read_table("google_observations", proot).to_pylist()
    except Exception:
        stored = []
    first_known: dict = {}
    for row in stored:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("observation_id")), str(row.get("content_hash")))
        known = str(row.get("known_at") or "")
        if key not in first_known or known < first_known[key]:
            first_known[key] = known
    warehouse_rows = []
    for obs in observations:
        metrics = dict(obs.get("metrics") or {})
        evidence = list(obs.get("evidence") or [])
        content_hash = hashlib.sha256(
            json.dumps({"metrics": metrics, "evidence": evidence},
                       sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        observation_id = hashlib.sha256(
            f"{SOURCE}|{obs['table']}|{obs['period']}|{obs['geo']}|{obs['term']}|{obs['list_kind']}".encode()
        ).hexdigest()
        known_at = first_known.get((observation_id, content_hash), retrieved_at)
        warehouse_rows.append({
            "observation_id": observation_id, "source": SOURCE, "table": obs["table"],
            "term": obs["term"], "geo": obs["geo"], "list_kind": obs["list_kind"],
            "period": obs["period"], "observed_at": obs.get("observed_at") or obs["period"],
            "known_at": known_at, "retrieved_at": retrieved_at,
            "source_record_id": obs.get("source_record_id") or observation_id,
            "content_hash": content_hash, "collector_version": _COLLECTOR_VERSION,
            "calc_version": "1",
            "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
            "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
            "source_url": f"bq://{obs['table']}",
        })
    _parquet.write_rows("google_observations", warehouse_rows, root=proot)


def _archive_raw(data_root, template: str, job_id: str, table: str, params: dict, rows: list) -> None:
    if data_root is None or _raw_archive is None:
        return
    try:
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
        _raw_archive.archive("google", kind=template, key=job_id, payload=payload,
                             url=f"bq://{table}", metadata={"params": params},
                             root=Path(data_root) / "raw")
    except Exception:
        pass


def _mark_complete(data_root, template: str, refresh: str, payload_hash: str, count: int) -> None:
    proot = _parquet_root(data_root)
    if proot is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    _parquet.write_rows("ingestion_checkpoints", [{
        "pipeline": _CHECKPOINT_PIPELINE, "source": "bigquery",
        "key": f"{template}|{refresh}", "payload_hash": payload_hash,
        "status": "complete", "record_count": count,
        "started_at": now, "finished_at": now, "parser_version": _COLLECTOR_VERSION,
        "last_key": refresh, "error": None, "totals_json": "{}",
    }], root=proot)


def _enumerate_refreshes(table: str, start_date: str, end_date: str, executor, data_root) -> list | None:
    """Known refresh_date partitions for one table; None when the executor errors."""
    result = _submit("trends_refreshes", {
        "table": table, "start_date": start_date, "end_date": end_date,
        "limit": _MAX_LIMIT, "collector_version": _COLLECTOR_VERSION,
        "sql_version": _SQL_VERSION}, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return None
    refreshes = sorted({str(r.get("refresh_date")) for r in (result.get("rows") or [])
                        if isinstance(r, dict) and r.get("refresh_date")})
    return refreshes


def _rollup_national(rows: list) -> list:
    """Aggregate DMA rows at the latest refresh into geo=US observations."""
    if not rows:
        return rows
    latest = max(str(r.get("_refresh", "")) for r in rows)
    fresh = [r for r in rows if str(r.get("_refresh", "")) == latest]
    grouped: dict = {}
    for row in fresh:
        key = (str(row.get("_week")), str(row.get("term")), str(row.get("_kind")))
        bucket = grouped.setdefault(key, [])
        bucket.append(row)
    rolled = []
    for (week, term, kind), bucket in grouped.items():
        ranks = [b.get("rank") for b in bucket if isinstance(b.get("rank"), int)]
        scores = [b.get("score") for b in bucket if isinstance(b.get("score"), int)]
        gains = [b.get("percent_gain") for b in bucket if isinstance(b.get("percent_gain"), int)]
        table = bucket[0].get("_table")
        first = bucket[0]
        rolled.append({
            "_table": table, "_week": week, "_refresh": latest,
            "_geo": "US", "_kind": kind, "_template": first.get("_template"),
            "table": table, "week": week, "refresh_date": latest,
            "geo": "US", "term": term, "list_kind": kind,
            "rank": min(ranks) if ranks else None,
            "score": max(scores) if scores else None,
            "percent_gain": max(gains) if gains else None,
            "dma_id": None, "country_name": None, "region_name": None,
            "source_record_id": f"{table}|{latest}|US|{term}|{kind}",
            "job_id": first.get("job_id"),
            "dma_count": len(bucket),
        })
    return rolled


def collect_trends(*, start_date, end_date, geos, limit=100,
                   data_root=None, executor=None, week_start=None,
                   week_end=None, interval="daily") -> dict:
    """Collect top/rising lists; each row becomes an idempotent candidate."""
    if isinstance(geos, str):
        geos = [geos]
    geos = list(geos or [])
    if not _bq_ready():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    bad = _check_bq_limits()
    if bad:
        return bad
    if (interval or "daily") == "hourly":
        return {"status": "unavailable", "source": SOURCE,
                "error": "hourly trends tables are not allowlisted",
                "error_type": "source_unavailable",
                "coverage": {"reason": "hourly_unavailable"}}
    if (interval or "daily") != "daily":
        return {"status": "error", "source": SOURCE,
                "error": f"invalid interval: {interval!r} (daily)",
                "error_type": "invalid_params"}
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return {"status": "error", "source": SOURCE,
                    "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                    "error_type": "invalid_params"}
    if start_date > end_date:
        return {"status": "error", "source": SOURCE,
                "error": "start_date after end_date", "error_type": "invalid_params"}
    span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    if span > _MAX_SPAN_DAYS:
        return {"status": "error", "source": SOURCE,
                "error": f"date span {span}d exceeds {_MAX_SPAN_DAYS}d refresh bound",
                "error_type": "invalid_params"}
    for label, value in (("week_start", week_start), ("week_end", week_end)):
        if value is not None and (not isinstance(value, str) or not _DATE_RE.match(value)):
            return {"status": "error", "source": SOURCE,
                    "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                    "error_type": "invalid_params"}
    if week_start and week_end and week_start > week_end:
        return {"status": "error", "source": SOURCE,
                "error": "week_start after week_end", "error_type": "invalid_params"}
    if not geos:
        return {"status": "error", "source": SOURCE,
                "error": "at least one geo is required", "error_type": "invalid_params"}
    if len(geos) > _MAX_GEOS:
        return {"status": "error", "source": SOURCE,
                "error": f"{len(geos)} geos exceeds bound {_MAX_GEOS}",
                "error_type": "invalid_params"}
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}
    limit = max(1, min(limit, _MAX_LIMIT))
    week_start = week_start or _WIDE_WEEK_START
    week_end = week_end or _WIDE_WEEK_END

    groups = _plan_groups(geos)
    templates = [t for g in groups
                 for t in (_US_TEMPLATES if g["kind"] == "us" else _INTL_TEMPLATES)]
    completed = _completed_refreshes(data_root, templates) if data_root is not None else set()

    merged: dict = {}
    used_templates: list = []
    refresh_seen: set = set()
    fetched: list = []
    for group in groups:
        pair = _US_TEMPLATES if group["kind"] == "us" else _INTL_TEMPLATES
        for template in pair:
            table = _template_table(template)
            refreshes = _enumerate_refreshes(table, start_date, end_date, executor, data_root)
            if refreshes is None:
                enum_result = {"error": "refresh enumeration failed",
                               "error_type": "source_unavailable", "source": "bigquery"}
                return _wrap_error(enum_result)
            if not refreshes:
                refreshes = [end_date]
            for refresh in refreshes:
                refresh_seen.add(refresh)
                if (template, refresh) in completed:
                    cached_rows = _warehouse_rows(data_root, table, refresh)
                    if cached_rows:
                        for cached in cached_rows:
                            crefresh = str((cached.get("metrics") or {}).get("refresh_date") or "")
                            if not crefresh:
                                parts = str(cached.get("source_record_id") or "").split("|")
                                if len(parts) >= 2 and parts[0] == cached.get("table"):
                                    crefresh = parts[1]
                            if not crefresh:
                                crefresh = str(cached.get("week") or cached.get("period") or refresh)
                            key = (cached["table"], crefresh,
                                   str(cached.get("week") or cached.get("period")),
                                   str(cached["geo"]), str(cached["term"]), str(cached["list_kind"]))
                            merged.setdefault(key, cached)
                        if template not in used_templates:
                            used_templates.append(template)
                        continue
                    # Checkpointed but warehouse empty: fall through to a fresh fetch.
                if group["kind"] == "us":
                    params = {"start_date": refresh, "end_date": refresh,
                              "dmas": [] if group["national"] else group["dmas"],
                              "all_dmas": bool(group["national"]),
                              "week_start": week_start, "week_end": week_end,
                              "limit": limit, "collector_version": _COLLECTOR_VERSION,
                              "sql_version": _SQL_VERSION}
                else:
                    params = {"start_date": refresh, "end_date": refresh,
                              "country_code": group["country"], "region_codes": [],
                              "week_start": week_start, "week_end": week_end,
                              "limit": limit, "collector_version": _COLLECTOR_VERSION,
                              "sql_version": _SQL_VERSION}
                result = _submit(template, params, executor, data_root)
                if not isinstance(result, dict) or "error" in result:
                    return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
                rows = result.get("rows", []) or []
                job_id = result.get("job_id")
                if data_root is not None:
                    _archive_raw(data_root, template, job_id or template, table, params, rows)
                    payload_hash = hashlib.sha256(json.dumps(
                        rows, sort_keys=True, default=str).encode()).hexdigest()
                if template not in used_templates:
                    used_templates.append(template)
                tables_seen = set()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row = dict(row)
                    row["_template"] = template
                    row["_table"] = row.get("table") or table
                    row["_refresh"] = str(row.get("refresh_date") or refresh)
                    row["_week"] = str(row.get("week") or row.get("period")
                                       or row.get("source_period") or refresh)
                    row["_kind"] = (row.get("list_kind") or row.get("list")
                                    or _TEMPLATE_LIST_KIND.get(template, "top"))
                    if group["kind"] == "us":
                        row["_geo"] = row.get("dma_name") or row.get("geo")
                    else:
                        region = row.get("region_code") or ""
                        row["_geo"] = (row.get("country_code") or group["country"])
                        if region:
                            row["_geo"] += f":{region}"
                        if not row.get("country_code"):
                            row["_geo"] = row.get("geo") or row["_geo"]
                    row["job_id"] = job_id
                    key = (row["_table"], row["_refresh"], row["_week"], str(row["_geo"]),
                           str(row.get("term")), row["_kind"])
                    merged.setdefault(key, row)
                    tables_seen.add(row["_table"])
                if data_root is not None:
                    fetched.append((template, refresh, payload_hash, len(rows),
                                    sorted(tables_seen)))

    # Cached warehouse rows carry plain dicts (no _-markers); normalize keys.
    def _as_staged(row: dict) -> dict:
        staged_row = dict(row)
        if "_table" not in staged_row:
            staged_row["_table"] = staged_row.get("table")
            staged_row["_week"] = str(staged_row.get("week") or staged_row.get("period"))
            _parts = str(staged_row.get("source_record_id") or "").split("|")
            _id_refresh = _parts[1] if len(_parts) >= 2 and _parts[0] == staged_row.get("table") else ""
            staged_row["_refresh"] = str((staged_row.get("metrics") or {}).get("refresh_date")
                                         or _id_refresh or staged_row.get("period"))
            staged_row["_kind"] = staged_row.get("list_kind")
            staged_row["_geo"] = staged_row.get("geo")
            staged_row["_cached"] = True
        return staged_row

    us_tables = {_template_table("trends_us_top"), _template_table("trends_us_rising"),
                 "trends_top", "trends_rising"}
    intl_tables = {_template_table("trends_intl_top"), _template_table("trends_intl_rising")}
    national = any(g.get("kind") == "us" and g.get("national") for g in groups)
    dma_set = {d for g in groups if g.get("kind") == "us" and not g.get("national")
               for d in (g.get("dmas") or [])}
    national_input, dma_rows, intl_rows = [], [], []
    for key, row in merged.items():
        table = row.get("_table") or row.get("table")
        staged_row = _as_staged(row)
        if not staged_row.get("term") or not staged_row.get("_geo"):
            return {"status": "error", "source": SOURCE,
                    "error": f"malformed trends row for key {key!r}",
                    "error_type": "malformed_row"}
        if table in us_tables:
            if national:
                national_input.append(staged_row)
            elif str(staged_row["_geo"]) in dma_set:
                dma_rows.append(staged_row)
        elif table in intl_tables:
            intl_rows.append(staged_row)

    staged_all = national_input + dma_rows + intl_rows
    final_geos = ({"US"} if national else set()) | {str(r["_geo"]) for r in dma_rows + intl_rows}
    periods_all = sorted({str(r["_week"]) for r in staged_all}) or [end_date]
    by_term: dict = {}
    for staged_row in staged_all:
        by_term.setdefault(staged_row.get("term"), []).append({
            "table": staged_row.get("_table"), "period": staged_row.get("_week"),
            "geo": staged_row.get("_geo"), "term": staged_row.get("term"),
            "list_kind": staged_row.get("_kind"), "rank": staged_row.get("rank"),
            "score": staged_row.get("score")})
    term_features = {term: _signals.compute_candidate_features(
        rows, periods_covered=periods_all, geos_covered=sorted(final_geos))
        for term, rows in by_term.items()}

    observations = []
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for row in _rollup_national(national_input):
        observations.append(_normalize_row(row, retrieved_at, data_root,
                                           features=term_features.get(row.get("term"))))
    for row in dma_rows + intl_rows:
        observations.append(_normalize_row(row, retrieved_at, data_root,
                                           features=term_features.get(row.get("term"))))

    continuation = len(observations) > limit
    if continuation:
        observations = observations[:limit]
    if data_root is not None and observations:
        try:
            _store_observations(data_root, observations, retrieved_at)
            for _template, _refresh, _hash, _count, _tables in fetched:
                for _table in _tables:
                    if not _warehouse_rows(data_root, _table, _refresh):
                        raise RuntimeError(f"warehouse verify failed for {_table}|{_refresh}")
            for _template, _refresh, _hash, _count, _tables in fetched:
                _mark_complete(data_root, _template, _refresh, _hash, _count)
        except Exception as exc:
            return _wrap_error({"error": f"{SOURCE} store failed: {exc}",
                                "error_type": "source_unavailable"})
    weeks = sorted({str(o["period"]) for o in observations})
    geos_covered = sorted({str(o["geo"]) for o in observations})
    return {"status": "ok", "source": SOURCE, "observations": observations,
            "rows": observations, "count": len(observations),
            "coverage": {"periods_covered": weeks, "geos_covered": geos_covered,
                         "templates": used_templates,
                         "refresh_dates": sorted(refresh_seen)},
            "continuation": continuation,
            "warnings": ["truncated"] if continuation else []}


def _normalize_row(row: dict, retrieved_at: str, data_root, features=None) -> dict:
    """Map one merged row to a persisted candidate observation."""
    table = row.get("_table") or row.get("table") or "trends"
    week = str(row.get("_week") or row.get("week") or row.get("period"))
    geo = str(row.get("_geo") if row.get("_geo") is not None else row.get("geo"))
    term = row.get("term")
    kind = row.get("_kind") or row.get("list_kind") or "top"
    refresh = str(row.get("_refresh") or row.get("refresh_date") or week)
    if row.get("_cached") and isinstance(row.get("metrics"), dict):
        # Warehouse roundtrip: reuse stored metrics/evidence verbatim so a
        # recollect re-hashes identically (durable dedup, first known_at kept).
        metrics = dict(row["metrics"])
        evidence = list(row.get("evidence") or [])
    else:
        metrics = {"rank": row.get("rank"), "score": row.get("score"),
                   "percent_gain": row.get("percent_gain"),
                   "refresh_date": refresh, "week": week,
                   "dma_id": row.get("dma_id"),
                   "dma_name": row.get("dma_name"),
                   "country_name": row.get("country_name"),
                   "country_code": row.get("country_code"),
                   "region_name": row.get("region_name"),
                   "region_code": row.get("region_code")}
        if row.get("dma_count") is not None:
            metrics["dma_count"] = row["dma_count"]
        evidence = [{"table": table, "row": {k: v for k, v in row.items() if not k.startswith("_")},
                     "job_id": row.get("job_id"), "template": row.get("_template")}]
    return _signals.normalize_candidate(
        table=table, period=week, geo=geo, term=term,
        list_kind=kind, rank=row.get("rank"), source=SOURCE,
        source_record_id=f"{table}|{refresh}|{geo}|{term}|{kind}",
        observed_at=week, entities=[],
        metrics=metrics, evidence=evidence, features=features,
        retrieved_at=row.get("retrieved_at") or retrieved_at,
        known_at=row.get("known_at"),
        # Warehouse is the durable store; JSONL stays a read-only legacy trail.
        data_root=None, persist=False)
