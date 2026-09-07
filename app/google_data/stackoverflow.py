"""Stack Overflow tag-activity experiment via one allowlisted template.

Template ``stackoverflow_tags`` only: tag-split aggregation over
``posts_questions`` (``UNNEST(SPLIT(tags, '|'))``), never agent SQL. Reports
observed activity with source coverage and extraction time; a snapshot whose
coverage lags the request by 90 days is labeled stale, never current
adoption. Tag counts are activity, never company revenue. Over-cap
aggregates refuse with the executor's cost error instead of weakening the
safeguard.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

SOURCE = "stackoverflow"
_TEMPLATE = "stackoverflow_tags"
_MAX_LIMIT = 100
_STALE_LAG_DAYS = 90
_EPOCH_START = "2008-01-01"


def _data_enabled() -> bool:
    fn = getattr(_config, "google_data_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return os.getenv("GOOGLE_DATA_ENABLED", "").strip().lower() in ("1", "true", "yes")


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


def get_tag_activity(tags, *, start_date=None, end_date=None, limit=100,
                     executor=None, data_root=None) -> dict:
    """Observed tag activity per tag/period with last-covered date."""
    if isinstance(tags, str):
        tags = [tags]
    tags = [t for t in (tags or []) if t and str(t).strip()]
    if not tags:
        return {"status": "error", "source": SOURCE,
                "error": "at least one tag is required", "error_type": "invalid_params"}
    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}
    if not _data_enabled() or not _setting("get_google_cloud_project",
                                           "GOOGLE_CLOUD_PROJECT"):
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled or no BigQuery project"}
    end_date = end_date or datetime.now(timezone.utc).date().isoformat()
    start_date = start_date or _EPOCH_START
    result = _submit(_TEMPLATE, {"tags": tags, "start_date": start_date,
                                 "end_date": end_date, "limit": limit,
                                 "collector_version": "1", "sql_version": "1"},
                     executor, data_root)
    if not isinstance(result, dict) or "error" in result:
        if isinstance(result, dict) and "status" in result:
            return result
        out = dict(result) if isinstance(result, dict) else {"error": "bad executor result"}
        out["status"] = "unavailable" if out.get("error_type") in _UNAVAILABLE else "error"
        out["engine"] = out.get("source", "bigquery")
        out["source"] = SOURCE
        return out
    activity = []
    for row in (result.get("rows", []) or [])[:limit]:
        if not isinstance(row, dict):
            continue
        questions = row.get("question_count", row.get("count"))
        activity.append({
            "tag": row.get("tag"),
            "period": str(row.get("period") or row.get("date") or row.get("week") or ""),
            "activity_count": questions,
            "question_count": questions,
            "total_views": row.get("total_views"),
            "avg_views": row.get("avg_views"),
            "accepted_count": row.get("accepted_count"),
        })
    periods = sorted({str(a["period"]) for a in activity if a.get("period")})
    last_covered = periods[-1] if periods else result.get("last_covered")
    warnings = []
    if last_covered:
        try:
            lag = (date.fromisoformat(str(end_date)[:10])
                   - date.fromisoformat(str(last_covered)[:10])).days
            if lag > _STALE_LAG_DAYS:
                warnings.append(
                    f"stale_snapshot: last covered {last_covered} lags request "
                    f"end {end_date} by {lag}d; treat as historical activity, not current adoption")
        except ValueError:
            pass
    return {"status": "ok", "source": SOURCE, "activity": activity,
            "count": len(activity),
            "last_covered": last_covered,
            "warnings": warnings,
            "retrieved_at": datetime.now(timezone.utc).isoformat()}
