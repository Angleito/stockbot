"""Bounded patent research through the checked-in BigQuery templates.

Templates ``patents_assignee`` (detail) and ``patents_assignee_stats``
(yearly aggregates) only. Caller-supplied assignee aliases are required — a
company relationship is never inferred from matching text. Counts are
publications and families explicitly, never unique inventions or signals.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
import re
from datetime import datetime, timedelta, timezone

from ._guards import as_int, result_rows
from ._lazy_config import google_data_enabled

SOURCE = "patents"
_TEMPLATE = "patents_assignee"
_STATS_TEMPLATE = "patents_assignee_stats"
_MAX_LIMIT = 20
_MAX_STATS = 100
_DEFAULT_COUNTRIES = ("US",)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

class _Submitter(Protocol):
    """Anything submit_template-compatible: the real client or a test double."""
    def submit_template(self, template: str, params: dict[str, object]) -> dict[str, object]: ...


_Executor = Callable[[str, dict[str, object]], dict[str, object]] | _Submitter


def _data_enabled() -> bool:
    return google_data_enabled()


def _submit(template: str, params: dict[str, object], executor: _Executor | None,
            data_root: Path | None) -> dict[str, object]:
    if executor is None:
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


def _wrap_error(result: dict[str, object]) -> dict[str, object]:
    if "status" in result:
        return result
    out = dict(result)
    out["status"] = "unavailable" if out.get("error_type") in _UNAVAILABLE else "error"
    out["engine"] = out.get("source", "bigquery")
    out["source"] = SOURCE
    return out


def _check_dates(
    start_date: str | None, end_date: str | None
) -> tuple[dict[str, object] | None, str | None, str | None]:
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


def _clean_list(values: list[str] | str | None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [v.strip() for v in (values or []) if v and v.strip()]


def search_company_patents(company_id: str, *, start_date: str | None = None,
                           end_date: str | None = None, limit: int = 20,
                           assignees: list[str] | None = None,
                           aliases: list[str] | None = None,
                           country_codes: list[str] | None = None,
                           executor: _Executor | None = None,
                           data_root: Path | None = None) -> dict[str, object]:
    """Publications for documented assignee aliases; empty aliases refuse."""
    if not company_id or not company_id.strip():
        return {"status": "error", "source": SOURCE,
                "error": "company_id is required", "error_type": "invalid_params"}
    assignees = _clean_list(assignees) + _clean_list(aliases)
    if not assignees:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "documented assignee aliases required; never inferred",
                "error": "assignees required", "error_type": "source_unavailable"}
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    _project: str | None = None
    if _cfg is not None:
        try:
            _project = _cfg.get_google_cloud_project()
        except Exception:
            _project = None
    if _project is None:
        _project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    if not _project:
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    try:
        limit = max(1, min(limit, _MAX_LIMIT))
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
    params: dict[str, object] = {"assignees": assignees, "country_codes": countries,
                                 "limit": limit, "collector_version": "1", "sql_version": "1"}
    if start_date is not None:
        params["start_yyyymmdd"] = int(start_date.replace("-", ""))
    if end_date is not None:
        params["end_yyyymmdd"] = int(end_date.replace("-", ""))
    result = _submit(_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    publications: list[dict[str, object]] = []
    rows = result_rows(result)
    for row in rows[:limit]:
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
    return {"status": "ok", "source": SOURCE, "company_id": company_id.strip(),
            "publications": publications, "count": len(publications)}


def get_assignee_stats(company_id: str, *, start_date: str | None = None,
                       end_date: str | None = None,
                       assignees: list[str] | None = None,
                       aliases: list[str] | None = None,
                       country_codes: list[str] | None = None,
                       executor: _Executor | None = None,
                       data_root: Path | None = None) -> dict[str, object]:
    """Yearly publication/family/citation aggregates plus top inventive CPC.

    Null publication dates become gaps (excluded, counted); null citations
    count zero; years with no inventive CPC report ``__NONE__``.
    """
    if not company_id or not company_id.strip():
        return {"status": "error", "source": SOURCE,
                "error": "company_id is required", "error_type": "invalid_params"}
    assignees = _clean_list(assignees) + _clean_list(aliases)
    if not assignees:
        return {"status": "unavailable", "source": SOURCE,
                "reason": "documented assignee aliases required; never inferred",
                "error": "assignees required", "error_type": "source_unavailable"}
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    try:
        from .. import config as _cfg2
    except ImportError:
        _cfg2 = None
    _project2: str | None = None
    if _cfg2 is not None:
        try:
            _project2 = _cfg2.get_google_cloud_project()
        except Exception:
            _project2 = None
    if _project2 is None:
        _project2 = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    if not _project2:
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
    params: dict[str, object] = {"assignees": assignees, "country_codes": countries,
                                 "limit": _MAX_STATS, "collector_version": "1", "sql_version": "1"}
    if start_date is not None:
        params["start_yyyymmdd"] = int(start_date.replace("-", ""))
    if end_date is not None:
        params["end_yyyymmdd"] = int(end_date.replace("-", ""))
    result = _submit(_STATS_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    by_year: dict[int, dict[str, object]] = {}
    gaps = 0
    rows = result_rows(result)
    for row in rows:
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
                bucket[field] = as_int(bucket.get(field, 0), what=field) + as_int(row.get(key) or 0, what=key)
            except (TypeError, ValueError):
                pass
        _cpc = bucket.get("cpc_counts")
        if not isinstance(_cpc, dict):
            _cpc = dict[str, object]()
            bucket["cpc_counts"] = _cpc
        cpc_counts: dict[str, int] = {}
        for _ck, _cv in _cpc.items():
            if isinstance(_ck, str) and isinstance(_cv, int) and not isinstance(_cv, bool):
                cpc_counts[_ck] = _cv
        for code in row.get("cpc_bag") or []:
            code_key = str(code)
            cpc_counts[code_key] = cpc_counts.get(code_key, 0) + 1
        bucket["cpc_counts"] = cpc_counts
    years: list[dict[str, object]] = []
    for year in sorted(by_year):
        bucket = by_year[year]
        _cpc2 = bucket.get("cpc_counts")
        cpc_counts2: dict[str, int] = {}
        if isinstance(_cpc2, dict):
            for _ck2, _cv2 in _cpc2.items():
                if isinstance(_ck2, str) and isinstance(_cv2, int) and not isinstance(_cv2, bool):
                    cpc_counts2[_ck2] = _cv2
        ranked = sorted(((n, c) for c, n in cpc_counts2.items() if c != "__NONE__"),
                        reverse=True)
        years.append({"pub_year": year, "pub_count": bucket["pub_count"],
                      "family_count": bucket["family_count"],
                      "total_citations": bucket["total_citations"],
                      "top_cpc": ranked[0][1] if ranked else "__NONE__"})
    return {"status": "ok", "source": SOURCE, "company_id": company_id.strip(),
            "years": years, "gaps": gaps}
