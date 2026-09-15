"""Bounded Census/NOAA context through allowlisted BigQuery templates.

Census detail runs only through the wide ACS vintages
(``county_2020_5yr``/``state_2020_5yr``/``censustract_2020_5yr``); vintage
lives in the table name, there is no ``vintage`` column. Population-style
basics stay on Data Commons; this path is for detail absent there. NOAA runs
only through year-sharded GSOD plus explicit station IDs. Sentinel
fill values become null; units are reported verbatim, never converted.
Missing geos yield missing-coverage; severe-storm damage queries stay
unavailable until a Marketplace table is confirmed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from ._guards import result_rows
from ._lazy_config import get_data_root_or_cwd, google_data_enabled

SOURCE = "geo_context"
_CENSUS_TEMPLATE = "census_acs"
_NOAA_TEMPLATE = "noaa_obs"
_VINTAGE_TTL_DAYS = 30
_NOAA_HINTS = ("NOAA", "STATION", "TEMP", "PRCP", "WEATHER", "CLIMATE", "GHCN")

# Mirrors the bigquery_client interpolation allowlists; unknown values fail
# closed with the available list, never SELECT *.
_ACS_SUFFIXES = ("county_2020_5yr", "state_2020_5yr", "censustract_2020_5yr")
_ACS_COLUMNS = ("total_pop", "median_age", "median_income", "median_home_value")
_ACS_UNITS = {"total_pop": "people", "median_age": "years",
              "median_income": "USD", "median_home_value": "USD"}
# ACS reserve codes: not reported, uninhabited, or withheld.
_ACS_SENTINELS = frozenset({-666666666, -999999999, -888888888})

class _Submitter(Protocol):
    """Anything submit_template-compatible: the real client or a test double."""
    def submit_template(self, template: str, params: dict[str, object]) -> dict[str, object]: ...


_Executor = Callable[[str, dict[str, object]], dict[str, object]] | _Submitter


# GSOD long-form metric columns with verbatim units (F/inches/knots/millibars/miles).
_NOAA_UNITS = {"temp": "Fahrenheit", "dewp": "Fahrenheit",
               "max": "Fahrenheit", "min": "Fahrenheit",
               "prcp": "inches", "sndp": "inches",
               "visib": "miles", "wdsp": "knots", "mxspd": "knots", "gust": "knots",
               "slp": "millibars", "stp": "millibars",
               "frshtt": "flags"}
# GSOD missing-value sentinels per magnitude family.
_NOAA_SENTINELS = frozenset({9999.9, 99.99, 999.9})


def _data_enabled() -> bool:
    return google_data_enabled()


def _resolve_root(data_root: Path | None = None) -> Path:
    if data_root:
        return Path(data_root)
    return get_data_root_or_cwd()


def _submit_failure(exc: Exception) -> dict[str, object]:
    """Fixed-shape executor failure; LedgerCorrupt is never wrapped here."""
    return {"status": "error", "source": SOURCE,
            "error": f"{SOURCE} query failed: {exc}", "error_type": "executor_error"}


def _submit_via_client(template: str, params: dict[str, object],
                       data_root: Path | None) -> dict[str, object]:
    """Submit through the real BigQuery client; import failure stays fixed-shape."""
    try:
        from . import bigquery_client as _bq
    except ImportError:
        return {"error": "bigquery client unavailable",
                "error_type": "source_unavailable", "source": "bigquery"}
    try:
        return _bq.submit_template(template, params, data_root=data_root)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return _submit_failure(exc)


def _direct_submit(template: str, params: dict[str, object],
                   executor: _Executor) -> dict[str, object]:
    """Submit through a caller-provided callable or test-double client."""
    try:
        if callable(executor):
            return executor(template, params)
        return executor.submit_template(template, params)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return _submit_failure(exc)


def _submit(template: str, params: dict[str, object], executor: _Executor | None,
            data_root: Path | None) -> dict[str, object]:
    if executor is None:
        return _submit_via_client(template, params, data_root)
    return _direct_submit(template, params, executor)


def _looks_noaa(variables: list[str]) -> bool:
    return any(any(hint in v.upper() for hint in _NOAA_HINTS) for v in variables)


def _vintage_key(template: str, geo_ids: list[str], variables: list[str],
                 extra: dict[str, str]) -> str:
    blob = json.dumps({"t": template, "g": sorted(map(str, geo_ids)),
                       "v": sorted(map(str, variables)),
                       "x": {k: v for k, v in sorted(extra.items())}},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _project_name() -> str | None:
    """Configured BigQuery project or None; config first, env fallback."""
    try:
        from .. import config as _cfg
    except ImportError:
        return (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    project = _cfg.get_google_cloud_project()
    if project is None:
        project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    return project


def _disabled_error() -> dict[str, object]:
    """Fixed-shape error when google data is off or no project is set."""
    return {"status": "disabled", "source": SOURCE,
            "reason": "google data disabled or no BigQuery project"}


def _project_error() -> dict[str, object] | None:
    """Disabled error when data is off or no project, else None."""
    if not _data_enabled():
        return _disabled_error()
    if not _project_name():
        return _disabled_error()
    return None


def _normalize_inputs(geo_ids: list[str] | str | None,
                      variables: list[str] | str | None) -> tuple[list[str], list[str]]:
    """Caller geos/variables as plain lists."""
    geos = [geo_ids] if isinstance(geo_ids, str) else list(geo_ids or [])
    vars_ = [variables] if isinstance(variables, str) else list(variables or [])
    return geos, vars_


def _clamp_limit(limit: int) -> tuple[dict[str, object] | None, int]:
    """Clamped limit or (invalid-params error, 100)."""
    try:
        return None, max(1, min(limit, 100))
    except (TypeError, ValueError):
        return ({"status": "error", "source": SOURCE,
                 "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}, 100)


def _census_request(geo_ids: list[str], columns: list[str] | None,
                    variables: list[str], table_suffix: str,
                    limit: int) -> tuple[dict[str, object] | None,
                                        list[str], dict[str, object], dict[str, str], str | None]:
    """Census template params or (unavailable error, rest empty)."""
    if table_suffix not in _ACS_SUFFIXES:
        return ({"status": "unavailable", "source": SOURCE,
                 "error": f"unknown census table: {table_suffix!r}",
                 "error_type": "source_unavailable",
                 "available": list(_ACS_SUFFIXES)}, [], {}, {}, None)
    wanted = list(columns or variables or ["total_pop"])
    bad = [c for c in wanted if c not in _ACS_COLUMNS]
    if bad:
        return ({"status": "unavailable", "source": SOURCE,
                 "error": f"unknown census columns: {bad!r}",
                 "error_type": "source_unavailable",
                 "available": list(_ACS_COLUMNS)}, [], {}, {}, None)
    params: dict[str, object] = {"geo_ids": geo_ids, "table_suffix": table_suffix,
                                 "columns": wanted, "limit": limit,
                                 "collector_version": "1", "sql_version": "1"}
    extra = {"suffix": table_suffix, "columns": ",".join(wanted)}
    vintage = table_suffix.split("_")[1] if "_" in table_suffix else table_suffix
    return None, wanted, params, extra, vintage


def _noaa_request(geo_ids: list[str], limit: int,
                  start_date: str | None,
                  end_date: str | None) -> tuple[dict[str, object], dict[str, str], None]:
    """NOAA template params with a trailing-30d default window."""
    today = datetime.now(timezone.utc).date()
    start = start_date or (today - timedelta(days=30)).isoformat()
    end = end_date or today.isoformat()
    params: dict[str, object] = {"station_ids": geo_ids, "start_date": start,
                                 "end_date": end, "limit": limit,
                                 "collector_version": "1", "sql_version": "1"}
    return params, {"start": start, "end": end}, None


def _load_cache(path: Path) -> dict[str, object]:
    """Vintage cache dict; corrupt JSON becomes empty."""
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def _cache_hit(cache: object, key: str) -> dict[str, object] | None:
    """Fresh cached payload with cached=True, else None."""
    hit = cache.get(key) if isinstance(cache, dict) else None
    if not (isinstance(hit, dict) and hit.get("payload")):
        return None
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(str(hit.get("retrieved_at")))).days
    except (TypeError, ValueError):
        return None
    if age >= _VINTAGE_TTL_DAYS:
        return None
    out = dict(hit["payload"])
    out["cached"] = True
    return out

def _submit_error(result: object) -> dict[str, object] | None:
    """Normalized executor failure, or None when the result is usable."""
    if isinstance(result, dict) and "error" not in result:
        return None
    if isinstance(result, dict) and "status" in result:
        return result
    out: dict[str, object] = dict(result) if isinstance(result, dict) else {"error": "bad executor result"}
    out.setdefault("status", "unavailable")
    out["engine"] = out.get("source", "bigquery")
    out["source"] = SOURCE
    out.setdefault("reason", "missing-coverage")
    return out


def _join_error() -> dict[str, object]:
    """Fixed-shape error for rows lacking explicit geo/time keys."""
    return {"status": "unavailable", "source": SOURCE,
            "reason": "unsupported-join",
            "error": "row lacks explicit geo/time keys",
            "error_type": "unsupported_join"}


def _census_row(row: dict[str, object], wanted: list[str],
                vintage: str | None) -> list[dict[str, object]]:
    """One result row expanded to per-column context entries."""
    entries: list[dict[str, object]] = []
    for column in wanted:
        value = row.get(column)
        if value in _ACS_SENTINELS:
            value = None
        entries.append({
            "geo_id": row.get("geo_id"), "variable": column,
            "value": value, "unit": _ACS_UNITS[column],
            "vintage": vintage,
            "provider": row.get("provider"),
        })
    return entries


def _collect_census(rows: list[object],
                    wanted: list[str], vintage: str | None,
                    geo_ids: list[str]) -> tuple[dict[str, object] | None,
                                                list[dict[str, object]], list[str]]:
    """Census context entries plus missing geos, or (join error, [], [])."""
    context: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("geo_id"):
            return _join_error(), [], []
        context.extend(_census_row(row, wanted, vintage))
    return None, context, _missing_ids(rows, "geo_id", geo_ids)


def _frshtt_warning(raw: object, station: object, observed: str,
                    warnings: list[str]) -> None:
    """Append the tornado/hail flag warning when present."""
    if isinstance(raw, str) and any(flag in raw for flag in ("1", "2", "3", "4", "5", "6")):
        warnings.append(f"tornado/hail occurrence flag present for {station} "
                        f"on {observed or 'unknown date'}")


def _noaa_metric(row: dict[str, object], metric: str, unit: str,
                 observed: str, warnings: list[str]) -> dict[str, object] | None:
    """One NOAA metric entry; absent columns become None (skipped)."""
    if metric not in row:
        return None
    raw = row.get(metric)
    value = _clean_float(raw)
    if raw is not None and value is None:
        warnings.append(f"incomplete quality: {metric} sentinel for {row.get('station_id')}")
    if metric == "frshtt":
        _frshtt_warning(raw, row.get("station_id"), observed, warnings)
    return {
        "geo_id": row.get("station_id"), "variable": metric,
        "value": value, "unit": unit,
        "vintage": observed or None,
        "provider": "NOAA GSOD",
    }


def _noaa_row(row: dict[str, object], warnings: list[str]) -> list[dict[str, object]]:
    """One result row expanded to per-metric NOAA entries."""
    observed = str(row.get("observed_at") or "")
    entries: list[dict[str, object]] = []
    for metric, unit in _NOAA_UNITS.items():
        entry = _noaa_metric(row, metric, unit, observed, warnings)
        if entry is not None:
            entries.append(entry)
    return entries


def _missing_ids(rows: list[object], key: str, geo_ids: list[str]) -> list[str]:
    """Requested ids absent from the result rows."""
    seen = {str(r.get(key)) for r in rows if isinstance(r, dict)}
    return [g for g in geo_ids if g not in seen]


def _collect_noaa(rows: list[object],
                  geo_ids: list[str]) -> tuple[dict[str, object] | None,
                                              list[dict[str, object]], list[str], list[str]]:
    """NOAA context entries, warnings, and missing stations."""
    context: list[dict[str, object]] = []
    warnings: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("station_id"):
            return _join_error(), [], [], []
        context.extend(_noaa_row(row, warnings))
    return None, context, _missing_ids(rows, "station_id", geo_ids), warnings


def _finalize(template: str, context: list[dict[str, object]], limit: int,
              vintage: str | None, missing_geos: list[str],
              warnings: list[str]) -> dict[str, object]:
    """Fixed-shape ok payload with vintage and missing-coverage warnings."""
    if missing_geos:
        warnings.append(f"missing-coverage for {missing_geos}")
    vintages = sorted({str(c["vintage"]) for c in context if c.get("vintage")})
    return {"status": "ok", "source": SOURCE, "template": template,
            "context": context[:limit], "count": len(context[:limit]),
            "vintage": vintages[-1] if vintages else (vintage or "unknown"),
            "missing_geos": missing_geos, "warnings": sorted(set(warnings)),
            "retrieved_at": datetime.now(timezone.utc).isoformat(), "cached": False}


def _store_cache(path: Path, cache: object, key: str,
                 retrieved_at: str, vintage: object,
                 payload: dict[str, object]) -> None:
    """Persist the ok payload keyed by request hash; OSError is ignored."""
    try:
        store = cache if isinstance(cache, dict) else {}
        store[key] = {"retrieved_at": retrieved_at,
                      "vintage": vintage, "payload": payload}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(store))
    except OSError:
        pass


def _plan_request(geo_ids: list[str], variables: list[str], columns: list[str] | None,
                  table_suffix: str, limit: int,
                  start_date: str | None, end_date: str | None) -> tuple[
                      dict[str, object] | None, str, list[str], dict[str, object],
                      dict[str, str], str | None]:
    """(error, template, wanted, params, extra, vintage) for the chosen engine."""
    if _looks_noaa(variables):
        params, extra, vintage = _noaa_request(geo_ids, limit, start_date, end_date)
        return None, _NOAA_TEMPLATE, [], params, extra, vintage
    req_err, wanted, params, extra, vintage = _census_request(
        geo_ids, columns, variables, table_suffix, limit)
    if req_err is not None:
        return req_err, _CENSUS_TEMPLATE, [], {}, {}, None
    return None, _CENSUS_TEMPLATE, wanted, params, extra, vintage


def _collect_rows(template: str, rows: list[object], wanted: list[str],
                  vintage: str | None,
                  geo_ids: list[str]) -> tuple[dict[str, object] | None,
                                              list[dict[str, object]], list[str], list[str]]:
    """(error, context, missing_geos, warnings) for either engine."""
    if template == _CENSUS_TEMPLATE:
        err, context, missing = _collect_census(rows, wanted, vintage, geo_ids)
        return err, context, missing, []
    return _collect_noaa(rows, geo_ids)


def _clean_float(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if float(value) in _NOAA_SENTINELS else value
    return value


def get_geo_context(geo_ids: list[str] | str | None, *, variables: list[str] | str | None = None,
                    start_date: str | None = None, end_date: str | None = None,
                    table_suffix: str = "county_2020_5yr", columns: list[str] | None = None,
                    limit: int = 100, executor: _Executor | None = None,
                    data_root: Path | None = None) -> dict[str, object]:
    """Census/NOAA context for explicit geo/time keys; gaps stay explicit."""
    geos, vars_ = _normalize_inputs(geo_ids, variables)
    if not geos:
        return {"status": "error", "source": SOURCE,
                "error": "at least one geo_id is required", "error_type": "invalid_params"}
    proj_err = _project_error()
    if proj_err is not None:
        return proj_err
    lim_err, limit = _clamp_limit(limit)
    if lim_err is not None:
        return lim_err
    plan_err, template, wanted, params, extra, vintage = _plan_request(
        geos, vars_, columns, table_suffix, limit, start_date, end_date)
    if plan_err is not None:
        return plan_err
    cache_path = _resolve_root(data_root) / "google_data" / "geo_vintage.json"
    cache_key = _vintage_key(template, geos, vars_, extra)
    cache = _load_cache(cache_path)
    hit = _cache_hit(cache, cache_key)
    if hit is not None:
        return hit
    result = _submit(template, params, executor, data_root)
    failed = _submit_error(result)
    if failed is not None:
        return failed
    rows = result_rows(result)
    if not rows:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "missing-coverage",
                "error": "no supported geo/time coverage", "error_type": "missing_coverage"}
    collect_err, context, missing_geos, warnings = _collect_rows(
        template, rows, wanted, vintage, geos)
    if collect_err is not None:
        return collect_err
    out = _finalize(template, context, limit, vintage, missing_geos, warnings)
    _store_cache(cache_path, cache, cache_key, str(out["retrieved_at"]),
                 out["vintage"], out)
    return out
