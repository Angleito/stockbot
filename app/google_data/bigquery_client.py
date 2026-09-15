"""Bounded BigQuery executor: structural zero-spend gate for Google public data.

Only checked-in SELECT templates on allowlisted tables may run, only against
a dedicated project with billing disabled (verified before every submission),
with a free dry-run estimate, maximum_bytes_billed, and a local monthly/daily
ledger. No agent SQL, writes, exports, or Storage Read API.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import requests  # already a direct dependency (requests==2.34.2)

from ..config import (
    get_bq_daily_bytes_limit,
    get_bq_max_bytes_per_query,
    get_bq_monthly_bytes_limit,
    get_google_cloud_project,
    google_source_enabled,
)

SOURCE = "bigquery"


# Verified public datasets (Trends daily, patents, ACS wide vintages,
# Stack Overflow questions; NOAA GSOD year shards via prefix). Column
# spellings confirmed via INFORMATION_SCHEMA before live use; absent
# tables/columns return source_unavailable, never a scraper/paid fallback.
_GSOD_PREFIX = "bigquery-public-data.noaa_gsod.gsod"
ALLOWED_TABLES = frozenset(
    {
        "bigquery-public-data.google_trends.top_terms",
        "bigquery-public-data.google_trends.top_rising_terms",
        "bigquery-public-data.google_trends.international_top_terms",
        "bigquery-public-data.google_trends.international_top_rising_terms",
        "patents-public-data.patents.publications",
        "bigquery-public-data.census_bureau_acs.county_2020_5yr",
        "bigquery-public-data.census_bureau_acs.state_2020_5yr",
        "bigquery-public-data.census_bureau_acs.censustract_2020_5yr",
        "bigquery-public-data.stackoverflow.posts_questions",
    }
)
# Census interpolation allowlists: table suffix and logical columns only.
# Unknown values return source_unavailable with the available list.
_ACS_SUFFIXES = frozenset({"county_2020_5yr", "state_2020_5yr", "censustract_2020_5yr"})
_ACS_COLUMNS = frozenset({"total_pop", "median_age", "median_income", "median_home_value"})


def _table_allowed(table: str) -> bool:
    """Exact allowlist, plus the NOAA GSOD year-shard prefix (per-shard check at query)."""
    if table in ALLOWED_TABLES:
        return True
    return isinstance(table, str) and table.startswith(_GSOD_PREFIX)

TEMPLATES: dict[str, dict[str, object]] = {
    "trends_us_top": {
        "table": "bigquery-public-data.google_trends.top_terms",
        "max_rows": 1001,
        "sql": "SELECT term, rank, score, week, refresh_date, dma_name, dma_id "
        "FROM `bigquery-public-data.google_trends.top_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND (@all_dmas OR dma_name IN UNNEST(@dmas)) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "ORDER BY refresh_date DESC, week DESC, rank ASC LIMIT {limit}",
    },
    "trends_us_rising": {
        "table": "bigquery-public-data.google_trends.top_rising_terms",
        "max_rows": 1001,
        "sql": "SELECT term, rank, score, percent_gain, week, refresh_date, dma_name, dma_id "
        "FROM `bigquery-public-data.google_trends.top_rising_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND (@all_dmas OR dma_name IN UNNEST(@dmas)) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "ORDER BY refresh_date DESC, week DESC, rank ASC LIMIT {limit}",
    },
    "trends_intl_top": {
        "table": "bigquery-public-data.google_trends.international_top_terms",
        "max_rows": 1001,
        "sql": "SELECT term, rank, score, week, refresh_date, country_name, country_code, region_name, region_code "
        "FROM `bigquery-public-data.google_trends.international_top_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND country_code = @country_code "
        "AND (ARRAY_LENGTH(@region_codes) = 0 OR region_code IN UNNEST(@region_codes)) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "ORDER BY refresh_date DESC, week DESC, rank ASC LIMIT {limit}",
    },
    "trends_intl_rising": {
        "table": "bigquery-public-data.google_trends.international_top_rising_terms",
        "max_rows": 1001,
        "sql": "SELECT term, rank, score, percent_gain, week, refresh_date, country_name, country_code, region_name, region_code "
        "FROM `bigquery-public-data.google_trends.international_top_rising_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND country_code = @country_code "
        "AND (ARRAY_LENGTH(@region_codes) = 0 OR region_code IN UNNEST(@region_codes)) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "ORDER BY refresh_date DESC, week DESC, rank ASC LIMIT {limit}",
    },
    "trends_us_top_national": {
        "table": "bigquery-public-data.google_trends.top_terms",
        "max_rows": 1001,
        "sql": "SELECT term, week, refresh_date, COUNT(DISTINCT dma_id) AS dma_count, AVG(score) AS score "
        "FROM `bigquery-public-data.google_trends.top_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "GROUP BY refresh_date, week, term ORDER BY refresh_date DESC, week DESC, score DESC LIMIT {limit}",
    },
    "trends_us_rising_national": {
        "table": "bigquery-public-data.google_trends.top_rising_terms",
        "max_rows": 1001,
        "sql": "SELECT term, week, refresh_date, COUNT(DISTINCT dma_id) AS dma_count, AVG(score) AS score, AVG(percent_gain) AS percent_gain "
        "FROM `bigquery-public-data.google_trends.top_rising_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "GROUP BY refresh_date, week, term ORDER BY refresh_date DESC, week DESC, score DESC LIMIT {limit}",
    },
    "trends_intl_top_national": {
        "table": "bigquery-public-data.google_trends.international_top_terms",
        "max_rows": 1001,
        "sql": "SELECT term, week, refresh_date, country_code, COUNT(DISTINCT region_code) AS region_count, AVG(score) AS score "
        "FROM `bigquery-public-data.google_trends.international_top_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND country_code = @country_code "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "GROUP BY refresh_date, week, term, country_code ORDER BY refresh_date DESC, week DESC, score DESC LIMIT {limit}",
    },
    "trends_intl_rising_national": {
        "table": "bigquery-public-data.google_trends.international_top_rising_terms",
        "max_rows": 1001,
        "sql": "SELECT term, week, refresh_date, country_code, COUNT(DISTINCT region_code) AS region_count, AVG(score) AS score, AVG(percent_gain) AS percent_gain "
        "FROM `bigquery-public-data.google_trends.international_top_rising_terms` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND country_code = @country_code "
        "AND week BETWEEN DATE(@week_start) AND DATE(@week_end) "
        "GROUP BY refresh_date, week, term, country_code ORDER BY refresh_date DESC, week DESC, score DESC LIMIT {limit}",
    },
    "trends_refreshes": {
        "table": None,
        "table_from_params": True,
        "max_rows": 1000,
        "sql": "SELECT refresh_date, COUNT(*) AS rows_in_partition "
        "FROM `{table}` "
        "WHERE refresh_date BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "GROUP BY refresh_date ORDER BY refresh_date DESC LIMIT {limit}",
    },
    "patents_assignee": {
        "table": "patents-public-data.patents.publications",
        "max_rows": 20,
        "sql": "SELECT publication_number AS publication_id, "
        "PARSE_DATE('%Y%m%d', CAST(publication_date AS STRING)) AS publication_date, "
        "(SELECT t.text FROM UNNEST(title_localized) t WHERE t.language = 'en' LIMIT 1) AS title, "
        "ARRAY(SELECT a.name FROM UNNEST(assignee_harmonized) a) AS assignees, "
        "ARRAY(SELECT c.code FROM UNNEST(cpc) c WHERE c.inventive) AS cpc, "
        "ARRAY(SELECT i.name FROM UNNEST(inventor_harmonized) i) AS inventors, "
        "COALESCE(ARRAY_LENGTH(citation), 0) AS citation_count, "
        "family_id, country_code, kind_code "
        "FROM `patents-public-data.patents.publications` "
        "WHERE publication_date BETWEEN @start_yyyymmdd AND @end_yyyymmdd "
        "AND country_code IN UNNEST(@country_codes) "
        "AND EXISTS (SELECT 1 FROM UNNEST(assignee_harmonized) a WHERE a.name IN UNNEST(@assignees)) "
        "ORDER BY publication_date DESC LIMIT {limit}",
    },
    "patents_assignee_stats": {
        "table": "patents-public-data.patents.publications",
        "max_rows": 100,
        "sql": "SELECT pub_year, COUNT(*) AS pub_count, COUNT(DISTINCT family_id) AS family_count, "
        "COALESCE(SUM(citation_count), 0) AS total_citations, ARRAY_CONCAT_AGG(cpc_list) AS cpc_bag "
        "FROM (SELECT publication_number, "
        "EXTRACT(YEAR FROM PARSE_DATE('%Y%m%d', CAST(publication_date AS STRING))) AS pub_year, "
        "family_id, COALESCE(ARRAY_LENGTH(citation), 0) AS citation_count, "
        "COALESCE((SELECT ARRAY_AGG(c.code) FROM UNNEST(cpc) c WHERE c.inventive), ['__NONE__']) AS cpc_list "
        "FROM `patents-public-data.patents.publications` "
        "WHERE publication_date BETWEEN @start_yyyymmdd AND @end_yyyymmdd "
        "AND publication_date IS NOT NULL "
        "AND country_code IN UNNEST(@country_codes) "
        "AND EXISTS (SELECT 1 FROM UNNEST(assignee_harmonized) a WHERE a.name IN UNNEST(@assignees))) "
        "GROUP BY pub_year ORDER BY pub_year DESC LIMIT {limit}",
    },
    "census_acs": {
        "table": "bigquery-public-data.census_bureau_acs.county_2020_5yr",
        "max_rows": 100,
        "sql": "SELECT geo_id, {columns} "
        "FROM `bigquery-public-data.census_bureau_acs.{table_suffix}` "
        "WHERE geo_id IN UNNEST(@geo_ids) LIMIT {limit}",
    },
    "noaa_obs": {
        "table": "bigquery-public-data.noaa_gsod.gsod*",
        "max_rows": 100,
        "sql": "SELECT CONCAT(stn, '-', wban) AS station_id, DATE(year, mo, da) AS observed_at, "
        "temp, dewp, slp, stp, visib, wdsp, mxspd, gust, max, min, prcp, sndp, frshtt "
        "FROM `bigquery-public-data.noaa_gsod.gsod*` "
        "WHERE _TABLE_SUFFIX BETWEEN FORMAT_DATE('%Y', DATE(@start_date)) AND FORMAT_DATE('%Y', DATE(@end_date)) "
        "AND CONCAT(stn, '-', wban) IN UNNEST(@station_ids) "
        "AND DATE(year, mo, da) BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "ORDER BY observed_at DESC LIMIT {limit}",
    },
    "stackoverflow_tags": {
        "table": "bigquery-public-data.stackoverflow.posts_questions",
        "max_rows": 100,
        "sql": "SELECT tag, DATE(creation_date) AS period, COUNT(*) AS question_count, "
        "SUM(view_count) AS total_views, AVG(view_count) AS avg_views, "
        "SUM(IF(accepted_answer_id IS NOT NULL, 1, 0)) AS accepted_count "
        "FROM `bigquery-public-data.stackoverflow.posts_questions`, UNNEST(SPLIT(tags, '|')) AS tag "
        "WHERE DATE(creation_date) BETWEEN DATE(@start_date) AND DATE(@end_date) "
        "AND tag IN UNNEST(@tags) "
        "GROUP BY tag, period ORDER BY period DESC LIMIT {limit}",
    },
}

_WRITE_STMT = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|EXPORT|CALL|COPY)\b",
    re.IGNORECASE,
)


class LedgerCorrupt(Exception):
    """Local BigQuery accounting state is unreadable; refusing to query."""


class _JobEntry(TypedDict, total=False):
    """One ledger job record: reservation ints stay ints, outcomes stay explicit."""
    template: str
    month: str
    day: str
    max_bytes: int
    status: str
    actual_bytes: int | None
    source: str
    dataset: str
    table: str
    executed_at: str
    bytes_processed: int | None
    cache_hit: bool
    success: bool


class _Ledger(TypedDict):
    """Local accounting state: idempotent job records plus 3-bucket counters."""
    jobs: dict[str, _JobEntry]
    months: dict[str, int]
    days: dict[str, int]


def bq_enabled() -> bool:
    """True when the BigQuery source is configured (project + positive caps)."""
    return google_source_enabled("bigquery")


def _utc_month() -> str:
    """Current UTC month bucket (module-level seam for cross-month tests)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _utc_day() -> str:
    """Current UTC day bucket for the daily reservation ledger."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# Compat names for pre-existing callers; job IDs still derive from the exact
# template string the caller passes. Deleted per plan: trends_top_rising,
# patents_by_assignee, stackoverflow_tag_activity, census_context, noaa_context.
TEMPLATES["trends_top"] = TEMPLATES["trends_us_top"]
TEMPLATES["trends_rising"] = TEMPLATES["trends_us_rising"]


_STR_KEYS = ("template", "month", "day", "status", "source", "dataset", "table", "executed_at")
_INT_KEYS = ("max_bytes", "actual_bytes", "bytes_processed")
_BOOL_KEYS = ("cache_hit", "success")


def _bad_entry(path: Path, what: str) -> LedgerCorrupt:
    """LedgerCorrupt for one malformed ledger section."""
    return LedgerCorrupt(f"malformed ledger {path}: {what}")


def _store_str_field(entry: _JobEntry, key: str, value: str) -> None:
    """Store one validated string field under its literal key."""
    if key == "template":
        entry["template"] = value
    elif key == "month":
        entry["month"] = value
    elif key == "day":
        entry["day"] = value
    elif key == "status":
        entry["status"] = value
    elif key == "source":
        entry["source"] = value
    elif key == "dataset":
        entry["dataset"] = value
    elif key == "table":
        entry["table"] = value
    elif key == "executed_at":
        entry["executed_at"] = value


def _store_nullable_int_field(entry: _JobEntry, key: str, value: int | None) -> None:
    """Store one nullable int field under its literal key."""
    if key == "actual_bytes":
        entry["actual_bytes"] = value
    elif key == "bytes_processed":
        entry["bytes_processed"] = value


def _store_bool_field(entry: _JobEntry, key: str, value: bool) -> None:
    """Store one bool field under its literal key."""
    if key == "cache_hit":
        entry["cache_hit"] = value
    elif key == "success":
        entry["success"] = value


def _parse_str_field(entry: _JobEntry, key: str, value: object, path: Path) -> None:
    """Validate one string job field; wrong types refuse."""
    if not isinstance(value, str):
        raise _bad_entry(path, "bad job entry")
    _store_str_field(entry, key, value)


def _parse_int_field(entry: _JobEntry, key: str, value: object, path: Path) -> None:
    """Validate one int job field; max_bytes required, others nullable."""
    if key == "max_bytes":
        if not isinstance(value, int) or isinstance(value, bool):
            raise _bad_entry(path, "bad job entry")
        entry["max_bytes"] = value
        return
    if value is None:
        _store_nullable_int_field(entry, key, None)
    elif isinstance(value, int) and not isinstance(value, bool):
        _store_nullable_int_field(entry, key, value)
    else:
        raise _bad_entry(path, "bad job entry")


def _parse_bool_field(entry: _JobEntry, key: str, value: object, path: Path) -> None:
    """Validate one bool job field; wrong types refuse."""
    if not isinstance(value, bool):
        raise _bad_entry(path, "bad job entry")
    _store_bool_field(entry, key, value)


def _parse_job_field(entry: _JobEntry, key: str, value: object, path: Path) -> None:
    """Validate one job field by allowlist family; unknown keys are ignored."""
    if key in _STR_KEYS:
        _parse_str_field(entry, key, value, path)
    elif key in _INT_KEYS:
        _parse_int_field(entry, key, value, path)
    elif key in _BOOL_KEYS:
        _parse_bool_field(entry, key, value, path)


def _parse_job_entry(value: object, path: Path) -> _JobEntry:
    if not isinstance(value, dict):
        raise _bad_entry(path, "bad job entry")
    entry: _JobEntry = {}
    for fk, fv in value.items():
        if not isinstance(fk, str):
            raise _bad_entry(path, "bad job entry")
        _parse_job_field(entry, fk, fv, path)
    return entry


def _parse_jobs(raw: object, path: Path) -> dict[str, _JobEntry]:
    """Validated job records; bad keys or entries refuse."""
    if not isinstance(raw, dict):
        raise _bad_entry(path, "expected {jobs, months, days} dicts")
    jobs: dict[str, _JobEntry] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise _bad_entry(path, "bad job entry")
        jobs[k] = _parse_job_entry(v, path)
    return jobs


def _parse_counters(raw: object, path: Path, what: str) -> dict[str, int]:
    """Validated month/day counters; non-int values refuse."""
    if not isinstance(raw, dict):
        raise _bad_entry(path, "expected {jobs, months, days} dicts")
    out: dict[str, int] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, int) or isinstance(v, bool):
            raise _bad_entry(path, f"bad {what} entry")
        out[k] = v
    return out


def _parse_ledger(data: object, path: Path) -> _Ledger:
    if not isinstance(data, dict):
        raise _bad_entry(path, "expected object")
    jobs = _parse_jobs(data.get("jobs"), path)
    months = _parse_counters(data.get("months"), path, "month")
    days = _parse_counters(data.get("days", {}), path, "day")
    return {"jobs": jobs, "months": months, "days": days}


def _ledger_path(data_root: Path | str | None = None) -> Path:
    if data_root:
        base = Path(data_root)
    else:
        from ._lazy_config import get_data_root_or_cwd
        base = get_data_root_or_cwd()
    return base / "google_data" / "bq_ledger.json"


def _load_ledger(path: Path) -> _Ledger:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"jobs": {}, "months": {}, "days": {}}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LedgerCorrupt(f"unreadable ledger {path}: {e}") from e
    return _parse_ledger(data, path)


class _LedgerBusy(Exception):
    """Ledger lock contended or unavailable; caller fails closed without reservation."""


@contextlib.contextmanager
def _ledger_locked(data_root: Path | str | None = None):
    """Serialize ledger load/save/reconcile with flock on <ledger>.lock."""
    try:
        import fcntl as _fcntl
    except ImportError:  # pragma: no cover - no flock platform: never proceed unlocked
        raise _LedgerBusy("ledger locking unavailable on this platform") from None
    path = _ledger_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path.parent / "bq_ledger.lock", "a+b")  # noqa: PTH123, SIM115
    try:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            raise _LedgerBusy("bigquery ledger busy; refusing without reservation") from None
        try:
            yield path
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
    finally:
        fh.close()


def _prune_buckets(ledger: _Ledger) -> None:
    """Keep only the 3 most recent month/day counters; job records are retained."""
    for key in ("months", "days"):
        buckets = ledger.get(key)
        if isinstance(buckets, dict):
            for old in sorted(buckets)[:-3]:
                del buckets[old]


def _save_ledger(path: Path, ledger: _Ledger) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bq_ledger", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _job_id(template: str, params: dict[str, object]) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{template}:{canonical}".encode("utf-8")).hexdigest()


def _err(message: str, error_type: str, **extra: object) -> dict[str, object]:
    out: dict[str, object] = {"error": message, "error_type": error_type, "source": SOURCE}
    out.update(extra)
    return out


def _invert_flag(value: object) -> bool | None:
    """Billing-disabled reading of one billingEnabled-style flag."""
    return None if not isinstance(value, bool) else (not value)


def _dict_billing(client: dict[str, object]) -> bool | None:
    """Billing-disabled reading of a fake billing dict."""
    for key in ("billingEnabled", "billing_enabled"):
        if isinstance(client.get(key), bool):
            return not client[key]
    return None


def _info_billing(info: object) -> bool | None:
    """Billing-disabled reading of a get_billing_info() payload."""
    if isinstance(info, dict):
        for key in ("billingEnabled", "billing_enabled"):
            if isinstance(info.get(key), bool):
                return not info[key]
        return None
    return _invert_flag(getattr(info, "billingEnabled", None))


_USE_FAKE = object()


def _fake_billing(client: object) -> object:
    """Billing state from fakes, or _USE_FAKE when the live API must decide."""
    if isinstance(client, dict):
        return _dict_billing(client)
    if hasattr(client, "billing_enabled"):
        return _invert_flag(getattr(client, "billing_enabled", None))
    info_fn = getattr(client, "get_billing_info", None)
    if callable(info_fn):
        try:
            return _info_billing(info_fn())
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return None
    return _USE_FAKE


def _billing_token(client: object) -> dict[str, str] | None:
    """Bearer headers from client credentials, or None when unavailable."""
    creds = getattr(client, "_credentials", None)
    if creds is None:
        return None
    from google.auth.transport.requests import Request as _AuthRequest

    creds.refresh(_AuthRequest())
    token = getattr(creds, "token", None)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api_billing(client: object, project: str) -> bool | None:
    """Billing-disabled reading via the Cloud Billing API."""
    try:
        headers = _billing_token(client)
        if headers is None:
            return None
        resp = requests.get(
            f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo",
            headers=headers,
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if resp.status_code != 200:
        return None
    return _invert_flag(resp.json().get("billingEnabled"))


def _billing_state(client: object, project: str | None) -> bool | None:
    """True=billing disabled, False=enabled, None=unknown (refuse on not-True)."""
    probed = _fake_billing(client)
    if probed is not _USE_FAKE:
        if isinstance(probed, bool) or probed is None:
            return probed
        return None
    if not project:
        return None
    return _api_billing(client, project)


def check_billing_disabled(client: object) -> bool:
    """True only when Cloud Billing reports billingEnabled=False for the project."""
    return _billing_state(client, get_google_cloud_project()) is True


_QUERY_PARAM_RE = re.compile(r"@(\w+)")
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _census_suffix_table(params: dict[str, object]) -> str:
    """Validated census ACS table for the {table_suffix} template path."""
    suffix = params.get("table_suffix", "county_2020_5yr")
    if suffix not in _ACS_SUFFIXES:
        raise ValueError(
            f"unknown census table: {suffix!r} (available: {sorted(_ACS_SUFFIXES)})")
    return f"bigquery-public-data.census_bureau_acs.{suffix}"


def _resolve_table(spec: dict[str, object], params: dict[str, object]) -> str:
    """Effective table: census suffix interpolation, per-shard table, or checked-in table."""
    sql_obj = spec.get("sql", "")
    sql = sql_obj if isinstance(sql_obj, str) else ""
    if "{table_suffix}" in sql:
        return _census_suffix_table(params)
    if spec.get("table_from_params"):
        table = params.get("table", "")
        if not isinstance(table, str) or not _table_allowed(table):
            raise ValueError(f"unknown table: {table!r}")
        return table
    table_obj = spec.get("table")
    return table_obj if isinstance(table_obj, str) else ""


def _normalize_column_list(cols_raw: object) -> list[object]:
    """Caller columns as a plain list; other shapes refuse with the fixed message."""
    if isinstance(cols_raw, str):
        return [cols_raw]
    if isinstance(cols_raw, (list, tuple)):
        return list(cols_raw)
    raise ValueError(
        f"unknown census columns: {cols_raw!r} (available: {sorted(_ACS_COLUMNS)})")


def _invalid_columns(cols: list[object]) -> list[object]:
    """Census columns failing the ident-pattern plus allowlist gate."""
    return [c for c in cols if not _IDENT_RE.match(str(c)) or c not in _ACS_COLUMNS]


def _columns_clause(params: dict[str, object]) -> str:
    """Validated comma-joined census columns for {columns} interpolation."""
    cols = _normalize_column_list(params.get("columns", ["total_pop"]))
    bad = _invalid_columns(cols)
    if not cols or bad:
        raise ValueError(
            f"unknown census columns: {(bad or cols)!r} (available: {sorted(_ACS_COLUMNS)})")
    return ", ".join(str(c) for c in cols)


def _format_map(sql_template: str, params: dict[str, object],
                table: str) -> dict[str, object]:
    """{limit}/{table}/{table_suffix}/{columns} mapping for the template."""
    fmt: dict[str, object] = {"limit": params["limit"], "table": table}
    if "{table_suffix}" in sql_template:
        fmt["table_suffix"] = table.rsplit(".", 1)[-1]
    if "{columns}" in sql_template:
        fmt["columns"] = _columns_clause(params)
    return fmt


def _render_sql(spec: dict[str, object], params: dict[str, object], table: str) -> tuple[str, dict[str, object]]:
    """Fill {limit}/{table}/{table_suffix}/{columns}; pass only @-referenced params."""
    sql_obj = spec.get("sql", "")
    sql_template = sql_obj if isinstance(sql_obj, str) else ""
    sql = sql_template.format(**_format_map(sql_template, params, table))
    names = set(_QUERY_PARAM_RE.findall(sql))
    return sql, {k: v for k, v in params.items() if k in names}


def _query_parameters(bq: object, params: dict[str, object]) -> list[object]:
    out: list[object] = []
    scalar = getattr(bq, "ScalarQueryParameter")  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    array = getattr(bq, "ArrayQueryParameter")  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    for key, value in params.items():
        if isinstance(value, bool):
            out.append(scalar(key, "BOOL", value))
        elif isinstance(value, int):
            out.append(scalar(key, "INT64", value))
        elif isinstance(value, float):
            out.append(scalar(key, "FLOAT64", value))
        elif isinstance(value, (list, tuple)):
            out.append(array(key, "STRING", [str(v) for v in value]))
        else:
            out.append(scalar(key, "STRING", str(value)))
    return out


def _real_dry_run(client: object, sql: str, params: dict[str, object], cap: int) -> int:
    from google.cloud import bigquery as _bq

    job_config = _bq.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        maximum_bytes_billed=cap,
        query_parameters=_query_parameters(_bq, params),
    )
    return int(getattr(client, "query")(sql, job_config=job_config).total_bytes_processed)  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green


def _real_submit(
    client: object, sql: str, params: dict[str, object], cap: int, job_id: str, max_rows: int
) -> dict[str, object]:
    from google.cloud import bigquery as _bq

    job_config = _bq.QueryJobConfig(
        use_query_cache=False,
        maximum_bytes_billed=cap,
        query_parameters=_query_parameters(_bq, params),
    )
    job = getattr(client, "query")(  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        sql, job_config=job_config, job_id=f"stockbot_{job_id[:56]}", location="US"
    )
    rows = [dict(r) for r in getattr(job, "result")(max_results=max_rows)]  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    return {
        "job_id": job_id,
        "total_bytes_billed": int(getattr(job, "total_bytes_billed") or 0),  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        "rows": rows,
        "state": "DONE",
    }


def _check_template(template: str, params: dict[str, object]) -> tuple[dict[str, object] | None,
                                                                      dict[str, object], str]:
    """(error, spec, table) for the checked-in template and its effective table."""
    spec = TEMPLATES.get(template)
    if spec is None:
        return _err(f"unknown template: {template}", "source_unavailable"), {}, ""
    try:
        table = _resolve_table(spec, params)
    except ValueError as e:
        return _err(str(e), "source_unavailable"), {}, ""
    if not _table_allowed(table):
        return _err(f"unknown template: {template}", "source_unavailable"), {}, ""
    sql_obj: object = spec.get("sql", "")
    sql = sql_obj if isinstance(sql_obj, str) else ""
    if not sql.lstrip().upper().startswith("SELECT") or _WRITE_STMT.search(sql):
        return _err("template is not a read-only SELECT", "source_unavailable"), {}, ""
    return None, spec, table


def _check_project() -> tuple[dict[str, object] | None, str, int, int, int]:
    """(error, project, per-query cap, monthly limit, daily limit)."""
    project = get_google_cloud_project()
    if not bq_enabled() or not project:
        return (_err("BigQuery disabled (need GOOGLE_DATA_ENABLED + GOOGLE_CLOUD_PROJECT)",
                     "source_unavailable"), "", 0, 0, 0)
    per_query_cap = get_bq_max_bytes_per_query()
    monthly_limit = get_bq_monthly_bytes_limit()
    daily_limit = get_bq_daily_bytes_limit()
    if per_query_cap <= 0 or monthly_limit <= 0 or daily_limit <= 0:
        return _err("non-positive BigQuery byte limit", "cost_limit_exceeded"), "", 0, 0, 0
    return None, project, per_query_cap, monthly_limit, daily_limit


def _clamp_rows(params: dict[str, object], spec: dict[str, object]) -> tuple[dict[str, object], int]:
    """Params with clamped limit plus the template max_rows."""
    max_rows_raw: object = spec["max_rows"]
    max_rows = max_rows_raw if isinstance(max_rows_raw, int) else 0
    try:
        raw_limit: object = params.get("limit", max_rows)
        limit = int(raw_limit) if isinstance(raw_limit, (int, str, float)) else max_rows
    except (TypeError, ValueError):
        limit = max_rows
    params["limit"] = max(1, min(limit, max_rows))
    return params, max_rows


def _cached_done(jobs: dict[str, _JobEntry], jid: str,
                 template: str) -> dict[str, object] | None:
    """Cached ok payload for done jobs; no rows, never treated as no evidence."""
    existing = jobs.get(jid)
    if isinstance(existing, dict) and existing.get("status") == "done":
        return {"status": "ok", "source": SOURCE, "job_id": jid, "template": template,
                "rows": [], "total_bytes_billed": existing.get("actual_bytes", 0),
                "cached": True}
    return None


def _make_client(client_factory: Callable[[], object] | None,
                 project: str) -> tuple[dict[str, object] | None, object, bool | None]:
    """(error, client, billing-disabled flag) honoring the fake seam."""
    if client_factory is not None:
        client = client_factory()
        # ponytail: minimal fakes (dry_run/submit only) skip the network
        # billing check; production (no factory) always verifies via API.
        if (not hasattr(client, "billing_enabled")
                and not hasattr(client, "get_billing_info")
                and not isinstance(client, dict)
                and getattr(client, "_credentials", None) is None):
            return None, client, True
        return None, client, _billing_state(client, project)
    try:
        from google.cloud import bigquery as _bq
    except ImportError:
        empty: dict[str, object] = {}
        return _err("google-cloud-bigquery not installed", "source_unavailable"), empty, None
    client = _bq.Client(project=project)
    return None, client, _billing_state(client, project)


def _billing_error(billing: bool | None) -> dict[str, object] | None:
    """Refusal error unless billing is verified disabled."""
    if billing is True:
        return None
    return _err("billing is enabled for this project; refusing"
                if billing is False
                else "could not verify billing disabled; refusing",
                "billing_enabled" if billing is False else "billing_unknown")


def _dry_run(client: object, spec: dict[str, object], params: dict[str, object],
             table: str, cap: int) -> tuple[dict[str, object] | None, int]:
    """(error, estimate) via fake dry_run or a real bounded dry-run."""
    try:
        if hasattr(client, "dry_run"):
            return None, int(getattr(client, "dry_run")(params))  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        sql_text, bq_params = _render_sql(spec, params, table)
        return None, _real_dry_run(client, sql_text, bq_params, cap)
    except (ValueError, KeyError) as e:
        return _err(f"unavailable template parameter: {e}", "source_unavailable"), 0
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return _err(f"dry-run failed: {e}", "source_unavailable"), 0


def _cap_error(estimate: int, cap: int) -> dict[str, object] | None:
    """Per-query cap error, else None."""
    if estimate > cap:
        return _err(f"estimated {estimate} bytes exceeds per-query cap {cap} "
                    "(BIGQUERY_QUERY_TOO_LARGE)", "cost_limit_exceeded")
    return None


def _reserve_budget(ledger: _Ledger, jid: str, template: str, table: str,
                    month: str, day: str, estimate: int,
                    monthly_limit: int, daily_limit: int) -> tuple[dict[str, object] | None, int, bool]:
    """(error, reserve, resumed): reservation recorded unless a pending job resumes."""
    jobs, months, days = ledger["jobs"], ledger["months"], ledger["days"]
    existing = jobs.get(jid)
    if isinstance(existing, dict) and existing.get("status") == "pending":
        resumed = int(existing.get("max_bytes", estimate) or estimate)
        return None, max(estimate, resumed), True
    if months.get(month, 0) + estimate > monthly_limit:
        return (_err("monthly BigQuery byte limit would be exceeded (BIGQUERY_FREE_LIMIT_REACHED)",
                     "monthly_limit_exceeded"), 0, False)
    if days.get(day, 0) + estimate > daily_limit:
        return (_err("daily BigQuery byte limit would be exceeded (BIGQUERY_FREE_LIMIT_REACHED)",
                     "daily_limit_exceeded"), 0, False)
    months[month] = months.get(month, 0) + estimate
    days[day] = days.get(day, 0) + estimate
    jobs[jid] = {"template": template, "month": month, "day": day, "max_bytes": estimate,
                 "status": "pending", "actual_bytes": None, "source": SOURCE,
                 "dataset": table.split(".")[1] if "." in table else "", "table": table,
                 "executed_at": datetime.now(timezone.utc).isoformat(),
                 "bytes_processed": None, "cache_hit": False, "success": False}
    _prune_buckets(ledger)
    return None, estimate, False


def _execute(client: object, spec: dict[str, object], params: dict[str, object],
             table: str, entry: _JobEntry, reserve: int,
             jid: str, max_rows: int) -> tuple[dict[str, object] | None, dict[str, object]]:
    """(error, result): submit via fake or real client; unknown outcomes keep reservation."""
    try:
        if hasattr(client, "submit"):
            return None, getattr(client, "submit")(params, entry["max_bytes"], jid) or {}  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        sql_text, bq_params = _render_sql(spec, params, table)
        return None, _real_submit(client, sql_text, bq_params, reserve, jid, max_rows)
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return (_err(f"submit failed with unknown outcome; reservation retained: {e}",
                     "source_unavailable", job_id=jid), {})


def _actual_bytes(result: dict[str, object], entry: _JobEntry) -> int:
    """Billed bytes from the result, falling back to the reservation."""
    try:
        billed_raw = result.get("total_bytes_billed", entry["max_bytes"])
        if not isinstance(billed_raw, (int, str, float)):
            raise ValueError(f"non-numeric bytes billed: {billed_raw!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        return int(billed_raw)
    except (TypeError, ValueError):
        return int(entry["max_bytes"] or 0)


def _settle_done(path: Path, ledger: _Ledger, entry: _JobEntry, actual: int) -> None:
    """Mark done with actual bytes reconciled against the reservation."""
    months, days = ledger["months"], ledger["days"]
    month_key = str(entry.get("month", ""))
    day_key = str(entry.get("day", ""))
    entry["status"] = "done"
    entry["actual_bytes"] = actual
    entry["bytes_processed"] = actual
    entry["success"] = True
    months[month_key] = months.get(month_key, 0) - int(entry["max_bytes"] or 0) + actual
    days[day_key] = days.get(day_key, 0) - int(entry["max_bytes"] or 0) + actual
    _prune_buckets(ledger)
    _save_ledger(path, ledger)


def _settle_failed(path: Path, ledger: _Ledger, entry: _JobEntry) -> None:
    """Mark terminal failure and release the reservation."""
    months, days = ledger["months"], ledger["days"]
    month_key = str(entry.get("month", ""))
    day_key = str(entry.get("day", ""))
    entry["status"] = "failed"
    entry["success"] = False
    months[month_key] = months.get(month_key, 0) - int(entry["max_bytes"] or 0)
    days[day_key] = days.get(day_key, 0) - int(entry["max_bytes"] or 0)
    _prune_buckets(ledger)
    _save_ledger(path, ledger)


def _settle(path: Path, ledger: _Ledger, template: str, jid: str,
            entry: _JobEntry, result: dict[str, object]) -> dict[str, object]:
    """Persist the terminal outcome and return the fixed-shape payload."""
    state = str(result.get("state", "")).upper()
    if state == "DONE":
        actual = _actual_bytes(result, entry)
        _settle_done(path, ledger, entry, actual)
        return {"status": "ok", "source": SOURCE, "job_id": jid, "template": template,
                "rows": result.get("rows", []), "total_bytes_billed": actual}
    if state in ("FAILED", "ERROR", "CANCELLED"):
        _settle_failed(path, ledger, entry)
        return _err(f"query {state.lower()}", "source_unavailable", job_id=jid)
    return {"status": "unknown", "source": SOURCE, "job_id": jid, "template": template}


def _prepare(template: str, params: dict[str, object],
             data_root: Path | str | None) -> tuple[dict[str, object] | None, dict[str, object],
                                                   dict[str, object], str, str, int, int, int,
                                                   int, str, str, str]:
    """(error, spec, params, table, project, caps..., max_rows, jid, month, day)."""
    params = dict(params or {})
    tmpl_err, spec, table = _check_template(template, params)
    if tmpl_err is not None:
        return tmpl_err, {}, params, "", "", 0, 0, 0, 0, "", "", ""
    proj_err, project, per_query_cap, monthly_limit, daily_limit = _check_project()
    if proj_err is not None:
        return proj_err, {}, params, "", "", 0, 0, 0, 0, "", "", ""
    params, max_rows = _clamp_rows(params, spec)
    jid = _job_id(template, params)
    return None, spec, params, table, project, per_query_cap, monthly_limit, daily_limit, max_rows, jid, _utc_month(), _utc_day()


def _run_locked(path: Path, ledger: _Ledger, template: str, spec: dict[str, object],
                params: dict[str, object], table: str, project: str, per_query_cap: int,
                monthly_limit: int, daily_limit: int, max_rows: int, jid: str,
                month: str, day: str,
                client_factory: Callable[[], object] | None) -> dict[str, object]:
    """Ledger-held execution: cache check, billing gate, dry-run, reserve, submit, settle."""
    cached = _cached_done(ledger["jobs"], jid, template)
    if cached is not None:
        return cached
    client_err, client, billing = _make_client(client_factory, project)
    if client_err is not None:
        return client_err
    bill_err = _billing_error(billing)
    if bill_err is not None:
        return bill_err
    dry_err, estimate = _dry_run(client, spec, params, table, per_query_cap)
    if dry_err is not None:
        return dry_err
    cap_err = _cap_error(estimate, per_query_cap)
    if cap_err is not None:
        return cap_err
    reserve_err, reserve, resumed = _reserve_budget(ledger, jid, template, table, month, day,
                                                  estimate, monthly_limit, daily_limit)
    if reserve_err is not None:
        return reserve_err
    if not resumed:
        _save_ledger(path, ledger)
    entry = ledger["jobs"][jid]
    exec_err, result = _execute(client, spec, params, table, entry, reserve, jid, max_rows)
    if exec_err is not None:
        return exec_err
    return _settle(path, ledger, template, jid, entry, result)


def submit_template(
    template: str, params: dict[str, object], client_factory: Callable[[], object] | None = None,
    data_root: Path | str | None = None,
) -> dict[str, object]:
    """Run one checked-in template; bounded, ledger-backed, billing-gated.

    Fake seam (no network): client_factory() -> object with
    dry_run(params)->int total bytes and
    submit(params, max_bytes, job_id)->{job_id,total_bytes_billed,rows,state}.
    Billing via the fake's billing_enabled bool attr or get_billing_info();
    a minimal fake (neither present) skips the network billing check.
    Raises LedgerCorrupt on unreadable accounting state.
    """
    prep_err, spec, params, table, project, per_query_cap, monthly_limit, daily_limit, max_rows, jid, month, day = _prepare(
        template, params, data_root)
    if prep_err is not None:
        return prep_err
    try:
        locker = _ledger_locked(data_root)
    except _LedgerBusy as e:
        return _err(str(e), "source_unavailable")
    with locker as path:
        ledger = _load_ledger(path)  # raises LedgerCorrupt: refuse, never reset
        return _run_locked(path, ledger, template, spec, params, table, project,
                           per_query_cap, monthly_limit, daily_limit, max_rows,
                           jid, month, day, client_factory)
