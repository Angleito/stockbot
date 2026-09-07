"""Bounded patent research through the checked-in BigQuery templates.

Templates ``patents_assignee`` (detail) and ``patents_assignee_stats``
(yearly aggregates) only. Caller-supplied assignee aliases are required — a
company relationship is never inferred from matching text. Counts are
publications and families explicitly, never unique inventions or signals.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

SOURCE = "patents"
_TEMPLATE = "patents_assignee"
_STATS_TEMPLATE = "patents_assignee_stats"
_MAX_LIMIT = 20
_MAX_STATS = 100
_DEFAULT_COUNTRIES = ("US",)
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


def _submit(template: str, params: dict, executor, data_root) -> dict:
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


def _check_dates(start_date, end_date):
    """Validated (start, end) or (error, None, None); absent dates stay unbounded."""
    if start_date is None and end_date is None:
        return None, None, None
    if (start_date is None) != (end_date is None):
        return ({"status": "error", "source": SOURCE,
                 "error": "start_date and end_date must both be set or both omitted (YYYY-MM-DD)",
                 "error_type": "invalid_params"}, None, None)
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if value is None:
            continue
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return ({"status": "error", "source": SOURCE,
                     "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                     "error_type": "invalid_params"}, None, None)
    if start_date and end_date and start_date > end_date:
        return ({"status": "error", "source": SOURCE,
                 "error": "start_date after end_date", "error_type": "invalid_params"},
                None, None)
    return None, start_date, end_date


def _clean_list(values) -> list:
    if isinstance(values, str):
        values = [values]
    return [str(v).strip() for v in (values or []) if v and str(v).strip()]


def search_company_patents(company_id: str, *, start_date=None, end_date=None,
                           limit: int = 20, assignees: list | None = None,
                           aliases: list | None = None, country_codes: list | None = None,
                           executor=None, data_root=None) -> dict:
    """Publications for documented assignee aliases; empty aliases refuse."""
    if not company_id or not str(company_id).strip():
        return {"status": "error", "source": SOURCE,
                "error": "company_id is required", "error_type": "invalid_params"}
    assignees = _clean_list(assignees) + _clean_list(aliases)
    if not assignees:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "documented assignee aliases required; never inferred",
                "error": "assignees required", "error_type": "source_unavailable"}
    if not _data_enabled() or not _setting("get_google_cloud_project", "GOOGLE_CLOUD_PROJECT"):
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}
    date_error, start_date, end_date = _check_dates(start_date, end_date)
    if date_error is not None:
        return date_error
    # Dateless -> bounded trailing 5-year window (5x365, deterministic).
    if start_date is None and end_date is None:
        _today = datetime.now(timezone.utc).date()
        end_date = _today.isoformat()
        start_date = (_today - timedelta(days=1825)).isoformat()
    countries = _clean_list(country_codes) or list(_DEFAULT_COUNTRIES)
    params: dict = {"assignees": assignees, "country_codes": countries,
                    "limit": limit, "collector_version": "1", "sql_version": "1"}
    if start_date is not None:
        params["start_yyyymmdd"] = int(start_date.replace("-", ""))
    if end_date is not None:
        params["end_yyyymmdd"] = int(end_date.replace("-", ""))
    result = _submit(_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    publications = []
    for row in (result.get("rows", []) or [])[:limit]:
        if not isinstance(row, dict):
            continue
        publications.append({
            "publication_id": row.get("publication_id") or row.get("id"),
            "publication_date": str(row.get("publication_date") or row.get("date") or ""),
            "assignees": row.get("assignees") or row.get("assignee"),
            "classes": row.get("classes") or row.get("classification_ids") or row.get("cpc"),
            "inventors": row.get("inventors") or row.get("inventor_harmonized"),
            "citation_count": row.get("citation_count", 0),
            "family_id": row.get("family_id"),
            "country_code": row.get("country_code"),
            "kind_code": row.get("kind_code"),
            "title": row.get("title"),
            "url": row.get("url") or row.get("source_link"),
        })
    return {"status": "ok", "source": SOURCE, "company_id": str(company_id).strip(),
            "publications": publications, "count": len(publications)}


def get_assignee_stats(company_id: str, *, start_date=None, end_date=None,
                       assignees: list | None = None, aliases: list | None = None,
                       country_codes: list | None = None,
                       executor=None, data_root=None) -> dict:
    """Yearly publication/family/citation aggregates plus top inventive CPC.

    Null publication dates become gaps (excluded, counted); null citations
    count zero; years with no inventive CPC report ``__NONE__``.
    """
    if not company_id or not str(company_id).strip():
        return {"status": "error", "source": SOURCE,
                "error": "company_id is required", "error_type": "invalid_params"}
    assignees = _clean_list(assignees) + _clean_list(aliases)
    if not assignees:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "documented assignee aliases required; never inferred",
                "error": "assignees required", "error_type": "source_unavailable"}
    if not _data_enabled() or not _setting("get_google_cloud_project", "GOOGLE_CLOUD_PROJECT"):
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    date_error, start_date, end_date = _check_dates(start_date, end_date)
    if date_error is not None:
        return date_error
    # Dateless -> bounded trailing 5-year window (5x365, deterministic).
    if start_date is None and end_date is None:
        _today = datetime.now(timezone.utc).date()
        end_date = _today.isoformat()
        start_date = (_today - timedelta(days=1825)).isoformat()
    countries = _clean_list(country_codes) or list(_DEFAULT_COUNTRIES)
    params: dict = {"assignees": assignees, "country_codes": countries,
                    "limit": _MAX_STATS, "collector_version": "1", "sql_version": "1"}
    if start_date is not None:
        params["start_yyyymmdd"] = int(start_date.replace("-", ""))
    if end_date is not None:
        params["end_yyyymmdd"] = int(end_date.replace("-", ""))
    result = _submit(_STATS_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    by_year: dict = {}
    gaps = 0
    for row in result.get("rows", []) or []:
        if not isinstance(row, dict):
            continue
        year = row.get("pub_year")
        if year is None:
            gaps += 1
            continue
        try:
            year = int(year)
        except (TypeError, ValueError):
            gaps += 1
            continue
        bucket = by_year.setdefault(year, {"pub_count": 0, "family_count": 0,
                                           "total_citations": 0, "cpc_counts": {}})
        for key, field in (("pub_count", "pub_count"), ("family_count", "family_count"),
                           ("total_citations", "total_citations")):
            try:
                bucket[field] += int(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
        for code in row.get("cpc_bag") or []:
            bucket["cpc_counts"][str(code)] = bucket["cpc_counts"].get(str(code), 0) + 1
    years = []
    for year in sorted(by_year):
        bucket = by_year[year]
        ranked = sorted(((n, c) for c, n in bucket["cpc_counts"].items() if c != "__NONE__"),
                        reverse=True)
        years.append({"pub_year": year, "pub_count": bucket["pub_count"],
                      "family_count": bucket["family_count"],
                      "total_citations": bucket["total_citations"],
                      "top_cpc": ranked[0][1] if ranked else "__NONE__"})
    return {"status": "ok", "source": SOURCE, "company_id": str(company_id).strip(),
            "years": years, "gaps": gaps}
