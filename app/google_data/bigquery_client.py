"""Bounded BigQuery executor: structural zero-spend gate for Google public data.

Only checked-in SELECT templates on allowlisted tables may run, only against
a dedicated project with billing disabled (verified before every submission),
with a free dry-run estimate, maximum_bytes_billed, and a local monthly/daily
ledger. No agent SQL, writes, exports, or Storage Read API.
"""

from __future__ import annotations

import contextlib
import hashlib
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None  # type: ignore[assignment]
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests  # already a direct dependency (requests==2.34.2)

from ..config import (
    get_bq_daily_bytes_limit,
    get_bq_max_bytes_per_query,
    get_bq_monthly_bytes_limit,
    get_google_cloud_project,
    google_source_enabled,
)

try:
    from ..config import get_data_root as _get_data_root
except Exception:  # pragma: no cover - config import never fails in practice
    _get_data_root = None

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

TEMPLATES: dict[str, dict[str, Any]] = {
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


def _ledger_path(data_root: Any = None) -> Path:
    base = Path(data_root) if data_root else (_get_data_root() if _get_data_root else Path("data"))
    return base / "google_data" / "bq_ledger.json"


def _load_ledger(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"jobs": {}, "months": {}, "days": {}}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise LedgerCorrupt(f"unreadable ledger {path}: {e}") from e
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("jobs"), dict)
        or not isinstance(data.get("months"), dict)
    ):
        raise LedgerCorrupt(f"malformed ledger {path}: expected {{jobs, months}}")
    if "days" not in data:
        data["days"] = {}
    if not isinstance(data["days"], dict):
        raise LedgerCorrupt(f"malformed ledger {path}: expected days dict")
    return data


class _LedgerBusy(Exception):
    """Ledger lock contended or unavailable; caller fails closed without reservation."""


@contextlib.contextmanager
def _ledger_locked(data_root: Any = None):
    """Serialize ledger load/save/reconcile with flock on <ledger>.lock."""
    if fcntl is None:  # pragma: no cover - no flock platform: never proceed unlocked
        raise _LedgerBusy("ledger locking unavailable on this platform")
    path = _ledger_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path.parent / "bq_ledger.lock", "a+b")  # noqa: PTH123, SIM115
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _LedgerBusy("bigquery ledger busy; refusing without reservation") from None
        try:
            yield path
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _prune_buckets(ledger: dict) -> None:
    """Keep only the 3 most recent month/day counters; job records are retained."""
    for key in ("months", "days"):
        buckets = ledger.get(key)
        if isinstance(buckets, dict):
            for old in sorted(buckets)[:-3]:
                del buckets[old]


def _save_ledger(path: Path, ledger: dict) -> None:
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


def _job_id(template: str, params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{template}:{canonical}".encode("utf-8")).hexdigest()


def _err(message: str, error_type: str, **extra: Any) -> dict:
    out = {"error": message, "error_type": error_type, "source": SOURCE}
    out.update(extra)
    return out


def _billing_state(client: Any, project: Optional[str]) -> Optional[bool]:
    """True=billing disabled, False=enabled, None=unknown (refuse on not-True)."""
    if isinstance(client, dict):
        for key in ("billingEnabled", "billing_enabled"):
            if isinstance(client.get(key), bool):
                return not client[key]
        return None
    if hasattr(client, "billing_enabled"):
        value = client.billing_enabled
        return None if value is None else (not value)
    info_fn = getattr(client, "get_billing_info", None)
    if callable(info_fn):
        try:
            info = info_fn()
        except Exception:
            return None
        if isinstance(info, dict):
            for key in ("billingEnabled", "billing_enabled"):
                if isinstance(info.get(key), bool):
                    return not info[key]
            return None
        value = getattr(info, "billingEnabled", None)
        return None if not isinstance(value, bool) else (not value)
    if not project:
        return None
    try:
        creds = getattr(client, "_credentials", None)
        if creds is None:
            return None  # no credentials to verify with -> unknown
        from google.auth.transport.requests import Request as _AuthRequest

        creds.refresh(_AuthRequest())
        token = getattr(creds, "token", None)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        resp = requests.get(
            f"https://cloudbilling.googleapis.com/v1/projects/{project}/billingInfo",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        value = resp.json().get("billingEnabled")
        return None if not isinstance(value, bool) else (not value)
    except Exception:
        return None


def check_billing_disabled(client: Any) -> bool:
    """True only when Cloud Billing reports billingEnabled=False for the project."""
    return _billing_state(client, get_google_cloud_project()) is True


_QUERY_PARAM_RE = re.compile(r"@(\w+)")
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _resolve_table(spec: dict, params: dict) -> str:
    """Effective table: census suffix interpolation, per-shard table, or checked-in table."""
    sql = spec.get("sql", "")
    if "{table_suffix}" in sql:
        suffix = params.get("table_suffix", "county_2020_5yr")
        if suffix not in _ACS_SUFFIXES:
            raise ValueError(
                f"unknown census table: {suffix!r} (available: {sorted(_ACS_SUFFIXES)})")
        return f"bigquery-public-data.census_bureau_acs.{suffix}"
    if spec.get("table_from_params"):
        table = params.get("table", "")
        if not _table_allowed(table):
            raise ValueError(f"unknown table: {table!r}")
        return table
    return spec.get("table") or ""


def _render_sql(spec: dict, params: dict, table: str) -> tuple[str, dict]:
    """Fill {limit}/{table}/{table_suffix}/{columns}; pass only @-referenced params."""
    sql_template = spec.get("sql", "")
    fmt: dict[str, Any] = {"limit": params["limit"], "table": table}
    if "{table_suffix}" in sql_template:
        fmt["table_suffix"] = table.rsplit(".", 1)[-1]
    if "{columns}" in sql_template:
        cols = params.get("columns", ["total_pop"])
        cols = [cols] if isinstance(cols, str) else list(cols)
        bad = [c for c in cols if not _IDENT_RE.match(str(c)) or c not in _ACS_COLUMNS]
        if not cols or bad:
            raise ValueError(
                f"unknown census columns: {(bad or cols)!r} (available: {sorted(_ACS_COLUMNS)})")
        fmt["columns"] = ", ".join(cols)
    sql = sql_template.format(**fmt)
    names = set(_QUERY_PARAM_RE.findall(sql))
    return sql, {k: v for k, v in params.items() if k in names}


def _query_parameters(bq: Any, params: dict) -> list:
    out = []
    for key, value in params.items():
        if isinstance(value, bool):
            out.append(bq.ScalarQueryParameter(key, "BOOL", value))
        elif isinstance(value, int):
            out.append(bq.ScalarQueryParameter(key, "INT64", value))
        elif isinstance(value, float):
            out.append(bq.ScalarQueryParameter(key, "FLOAT64", value))
        elif isinstance(value, (list, tuple)):
            out.append(bq.ArrayQueryParameter(key, "STRING", [str(v) for v in value]))
        else:
            out.append(bq.ScalarQueryParameter(key, "STRING", str(value)))
    return out


def _real_dry_run(client: Any, sql: str, params: dict, cap: int) -> int:
    from google.cloud import bigquery as _bq

    job_config = _bq.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        maximum_bytes_billed=cap,
        query_parameters=_query_parameters(_bq, params),
    )
    return int(client.query(sql, job_config=job_config).total_bytes_processed)


def _real_submit(
    client: Any, sql: str, params: dict, cap: int, job_id: str, max_rows: int
) -> dict:
    from google.cloud import bigquery as _bq

    job_config = _bq.QueryJobConfig(
        use_query_cache=False,
        maximum_bytes_billed=cap,
        query_parameters=_query_parameters(_bq, params),
    )
    job = client.query(
        sql, job_config=job_config, job_id=f"stockbot_{job_id[:56]}", location="US"
    )
    rows = [dict(r) for r in job.result(max_results=max_rows)]
    return {
        "job_id": job_id,
        "total_bytes_billed": int(job.total_bytes_billed or 0),
        "rows": rows,
        "state": "DONE",
    }


def submit_template(
    template: str, params: dict, client_factory=None, data_root=None
) -> dict:
    """Run one checked-in template; bounded, ledger-backed, billing-gated.

    Fake seam (no network): client_factory() -> object with
    dry_run(params)->int total bytes and
    submit(params, max_bytes, job_id)->{job_id,total_bytes_billed,rows,state}.
    Billing via the fake's billing_enabled bool attr or get_billing_info();
    a minimal fake (neither present) skips the network billing check.
    Raises LedgerCorrupt on unreadable accounting state.
    """
    params = dict(params or {})
    spec = TEMPLATES.get(template)
    if spec is None:
        return _err(f"unknown template: {template}", "source_unavailable")
    try:
        table = _resolve_table(spec, params)
    except ValueError as e:
        return _err(str(e), "source_unavailable")
    if not _table_allowed(table):
        return _err(f"unknown template: {template}", "source_unavailable")
    sql = spec.get("sql", "")
    if not sql.lstrip().upper().startswith("SELECT") or _WRITE_STMT.search(sql):
        return _err("template is not a read-only SELECT", "source_unavailable")
    project = get_google_cloud_project()
    if not bq_enabled() or not project:
        return _err(
            "BigQuery disabled (need GOOGLE_DATA_ENABLED + GOOGLE_CLOUD_PROJECT)",
            "source_unavailable",
        )
    per_query_cap = get_bq_max_bytes_per_query()
    monthly_limit = get_bq_monthly_bytes_limit()
    daily_limit = get_bq_daily_bytes_limit()
    if per_query_cap <= 0 or monthly_limit <= 0 or daily_limit <= 0:
        return _err("non-positive BigQuery byte limit", "cost_limit_exceeded")
    try:
        limit = int(params.get("limit", spec["max_rows"]))
    except (TypeError, ValueError):
        limit = spec["max_rows"]
    params["limit"] = max(1, min(limit, spec["max_rows"]))
    jid = _job_id(template, params)
    month = _utc_month()
    day = _utc_day()
    try:
        locker = _ledger_locked(data_root)
    except _LedgerBusy as e:
        return _err(str(e), "source_unavailable")
    with locker as path:
        ledger = _load_ledger(path)  # raises LedgerCorrupt: refuse, never reset
        jobs, months, days = ledger["jobs"], ledger["months"], ledger["days"]
        existing = jobs.get(jid)
        if isinstance(existing, dict) and existing.get("status") == "done":
            # Cached: no rows (callers reload the warehouse, never treat this as no evidence).
            return {
                "status": "ok",
                "source": SOURCE,
                "job_id": jid,
                "template": template,
                "rows": [],
                "total_bytes_billed": existing.get("actual_bytes", 0),
                "cached": True,
            }
        if client_factory is not None:
            client = client_factory()
            # ponytail: minimal fakes (dry_run/submit only) skip the network
            # billing check; production (no factory) always verifies via API.
            if (
                not hasattr(client, "billing_enabled")
                and not hasattr(client, "get_billing_info")
                and not isinstance(client, dict)
                and getattr(client, "_credentials", None) is None
            ):
                billing: Optional[bool] = True
            else:
                billing = _billing_state(client, project)
        else:
            try:
                from google.cloud import bigquery as _bq
            except ImportError:
                return _err("google-cloud-bigquery not installed", "source_unavailable")
            client = _bq.Client(project=project)
            billing = _billing_state(client, project)
        if billing is not True:
            return _err(
                "billing is enabled for this project; refusing"
                if billing is False
                else "could not verify billing disabled; refusing",
                "billing_enabled" if billing is False else "billing_unknown",
            )
        try:
            if hasattr(client, "dry_run"):
                estimate = int(client.dry_run(params))
            else:
                sql_text, bq_params = _render_sql(spec, params, table)
                estimate = int(_real_dry_run(client, sql_text, bq_params, per_query_cap))
        except (ValueError, KeyError) as e:
            return _err(f"unavailable template parameter: {e}", "source_unavailable")
        except Exception as e:
            return _err(f"dry-run failed: {e}", "source_unavailable")
        if estimate > per_query_cap:
            return _err(
                f"estimated {estimate} bytes exceeds per-query cap {per_query_cap} "
                "(BIGQUERY_QUERY_TOO_LARGE)",
                "cost_limit_exceeded",
            )
        pending_resume = isinstance(existing, dict) and existing.get("status") == "pending"
        reserve = max(estimate, int(existing.get("max_bytes", estimate))) if pending_resume else estimate
        if not pending_resume:
            if months.get(month, 0) + reserve > monthly_limit:
                return _err(
                    "monthly BigQuery byte limit would be exceeded (BIGQUERY_FREE_LIMIT_REACHED)",
                    "monthly_limit_exceeded",
                )
            if days.get(day, 0) + reserve > daily_limit:
                return _err(
                    "daily BigQuery byte limit would be exceeded (BIGQUERY_FREE_LIMIT_REACHED)",
                    "daily_limit_exceeded",
                )
            months[month] = months.get(month, 0) + reserve
            days[day] = days.get(day, 0) + reserve
            jobs[jid] = {
                "template": template,
                "month": month,
                "day": day,
                "max_bytes": reserve,
                "status": "pending",
                "actual_bytes": None,
                "source": SOURCE,
                "dataset": table.split(".")[1] if "." in table else "",
                "table": table,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "bytes_processed": None,
                "cache_hit": False,
                "success": False,
            }
            _prune_buckets(ledger)
            _save_ledger(path, ledger)
        entry = jobs[jid]
        try:
            if hasattr(client, "submit"):
                result = client.submit(params, entry["max_bytes"], jid) or {}
            else:
                sql_text, bq_params = _render_sql(spec, params, table)
                result = _real_submit(client, sql_text, bq_params, reserve, jid, spec["max_rows"])
        except Exception as e:
            # Unknown outcome: reservation retained, never refunded blindly.
            return _err(
                f"submit failed with unknown outcome; reservation retained: {e}",
                "source_unavailable",
                job_id=jid,
            )
        state = str(result.get("state", "")).upper()
        month_key = entry.get("month", month)
        day_key = entry.get("day", day)
        if state == "DONE":
            try:
                actual = int(result.get("total_bytes_billed", entry["max_bytes"]))
            except (TypeError, ValueError):
                actual = entry["max_bytes"]
            entry["status"] = "done"
            entry["actual_bytes"] = actual
            entry["bytes_processed"] = actual
            entry["success"] = True
            months[month_key] = months.get(month_key, 0) - entry["max_bytes"] + actual
            days[day_key] = days.get(day_key, 0) - entry["max_bytes"] + actual
            _prune_buckets(ledger)
            _save_ledger(path, ledger)
            return {
                "status": "ok",
                "source": SOURCE,
                "job_id": jid,
                "template": template,
                "rows": result.get("rows", []),
                "total_bytes_billed": actual,
            }
        if state in ("FAILED", "ERROR", "CANCELLED"):
            entry["status"] = "failed"  # terminal and known: release reservation
            entry["success"] = False
            months[month_key] = months.get(month_key, 0) - entry["max_bytes"]
            days[day_key] = days.get(day_key, 0) - entry["max_bytes"]
            _prune_buckets(ledger)
            _save_ledger(path, ledger)
            return _err(f"query {state.lower()}", "source_unavailable", job_id=jid)
        return {"status": "unknown", "source": SOURCE, "job_id": jid, "template": template}
