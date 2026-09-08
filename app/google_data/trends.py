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

import copy
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone, timedelta
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
_US_NATIONAL_TEMPLATES = ("trends_us_top_national", "trends_us_rising_national")
_INTL_NATIONAL_TEMPLATES = ("trends_intl_top_national", "trends_intl_rising_national")
_TEMPLATE_LIST_KIND = {
    "trends_us_top": "top", "trends_us_rising": "rising",
    "trends_intl_top": "top", "trends_intl_rising": "rising",
    "trends_us_top_national": "top", "trends_us_rising_national": "rising",
    "trends_intl_top_national": "top", "trends_intl_rising_national": "rising",
    "trends_top": "top", "trends_rising": "rising",
}
_FALLBACK_TABLES = {
    "trends_us_top": "bigquery-public-data.google_trends.top_terms",
    "trends_us_rising": "bigquery-public-data.google_trends.top_rising_terms",
    "trends_intl_top": "bigquery-public-data.google_trends.international_top_terms",
    "trends_intl_rising": "bigquery-public-data.google_trends.international_top_rising_terms",
    "trends_us_top_national": "bigquery-public-data.google_trends.top_terms",
    "trends_us_rising_national": "bigquery-public-data.google_trends.top_rising_terms",
    "trends_intl_top_national": "bigquery-public-data.google_trends.international_top_terms",
    "trends_intl_rising_national": "bigquery-public-data.google_trends.international_top_rising_terms",
}
_COLLECTOR_VERSION = "1"
_SQL_VERSION = "1"
_CHECKPOINT_PIPELINE = "google_trends"
_MAX_LIMIT = 1000
_FETCH_LIMIT = _MAX_LIMIT + 1
_MAX_GEOS = 50
_MAX_SPAN_DAYS = 31
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
    dmas = [g for g in geos if g != "US" and not _is_country_code(g)]
    countries = [g for g in geos if g != "US" and _is_country_code(g)]
    groups = []
    if "US" in geos:
        groups.append({"kind": "us", "national": True, "dmas": []})
    if dmas:
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


def _checkpoint_key(template: str, refresh: str, params: dict) -> str:
    """Scoped completion key for the exact canonical query submitted."""
    scope_hash = hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{template}|{refresh}|{scope_hash}"


def _observation_identity(table: str, period: str, geo: str, term: str,
                          list_kind: str, metrics: dict, evidence: list) -> tuple:
    """Durable (observation_id, content_hash) identity shared by store and verify."""
    content_hash = hashlib.sha256(
        json.dumps({"metrics": metrics, "evidence": evidence,
                    "collector_version": _COLLECTOR_VERSION},
                   sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    observation_id = hashlib.sha256(
        f"{SOURCE}|{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()
    return observation_id, content_hash


def _feature_scope_json(*, week_start: str, week_end: str, geos: list,
                        table: str, list_kind: str) -> dict:
    return {"week_start": week_start, "week_end": week_end, "geos": sorted(geos),
            "table": table, "list_kind": list_kind}


def _feature_scope_hash(scope: dict) -> str:
    return hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _series_basis(staged_row: dict) -> str:
    metrics = staged_row.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    basis = metrics.get("score_basis")
    if basis:
        return str(basis)
    if (staged_row.get("dma_count") is not None
            or staged_row.get("region_count") is not None
            or metrics.get("dma_count") is not None
            or metrics.get("region_count") is not None):
        return "mean_list_score_where_listed"
    return ""


def expected_inputs_hash(scope: dict, term: str, table: str, list_kind: str,
                          basis: str, geo: str, candidates: list) -> str:
    """Scope-complete input identity shared by writes, replay gating, and PIT reads."""
    geos = scope.get("geos") or []
    week_start = str(scope.get("week_start") or "")
    week_end = str(scope.get("week_end") or "")
    scope_table = str(scope.get("table") or "")
    scope_kind = str(scope.get("list_kind") or "")
    pairs = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if str(cand.get("table") or "") != scope_table:
            continue
        if str(cand.get("list_kind") or "") != scope_kind:
            continue
        if str(cand.get("term") or "") != str(term or ""):
            continue
        period = str(cand.get("period") or cand.get("week") or "")
        if not period or period < week_start or period > week_end:
            continue
        cgeo = str(cand.get("geo") or "")
        if cgeo.split(":")[0] not in geos:
            continue
        if _series_basis(cand) != str(basis or ""):
            continue
        pairs.append([str(cand.get("observation_id") or ""),
                      str(cand.get("content_hash") or "")])
    target_table = str(table or "")
    target_kind = str(list_kind or "")
    target_geo = str(geo or "")
    target_basis = str(basis or "")
    periods: set = set()
    if target_geo.split(":")[0] in geos or (not geos and not target_geo):
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            if str(cand.get("table") or "") != target_table:
                continue
            if str(cand.get("list_kind") or "") != target_kind:
                continue
            if str(cand.get("geo") or "") != target_geo:
                continue
            if _series_basis(cand) != target_basis:
                continue
            period = str(cand.get("period") or cand.get("week") or "")
            if not period or period < week_start or period > week_end:
                continue
            periods.add(period)
    periods_covered = sorted(periods)
    if target_geo in geos:
        geos_covered = sorted(geos)
    elif target_geo:
        geos_covered = [target_geo]
    else:
        geos_covered = []
    payload = {"source_pairs": sorted(pairs), "periods_covered": periods_covered,
               "geos_covered": geos_covered}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _completed_refreshes(data_root, templates: list) -> set:
    """Completed scoped checkpoint keys; missing warehouse reads as none."""
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
        template, _, _rest = key.partition("|")
        if template in wanted and key:
            done.add(key)
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
    collapsed: dict = {}
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
        try:
            metrics_canon = json.dumps(metrics, sort_keys=True, separators=(",", ":"),
                                       default=str)
            evidence_canon = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                                        default=str)
        except (TypeError, ValueError):
            continue
        key = (str(row.get("table") or table), str(row.get("period") or ""),
               str(row.get("geo") or ""), str(row.get("term") or ""),
               str(row.get("list_kind") or ""), str(row.get("source_record_id") or ""),
               metrics_canon, evidence_canon)
        known = str(row.get("known_at") or "")
        prev = collapsed.get(key)
        if prev is not None and str(prev.get("known_at") or "") <= known:
            continue
        collapsed[key] = {
            "table": row.get("table") or table,
            "period": row.get("period"), "week": row.get("period"),
            "geo": row.get("geo"), "term": row.get("term"),
            "list_kind": row.get("list_kind"), "rank": metrics.get("rank"),
            "source_record_id": row.get("source_record_id"),
            "observed_at": row.get("observed_at"),
            "known_at": row.get("known_at"), "retrieved_at": row.get("retrieved_at"),
            "metrics": metrics, "evidence": evidence,
            "features": None, "calc_version": row.get("calc_version") or "1",
        }
    return list(collapsed.values())


def _features_for(data_root, observation_id: str, scope_hash: str,
                  calc_version: str | None = None, expected_inputs_hash=None):
    proot = _parquet_root(data_root)
    if proot is None or _parquet is None:
        return None
    if calc_version is None:
        calc_version = _signals.CALC_VERSION
    try:
        rows = _parquet.read_table("google_signal_features", proot).to_pylist()
    except Exception:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("observation_id") or "") != str(observation_id):
            continue
        if str(row.get("feature_scope_hash") or "") != str(scope_hash):
            continue
        if str(row.get("calc_version") or "") != str(calc_version):
            continue
        if expected_inputs_hash is not None and str(row.get("inputs_hash") or "") != str(expected_inputs_hash):
            continue
        raw = row.get("features_json")
        if raw is None or raw == "":
            return None
        try:
            decoded = json.loads(raw)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _scope_geos_from_params(params: dict) -> list:
    if params.get("national"):
        if "country_code" not in params:
            return ["US"]
        return [str(params.get("country_code") or "")]
    if "all_dmas" in params:
        if params.get("all_dmas"):
            return ["US"]
        return sorted(params.get("dmas") or [])
    if "country_code" in params:
        return [str(params.get("country_code") or "")]
    return []


def _requested_scope(template: str, params: dict) -> tuple:
    table = _template_table(template)
    scope = _feature_scope_json(
        week_start=str(params.get("week_start") or ""),
        week_end=str(params.get("week_end") or ""),
        geos=_scope_geos_from_params(params),
        table=table, list_kind=_TEMPLATE_LIST_KIND.get(template, "top"))
    return scope, _feature_scope_hash(scope)


def _requested_scope_hash(template: str, params: dict) -> str:
    return _requested_scope(template, params)[1]


def backfill_legacy_feature_rows(data_root) -> int:
    if data_root is None or _parquet is None:
        return 0
    proot = _parquet_root(data_root)
    if proot is None:
        return 0
    marker = Path(data_root) / "google_data" / ".signal_features_backfilled"
    try:
        if marker.exists():
            return 0
    except OSError:
        pass
    try:
        rows = _parquet.read_table("google_observations", proot).to_pylist()
    except Exception:
        rows = []
    feature_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row.get("features_json")
        if raw is None or raw == "":
            continue
        try:
            decoded = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(decoded, dict):
            continue
        feature_rows.append({
            "observation_id": str(row.get("observation_id") or ""),
            "feature_scope_hash": "legacy-unknown",
            "feature_scope_json": json.dumps(
                {"legacy": True, "reason": "pre-scope-backfill"}),
            "features_json": json.dumps(decoded, sort_keys=True, default=str),
            "calc_version": str(row.get("calc_version") or "1"),
            "calculated_at": str(row.get("known_at") or ""),
            "inputs_hash": "",
        })
    written = 0
    if feature_rows:
        try:
            written = _parquet.write_rows("google_signal_features", feature_rows, root=proot)
        except Exception:
            return 0
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("backfilled\n")
    except OSError:
        pass
    return written


def _cache_in_scope(cached: dict, params: dict) -> bool:
    """Keep only cached rows matching the current canonical query scope."""
    week = str(cached.get("week") or cached.get("period") or "")
    if week < str(params.get("week_start") or "") or week > str(params.get("week_end") or ""):
        return False
    geo = str(cached.get("geo") or "")
    if params.get("national"):
        if "country_code" not in params:
            return geo == str(params["national"])
        return geo == str(params.get("country_code"))
    if "all_dmas" in params:
        if params.get("all_dmas"):
            return geo == "US"
        return geo in set(params.get("dmas") or [])
    if "country_code" in params:
        country = str(params.get("country_code") or "")
        return geo == country or geo.startswith(country + ":")
    return True


def _store_observations(data_root, observations: list, retrieved_at: str) -> dict:
    """Write normalized observations; return expected identities per (table, refresh)."""
    proot = _parquet_root(data_root)
    if proot is None:
        return {}
    try:
        backfill_legacy_feature_rows(data_root)
    except Exception:
        pass
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
    expected: dict = {}
    for obs in observations:
        metrics = dict(obs.get("metrics") or {})
        evidence = list(obs.get("evidence") or [])
        observation_id, content_hash = _observation_identity(
            obs["table"], obs["period"], obs["geo"], obs["term"], obs["list_kind"],
            metrics, evidence)
        refresh_date = str(metrics.get("refresh_date") or "")
        if not refresh_date:
            parts = str(obs.get("source_record_id") or "").split("|")
            if len(parts) >= 2 and parts[0] == obs.get("table"):
                refresh_date = parts[1]
        expected.setdefault((obs["table"], refresh_date), set()).add(
            (observation_id, content_hash))
        known_at = first_known.get((observation_id, content_hash), retrieved_at)
        warehouse_rows.append({
            "observation_id": observation_id, "source": SOURCE, "table": obs["table"],
            "term": obs["term"], "geo": obs["geo"], "list_kind": obs["list_kind"],
            "period": obs["period"], "observed_at": obs.get("observed_at") or obs["period"],
            "known_at": known_at, "retrieved_at": retrieved_at,
            "source_record_id": obs.get("source_record_id") or observation_id,
            "content_hash": content_hash, "collector_version": _COLLECTOR_VERSION,
            "calc_version": _COLLECTOR_VERSION,
            "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
            "features_json": None,
            "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
            "source_url": f"bq://{obs['table']}",
        })
    _parquet.write_rows("google_observations", warehouse_rows, root=proot)
    return expected


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


def _archived_rows(data_root, template: str, job_id: str, params: dict) -> list | None:
    if data_root is None or _raw_archive is None:
        return None
    best: list | None = None
    try:
        records = _raw_archive.iter_archive(
            "google", template, job_id, root=Path(data_root) / "raw")
        for record in records:
            try:
                if (record.metadata or {}).get("params") != params:
                    continue
                payload = json.loads(record.payload_path.read_bytes())
            except Exception:
                continue
            if not isinstance(payload, list):
                continue
            if best is None or len(payload) > len(best):
                best = payload
    except Exception:
        return best
    return best


def _mark_complete(data_root, checkpoint_key: str, refresh: str, payload_hash: str, count: int) -> None:
    proot = _parquet_root(data_root)
    if proot is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    _parquet.write_rows("ingestion_checkpoints", [{
        "pipeline": _CHECKPOINT_PIPELINE, "source": "bigquery",
        "key": checkpoint_key, "payload_hash": payload_hash,
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
    if week_start is None and week_end is None:
        week_end = end_date
        week_start = (date.fromisoformat(end_date) - timedelta(days=13)).isoformat()
    elif week_start is None:
        week_start = (date.fromisoformat(week_end) - timedelta(days=13)).isoformat()
    elif week_end is None:
        week_end = (date.fromisoformat(week_start) + timedelta(days=13)).isoformat()

    def _pair_for(group: dict) -> tuple:
        if group["kind"] == "us":
            return _US_NATIONAL_TEMPLATES if group.get("national") else _US_TEMPLATES
        return _INTL_NATIONAL_TEMPLATES
    groups = _plan_groups(geos)
    templates = [t for g in groups for t in _pair_for(g)]
    completed = _completed_refreshes(data_root, templates) if data_root is not None else set()

    merged: dict = {}
    used_templates: list = []
    refresh_seen: set = set()
    fetched: list = []
    for group in groups:
        pair = _pair_for(group)
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
                if group["kind"] == "us" and group.get("national"):
                    params = {"start_date": refresh, "end_date": refresh,
                              "week_start": week_start, "week_end": week_end,
                              "limit": _FETCH_LIMIT, "collector_version": _COLLECTOR_VERSION,
                              "sql_version": _SQL_VERSION, "national": "US"}
                elif group["kind"] == "us":
                    params = {"start_date": refresh, "end_date": refresh,
                              "dmas": sorted(group["dmas"]),
                              "all_dmas": False,
                              "week_start": week_start, "week_end": week_end,
                              "limit": _FETCH_LIMIT, "collector_version": _COLLECTOR_VERSION,
                              "sql_version": _SQL_VERSION}
                else:
                    params = {"start_date": refresh, "end_date": refresh,
                              "country_code": group["country"],
                              "week_start": week_start, "week_end": week_end,
                              "limit": _FETCH_LIMIT, "collector_version": _COLLECTOR_VERSION,
                              "sql_version": _SQL_VERSION, "national": group["country"]}
                checkpoint_key = _checkpoint_key(template, refresh, params)
                if checkpoint_key in completed:
                    cached_rows = [c for c in _warehouse_rows(data_root, table, refresh)
                                   if _cache_in_scope(c, params)]
                    if len(cached_rows) > _MAX_LIMIT:
                        return {"status": "unavailable", "source": SOURCE,
                                "reason": "query_scope_too_large",
                                "error": f"{template} query scope exceeds 1000 rows for {refresh}",
                                "error_type": "missing_coverage"}
                    cached_rows.sort(key=lambda c: str(c.get("source_record_id") or ""))
                    cached_rows.sort(key=lambda c: (c.get("rank") if isinstance(
                        c.get("rank"), (int, float)) else float("inf")))
                    cached_rows.sort(key=lambda c: str(c.get("week") or c.get("period") or ""),
                                     reverse=True)
                    cached_rows = cached_rows[:_MAX_LIMIT]
                    if cached_rows:
                        scope, scope_hash = _requested_scope(template, params)
                        _gate_cands = []
                        _gate_ids = []
                        _gate_ok = True
                        for _c in cached_rows:
                            try:
                                _oid, _ch = _observation_identity(
                                    _c.get("table") or table,
                                    str(_c.get("period") or _c.get("week") or ""),
                                    str(_c.get("geo") or ""), str(_c.get("term") or ""),
                                    str(_c.get("list_kind") or ""),
                                    _c.get("metrics") or {}, _c.get("evidence") or [])
                            except Exception:
                                _gate_ok = False
                                break
                            _gate_ids.append((_c, _oid))
                            _gate_cands.append({
                                "observation_id": _oid, "content_hash": _ch,
                                "table": _c.get("table") or table,
                                "period": str(_c.get("period") or _c.get("week") or ""),
                                "geo": str(_c.get("geo") or ""),
                                "term": str(_c.get("term") or ""),
                                "list_kind": str(_c.get("list_kind") or ""),
                                "metrics": _c.get("metrics") or {}})
                        gated = []
                        if _gate_ok:
                            for _c, _oid in _gate_ids:
                                _cand = {"table": _c.get("table") or table,
                                         "period": str(_c.get("period") or _c.get("week") or ""),
                                         "geo": str(_c.get("geo") or ""),
                                         "term": str(_c.get("term") or ""),
                                         "list_kind": str(_c.get("list_kind") or ""),
                                         "metrics": _c.get("metrics") or {}}
                                _expected = expected_inputs_hash(
                                    scope, str(_c.get("term") or ""),
                                    str(_c.get("table") or table),
                                    str(_c.get("list_kind") or ""),
                                    _series_basis(_cand), str(_c.get("geo") or ""), _gate_cands)
                                if _features_for(data_root, _oid, scope_hash,
                                                  expected_inputs_hash=_expected) is not None:
                                    gated.append(_c)
                        if len(gated) != len(cached_rows):
                            pass
                        else:
                            for cached in gated:
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
                    # Checkpointed but warehouse empty/out of scope: fall through to a fresh fetch.
                result = _submit(template, params, executor, data_root)
                if not isinstance(result, dict) or "error" in result:
                    return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
                cached = bool(result.get("cached"))
                rows = result.get("rows", []) or []
                job_id = result.get("job_id")
                if cached and not rows:
                    recovered = _archived_rows(data_root, template, job_id or template, params)
                    if recovered is None:
                        return _wrap_error({"error": f"cached Trends result unavailable for {template}|{refresh}; refusing to checkpoint",
                                            "error_type": "source_unavailable", "source": "bigquery"})
                    rows = recovered
                if data_root is not None and not cached:
                    _archive_raw(data_root, template, job_id or template, table, params, rows)
                if data_root is not None:
                    payload_hash = hashlib.sha256(json.dumps(
                        rows, sort_keys=True, default=str).encode()).hexdigest()
                if len(rows) > _MAX_LIMIT:
                    return {"status": "unavailable", "source": SOURCE,
                            "reason": "query_scope_too_large",
                            "error": f"{template} query scope exceeds 1000 rows for {refresh}",
                            "error_type": "missing_coverage"}
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
                    if template in _US_NATIONAL_TEMPLATES:
                        row["_geo"] = "US"
                    elif template in _INTL_NATIONAL_TEMPLATES:
                        row["_geo"] = (row.get("country_code") or group["country"]
                                       or row.get("geo"))
                    elif group["kind"] == "us":
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
                    fetched.append((checkpoint_key, template, refresh, payload_hash, len(rows),
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
    national_templates = _US_NATIONAL_TEMPLATES + _INTL_NATIONAL_TEMPLATES
    national_rows, dma_rows, intl_rows = [], [], []
    for key, row in merged.items():
        table = row.get("_table") or row.get("table")
        staged_row = _as_staged(row)
        if not staged_row.get("term") or not staged_row.get("_geo"):
            return {"status": "error", "source": SOURCE,
                    "error": f"malformed trends row for key {key!r}",
                    "error_type": "malformed_row"}
        if row.get("_template") in national_templates:
            national_rows.append(staged_row)
        elif table in us_tables:
            if national and str(staged_row["_geo"]) == "US":
                national_rows.append(staged_row)
            elif str(staged_row["_geo"]) in dma_set:
                dma_rows.append(staged_row)
        elif table in intl_tables:
            intl_rows.append(staged_row)

    staged_all = national_rows + dma_rows + intl_rows
    winners: dict = {}
    for staged_row in staged_all:
        wkey = (staged_row.get("_table"), staged_row.get("_kind"),
                staged_row.get("_geo"), staged_row.get("term"), staged_row.get("_week"))
        prev = winners.get(wkey)
        if prev is None or str(staged_row.get("_refresh") or "") > str(prev.get("_refresh") or ""):
            winners[wkey] = staged_row

    def _series_score(staged_row: dict):
        score = staged_row.get("score")
        if isinstance(score, bool):
            score = None
        if score is None:
            metrics = staged_row.get("metrics")
            if isinstance(metrics, dict):
                score = metrics.get("score")
        return score

    def _series_rank(staged_row: dict):
        rank = staged_row.get("rank")
        if rank is None:
            metrics = staged_row.get("metrics")
            if isinstance(metrics, dict):
                rank = metrics.get("rank")
        return rank

    by_series: dict = {}
    periods_by_series: dict = {}
    for staged_row in winners.values():
        basis = _series_basis(staged_row)
        skey = (staged_row.get("term"), staged_row.get("_table"), staged_row.get("_kind"),
                staged_row.get("_geo"), basis)
        by_series.setdefault(skey, []).append({
            "table": staged_row.get("_table"), "period": staged_row.get("_week"),
            "geo": staged_row.get("_geo"), "term": staged_row.get("term"),
            "list_kind": staged_row.get("_kind"), "rank": _series_rank(staged_row),
            "score": _series_score(staged_row)})
        pkey = (staged_row.get("_table"), staged_row.get("_kind"), staged_row.get("_geo"), basis)
        periods_by_series.setdefault(pkey, set()).add(str(staged_row.get("_week")))
    series_features: dict = {}
    for skey, rows in by_series.items():
        _term, _table, _kind, _geo, _basis = skey
        pkey = (_table, _kind, _geo, _basis)
        periods = sorted(periods_by_series.get(pkey) or set())
        series_features[skey] = _signals.compute_candidate_features(rows, periods_covered=periods)
    if dma_set:
        dma_groups: dict = {}
        for staged_row in winners.values():
            if str(staged_row.get("_geo")) not in dma_set:
                continue
            basis = _series_basis(staged_row)
            gkey = (staged_row.get("term"), staged_row.get("_table"),
                    staged_row.get("_kind"), basis)
            dma_groups.setdefault(gkey, []).append({
                "table": staged_row.get("_table"), "period": staged_row.get("_week"),
                "geo": staged_row.get("_geo"), "term": staged_row.get("term"),
                "list_kind": staged_row.get("_kind"), "rank": _series_rank(staged_row),
                "score": _series_score(staged_row)})
        for gkey, rows in dma_groups.items():
            diff = _signals.compute_candidate_features(rows, geos_covered=sorted(dma_set))
            for skey in [k for k in series_features
                         if k[0] == gkey[0] and k[1] == gkey[1] and k[2] == gkey[2]
                         and k[4] == gkey[3] and str(k[3]) in dma_set]:
                feat = series_features[skey]
                feat["diffusion"] = diff.get("diffusion")
                feat["rules"]["diffusion"] = dict(diff.get("rules", {}).get("diffusion", {}))
                feat["coverage"]["geos_covered"] = list(
                    diff.get("coverage", {}).get("geos_covered", []))
    observations = []
    retrieved_at = datetime.now(timezone.utc).isoformat()
    for row in national_rows + dma_rows + intl_rows:
        basis = _series_basis(row)
        skey = (row.get("term"), row.get("_table"), row.get("_kind"), row.get("_geo"), basis)
        features = series_features.get(skey)
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        is_aggregate = (row.get("dma_count") is not None
                        or row.get("region_count") is not None
                        or metrics.get("dma_count") is not None
                        or metrics.get("region_count") is not None)
        if is_aggregate and isinstance(features, dict):
            features = copy.deepcopy(features)
            features["diffusion"] = None
            features["rules"]["diffusion"]["value"] = None
            features["coverage"]["missing"]["diffusion"] = "subregion rows aggregated"
        observations.append(_normalize_row(row, retrieved_at, data_root, features=features))

    continuation = len(observations) > limit
    if data_root is not None and (observations or fetched):
        try:
            expected = _store_observations(data_root, observations, retrieved_at)
            proot = _parquet_root(data_root)
            if proot is not None and _parquet is not None:
                infos = []
                for _obs in observations:
                    _feats = _obs.get("features")
                    if not isinstance(_feats, dict):
                        continue
                    _metrics = dict(_obs.get("metrics") or {})
                    _evidence = list(_obs.get("evidence") or [])
                    _oid, _ch = _observation_identity(
                        _obs["table"], _obs["period"], _obs["geo"], _obs["term"],
                        _obs["list_kind"], _metrics, _evidence)
                    _geo = str(_obs.get("geo") or "")
                    if _geo == "US":
                        _geos = ["US"]
                    elif _geo in dma_set:
                        _geos = sorted(dma_set)
                    else:
                        _geos = [_geo.split(":")[0]]
                    _scope = _feature_scope_json(
                        week_start=str(week_start or ""), week_end=str(week_end or ""),
                        geos=_geos, table=str(_obs.get("table") or ""),
                        list_kind=str(_obs.get("list_kind") or ""))
                    _shash = _feature_scope_hash(_scope)
                    infos.append((_obs, _oid, _ch, _scope, _shash, _series_basis(_obs), _feats))
                if infos:
                    _candidates = [{
                        "observation_id": _oid, "content_hash": _ch,
                        "table": str(_obs.get("table") or ""),
                        "period": str(_obs.get("period") or ""),
                        "geo": str(_obs.get("geo") or ""),
                        "term": str(_obs.get("term") or ""),
                        "list_kind": str(_obs.get("list_kind") or ""),
                        "metrics": _obs.get("metrics") or {},
                    } for _obs, _oid, _ch, _, _, _, _ in infos]
                    _empty_hash = expected_inputs_hash({}, "", "", "", "", "", [])
                    try:
                        _existing = _parquet.read_table(
                            "google_signal_features", proot).to_pylist()
                    except Exception:
                        _existing = []
                    _first_calc: dict = {}
                    for _row in _existing:
                        if not isinstance(_row, dict):
                            continue
                        _k = (str(_row.get("observation_id") or ""),
                              str(_row.get("feature_scope_hash") or ""),
                              str(_row.get("calc_version") or ""),
                              str(_row.get("inputs_hash") or ""))
                        _c = str(_row.get("calculated_at") or "")
                        if _k not in _first_calc or _c < _first_calc[_k]:
                            _first_calc[_k] = _c
                    _feature_rows = []
                    for _obs, _oid, _ch, _scope, _shash, _basis, _feats in infos:
                        _expected = expected_inputs_hash(
                            _scope, str(_obs.get("term") or ""),
                            str(_obs.get("table") or ""),
                            str(_obs.get("list_kind") or ""), _basis, str(_obs.get("geo") or ""), _candidates)
                        if _expected == _empty_hash:
                            continue
                        _k = (_oid, _shash, _signals.CALC_VERSION, _expected)
                        _calc_at = _first_calc.get(_k, retrieved_at)
                        if _calc_at > retrieved_at:
                            _calc_at = retrieved_at
                        _feature_rows.append({
                            "observation_id": _oid,
                            "feature_scope_hash": _shash,
                            "feature_scope_json": json.dumps(_scope, sort_keys=True),
                            "features_json": json.dumps(_feats, sort_keys=True, default=str),
                            "calc_version": _signals.CALC_VERSION,
                            "calculated_at": _calc_at,
                            "inputs_hash": _expected,
                        })
                    _parquet.write_rows("google_signal_features", _feature_rows, root=proot)
            for _key, _template, _refresh, _hash, _count, _tables in fetched:
                for _table in _tables:
                    actual = set()
                    for _cached in _warehouse_rows(data_root, _table, _refresh):
                        _metrics = _cached.get("metrics") or {}
                        _evidence = _cached.get("evidence") or []
                        actual.add(_observation_identity(
                            _cached.get("table") or _table,
                            str(_cached.get("period") or _cached.get("week") or ""),
                            str(_cached.get("geo") or ""), str(_cached.get("term") or ""),
                            str(_cached.get("list_kind") or ""), _metrics, _evidence))
                    if not expected.get((_table, _refresh), set()) <= actual:
                        raise RuntimeError(f"warehouse verify failed for {_table}|{_refresh}")
            for _key, _template, _refresh, _hash, _count, _tables in fetched:
                _mark_complete(data_root, _key, _refresh, _hash, _count)
        except Exception as exc:
            return _wrap_error({"error": f"{SOURCE} store failed: {exc}",
                                "error_type": "source_unavailable"})
    weeks = sorted({str(o["period"]) for o in observations})
    geos_covered = sorted({str(o["geo"]) for o in observations})
    returned_observations = observations[:limit] if continuation else observations
    return {"status": "ok", "source": SOURCE, "observations": returned_observations,
            "rows": returned_observations, "count": len(returned_observations),
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
        if row.get("region_count") is not None:
            metrics["region_count"] = row["region_count"]
        if row.get("dma_count") is not None or row.get("region_count") is not None:
            metrics["score_basis"] = "mean_list_score_where_listed"
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
