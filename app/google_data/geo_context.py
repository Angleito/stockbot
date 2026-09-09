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
from collections.abc import Callable
from typing import Protocol, cast

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

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
    fn = getattr(_config, "google_data_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return os.getenv("GOOGLE_DATA_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _setting(fn_name: str, env_name: str, default: object = None) -> object:
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


def _resolve_root(data_root: Path | None = None) -> Path:
    if data_root:
        return Path(data_root)
    try:
        from .. import config as _cfg  # type: ignore
        return Path(_cfg.get_data_root())
    except Exception:
        return Path(os.getenv("STOCKBOT_DATA_DIR", "data"))


def _submit(template: str, params: dict[str, object], executor: _Executor | None,
            data_root: Path | None) -> dict[str, object]:
    if executor is None:
        try:
            try:
                from . import bigquery_client as _bq
            except ImportError:
                from app.google_data import bigquery_client as _bq  # type: ignore
        except ImportError:
            return {"error": "bigquery client unavailable",
                    "error_type": "source_unavailable", "source": "bigquery"}
        try:
            return _bq.submit_template(template, params, data_root=data_root)
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


def _looks_noaa(variables: list[str]) -> bool:
    return any(any(hint in v.upper() for hint in _NOAA_HINTS) for v in variables)


def _vintage_key(template: str, geo_ids: list[str], variables: list[str],
                 extra: dict[str, str]) -> str:
    blob = json.dumps({"t": template, "g": sorted(map(str, geo_ids)),
                       "v": sorted(map(str, variables)),
                       "x": {k: v for k, v in sorted(extra.items())}},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


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
    if isinstance(geo_ids, str):
        geo_ids = [geo_ids]
    geo_ids = list(geo_ids or [])
    variables = [variables] if isinstance(variables, str) else list(variables or [])
    if not geo_ids:
        return {"status": "error", "source": SOURCE,
                "error": "at least one geo_id is required", "error_type": "invalid_params"}
    if not _data_enabled() or not _setting("get_google_cloud_project", "GOOGLE_CLOUD_PROJECT"):
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    try:
        limit = max(1, min(limit, 100))
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}
    wanted: list[str] = []
    template = _NOAA_TEMPLATE if _looks_noaa(variables) else _CENSUS_TEMPLATE

    if template == _CENSUS_TEMPLATE:
        if table_suffix not in _ACS_SUFFIXES:
            return {"status": "unavailable", "source": SOURCE,
                    "error": f"unknown census table: {table_suffix!r}",
                    "error_type": "source_unavailable",
                    "available": list(_ACS_SUFFIXES)}
        wanted = list(columns or variables or ["total_pop"])
        bad = [c for c in wanted if c not in _ACS_COLUMNS]
        if bad:
            return {"status": "unavailable", "source": SOURCE,
                    "error": f"unknown census columns: {bad!r}",
                    "error_type": "source_unavailable",
                    "available": list(_ACS_COLUMNS)}
        params: dict[str, object] = {"geo_ids": geo_ids, "table_suffix": table_suffix,
                                     "columns": wanted, "limit": limit,
                                     "collector_version": "1", "sql_version": "1"}
        extra = {"suffix": table_suffix, "columns": ",".join(wanted)}
        vintage = table_suffix.split("_")[1] if "_" in table_suffix else table_suffix
    else:
        today = datetime.now(timezone.utc).date()
        start_date = start_date or (today - timedelta(days=30)).isoformat()
        end_date = end_date or today.isoformat()
        params = {"station_ids": geo_ids, "start_date": start_date,
                  "end_date": end_date, "limit": limit,
                  "collector_version": "1", "sql_version": "1"}
        extra = {"start": start_date, "end": end_date}
        vintage = None

    cache_path = _resolve_root(data_root) / "google_data" / "geo_vintage.json"
    cache_key = _vintage_key(template, geo_ids, variables, extra)
    try:
        cache: dict[str, object] = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    except ValueError:
        cache = {}
    hit = cache.get(cache_key) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and hit.get("payload"):
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(hit.get("retrieved_at")))).days
            if age < _VINTAGE_TTL_DAYS:
                out = dict(hit["payload"])
                out["cached"] = True
                return out
        except (TypeError, ValueError):
            pass

    result = _submit(template, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        if isinstance(result, dict) and "status" in result:
            return result
        out: dict[str, object] = dict(result) if isinstance(result, dict) else {"error": "bad executor result"}
        out.setdefault("status", "unavailable")
        out["engine"] = out.get("source", "bigquery")
        out["source"] = SOURCE
        out.setdefault("reason", "missing-coverage")
        return out
    rows = cast(list[object], result.get("rows", []) or [])
    if not rows:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "missing-coverage",
                "error": "no supported geo/time coverage", "error_type": "missing_coverage"}
    context: list[dict[str, object]] = []
    warnings: list[str] = []
    if template == _CENSUS_TEMPLATE:
        for row in rows:
            if not isinstance(row, dict) or not row.get("geo_id"):
                return {"status": "unavailable", "source": SOURCE,
                        "reason": "unsupported-join",
                        "error": "row lacks explicit geo/time keys",
                        "error_type": "unsupported_join"}
            for column in wanted:
                value = row.get(column)
                if value in _ACS_SENTINELS:
                    value = None
                context.append({
                    "geo_id": row.get("geo_id"), "variable": column,
                    "value": value, "unit": _ACS_UNITS[column],
                    "vintage": vintage,
                    "provider": row.get("provider"),
                })
        seen_geos = {str(row.get("geo_id")) for row in rows if isinstance(row, dict)}
        missing_geos = [g for g in geo_ids if g not in seen_geos]
    else:
        for row in rows:
            if not isinstance(row, dict) or not row.get("station_id"):
                return {"status": "unavailable", "source": SOURCE,
                        "reason": "unsupported-join",
                        "error": "row lacks explicit geo/time keys",
                        "error_type": "unsupported_join"}
            observed = str(row.get("observed_at") or "")
            for metric, unit in _NOAA_UNITS.items():
                if metric not in row:
                    continue
                raw = row.get(metric)
                value = _clean_float(raw)
                if raw is not None and value is None:
                    warnings.append(f"incomplete quality: {metric} sentinel for {row.get('station_id')}")
                if metric == "frshtt" and isinstance(raw, str) and any(
                        flag in raw for flag in ("1", "2", "3", "4", "5", "6")):
                    warnings.append(f"tornado/hail occurrence flag present for {row.get('station_id')} "
                                    f"on {observed or 'unknown date'}")
                context.append({
                    "geo_id": row.get("station_id"), "variable": metric,
                    "value": value, "unit": unit,
                    "vintage": observed or None,
                    "provider": "NOAA GSOD",
                })
        seen_geos = {str(row.get("station_id")) for row in rows if isinstance(row, dict)}
        missing_geos = [g for g in geo_ids if g not in seen_geos]
    if missing_geos:
        warnings.append(f"missing-coverage for {missing_geos}")
    retrieved_at = datetime.now(timezone.utc).isoformat()
    vintages = sorted({str(c["vintage"]) for c in context if c.get("vintage")})
    out = {"status": "ok", "source": SOURCE, "template": template,
           "context": context[:limit], "count": len(context[:limit]),
           "vintage": vintages[-1] if vintages else (vintage or "unknown"),
           "missing_geos": missing_geos, "warnings": sorted(set(warnings)),
           "retrieved_at": retrieved_at, "cached": False}
    try:
        cache = cache if isinstance(cache, dict) else {}
        cache[cache_key] = {"retrieved_at": retrieved_at,
                            "vintage": out["vintage"], "payload": out}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache))
    except OSError:
        pass
    return out
