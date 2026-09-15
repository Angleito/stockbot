"""Bounded patent research through the checked-in BigQuery templates.

Templates ``patents_assignee`` (detail) and ``patents_assignee_stats``
(yearly aggregates) only. Caller-supplied assignee aliases are required — a
company relationship is never inferred from matching text. Counts are
publications and families explicitly, never unique inventions or signals.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

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


def _date_error(label: str, value: str | None) -> dict[str, object] | None:
    """Invalid-params error for one YYYY-MM-DD value, else None."""
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                "error_type": "invalid_params"}
    return None


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
        err = _date_error(label, value)
        if err is not None:
            return err, None, None
    if start_date and end_date and start_date > end_date:
        return ({"status": "error", "source": SOURCE,
                 "error": "start_date after end_date", "error_type": "invalid_params"},
                None, None)
    return None, start_date, end_date


def _clean_list(values: list[str] | str | None) -> list[str]:
    if isinstance(values, str):
        values = [values]
    return [v.strip() for v in (values or []) if v and v.strip()]


def _company_error(
    company_id: str, assignees: list[str] | None, aliases: list[str] | None
) -> tuple[dict[str, object] | None, list[str]]:
    """Invalid-params error plus cleaned assignee aliases, else (None, aliases)."""
    if not company_id or not company_id.strip():
        return ({"status": "error", "source": SOURCE,
                 "error": "company_id is required", "error_type": "invalid_params"}, [])
    cleaned = _clean_list(assignees) + _clean_list(aliases)
    if not cleaned:
        return ({"status": "unavailable", "source": SOURCE,
                 "reason": "documented assignee aliases required; never inferred",
                 "error": "assignees required", "error_type": "source_unavailable"}, [])
    return None, cleaned


def _config_project() -> str | None:
    """BigQuery project from app config, or None when unavailable."""
    try:
        from .. import config as _cfg
    except ImportError:
        return None
    try:
        return _cfg.get_google_cloud_project()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _project_name() -> str | None:
    """Configured BigQuery project or None; config first, env fallback."""
    return _config_project() or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None


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


def _clamp_limit(limit: int, cap: int) -> tuple[dict[str, object] | None, int]:
    """Clamped limit or (invalid-params error, cap)."""
    try:
        return None, max(1, min(limit, cap))
    except (TypeError, ValueError):
        return ({"status": "error", "source": SOURCE,
                 "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}, cap)


def _trailing_window(
    start_date: str | None, end_date: str | None
) -> tuple[str | None, str | None]:
    """Dateless pair becomes the bounded trailing 5-year window."""
    if start_date is None and end_date is None:
        today = datetime.now(timezone.utc).date()
        start = (today - timedelta(days=1825)).isoformat()
        return start, today.isoformat()
    return start_date, end_date


def _patent_params(assignees: list[str], countries: list[str], limit: int,
                   start_date: str | None, end_date: str | None) -> dict[str, object]:
    """Executor params with YYYYMMDD bounds only when dated."""
    params: dict[str, object] = {"assignees": assignees, "country_codes": countries,
                                 "limit": limit, "collector_version": "1", "sql_version": "1"}
    if start_date is not None:
        params["start_yyyymmdd"] = int(start_date.replace("-", ""))
    if end_date is not None:
        params["end_yyyymmdd"] = int(end_date.replace("-", ""))
    return params


def _publication_row(row: object) -> dict[str, object] | None:
    """One normalized publication dict; non-dict rows become None."""
    if not isinstance(row, dict):
        return None
    return {
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
    }


def _collect_publications(rows: list[object], limit: int) -> list[dict[str, object]]:
    """Normalized publication dicts up to limit; non-dict rows are skipped."""
    publications: list[dict[str, object]] = []
    for row in rows[:limit]:
        pub = _publication_row(row)
        if pub is None:
            continue
        publications.append(pub)
    return publications


def _stats_year(row: object) -> tuple[str, int]:
    """(kind, year): skip non-dicts, gap null/unparseable years, else ok."""
    if not isinstance(row, dict):
        return "skip", 0
    raw = row.get("pub_year")
    if raw is None:
        return "gap", 0
    try:
        return "ok", int(raw)
    except (TypeError, ValueError):
        return "gap", 0


def _add_counts(bucket: dict[str, object], row: dict[str, object]) -> None:
    """Accumulate pub/family/citation counts; bad values keep the bucket."""
    for key, field in (("pub_count", "pub_count"), ("family_count", "family_count"),
                       ("total_citations", "total_citations")):
        try:
            bucket[field] = as_int(bucket.get(field, 0), what=field) + as_int(row.get(key) or 0, what=key)
        except (TypeError, ValueError):
            pass


def _clean_cpc_counts(current: object) -> dict[str, int]:
    """Validated CPC histogram copy; non-conforming entries are dropped."""
    counts: dict[str, int] = {}
    if not isinstance(current, dict):
        return counts
    for key, val in current.items():
        if isinstance(key, str) and isinstance(val, int) and not isinstance(val, bool):
            counts[key] = val
    return counts


def _add_cpc(bucket: dict[str, object], row: dict[str, object]) -> None:
    """Merge one row's cpc_bag into the bucket histogram."""
    counts = _clean_cpc_counts(bucket.get("cpc_counts"))
    bag = row.get("cpc_bag")
    if not isinstance(bag, (list, tuple)):
        bucket["cpc_counts"] = counts
        return
    for code in bag:
        key = str(code)
        counts[key] = counts.get(key, 0) + 1
    bucket["cpc_counts"] = counts


def _top_cpc(bucket: dict[str, object]) -> str:
    """Highest-count inventive CPC or __NONE__ when the bucket has none."""
    counts = _clean_cpc_counts(bucket.get("cpc_counts"))
    ranked = sorted(((n, c) for c, n in counts.items() if c != "__NONE__"),
                    reverse=True)
    return ranked[0][1] if ranked else "__NONE__"


def _summarize_years(by_year: dict[int, dict[str, object]]) -> list[dict[str, object]]:
    """Sorted yearly aggregates with top inventive CPC per year."""
    years: list[dict[str, object]] = []
    for year in sorted(by_year):
        bucket = by_year[year]
        years.append({"pub_year": year, "pub_count": bucket["pub_count"],
                      "family_count": bucket["family_count"],
                      "total_citations": bucket["total_citations"],
                      "top_cpc": _top_cpc(bucket)})
    return years


def _accumulate_stats(
    rows: list[object],
) -> tuple[dict[int, dict[str, object]], int]:
    """Year buckets plus gap count; null/unparseable years become gaps."""
    by_year: dict[int, dict[str, object]] = {}
    gaps = 0
    for row in rows:
        kind, year = _stats_year(row)
        if kind == "skip":
            continue
        if kind == "gap":
            gaps += 1
            continue
        if not isinstance(row, dict):
            continue
        bucket = by_year.setdefault(year, {"pub_count": 0, "family_count": 0,
                                           "total_citations": 0, "cpc_counts": {}})
        _add_counts(bucket, row)
        _add_cpc(bucket, row)
    return by_year, gaps



def search_company_patents(company_id: str, *, start_date: str | None = None,
                           end_date: str | None = None, limit: int = 20,
                           assignees: list[str] | None = None,
                           aliases: list[str] | None = None,
                           country_codes: list[str] | None = None,
                           executor: _Executor | None = None,
                           data_root: Path | None = None) -> dict[str, object]:
    """Publications for documented assignee aliases; empty aliases refuse."""
    comp_err, names = _company_error(company_id, assignees, aliases)
    if comp_err is not None:
        return comp_err
    proj_err = _project_error()
    if proj_err is not None:
        return proj_err
    lim_err, limit = _clamp_limit(limit, _MAX_LIMIT)
    if lim_err is not None:
        return lim_err
    date_error, start_date, end_date = _check_dates(start_date, end_date)
    if date_error is not None:
        return date_error
    start_date, end_date = _trailing_window(start_date, end_date)
    countries = _clean_list(country_codes) or list(_DEFAULT_COUNTRIES)
    params = _patent_params(names, countries, limit, start_date, end_date)
    result = _submit(_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    publications = _collect_publications(result_rows(result), limit)
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
    comp_err, names = _company_error(company_id, assignees, aliases)
    if comp_err is not None:
        return comp_err
    proj_err = _project_error()
    if proj_err is not None:
        return proj_err
    date_error, start_date, end_date = _check_dates(start_date, end_date)
    if date_error is not None:
        return date_error
    start_date, end_date = _trailing_window(start_date, end_date)
    countries = _clean_list(country_codes) or list(_DEFAULT_COUNTRIES)
    params = _patent_params(names, countries, _MAX_STATS, start_date, end_date)
    result = _submit(_STATS_TEMPLATE, params, executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        return _wrap_error(result if isinstance(result, dict) else {"error": "bad executor result"})
    by_year, gaps = _accumulate_stats(result_rows(result))
    years = _summarize_years(by_year)
    return {"status": "ok", "source": SOURCE, "company_id": company_id.strip(),
            "years": years, "gaps": gaps}
