"""Private FINRA analysis layer: deterministic summaries only.

finra_client hands raw FINRA rows to this module; it returns a compact,
source-linked structured analysis. Raw rows never leave this module toward
the model. There is no secondary phrasing model: briefings are pure
deterministic functions of the spec plus rows.
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Optional

ANALYSIS_MAX_RECORDS = 500
MAX_CATEGORIES = 20
MAX_TRENDS = 8
MAX_WARNINGS = 8

_NUMERIC_TYPE_HINTS = (
    "long",
    "integer",
    "int",
    "decimal",
    "double",
    "float",
    "short",
    "byte",
    "number",
)


def analyze_and_brief(
    spec: Any,
    records: list[dict],
    analysis_goal: Optional[str],
    query_key: str,
    pagination: Optional[dict] = None,
) -> dict:
    """Deterministic summary of FINRA rows. Pure function; never raises.

    analysis_goal and query_key are accepted for caller compatibility and
    ignored: with no phrasing model there is nothing to phrase or cache.
    Raw rows are never exposed and the request never fails.

    pagination: parsed FINRA pagination metadata (total_records, offset, ...)
    used to prove full-query coverage in the coverage block.
    """
    return summarize_records(spec, records, pagination)


def summarize_records(
    spec: Any, records: list[dict], pagination: Optional[dict] = None
) -> dict:
    """Deterministic summaries. Pure function of spec + rows; never raises.

    Coverage distinguishes retrieved-page coverage from full-query coverage:
      - page_complete: every returned page row was analyzed.
      - query_complete: this page holds every FINRA match (Record-Total based).
      - analysis_complete: deterministic metrics cover every matching record.
    When FINRA omits Record-Total, completeness cannot be proven: the
    query/analysis flags are null and a pagination-estimate warning is added.
    """
    total = len(records)
    capped = total > ANALYSIS_MAX_RECORDS
    rows = records[:ANALYSIS_MAX_RECORDS] if capped else records
    analyzed = len(rows)

    date_field = spec.date_field
    if date_field and rows:
        rows = sorted(
            rows,
            key=lambda r: (
                r.get(date_field) is None,
                _norm_date(r.get(date_field)),
            ),
        )

    numeric_fields = _numeric_fields(spec)
    field_stats = _numeric_metrics(rows, numeric_fields)
    latest_prior = _latest_prior(rows, date_field, numeric_fields)

    page_complete = analyzed == total
    query_complete = _query_complete(pagination, total)
    analysis_complete = (
        None
        if query_complete is None
        else bool(query_complete and page_complete and not capped)
    )

    coverage: dict[str, Any] = {
        "rows_matched": total,
        "rows_analyzed": analyzed,
        "complete": not capped,
        "page_complete": page_complete,
        "query_complete": query_complete,
        "analysis_complete": analysis_complete,
        "cap": ANALYSIS_MAX_RECORDS if capped else None,
    }
    first_date, last_date = _coverage_dates(rows, date_field)
    if first_date is not None:
        coverage["first_date"] = first_date
        coverage["last_date"] = last_date

    warnings = _missing_warnings(rows, spec)
    if query_complete is None:
        warnings.append(
            "FINRA did not return a Record-Total header; pagination is "
            "estimated and full-query completeness cannot be proven."
        )
    if capped:
        warnings.append(
            f"Analysis stopped at the internal cap of {ANALYSIS_MAX_RECORDS} "
            f"records ({total} matched); metrics cover the first {analyzed} "
            "records only."
        )
    warnings = warnings[:MAX_WARNINGS]

    return {
        "coverage": coverage,
        "metrics": {
            "fields": field_stats,
            "latest_vs_prior": latest_prior,
            "categorical": _categorical_breakdowns(spec, rows),
        },
        "trends": _derive_trends(latest_prior),
        "warnings": warnings,
        "briefing": None,
        "briefing_source": "deterministic_only",
        "analysis_model": None,
    }


# ---------------------------------------------------------------------------
# Deterministic summaries
# ---------------------------------------------------------------------------


def _numeric_fields(spec: Any) -> list[str]:
    out = []
    for f in spec.fields:
        name = f.get("name")
        if not name:
            continue
        t = str(f.get("type") or "").lower()
        if any(hint in t for hint in _NUMERIC_TYPE_HINTS) and "date" not in t:
            out.append(name)
    return out


def _to_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _fmt(value: float) -> Any:
    if value.is_integer():
        return int(value)
    return round(value, 4)


def _numeric_metrics(rows: list[dict], numeric_fields: list[str]) -> dict:
    metrics: dict[str, Any] = {}
    for name in numeric_fields:
        values = [_to_number(r.get(name)) for r in rows]
        present = [v for v in values if v is not None]
        if not present:
            continue
        entry: dict[str, Any] = {
            "min": _fmt(min(present)),
            "max": _fmt(max(present)),
            "mean": _fmt(statistics.fmean(present)),
            "median": _fmt(statistics.median(present)),
            "sum": _fmt(sum(present)),
        }
        if len(present) != len(values):
            entry["missing"] = len(values) - len(present)
        metrics[name] = entry
    return metrics


def _latest_prior(
    rows: list[dict], date_field: Optional[str], numeric_fields: list[str]
) -> list[dict]:
    """Latest-vs-prior values over date-ascending rows (last two rows)."""
    if len(rows) < 2:
        return []
    latest = rows[-1]
    prior = rows[-2]
    if (
        date_field
        and _norm_date(latest.get(date_field)) == _norm_date(prior.get(date_field))
    ):
        return []
    out = []
    for name in numeric_fields:
        cur = _to_number(latest.get(name))
        prev = _to_number(prior.get(name))
        if cur is None or prev is None:
            continue
        delta = round(cur - prev, 4)
        pct = None if prev == 0 else round((delta / prev) * 100, 2)
        out.append(
            {
                "field": name,
                "latest": _fmt(cur),
                "prior": _fmt(prev),
                "change": _fmt(delta),
                "change_percent": pct,
                "latest_date": _norm_date(latest.get(date_field)) if date_field else None,
                "prior_date": _norm_date(prior.get(date_field)) if date_field else None,
            }
        )
    return out


def _derive_trends(latest_prior: list[dict]) -> list[str]:
    trends = []
    for lp in latest_prior[:MAX_TRENDS]:
        if lp["change_percent"] is None:
            trends.append(
                f"{lp['field']}: {lp['latest']} vs prior {lp['prior']} "
                f"(change {lp['change']:+,})"
            )
        else:
            direction = (
                "up" if lp["change_percent"] > 0
                else "down" if lp["change_percent"] < 0
                else "flat"
            )
            trends.append(
                f"{lp['field']}: {lp['latest']} vs prior {lp['prior']} "
                f"({lp['change']:+,}, {lp['change_percent']:+.2f}%) — {direction}"
            )
    return trends


def _categorical_breakdowns(spec: Any, rows: list[dict]) -> dict:
    out: dict[str, Any] = {}
    symbol_field = spec.symbol_field
    for f in spec.fields:
        name = f.get("name")
        if not name:
            continue
        t = str(f.get("type") or "").lower()
        if any(hint in t for hint in _NUMERIC_TYPE_HINTS) or "date" in t:
            continue
        if name == symbol_field:
            continue
        counts: dict[str, int] = {}
        for r in rows:
            v = r.get(name)
            if v is None or v == "":
                continue
            key = str(v)
            counts[key] = counts.get(key, 0) + 1
        if not counts or len(counts) > MAX_CATEGORIES:
            continue
        if len(counts) < 2:
            continue  # a constant column adds no breakdown value
        out[name] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return out


def _missing_warnings(rows: list[dict], spec: Any) -> list[str]:
    warnings = []
    if not rows:
        return warnings
    for f in spec.fields:
        name = f.get("name")
        if not name:
            continue
        missing = sum(1 for r in rows if r.get(name) is None or r.get(name) == "")
        if missing:
            warnings.append(
                f"Field '{name}' missing in {missing}/{len(rows)} analyzed rows."
            )
    return warnings


def _norm_date(value: Any) -> Any:
    if value is None:
        return None
    s = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", s):
        return s[:10]
    return s


def _coverage_dates(
    rows: list[dict], date_field: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    if not date_field:
        return None, None
    dates = [_norm_date(r.get(date_field)) for r in rows]
    dates = [d for d in dates if d]
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _query_complete(pagination: Optional[dict], returned_count: int) -> Optional[bool]:
    """Whether this page holds every FINRA match, per Record-Total.

    None when FINRA omits Record-Total (completeness cannot be proven).
    """
    if not pagination:
        return None
    total_records = pagination.get("total_records")
    if total_records is None:
        return None
    offset = int(pagination.get("offset") or 0)
    return (offset + returned_count) >= int(total_records)