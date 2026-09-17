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
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from ._guards import result_rows
from ._lazy_config import google_data_enabled

SOURCE = "stackoverflow"
_TEMPLATE = "stackoverflow_tags"
_MAX_LIMIT = 100
_STALE_LAG_DAYS = 90
_EPOCH_START = "2008-01-01"


class _Submitter(Protocol):
    """Anything submit_template-compatible: the real client or a test double."""

    def submit_template(self, template: str, params: dict[str, object]) -> dict[str, object]: ...


_Executor = Callable[[str, dict[str, object]], dict[str, object]] | _Submitter


def _data_enabled() -> bool:
    return google_data_enabled()


def _submit_failure(exc: Exception) -> dict[str, object]:
    """Fixed-shape executor failure; LedgerCorrupt is never wrapped here."""
    return {
        "status": "error",
        "source": SOURCE,
        "error": f"{SOURCE} query failed: {exc}",
        "error_type": "executor_error",
    }


def _submit_via_client(template: str, params: dict[str, object], data_root: Path | None) -> dict[str, object]:
    """Submit through the real BigQuery client; import failure stays fixed-shape."""
    try:
        from . import bigquery_client as _bq
    except ImportError:
        return {"error": "bigquery client unavailable", "error_type": "source_unavailable", "source": "bigquery"}
    try:
        return _bq.submit_template(template, params, data_root=data_root)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return _submit_failure(exc)


def _direct_submit(template: str, params: dict[str, object], executor: _Executor) -> dict[str, object]:
    """Submit through a caller-provided callable or test-double client."""
    try:
        if callable(executor):
            return executor(template, params)
        return executor.submit_template(template, params)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return _submit_failure(exc)


def _submit(
    template: str, params: dict[str, object], executor: _Executor | None, data_root: Path | None
) -> dict[str, object]:
    if executor is None:
        return _submit_via_client(template, params, data_root)
    return _direct_submit(template, params, executor)


_UNAVAILABLE = frozenset(
    {
        "billing_enabled",
        "billing_unknown",
        "cost_limit_exceeded",
        "monthly_limit_exceeded",
        "daily_limit_exceeded",
        "source_unavailable",
        "ledger_corrupt",
    }
)


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
    return {"status": "disabled", "source": SOURCE, "reason": "google data disabled or no BigQuery project"}


def _project_error() -> dict[str, object] | None:
    """Disabled error when data is off or no project, else None."""
    if not _data_enabled():
        return _disabled_error()
    if not _project_name():
        return _disabled_error()
    return None


def _clean_tags(tags: list[str] | str | None) -> list[str]:
    """Caller tags as a stripped non-empty list."""
    if isinstance(tags, str):
        tags = [tags]
    return [t for t in (tags or []) if t and t.strip()]


def _clamp_limit(limit: int) -> tuple[dict[str, object] | None, int]:
    """Clamped limit or (invalid-params error, max)."""
    try:
        return None, max(1, min(limit, _MAX_LIMIT))
    except TypeError, ValueError:
        return (
            {"status": "error", "source": SOURCE, "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"},
            _MAX_LIMIT,
        )


def _executor_error(result: dict[str, object]) -> dict[str, object]:
    """Normalize an error executor payload to a fixed-shape error."""
    out = dict(result)
    out["status"] = "unavailable" if out.get("error_type") in _UNAVAILABLE else "error"
    out["engine"] = out.get("source", "bigquery")
    out["source"] = SOURCE
    return out


def _activity_row(row: object) -> dict[str, object] | None:
    """One normalized tag-activity dict; non-dict rows become None."""
    if not isinstance(row, dict):
        return None
    questions = row.get("question_count", row.get("count"))
    return {
        "tag": row.get("tag"),
        "period": str(row.get("period") or row.get("date") or row.get("week") or ""),
        "activity_count": questions,
        "question_count": questions,
        "total_views": row.get("total_views"),
        "avg_views": row.get("avg_views"),
        "accepted_count": row.get("accepted_count"),
    }


def _resolve_window(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """Effective (start, end) ISO dates; defaults are epoch start and today."""
    end = end_date or datetime.now(UTC).date().isoformat()
    return start_date or _EPOCH_START, end


def _submit_error(result: object) -> dict[str, object] | None:
    """Normalized executor error, or None when the result is usable."""
    if isinstance(result, dict) and "error" not in result:
        return None
    if isinstance(result, dict) and "status" in result:
        return result
    return _executor_error(result if isinstance(result, dict) else {"error": "bad executor result"})


def _collect_activity(result: dict[str, object], limit: int) -> list[dict[str, object]]:
    """Normalized activity rows up to limit; non-dict rows are skipped."""
    activity: list[dict[str, object]] = []
    for row in result_rows(result)[:limit]:
        item = _activity_row(row)
        if item is not None:
            activity.append(item)
    return activity


def _last_covered(result: dict[str, object], activity: list[dict[str, object]]) -> object:
    """Latest activity period, falling back to the executor's last_covered."""
    periods = sorted({str(a["period"]) for a in activity if a.get("period")})
    return periods[-1] if periods else result.get("last_covered")


def _stale_warning(end_date: str, last_covered: object) -> list[str]:
    """Stale-snapshot warning when coverage lags the request end."""
    if not last_covered:
        return []
    try:
        lag = (date.fromisoformat(end_date[:10]) - date.fromisoformat(str(last_covered)[:10])).days
    except ValueError:
        return []
    if lag > _STALE_LAG_DAYS:
        return [
            (
                f"stale_snapshot: last covered {last_covered} lags request "
                f"end {end_date} by {lag}d; treat as historical activity, not current adoption"
            )
        ]
    return []


def _ok_payload(activity: list[dict[str, object]], last_covered: object, warnings: list[str]) -> dict[str, object]:
    """Fixed-shape ok payload with coverage and extraction time."""
    return {
        "status": "ok",
        "source": SOURCE,
        "activity": activity,
        "count": len(activity),
        "last_covered": last_covered,
        "warnings": warnings,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


def get_tag_activity(
    tags: list[str] | str | None,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
    executor: _Executor | None = None,
    data_root: Path | None = None,
) -> dict[str, object]:
    """Observed tag activity per tag/period with last-covered date."""
    cleaned = _clean_tags(tags)
    if not cleaned:
        return {
            "status": "error",
            "source": SOURCE,
            "error": "at least one tag is required",
            "error_type": "invalid_params",
        }
    lim_err, limit = _clamp_limit(limit)
    if lim_err is not None:
        return lim_err
    proj_err = _project_error()
    if proj_err is not None:
        return proj_err
    start_date, end_date = _resolve_window(start_date, end_date)
    result = _submit(
        _TEMPLATE,
        {
            "tags": cleaned,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
            "collector_version": "1",
            "sql_version": "1",
        },
        executor,
        data_root,
    )
    failed = _submit_error(result)
    if failed is not None:
        return failed
    activity = _collect_activity(result, limit)
    last = _last_covered(result, activity)
    return _ok_payload(activity, last, _stale_warning(end_date, last))
