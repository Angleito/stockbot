"""Google Trends discovery evidence via bounded BigQuery templates only.

Daily top/rising tables (US + international) through the shared bounded
executor: checked-in SELECTs with ``refresh_date`` bounds for partition
pruning and an optional ``week`` pushdown for long lookbacks. ``refresh_date``
is load provenance (retrieval/knowledge time); ``week`` is the observed
interest week (analysis time). Scores/ranks are comparable only within one
refresh/week/geo/granularity — analytics stays list-membership. No hourly
template exists; hourly requests fail closed. No scraping, no paid fallback.
Empty results are a valid empty batch; malformed rows fail that batch with an
actionable error instead of inventing observations.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..storage.raw_archive import ArchiveRecord

from ._guards import as_dict, as_list, as_str_list, json_from_text, result_rows
from ._lazy_config import google_data_enabled

SOURCE = "trends"
_US_TEMPLATES = ("trends_us_top", "trends_us_rising")
_INTL_TEMPLATES = ("trends_intl_top", "trends_intl_rising")
_US_NATIONAL_TEMPLATES = ("trends_us_top_national", "trends_us_rising_national")
_INTL_NATIONAL_TEMPLATES = ("trends_intl_top_national", "trends_intl_rising_national")
_TEMPLATE_LIST_KIND = {
    "trends_us_top": "top",
    "trends_us_rising": "rising",
    "trends_intl_top": "top",
    "trends_intl_rising": "rising",
    "trends_us_top_national": "top",
    "trends_us_rising_national": "rising",
    "trends_intl_top_national": "top",
    "trends_intl_rising_national": "rising",
    "trends_top": "top",
    "trends_rising": "rising",
}
_FALLBACK_TABLES = {
    "trends_us_top": "bigquery-public-data.google_trends.top_terms",
    "trends_us_rising": "bigquery-public-data.google_trends.top_rising_terms",
    "trends_intl_top": "bigquery-public-data.google_trends.international_top_terms",
    "trends_intl_rising": "bigquery-public-data.google_trends.international_top_rising_terms",
    "trends_us_top_national": "bigquery-public-data.google_trends.top_terms",
    "trends_us_rising_national": "bigquery-public-data.google_trends.top_rising_terms",
    "trends_intl_top_national": "bigquery-public-data.google_trends.international_top_terms",
    "trends_intl_rising_national": "bigquery-public-data.google_trends.international_top_rising_terms",
}
_COLLECTOR_VERSION = "1"
_SQL_VERSION = "1"
_CHECKPOINT_PIPELINE = "google_trends"
_MAX_LIMIT = 1000
_FETCH_LIMIT = _MAX_LIMIT + 1
_MAX_GEOS = 50
_MAX_SPAN_DAYS = 31
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _data_enabled() -> bool:
    return google_data_enabled()


def _bq_ready() -> bool:
    if not google_data_enabled():
        return False
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    _project: str | None = None
    if _cfg is not None:
        try:
            _project = _cfg.get_google_cloud_project()
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            _project = None
    if _project is None:
        _project = (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip() or None
    return bool(_project)


def _bq_env_int(name: str, default: int) -> int:
    return int((os.getenv(name) or "").strip() or default)


def _bq_limits_from_env() -> tuple[int, int, int]:
    return (
        _bq_env_int("BIGQUERY_MAX_BYTES_PER_QUERY", 1073741824),
        _bq_env_int("BIGQUERY_MONTHLY_BYTES_LIMIT", 536870912000),
        _bq_env_int("BIGQUERY_DAILY_BYTES_LIMIT", 10737418240),
    )


def _env_bq_limits() -> tuple[int, int, int] | None:
    try:
        return _bq_limits_from_env()
    except TypeError, ValueError:
        return None


def _invalid_bq_limit_error() -> dict[str, object]:
    return {"status": "error", "source": SOURCE, "error": "invalid BigQuery byte limit", "error_type": "invalid_config"}


def _nonpositive_bq_limit_error() -> dict[str, object]:
    return {
        "status": "error",
        "source": SOURCE,
        "error": "non-positive BigQuery byte limit",
        "error_type": "invalid_config",
    }


def _bq_limits_positive(per_q: int, per_m: int, per_d: int) -> bool:
    return per_q > 0 and per_m > 0 and per_d > 0


def _bq_limits_from_config() -> tuple[int, int, int] | dict[str, object] | None:
    """Config limits, invalid-config error, or None when env fallback applies."""
    try:
        from .. import config as _cfg2
    except ImportError:
        return None
    try:
        return (
            _cfg2.get_bq_max_bytes_per_query(),
            _cfg2.get_bq_monthly_bytes_limit(),
            _cfg2.get_bq_daily_bytes_limit(),
        )
    except TypeError, ValueError:
        return _invalid_bq_limit_error()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _check_bq_limits() -> dict[str, object] | None:
    vals: tuple[int, int, int] | dict[str, object] | None = _bq_limits_from_config()
    if isinstance(vals, dict):
        return vals
    if vals is None:
        vals = _env_bq_limits()
        if vals is None:
            return _invalid_bq_limit_error()
    per_q, per_m, per_d = vals
    if not _bq_limits_positive(per_q, per_m, per_d):
        return _nonpositive_bq_limit_error()
    return None


class _Submitter(Protocol):
    """Anything submit_template-compatible: the real client or a test double."""

    def submit_template(self, template: str, params: dict[str, object]) -> dict[str, object]: ...


_Executor = Callable[[str, dict[str, object]], dict[str, object]] | _Submitter


def _submit_via_client(template: str, params: dict[str, object], data_root: Path | str | None) -> dict[str, object]:
    try:
        from . import bigquery_client as _client
    except ImportError:
        return {"error": "bigquery client unavailable", "error_type": "source_unavailable", "source": "bigquery"}
    try:
        narrowed_root: Path | None = Path(data_root) if isinstance(data_root, str) else data_root
        return _client.submit_template(template, params, data_root=narrowed_root)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"{SOURCE} query failed: {exc}",
            "error_type": "executor_error",
        }


def _direct_submit(template: str, params: dict[str, object], executor: _Executor) -> dict[str, object]:
    try:
        if callable(executor):
            return executor(template, params)
        return executor.submit_template(template, params)
    except Exception as exc:
        if type(exc).__name__ == "LedgerCorrupt":
            raise
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"{SOURCE} query failed: {exc}",
            "error_type": "executor_error",
        }


def _submit(
    template: str, params: dict[str, object], executor: _Executor | None, data_root: Path | str | None
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


def _wrap_error(result: dict[str, object]) -> dict[str, object]:
    if "status" in result:
        return result
    out = dict(result)
    out["status"] = "unavailable" if out.get("error_type") in _UNAVAILABLE else "error"
    out["engine"] = out.get("source", "bigquery")
    out["source"] = SOURCE
    return out


def _template_table(template: str) -> str:
    try:
        from . import bigquery_client as _bq
    except ImportError:
        return _FALLBACK_TABLES.get(template, "trends")
    try:
        spec = _bq.TEMPLATES.get(template, {})
        table: object = spec.get("table")
        if isinstance(table, str) and table:
            return table
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    return _FALLBACK_TABLES.get(template, "trends")


def _is_country_code(value: str) -> bool:
    return len(value) == 2 and value.isalpha() and value.isupper()


def _split_geos(geos: list[str]) -> tuple[list[str], list[str]]:
    dmas = [g for g in geos if g != "US" and not _is_country_code(g)]
    countries = [g for g in geos if g != "US" and _is_country_code(g)]
    return dmas, countries


def _plan_groups(geos: list[str]) -> list[dict[str, object]]:
    """Split geos into a US group (national or DMA names) plus per-country groups."""
    dmas, countries = _split_geos(geos)
    groups: list[dict[str, object]] = []
    if "US" in geos:
        groups.append({"kind": "us", "national": True, "dmas": []})
    if dmas:
        groups.append({"kind": "us", "national": False, "dmas": dmas})
    for country in countries:
        groups.append({"kind": "intl", "country": country})
    if not groups:  # e.g. geos == [] handled earlier; defensive: treat as national
        groups.append({"kind": "us", "national": True, "dmas": []})
    return groups


def _parquet_root(data_root: Path | str | None) -> Path | None:
    if data_root is None:
        return None
    try:
        from importlib.util import find_spec as _find_spec
    except ImportError:
        return None
    if _find_spec("app.storage.parquet") is None:
        return None
    return Path(data_root) / "parquet"


def _checkpoint_key(template: str, refresh: str, params: dict[str, object]) -> str:
    """Scoped completion key for the exact canonical query submitted."""
    scope_hash = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{template}|{refresh}|{scope_hash}"


def _observation_identity(
    table: object,
    period: object,
    geo: object,
    term: object,
    list_kind: object,
    metrics: dict[str, object],
    evidence: list[object],
) -> tuple[str, str]:
    """Durable (observation_id, content_hash) identity shared by store and verify."""
    content_hash = hashlib.sha256(
        json.dumps(
            {"metrics": metrics, "evidence": evidence, "collector_version": _COLLECTOR_VERSION},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    observation_id = hashlib.sha256(f"{SOURCE}|{table}|{period}|{geo}|{term}|{list_kind}".encode()).hexdigest()
    return observation_id, content_hash


def _feature_scope_json(
    *, week_start: str, week_end: str, geos: list[str], table: str, list_kind: str
) -> dict[str, object]:
    return {
        "week_start": week_start,
        "week_end": week_end,
        "geos": sorted(geos),
        "table": table,
        "list_kind": list_kind,
    }


def _feature_scope_hash(scope: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _series_basis(staged_row: dict[str, object]) -> str:
    metrics_raw: object = staged_row.get("metrics")
    metrics: dict[str, object] = metrics_raw if isinstance(metrics_raw, dict) else {}
    basis = metrics.get("score_basis")
    if basis:
        return str(basis)
    if (
        staged_row.get("dma_count") is not None
        or staged_row.get("region_count") is not None
        or metrics.get("dma_count") is not None
        or metrics.get("region_count") is not None
    ):
        return "mean_list_score_where_listed"
    return ""


def _inputs_scope_parts(scope: dict[str, object]) -> tuple[list[str], str, str, str, str]:
    geos = as_str_list(scope.get("geos"), what="geos")
    week_start = str(scope.get("week_start") or "")
    week_end = str(scope.get("week_end") or "")
    scope_table = str(scope.get("table") or "")
    scope_kind = str(scope.get("list_kind") or "")
    return geos, week_start, week_end, scope_table, scope_kind


def _cand_matches_scope_identity(cand: dict[str, object], scope_table: str, scope_kind: str, term: str) -> bool:
    return (
        str(cand.get("table") or "") == scope_table
        and str(cand.get("list_kind") or "") == scope_kind
        and str(cand.get("term") or "") == (term or "")
    )


def _cand_period_in_scope(cand: dict[str, object], week_start: str, week_end: str) -> str | None:
    period = str(cand.get("period") or cand.get("week") or "")
    if not period or period < week_start or period > week_end:
        return None
    return period


def _cand_matches_scope_geo(cand: dict[str, object], geos: list[str], basis: str) -> bool:
    cgeo = str(cand.get("geo") or "")
    return cgeo.split(":")[0] in geos and _series_basis(cand) == (basis or "")


def _scope_pair_for(
    cand: object,
    scope_table: str,
    scope_kind: str,
    term: str,
    week_start: str,
    week_end: str,
    geos: list[str],
    basis: str,
) -> list[str] | None:
    if not isinstance(cand, dict):
        return None
    if not _cand_matches_scope_identity(cand, scope_table, scope_kind, term):
        return None
    if _cand_period_in_scope(cand, week_start, week_end) is None:
        return None
    if not _cand_matches_scope_geo(cand, geos, basis):
        return None
    return [str(cand.get("observation_id") or ""), str(cand.get("content_hash") or "")]


def _collect_scope_pairs(
    candidates: list[dict[str, object]],
    scope_table: str,
    scope_kind: str,
    term: str,
    week_start: str,
    week_end: str,
    geos: list[str],
    basis: str,
) -> list[list[str]]:
    pairs: list[list[str]] = []
    for cand in candidates:
        pair = _scope_pair_for(cand, scope_table, scope_kind, term, week_start, week_end, geos, basis)
        if pair is None:
            continue
        pairs.append(pair)
    return pairs


def _cand_matches_target(cand: object, target_table: str, target_kind: str, target_geo: str, target_basis: str) -> bool:
    if not isinstance(cand, dict):
        return False
    return (
        str(cand.get("table") or "") == target_table
        and str(cand.get("list_kind") or "") == target_kind
        and str(cand.get("geo") or "") == target_geo
        and _series_basis(cand) == target_basis
    )


def _target_gate_open(target_geo: str, geos: list[str]) -> bool:
    return target_geo.split(":")[0] in geos or (not geos and not target_geo)


def _collect_target_periods(
    candidates: list[dict[str, object]],
    target_table: str,
    target_kind: str,
    target_geo: str,
    target_basis: str,
    week_start: str,
    week_end: str,
    geos: list[str],
) -> set[str]:
    periods: set[str] = set()
    if not _target_gate_open(target_geo, geos):
        return periods
    for cand in candidates:
        if not _cand_matches_target(cand, target_table, target_kind, target_geo, target_basis):
            continue
        period = _cand_period_in_scope(cand, week_start, week_end)
        if period is None:
            continue
        periods.add(period)
    return periods


def _inputs_geos_covered(geos: list[str], target_geo: str) -> list[str]:
    if target_geo in geos:
        return sorted(geos)
    if target_geo:
        return [target_geo]
    return []


def expected_inputs_hash(
    scope: dict[str, object],
    term: str,
    table: str,
    list_kind: str,
    basis: str,
    geo: str,
    candidates: list[dict[str, object]],
) -> str:
    """Scope-complete input identity shared by writes and PIT reads."""
    geos, week_start, week_end, scope_table, scope_kind = _inputs_scope_parts(scope)
    pairs = _collect_scope_pairs(candidates, scope_table, scope_kind, term, week_start, week_end, geos, basis or "")
    target_table = table or ""
    target_kind = list_kind or ""
    target_geo = geo or ""
    target_basis = basis or ""
    periods_covered = sorted(
        _collect_target_periods(
            candidates, target_table, target_kind, target_geo, target_basis, week_start, week_end, geos
        )
    )
    geos_covered = _inputs_geos_covered(geos, target_geo)
    payload = {"source_pairs": sorted(pairs), "periods_covered": periods_covered, "geos_covered": geos_covered}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _load_checkpoint_rows(proot: Path) -> list[object]:
    try:
        from ..storage import parquet as _parquet
    except ImportError:
        return []
    try:
        table = _parquet.read_table("ingestion_checkpoints", proot)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []
    try:
        return table.to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _completion_row_eligible(row: dict[str, object]) -> bool:
    return (
        row.get("pipeline") == _CHECKPOINT_PIPELINE
        and row.get("source") == "bigquery"
        and row.get("status") == "complete"
    )


def _completion_template_key(key: str, wanted: set[str]) -> str | None:
    template, _, _rest = key.partition("|")
    if template in wanted and key:
        return key
    return None


def _completion_key_if_wanted(row: object, wanted: set[str]) -> str | None:
    if not isinstance(row, dict) or not _completion_row_eligible(row):
        return None
    return _completion_template_key(str(row.get("key") or ""), wanted)


def _completed_refreshes(data_root: Path | str | None, templates: list[str]) -> set[str]:
    """Completed scoped checkpoint keys; missing warehouse reads as none."""
    done: set[str] = set()
    proot = _parquet_root(data_root)
    if proot is None:
        return done
    wanted = set(templates)
    for row in _load_checkpoint_rows(proot):
        key = _completion_key_if_wanted(row, wanted)
        if key is not None:
            done.add(key)
    return done


def _read_observation_rows(proot: Path) -> list[object]:
    try:
        from ..storage import parquet as _parquet
    except ImportError:
        return []
    try:
        return _parquet.read_table("google_observations", proot).to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _decode_warehouse_content(row: dict[str, object]) -> tuple[dict[str, object], list[object], str, str] | None:
    metrics = as_dict(json_from_text(row.get("metrics_json"), what="metrics_json"), what="metrics")
    evidence = as_list(json_from_text(row.get("evidence_json"), what="evidence_json"), what="evidence")
    try:
        metrics_canon = json.dumps(metrics, sort_keys=True, separators=(",", ":"), default=str)
        evidence_canon = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError, ValueError:
        return None
    return metrics, evidence, metrics_canon, evidence_canon


def _warehouse_key(
    row: dict[str, object], table: str, metrics_canon: str, evidence_canon: str
) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        str(row.get("table") or table),
        str(row.get("period") or ""),
        str(row.get("geo") or ""),
        str(row.get("term") or ""),
        str(row.get("list_kind") or ""),
        str(row.get("source_record_id") or ""),
        metrics_canon,
        evidence_canon,
    )


def _warehouse_staged(
    row: dict[str, object], table: str, metrics: dict[str, object], evidence: list[object]
) -> dict[str, object]:
    return {
        "table": row.get("table") or table,
        "period": row.get("period"),
        "week": row.get("period"),
        "geo": row.get("geo"),
        "term": row.get("term"),
        "list_kind": row.get("list_kind"),
        "rank": metrics.get("rank"),
        "source_record_id": row.get("source_record_id"),
        "observed_at": row.get("observed_at"),
        "known_at": row.get("known_at"),
        "retrieved_at": row.get("retrieved_at"),
        "metrics": metrics,
        "evidence": evidence,
        "features": None,
        "calc_version": row.get("calc_version") or "1",
    }


def _warehouse_entry(
    row: object, table: str, prefix: str
) -> tuple[tuple[str, str, str, str, str, str, str, str], dict[str, object]] | None:
    if not isinstance(row, dict):
        return None
    if not str(row.get("source_record_id") or "").startswith(prefix):
        return None
    decoded = _decode_warehouse_content(row)
    if decoded is None:
        return None
    metrics, evidence, metrics_canon, evidence_canon = decoded
    return (_warehouse_key(row, table, metrics_canon, evidence_canon), _warehouse_staged(row, table, metrics, evidence))


def _warehouse_rows(data_root: Path | str | None, table: str, refresh: str) -> list[dict[str, object]]:
    """Normalized observations already stored for one table/refresh partition."""
    proot = _parquet_root(data_root)
    if proot is None:
        return []
    prefix = f"{table}|{refresh}|"
    collapsed: dict[tuple[str, str, str, str, str, str, str, str], dict[str, object]] = {}
    for row in _read_observation_rows(proot):
        entry = _warehouse_entry(row, table, prefix)
        if entry is None:
            continue
        key, staged = entry
        prev = collapsed.get(key)
        if prev is not None and str(prev.get("known_at") or "") <= str(staged.get("known_at") or ""):
            continue
        collapsed[key] = staged
    return list(collapsed.values())


def _backfill_context(data_root: Path | str | None) -> tuple[Path, Path] | None:
    if data_root is None:
        return None
    try:
        from ..storage import parquet as _parquet_b  # noqa: F401
    except ImportError:
        return None
    proot = _parquet_root(data_root)
    if proot is None:
        return None
    marker = Path(data_root) / "google_data" / ".signal_features_backfilled"
    try:
        if marker.exists():
            return None
    except OSError:
        pass
    return proot, marker


def _read_backfill_source(proot: Path) -> list[object]:
    try:
        from ..storage import parquet as _parquet_r
    except ImportError:
        return []
    try:
        return _parquet_r.read_table("google_observations", proot).to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _backfill_features_blob(row: dict[str, object]) -> dict[str, object] | None:
    raw = row.get("features_json")
    if raw is None or raw == "":
        return None
    decoded = json_from_text(raw, what="features_json")
    return decoded if isinstance(decoded, dict) else None


def _backfill_feature_payload(row: dict[str, object], decoded: dict[str, object]) -> dict[str, object]:
    return {
        "observation_id": str(row.get("observation_id") or ""),
        "feature_scope_hash": "legacy-unknown",
        "feature_scope_json": json.dumps({"legacy": True, "reason": "pre-scope-backfill"}),
        "features_json": json.dumps(decoded, sort_keys=True, default=str),
        "calc_version": str(row.get("calc_version") or "1"),
        "calculated_at": str(row.get("known_at") or ""),
        "inputs_hash": "",
    }


def _backfill_feature_row(row: object) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    decoded = _backfill_features_blob(row)
    if decoded is None:
        return None
    return _backfill_feature_payload(row, decoded)


def _collect_backfill_rows(rows: list[object]) -> list[dict[str, object]]:
    feature_rows: list[dict[str, object]] = []
    for row in rows:
        converted = _backfill_feature_row(row)
        if converted is None:
            continue
        feature_rows.append(converted)
    return feature_rows


def _finish_backfill(proot: Path, marker: Path, feature_rows: list[dict[str, object]]) -> int:
    try:
        from ..storage import parquet as _parquet_w
    except ImportError:
        return 0
    written = 0
    if feature_rows:
        try:
            written = _parquet_w.write_rows("google_signal_features", feature_rows, root=proot)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return 0
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("backfilled\n")
    except OSError:
        pass
    return written


def backfill_legacy_feature_rows(data_root: Path | str | None) -> int:
    ctx = _backfill_context(data_root)
    if ctx is None:
        return 0
    proot, marker = ctx
    rows = _read_backfill_source(proot)
    return _finish_backfill(proot, marker, _collect_backfill_rows(rows))


def _cached_week_in_scope(cached: dict[str, object], params: dict[str, object]) -> bool:
    week = str(cached.get("week") or cached.get("period") or "")
    return not (week < str(params.get("week_start") or "") or week > str(params.get("week_end") or ""))


def _national_scope_decision(geo: str, params: dict[str, object]) -> bool | None:
    if not params.get("national"):
        return None
    if "country_code" not in params:
        return geo == str(params["national"])
    return geo == str(params.get("country_code"))


def _dma_scope_decision(geo: str, params: dict[str, object]) -> bool | None:
    if "all_dmas" not in params:
        return None
    if params.get("all_dmas"):
        return geo == "US"
    return geo in set(as_str_list(params.get("dmas"), what="dmas"))


def _country_scope_decision(geo: str, params: dict[str, object]) -> bool | None:
    if "country_code" not in params:
        return None
    country = str(params.get("country_code") or "")
    return geo == country or geo.startswith(country + ":")


def _cache_in_scope(cached: dict[str, object], params: dict[str, object]) -> bool:
    """Keep only cached rows matching the current canonical query scope."""
    if not _cached_week_in_scope(cached, params):
        return False
    geo = str(cached.get("geo") or "")
    for decider in (_national_scope_decision, _dma_scope_decision, _country_scope_decision):
        decision = decider(geo, params)
        if decision is not None:
            return decision
    return True


def _cached_record_id(row: dict[str, object]) -> str:
    return str(row.get("source_record_id") or "")


def _cached_rank_key(row: dict[str, object]) -> int | float:
    rank = row.get("rank")
    return rank if isinstance(rank, (int, float)) else float("inf")


def _cached_week_str(row: dict[str, object]) -> str:
    return str(row.get("week") or row.get("period") or "")


def _first_known_map(stored: list[dict[str, object]]) -> dict[tuple[str, str], str]:
    first_known: dict[tuple[str, str], str] = {}
    for row in stored:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("observation_id")), str(row.get("content_hash")))
        known = str(row.get("known_at") or "")
        if key not in first_known or known < first_known[key]:
            first_known[key] = known
    return first_known


def _store_refresh_date(obs: dict[str, object], metrics: dict[str, object]) -> str:
    refresh_date = str(metrics.get("refresh_date") or "")
    if not refresh_date:
        parts = str(obs.get("source_record_id") or "").split("|")
        if len(parts) >= 2 and parts[0] == obs.get("table"):
            refresh_date = parts[1]
    return refresh_date


def _build_stored_row(
    obs: dict[str, object],
    metrics: dict[str, object],
    evidence: list[object],
    observation_id: str,
    content_hash: str,
    known_at: str,
    retrieved_at: str,
) -> dict[str, object]:
    return {
        "observation_id": observation_id,
        "source": SOURCE,
        "table": obs["table"],
        "term": obs["term"],
        "geo": obs["geo"],
        "list_kind": obs["list_kind"],
        "period": obs["period"],
        "observed_at": obs.get("observed_at") or obs["period"],
        "known_at": known_at,
        "retrieved_at": retrieved_at,
        "source_record_id": obs.get("source_record_id") or observation_id,
        "content_hash": content_hash,
        "collector_version": _COLLECTOR_VERSION,
        "calc_version": _COLLECTOR_VERSION,
        "metrics_json": json.dumps(metrics, sort_keys=True, default=str),
        "features_json": None,
        "evidence_json": json.dumps(evidence, sort_keys=True, default=str),
        "source_url": f"bq://{obs['table']}",
    }


def _store_one_observation(
    obs: dict[str, object],
    first_known: dict[tuple[str, str], str],
    retrieved_at: str,
    expected: dict[tuple[object, str], set[tuple[str, str]]],
) -> dict[str, object]:
    metrics = dict(as_dict(obs.get("metrics"), what="metrics"))
    evidence = list(as_list(obs.get("evidence"), what="evidence"))
    observation_id, content_hash = _observation_identity(
        obs["table"], obs["period"], obs["geo"], obs["term"], obs["list_kind"], metrics, evidence
    )
    refresh_date = _store_refresh_date(obs, metrics)
    expected.setdefault((obs["table"], refresh_date), set()).add((observation_id, content_hash))
    known_at = first_known.get((observation_id, content_hash), retrieved_at)
    return _build_stored_row(obs, metrics, evidence, observation_id, content_hash, known_at, retrieved_at)


def _store_observations(
    data_root: Path | str | None, observations: list[dict[str, object]], retrieved_at: str
) -> dict[tuple[object, str], set[tuple[str, str]]]:
    """Write normalized observations; return expected identities per (table, refresh)."""
    proot = _parquet_root(data_root)
    if proot is None:
        return {}
    try:
        from ..storage import parquet as _parquet_s
    except ImportError:
        return {}
    try:
        backfill_legacy_feature_rows(data_root)
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    stored: list[dict[str, object]] = []
    try:
        stored = _parquet_s.read_table("google_observations", proot).to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        stored = []
    first_known = _first_known_map(stored)
    warehouse_rows: list[dict[str, object]] = []
    expected: dict[tuple[object, str], set[tuple[str, str]]] = {}
    for obs in observations:
        warehouse_rows.append(_store_one_observation(obs, first_known, retrieved_at, expected))
    _parquet_s.write_rows("google_observations", warehouse_rows, root=proot)
    return expected


def _archive_raw(
    data_root: Path | str | None, template: str, job_id: str, table: str, params: dict[str, object], rows: list[object]
) -> None:
    if data_root is None:
        return
    try:
        from ..storage import raw_archive as _raw_archive
    except ImportError:
        return
    try:
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()
        _raw_archive.archive(
            "google",
            kind=template,
            key=job_id,
            payload=payload,
            url=f"bq://{table}",
            metadata={"params": params},
            root=Path(data_root) / "raw",
        )
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass


def _archive_root(data_root: Path | str | None) -> Path | None:
    if data_root is None:
        return None
    try:
        from ..storage import raw_archive as _raw_archive_a  # noqa: F401
    except ImportError:
        return None
    return Path(data_root) / "raw"


def _decode_archive_payload(metadata: object, raw: bytes, params: dict[str, object]) -> list[object] | None:
    try:
        if not isinstance(metadata, dict) or metadata.get("params") != params:
            return None
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if not isinstance(payload, list):
        return None
    return payload


def _archive_record_payload(record: ArchiveRecord, params: dict[str, object]) -> list[object] | None:
    try:
        return _decode_archive_payload(record.metadata, record.payload_path.read_bytes(), params)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _best_archive_payload(records: Iterable[ArchiveRecord], params: dict[str, object]) -> list[object] | None:
    best: list[object] | None = None
    for record in records:
        payload = _archive_record_payload(record, params)
        if payload is None:
            continue
        if best is None or len(payload) > len(best):
            best = payload
    return best


def _archived_rows(
    data_root: Path | str | None, template: str, job_id: str, params: dict[str, object]
) -> list[object] | None:
    root = _archive_root(data_root)
    if root is None:
        return None
    try:
        from ..storage import raw_archive as _raw_archive_r
    except ImportError:
        return None
    try:
        return _best_archive_payload(_raw_archive_r.iter_archive("google", template, job_id, root=root), params)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _mark_complete(
    data_root: Path | str | None, checkpoint_key: str, refresh: str, payload_hash: str, count: int
) -> None:
    proot = _parquet_root(data_root)
    if proot is None:
        return
    try:
        from ..storage import parquet as _parquet_m
    except ImportError:
        return
    now = datetime.now(UTC).isoformat()
    _parquet_m.write_rows(
        "ingestion_checkpoints",
        [
            {
                "pipeline": _CHECKPOINT_PIPELINE,
                "source": "bigquery",
                "key": checkpoint_key,
                "payload_hash": payload_hash,
                "status": "complete",
                "record_count": count,
                "started_at": now,
                "finished_at": now,
                "parser_version": _COLLECTOR_VERSION,
                "last_key": refresh,
                "error": None,
                "totals_json": "{}",
            }
        ],
        root=proot,
    )


def _enumerate_refreshes(
    table: str, start_date: str, end_date: str, executor: _Executor | None, data_root: Path | str | None
) -> list[str] | None:
    """Known refresh_date partitions for one table; None when the executor errors."""
    result = _submit(
        "trends_refreshes",
        {
            "table": table,
            "start_date": start_date,
            "end_date": end_date,
            "limit": _MAX_LIMIT,
            "collector_version": _COLLECTOR_VERSION,
            "sql_version": _SQL_VERSION,
        },
        executor,
        data_root,
    )
    if not isinstance(result, dict) or "error" in result:
        return None
    refreshes = sorted(
        {str(r.get("refresh_date")) for r in result_rows(result) if isinstance(r, dict) and r.get("refresh_date")}
    )
    return refreshes


def _normalize_geos(geos: list[str] | str) -> list[str]:
    if isinstance(geos, str):
        return [geos]
    return list(geos or [])


def _readiness_error() -> dict[str, object] | None:
    if not _bq_ready():
        return {"status": "disabled", "source": SOURCE, "reason": "google data disabled or no BigQuery project"}
    return _check_bq_limits()


def _interval_error(interval: str | None) -> dict[str, object] | None:
    if (interval or "daily") == "hourly":
        return {
            "status": "unavailable",
            "source": SOURCE,
            "error": "hourly trends tables are not allowlisted",
            "error_type": "source_unavailable",
            "coverage": {"reason": "hourly_unavailable"},
        }
    if (interval or "daily") != "daily":
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"invalid interval: {interval!r} (daily)",
            "error_type": "invalid_params",
        }
    return None


def _dates_error(start_date: str | None, end_date: str | None) -> dict[str, object] | None:
    for label, value in (("start_date", start_date), ("end_date", end_date)):
        if not isinstance(value, str) or not _DATE_RE.match(value):
            return {
                "status": "error",
                "source": SOURCE,
                "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                "error_type": "invalid_params",
            }
    assert isinstance(start_date, str) and isinstance(end_date, str)
    if start_date > end_date:
        return {
            "status": "error",
            "source": SOURCE,
            "error": "start_date after end_date",
            "error_type": "invalid_params",
        }
    span = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    if span > _MAX_SPAN_DAYS:
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"date span {span}d exceeds {_MAX_SPAN_DAYS}d refresh bound",
            "error_type": "invalid_params",
        }
    return None


def _week_params_error(week_start: str | None, week_end: str | None) -> dict[str, object] | None:
    for label, value in (("week_start", week_start), ("week_end", week_end)):
        if value is not None and (not isinstance(value, str) or not _DATE_RE.match(value)):
            return {
                "status": "error",
                "source": SOURCE,
                "error": f"invalid {label}: {value!r} (YYYY-MM-DD)",
                "error_type": "invalid_params",
            }
    if week_start and week_end and week_start > week_end:
        return {
            "status": "error",
            "source": SOURCE,
            "error": "week_start after week_end",
            "error_type": "invalid_params",
        }
    return None


def _geos_error(geos: list[str]) -> dict[str, object] | None:
    if not geos:
        return {
            "status": "error",
            "source": SOURCE,
            "error": "at least one geo is required",
            "error_type": "invalid_params",
        }
    if len(geos) > _MAX_GEOS:
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"{len(geos)} geos exceeds bound {_MAX_GEOS}",
            "error_type": "invalid_params",
        }
    return None


def _collect_params_error(
    interval: str | None,
    start_date: str | None,
    end_date: str | None,
    week_start: str | None,
    week_end: str | None,
    geos: list[str],
) -> dict[str, object] | None:
    checks = (
        _interval_error(interval),
        _dates_error(start_date, end_date),
        _week_params_error(week_start, week_end),
        _geos_error(geos),
    )
    for check in checks:
        if check is not None:
            return check
    return None


def _default_collect_weeks(end_date: str, week_start: str | None, week_end: str | None) -> tuple[str, str]:
    if week_start is None and week_end is None:
        week_end = end_date
        week_start = (date.fromisoformat(end_date) - timedelta(days=13)).isoformat()
    elif week_start is None and week_end is not None:
        week_start = (date.fromisoformat(week_end) - timedelta(days=13)).isoformat()
    elif week_start is not None and week_end is None:
        week_end = (date.fromisoformat(week_start) + timedelta(days=13)).isoformat()
    assert isinstance(week_start, str) and isinstance(week_end, str)
    return week_start, week_end


def _templates_for(group: dict[str, object]) -> tuple[str, ...]:
    if group["kind"] == "us":
        if group.get("national"):
            return _US_NATIONAL_TEMPLATES
        return _US_TEMPLATES
    return _INTL_NATIONAL_TEMPLATES


def _refresh_query_params(group: dict[str, object], refresh: str, week_start: str, week_end: str) -> dict[str, object]:
    base: dict[str, object] = {
        "start_date": refresh,
        "end_date": refresh,
        "week_start": week_start,
        "week_end": week_end,
        "limit": _FETCH_LIMIT,
        "collector_version": _COLLECTOR_VERSION,
        "sql_version": _SQL_VERSION,
    }
    if group["kind"] == "us" and group.get("national"):
        return {**base, "national": "US"}
    if group["kind"] == "us":
        return {**base, "dmas": sorted(as_str_list(group.get("dmas"), what="dmas")), "all_dmas": False}
    return {**base, "country_code": group["country"], "national": group["country"]}


def _completed_for(data_root: Path | str | None, groups: list[dict[str, object]]) -> set[str]:
    if data_root is None:
        return set()
    templates = [t for g in groups for t in _templates_for(g)]
    return _completed_refreshes(data_root, templates)


def _submit_or_error(
    template: str, params: dict[str, object], executor: _Executor | None, data_root: Path | str | None
) -> tuple[dict[str, object] | None, dict[str, object]]:
    result = _submit(template, params, executor, data_root)
    if not isinstance(result, dict):
        return _wrap_error({"error": "bad executor result"}), {}
    if "error" in result:
        return _wrap_error(result), {}
    return None, result


def _resolve_rows(
    template: str, params: dict[str, object], data_root: Path | str | None, result: dict[str, object]
) -> tuple[dict[str, object] | None, list[object]]:
    cached = bool(result.get("cached"))
    rows = result_rows(result)
    job_id = result.get("job_id")
    if cached and not rows:
        recovered = _archived_rows(data_root, template, str(job_id or template), params)
        if recovered is None:
            return _wrap_error(
                {
                    "error": f"cached Trends result unavailable for {template}; refusing to checkpoint",
                    "error_type": "source_unavailable",
                    "source": "bigquery",
                }
            ), []
        return None, list(recovered)
    return None, rows


def _maybe_archive(
    data_root: Path | str | None,
    template: str,
    job_id: str,
    table: str,
    params: dict[str, object],
    rows: list[object],
    cached: bool,
) -> None:
    if data_root is not None and not cached:
        _archive_raw(data_root, template, job_id, table, params, rows)


def _payload_hash(rows: list[object], data_root: Path | str | None) -> str:
    if data_root is None:
        return ""
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def _scope_limit_error(template: str, refresh: str, count: int) -> dict[str, object] | None:
    if count <= _MAX_LIMIT:
        return None
    return {
        "status": "unavailable",
        "source": SOURCE,
        "reason": "query_scope_too_large",
        "error": f"{template} query scope exceeds 1000 rows for {refresh}",
        "error_type": "missing_coverage",
    }


def _cached_record_refresh(cached: dict[str, object]) -> str:
    parts = str(cached.get("source_record_id") or "").split("|")
    if len(parts) >= 2 and parts[0] == cached.get("table"):
        return parts[1]
    return ""


def _cached_metrics_refresh(cached: dict[str, object]) -> str:
    return str(as_dict(cached.get("metrics"), what="metrics").get("refresh_date") or "")


def _cached_row_refresh(cached: dict[str, object], refresh: str) -> str:
    return (
        _cached_metrics_refresh(cached)
        or _cached_record_refresh(cached)
        or str(cached.get("week") or cached.get("period") or refresh)
    )


def _merge_cached_rows(
    cached_rows: list[dict[str, object]],
    refresh: str,
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
) -> None:
    for cached in cached_rows:
        key = (
            cached["table"],
            _cached_row_refresh(cached, refresh),
            str(cached.get("week") or cached.get("period")),
            str(cached["geo"]),
            str(cached["term"]),
            str(cached["list_kind"]),
        )
        merged.setdefault(key, cached)


def _intl_row_geo(row: dict[str, object], group: dict[str, object]) -> object:
    return row.get("country_code") or group["country"] or row.get("geo")


def _region_geo_base(row: dict[str, object], group: dict[str, object]) -> object:
    region = row.get("region_code") or ""
    geo = row.get("country_code") or group["country"]
    if region:
        return str(geo or "") + f":{region}"
    return geo


def _region_row_geo(row: dict[str, object], group: dict[str, object]) -> object:
    geo = _region_geo_base(row, group)
    if not row.get("country_code"):
        geo = row.get("geo") or geo
    return geo


def _row_geo(row: dict[str, object], template: str, group: dict[str, object]) -> object:
    if template in _US_NATIONAL_TEMPLATES:
        return "US"
    if template in _INTL_NATIONAL_TEMPLATES:
        return _intl_row_geo(row, group)
    if group["kind"] == "us":
        return row.get("dma_name") or row.get("geo")
    return _region_row_geo(row, group)


def _row_markers(staged: dict[str, object], template: str, table: str, refresh: str) -> None:
    staged["_table"] = staged.get("table") or table
    staged["_refresh"] = str(staged.get("refresh_date") or refresh)
    staged["_week"] = str(staged.get("week") or staged.get("period") or staged.get("source_period") or refresh)
    staged["_kind"] = staged.get("list_kind") or staged.get("list") or _TEMPLATE_LIST_KIND.get(template, "top")


def _tag_row(
    row: object, template: str, table: str, refresh: str, group: dict[str, object], job_id: object
) -> dict[str, object] | None:
    if not isinstance(row, dict):
        return None
    staged = dict(row)
    staged["_template"] = template
    _row_markers(staged, template, table, refresh)
    staged["_geo"] = _row_geo(staged, template, group)
    staged["job_id"] = job_id
    return staged


def _as_staged(row: dict[str, object]) -> dict[str, object]:
    """Cached warehouse rows carry plain dicts (no _-markers); normalize keys."""
    staged_row = dict(row)
    if "_table" not in staged_row:
        staged_row["_table"] = staged_row.get("table")
        staged_row["_week"] = str(staged_row.get("week") or staged_row.get("period"))
        parts = str(staged_row.get("source_record_id") or "").split("|")
        id_refresh = parts[1] if len(parts) >= 2 and parts[0] == staged_row.get("table") else ""
        staged_row["_refresh"] = str(
            as_dict(staged_row.get("metrics"), what="metrics").get("refresh_date")
            or id_refresh
            or staged_row.get("period")
        )
        staged_row["_kind"] = staged_row.get("list_kind")
        staged_row["_geo"] = staged_row.get("geo")
        staged_row["_cached"] = True
    return staged_row


def _table_sets() -> tuple[set[str], set[str]]:
    return (
        {_template_table("trends_us_top"), _template_table("trends_us_rising"), "trends_top", "trends_rising"},
        {_template_table("trends_intl_top"), _template_table("trends_intl_rising")},
    )


def _dma_scope_set(groups: list[dict[str, object]]) -> set[str]:
    return {
        d
        for g in groups
        if g.get("kind") == "us" and not g.get("national")
        for d in as_str_list(g.get("dmas"), what="dmas")
    }


def _route_staged(
    staged_row: dict[str, object],
    row: dict[str, object],
    table: object,
    us_tables: set[str],
    intl_tables: set[str],
    national: bool,
    dma_set: set[str],
    national_rows: list[dict[str, object]],
    dma_rows: list[dict[str, object]],
    intl_rows: list[dict[str, object]],
) -> None:
    if row.get("_template") in _US_NATIONAL_TEMPLATES + _INTL_NATIONAL_TEMPLATES:
        national_rows.append(staged_row)
    elif table in us_tables:
        if national and str(staged_row["_geo"]) == "US":
            national_rows.append(staged_row)
        elif str(staged_row["_geo"]) in dma_set:
            dma_rows.append(staged_row)
    elif table in intl_tables:
        intl_rows.append(staged_row)


def _split_staged(
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    groups: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], set[str]]:
    us_tables, intl_tables = _table_sets()
    national = any(g.get("kind") == "us" and g.get("national") for g in groups)
    dma_set = _dma_scope_set(groups)
    national_rows: list[dict[str, object]] = []
    dma_rows: list[dict[str, object]] = []
    intl_rows: list[dict[str, object]] = []
    for row in merged.values():
        table = row.get("_table") or row.get("table")
        staged_row = _as_staged(row)
        if not staged_row.get("term") or not staged_row.get("_geo"):
            raise ValueError(f"malformed trends row for {staged_row.get('term')!r}")
        _route_staged(
            staged_row, row, table, us_tables, intl_tables, national, dma_set, national_rows, dma_rows, intl_rows
        )
    return national_rows, dma_rows, intl_rows, dma_set


def _series_metric(row: dict[str, object], name: str) -> object:
    value = row.get(name)
    if name == "score" and isinstance(value, bool):
        value = None
    if value is None:
        metrics = row.get("metrics")
        if isinstance(metrics, dict):
            value = metrics.get(name)
    return value


def _series_point(row: dict[str, object]) -> dict[str, object]:
    return {
        "table": row.get("_table"),
        "period": row.get("_week"),
        "geo": row.get("_geo"),
        "term": row.get("term"),
        "list_kind": row.get("_kind"),
        "rank": _series_metric(row, "rank"),
        "score": _series_metric(row, "score"),
    }


def _group_series(
    winners: dict[tuple[object, object, object, object, object], dict[str, object]],
) -> tuple[
    dict[tuple[object, object, object, object, str], list[dict[str, object]]],
    dict[tuple[object, object, object, str], set[str]],
]:
    by_series: dict[tuple[object, object, object, object, str], list[dict[str, object]]] = {}
    periods_by_series: dict[tuple[object, object, object, str], set[str]] = {}
    for row in winners.values():
        basis = _series_basis(row)
        skey = (row.get("term"), row.get("_table"), row.get("_kind"), row.get("_geo"), basis)
        by_series.setdefault(skey, []).append(_series_point(row))
        pkey = (row.get("_table"), row.get("_kind"), row.get("_geo"), basis)
        periods_by_series.setdefault(pkey, set()).add(str(row.get("_week")))
    return by_series, periods_by_series


def _pick_winners(
    rows: list[dict[str, object]],
) -> dict[tuple[object, object, object, object, object], dict[str, object]]:
    winners: dict[tuple[object, object, object, object, object], dict[str, object]] = {}
    for row in rows:
        wkey = (row.get("_table"), row.get("_kind"), row.get("_geo"), row.get("term"), row.get("_week"))
        prev = winners.get(wkey)
        if prev is None or str(row.get("_refresh") or "") > str(prev.get("_refresh") or ""):
            winners[wkey] = row
    return winners


def _series_feature_map(
    signals_mod: ModuleType,
    by_series: dict[tuple[object, object, object, object, str], list[dict[str, object]]],
    periods_by_series: dict[tuple[object, object, object, str], set[str]],
) -> dict[tuple[object, object, object, object, str], dict[str, object]]:
    series_features: dict[tuple[object, object, object, object, str], dict[str, object]] = {}
    for skey, srows in by_series.items():
        _term, _table, _kind, _geo, _basis = skey
        periods = sorted(periods_by_series.get((_table, _kind, _geo, _basis)) or set())
        features = signals_mod.compute_candidate_features(srows, periods_covered=periods)
        if isinstance(features, dict):
            series_features[skey] = features
    return series_features


def _apply_diffusion_entry(feat: dict[str, object], diff: dict[str, object]) -> None:
    feat["diffusion"] = diff.get("diffusion")
    rules: object = feat.get("rules")
    if isinstance(rules, dict):
        diff_rules: object = diff.get("rules", {})
        diff_entry: object = diff_rules.get("diffusion", {}) if isinstance(diff_rules, dict) else {}
        rules["diffusion"] = dict(as_dict(diff_entry, what="diffusion"))
    cov: object = feat.get("coverage")
    if isinstance(cov, dict):
        cov_diff: object = diff.get("coverage", {})
        cov_geos: object = cov_diff.get("geos_covered", []) if isinstance(cov_diff, dict) else []
        cov["geos_covered"] = as_list(cov_geos, what="geos_covered")


def _dma_group_key(row: dict[str, object]) -> tuple[object, object, object, str]:
    return (row.get("term"), row.get("_table"), row.get("_kind"), _series_basis(row))


def _collect_dma_groups(
    winners: dict[tuple[object, object, object, object, object], dict[str, object]], dma_set: set[str]
) -> dict[tuple[object, object, object, str], list[dict[str, object]]]:
    dma_groups: dict[tuple[object, object, object, str], list[dict[str, object]]] = {}
    for row in winners.values():
        if str(row.get("_geo")) not in dma_set:
            continue
        dma_groups.setdefault(_dma_group_key(row), []).append(_series_point(row))
    return dma_groups


def _dma_key_matches(
    skey: tuple[object, object, object, object, str], gkey: tuple[object, object, object, str], dma_set: set[str]
) -> bool:
    return (
        skey[0] == gkey[0]
        and skey[1] == gkey[1]
        and skey[2] == gkey[2]
        and skey[4] == gkey[3]
        and str(skey[3]) in dma_set
    )


def _spread_dma_diff(
    signals_mod: ModuleType,
    gkey: tuple[object, object, object, str],
    grows: list[dict[str, object]],
    dma_set: set[str],
    series_features: dict[tuple[object, object, object, object, str], dict[str, object]],
) -> None:
    raw = signals_mod.compute_candidate_features(grows, geos_covered=sorted(dma_set))
    if not isinstance(raw, dict):
        return
    diff = raw
    for skey, sentry in series_features.items():
        if _dma_key_matches(skey, gkey, dma_set):
            _apply_diffusion_entry(sentry, diff)


def _apply_dma_diffusion(
    signals_mod: ModuleType,
    winners: dict[tuple[object, object, object, object, object], dict[str, object]],
    dma_set: set[str],
    series_features: dict[tuple[object, object, object, object, str], dict[str, object]],
) -> None:
    if not dma_set:
        return
    for gkey, grows in _collect_dma_groups(winners, dma_set).items():
        _spread_dma_diff(signals_mod, gkey, grows, dma_set, series_features)


def _features_for(
    signals_mod: ModuleType,
    winners: dict[tuple[object, object, object, object, object], dict[str, object]],
    dma_set: set[str],
) -> dict[tuple[object, object, object, object, str], dict[str, object]]:
    by_series, periods_by_series = _group_series(winners)
    features = _series_feature_map(signals_mod, by_series, periods_by_series)
    _apply_dma_diffusion(signals_mod, winners, dma_set, features)
    return features


def _strip_diffusion(features: object) -> object:
    if not isinstance(features, dict):
        return features
    out = copy.deepcopy(features)
    out["diffusion"] = None
    arules: object = out["rules"]
    if isinstance(arules, dict):
        ardiff: object = arules.get("diffusion")
        if isinstance(ardiff, dict):
            ardiff["value"] = None
    acov: object = out["coverage"]
    if isinstance(acov, dict):
        amissing: object = acov.get("missing")
        if isinstance(amissing, dict):
            amissing["diffusion"] = "subregion rows aggregated"
    return out


def _normalize_staged(
    rows: list[dict[str, object]],
    series_features: dict[tuple[object, object, object, object, str], dict[str, object]],
    retrieved_at: str,
    data_root: Path | str | None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    observations: list[dict[str, object]] = []
    effective: list[dict[str, object]] = []
    winner_ids = {id(w) for w in _pick_winners(rows).values()}
    for row in rows:
        basis = _series_basis(row)
        features = series_features.get((row.get("term"), row.get("_table"), row.get("_kind"), row.get("_geo"), basis))
        metrics_raw: object = row.get("metrics")
        metrics: dict[str, object] = metrics_raw if isinstance(metrics_raw, dict) else {}
        aggregate = (
            row.get("dma_count") is not None
            or row.get("region_count") is not None
            or metrics.get("dma_count") is not None
            or metrics.get("region_count") is not None
        )
        norm = _normalize_row(
            row, retrieved_at, data_root, features=_strip_diffusion(features) if aggregate else features
        )
        observations.append(norm)
        if id(row) in winner_ids:
            effective.append(norm)
    return observations, effective


def _feature_infos(
    observations: list[dict[str, object]], dma_set: set[str], week_start: str, week_end: str
) -> list[tuple[dict[str, object], str, str, dict[str, object], str, str, dict[str, object]]]:
    infos: list[tuple[dict[str, object], str, str, dict[str, object], str, str, dict[str, object]]] = []
    for obs in observations:
        feats = obs.get("features")
        if not isinstance(feats, dict):
            continue
        metrics = dict(as_dict(obs.get("metrics"), what="metrics"))
        evidence = list(as_list(obs.get("evidence"), what="evidence"))
        oid, ch = _observation_identity(
            obs["table"], obs["period"], obs["geo"], obs["term"], obs["list_kind"], metrics, evidence
        )
        geo = str(obs.get("geo") or "")
        if geo == "US":
            geos = ["US"]
        elif geo in dma_set:
            geos = sorted(dma_set)
        else:
            geos = [geo.split(":")[0]]
        scope = _feature_scope_json(
            week_start=week_start,
            week_end=week_end,
            geos=geos,
            table=str(obs.get("table") or ""),
            list_kind=str(obs.get("list_kind") or ""),
        )
        infos.append((obs, oid, ch, scope, _feature_scope_hash(scope), _series_basis(obs), feats))
    return infos


_FeatureInfo = tuple[dict[str, object], str, str, dict[str, object], str, str, dict[str, object]]


def _feature_candidates(infos: list[_FeatureInfo], effective: list[dict[str, object]]) -> list[dict[str, object]]:
    effective_ids = {id(o) for o in effective}
    return [
        {
            "observation_id": oid,
            "content_hash": ch,
            "table": str(obs.get("table") or ""),
            "period": str(obs.get("period") or ""),
            "geo": str(obs.get("geo") or ""),
            "term": str(obs.get("term") or ""),
            "list_kind": str(obs.get("list_kind") or ""),
            "metrics": as_dict(obs.get("metrics"), what="metrics"),
        }
        for obs, oid, ch, _, _, _, _ in infos
        if id(obs) in effective_ids
    ]


def _feature_calc_key(row: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(row.get("observation_id") or ""),
        str(row.get("feature_scope_hash") or ""),
        str(row.get("calc_version") or ""),
        str(row.get("inputs_hash") or ""),
    )


def _track_calc(first_calc: dict[tuple[str, str, str, str], str], row: dict[str, object]) -> None:
    k = _feature_calc_key(row)
    c = str(row.get("calculated_at") or "")
    if k not in first_calc or c < first_calc[k]:
        first_calc[k] = c


def _feature_calc_map(existing: list[dict[str, object]]) -> dict[tuple[str, str, str, str], str]:
    first_calc: dict[tuple[str, str, str, str], str] = {}
    for row in existing:
        if isinstance(row, dict):
            _track_calc(first_calc, row)
    return first_calc


def _feature_rows(
    infos: list[_FeatureInfo],
    candidates: list[dict[str, object]],
    calc_version: str,
    retrieved_at: str,
    first_calc: dict[tuple[str, str, str, str], str],
) -> list[dict[str, object]]:
    empty_hash = expected_inputs_hash({}, "", "", "", "", "", [])
    rows: list[dict[str, object]] = []
    for obs, oid, ch, scope, shash, basis, feats in infos:
        expected = expected_inputs_hash(
            scope,
            str(obs.get("term") or ""),
            str(obs.get("table") or ""),
            str(obs.get("list_kind") or ""),
            basis,
            str(obs.get("geo") or ""),
            candidates,
        )
        if expected == empty_hash:
            continue
        calc_at = first_calc.get((oid, shash, calc_version, expected), retrieved_at)
        rows.append(
            {
                "observation_id": oid,
                "feature_scope_hash": shash,
                "feature_scope_json": json.dumps(scope, sort_keys=True),
                "features_json": json.dumps(feats, sort_keys=True, default=str),
                "calc_version": calc_version,
                "calculated_at": min(calc_at, retrieved_at),
                "inputs_hash": expected,
            }
        )
    return rows


def _write_feature_rows(
    parquet_mod: ModuleType,
    proot: Path,
    observations: list[dict[str, object]],
    effective: list[dict[str, object]],
    dma_set: set[str],
    week_start: str,
    week_end: str,
    calc_version: str,
    retrieved_at: str,
) -> None:
    infos = _feature_infos(observations, dma_set, week_start, week_end)
    if not infos:
        return
    empty_rows: list[object] = []
    raw_existing: object = None
    try:
        raw_existing = parquet_mod.read_table("google_signal_features", proot).to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        raw_existing = empty_rows
    existing: list[dict[str, object]] = (
        [r for r in raw_existing if isinstance(r, dict)] if isinstance(raw_existing, list) else []
    )
    parquet_mod.write_rows(
        "google_signal_features",
        _feature_rows(
            infos, _feature_candidates(infos, effective), calc_version, retrieved_at, _feature_calc_map(existing)
        ),
        root=proot,
    )


def _warehouse_identities(data_root: Path | str | None, table: str, refresh: str) -> set[tuple[str, str]]:
    actual: set[tuple[str, str]] = set()
    for cached in _warehouse_rows(data_root, table, refresh):
        metrics = as_dict(cached.get("metrics"), what="metrics")
        evidence = as_list(cached.get("evidence"), what="evidence")
        actual.add(
            _observation_identity(
                cached.get("table") or table,
                str(cached.get("period") or cached.get("week") or ""),
                str(cached.get("geo") or ""),
                str(cached.get("term") or ""),
                str(cached.get("list_kind") or ""),
                metrics,
                evidence,
            )
        )
    return actual


def _verify_partition(
    data_root: Path | str | None, table: str, refresh: str, expected: dict[tuple[object, str], set[tuple[str, str]]]
) -> None:
    if not expected.get((table, refresh), set()) <= _warehouse_identities(data_root, table, refresh):
        raise RuntimeError(f"warehouse verify failed for {table}|{refresh}")


def _verify_warehouse(
    data_root: Path | str | None,
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    expected: dict[tuple[object, str], set[tuple[str, str]]],
) -> None:
    for _key, _template, _refresh, _hash, _count, _tables in fetched:
        for _table in _tables:
            _verify_partition(data_root, _table, _refresh, expected)


def _mark_fetched(data_root: Path | str | None, fetched: list[tuple[str, str, str, str, int, list[str]]]) -> None:
    for _key, _template, _refresh, _hash, _count, _tables in fetched:
        _mark_complete(data_root, _key, _refresh, _hash, _count)


def _persist_collect(
    data_root: Path | str | None,
    observations: list[dict[str, object]],
    effective: list[dict[str, object]],
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    dma_set: set[str],
    week_start: str,
    week_end: str,
    retrieved_at: str,
    signals_mod: ModuleType,
) -> None:
    expected = _store_observations(data_root, observations, retrieved_at)
    try:
        from ..storage import parquet as parquet_mod
    except ImportError:
        parquet_mod = None
    proot = _parquet_root(data_root)
    if proot is not None and parquet_mod is not None:
        version = getattr(signals_mod, "CALC_VERSION", "")
        calc_version = version if isinstance(version, str) else ""
        _write_feature_rows(
            parquet_mod, proot, observations, effective, dma_set, week_start, week_end, calc_version, retrieved_at
        )
    _verify_warehouse(data_root, fetched, expected)
    _mark_fetched(data_root, fetched)


def _collect_response(
    observations: list[dict[str, object]],
    limit: int,
    used_templates: list[str],
    refresh_seen: set[str],
    term: str | None,
) -> dict[str, object]:
    weeks = sorted({str(o["period"]) for o in observations})
    geos_covered = sorted({str(o["geo"]) for o in observations})
    if isinstance(term, str) and term.strip():
        needle = term.strip().lower()
        matched = [o for o in observations if needle in str(o.get("term", "")).lower()]
    else:
        matched = observations
    continuation = len(matched) > limit
    returned = matched[:limit] if continuation else matched
    return {
        "status": "ok",
        "source": SOURCE,
        "observations": returned,
        "rows": returned,
        "count": len(returned),
        "coverage": {
            "periods_covered": weeks,
            "geos_covered": geos_covered,
            "templates": used_templates,
            "refresh_dates": sorted(refresh_seen),
        },
        "continuation": continuation,
        "warnings": ["truncated"] if continuation else [],
    }


def _fetch_all(
    groups: list[dict[str, object]],
    week_start: str,
    week_end: str,
    start_date: str,
    end_date: str,
    executor: _Executor | None,
    data_root: Path | str | None,
    completed: set[str],
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    used_templates: list[str],
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    refresh_seen: set[str],
) -> dict[str, object] | None:
    for group in groups:
        for template in _templates_for(group):
            table = _template_table(template)
            refreshes = _enumerate_refreshes(table, start_date, end_date, executor, data_root)
            if refreshes is None:
                return _wrap_error(
                    {"error": "refresh enumeration failed", "error_type": "source_unavailable", "source": "bigquery"}
                )
            for refresh in refreshes or [end_date]:
                params = _refresh_query_params(group, refresh, week_start, week_end)
                checkpoint_key = _checkpoint_key(template, refresh, params)
                refresh_error = _collect_one_refresh(
                    template,
                    table,
                    refresh,
                    params,
                    group,
                    checkpoint_key,
                    completed,
                    merged,
                    used_templates,
                    fetched,
                    refresh_seen,
                    executor,
                    data_root,
                )
                if refresh_error is not None:
                    return refresh_error
    return None


def _collect_end(
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    groups: list[dict[str, object]],
    signals_mod: ModuleType,
    used_templates: list[str],
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    refresh_seen: set[str],
    data_root: Path | str | None,
    week_start: str,
    week_end: str,
    limit: int,
    term: str | None,
) -> dict[str, object]:
    try:
        national_rows, dma_rows, intl_rows, dma_set = _split_staged(merged, groups)
    except ValueError as exc:
        return {"status": "error", "source": SOURCE, "error": str(exc), "error_type": "malformed_row"}
    staged_all = national_rows + dma_rows + intl_rows
    winners = _pick_winners(staged_all)
    series_features = _features_for(signals_mod, winners, dma_set)
    retrieved_at = datetime.now(UTC).isoformat()
    observations, effective = _normalize_staged(
        national_rows + dma_rows + intl_rows, series_features, retrieved_at, data_root
    )
    if data_root is not None and (observations or fetched):
        try:
            _persist_collect(
                data_root, observations, effective, fetched, dma_set, week_start, week_end, retrieved_at, signals_mod
            )
        except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return _wrap_error({"error": f"{SOURCE} store failed: {exc}", "error_type": "source_unavailable"})
    return _collect_response(observations, limit, used_templates, refresh_seen, term)


def _merge_tagged(
    rows: list[object],
    template: str,
    table: str,
    refresh: str,
    group: dict[str, object],
    job_id: object,
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
) -> set[str]:
    tables_seen: set[str] = set()
    for row in rows:
        staged = _tag_row(row, template, table, refresh, group, job_id)
        if staged is None:
            continue
        key = (
            staged["_table"],
            staged["_refresh"],
            staged["_week"],
            str(staged["_geo"]),
            str(staged.get("term")),
            staged["_kind"],
        )
        merged.setdefault(key, staged)
        tables_seen.add(str(staged["_table"]))
    return tables_seen


def _cached_rows_for(
    data_root: Path | str | None, table: str, refresh: str, params: dict[str, object]
) -> list[dict[str, object]]:
    rows = [c for c in _warehouse_rows(data_root, table, refresh) if _cache_in_scope(c, params)]
    rows.sort(key=_cached_record_id)
    rows.sort(key=_cached_rank_key)
    rows.sort(key=_cached_week_str, reverse=True)
    return rows


def _replay_completed(
    template: str,
    refresh: str,
    params: dict[str, object],
    table: str,
    checkpoint_key: str,
    completed: set[str],
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    used_templates: list[str],
    data_root: Path | str | None,
) -> tuple[bool, dict[str, object] | None]:
    """Replay a checkpointed refresh; (False, None) means fall through to fetch."""
    if checkpoint_key not in completed:
        return False, None
    cached_rows = _cached_rows_for(data_root, table, refresh, params)
    if not cached_rows:
        return False, None
    over = _scope_limit_error(template, refresh, len(cached_rows))
    if over is not None:
        return True, over
    _merge_cached_rows(cached_rows[:_MAX_LIMIT], refresh, merged)
    if template not in used_templates:
        used_templates.append(template)
    return True, None


def _merge_fresh(
    template: str,
    table: str,
    refresh: str,
    params: dict[str, object],
    group: dict[str, object],
    checkpoint_key: str,
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    used_templates: list[str],
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    data_root: Path | str | None,
    result: dict[str, object],
) -> dict[str, object] | None:
    cached = bool(result.get("cached"))
    resolve_err, rows = _resolve_rows(template, params, data_root, result)
    if resolve_err is not None:
        return resolve_err
    job_id = result.get("job_id")
    _maybe_archive(data_root, template, str(job_id or template), table, params, rows, cached)
    over = _scope_limit_error(template, refresh, len(rows))
    if over is not None:
        return over
    if template not in used_templates:
        used_templates.append(template)
    tables_seen = _merge_tagged(rows, template, table, refresh, group, job_id, merged)
    if data_root is not None:
        fetched.append(
            (checkpoint_key, template, refresh, _payload_hash(rows, data_root), len(rows), sorted(tables_seen))
        )
    return None


def _collect_one_refresh(
    template: str,
    table: str,
    refresh: str,
    params: dict[str, object],
    group: dict[str, object],
    checkpoint_key: str,
    completed: set[str],
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]],
    used_templates: list[str],
    fetched: list[tuple[str, str, str, str, int, list[str]]],
    refresh_seen: set[str],
    executor: _Executor | None,
    data_root: Path | str | None,
) -> dict[str, object] | None:
    refresh_seen.add(refresh)
    replayed, replay_error = _replay_completed(
        template, refresh, params, table, checkpoint_key, completed, merged, used_templates, data_root
    )
    if replay_error is not None:
        return replay_error
    if replayed:
        return None
    err, result = _submit_or_error(template, params, executor, data_root)
    if err is not None:
        return err
    return _merge_fresh(
        template, table, refresh, params, group, checkpoint_key, merged, used_templates, fetched, data_root, result
    )


def collect_trends(
    *,
    start_date: str | None,
    end_date: str | None,
    geos: list[str] | str,
    limit: int = 100,
    data_root: Path | str | None = None,
    executor: _Executor | None = None,
    week_start: str | None = None,
    week_end: str | None = None,
    interval: str | None = "daily",
    term: str | None = None,
) -> dict[str, object]:
    """Collect top/rising lists; each row becomes an idempotent candidate."""
    geos = _normalize_geos(geos)
    ready = _readiness_error()
    if ready:
        return ready
    params_error = _collect_params_error(interval, start_date, end_date, week_start, week_end, geos)
    if params_error:
        return params_error
    assert isinstance(start_date, str) and isinstance(end_date, str)
    limit = max(1, min(limit, _MAX_LIMIT))
    week_start, week_end = _default_collect_weeks(end_date, week_start, week_end)
    groups = _plan_groups(geos)
    from . import signals as _signals

    completed = _completed_for(data_root, groups)
    merged: dict[tuple[object, object, object, object, object, object], dict[str, object]] = {}
    used_templates: list[str] = []
    refresh_seen: set[str] = set()
    fetched: list[tuple[str, str, str, str, int, list[str]]] = []
    fetch_error = _fetch_all(
        groups,
        week_start,
        week_end,
        start_date,
        end_date,
        executor,
        data_root,
        completed,
        merged,
        used_templates,
        fetched,
        refresh_seen,
    )
    if fetch_error is not None:
        return fetch_error
    return _collect_end(
        merged, groups, _signals, used_templates, fetched, refresh_seen, data_root, week_start, week_end, limit, term
    )


def _norm_table(row: dict[str, object]) -> object:
    return row.get("_table") or row.get("table") or "trends"


def _norm_week(row: dict[str, object]) -> str:
    return str(row.get("_week") or row.get("week") or row.get("period"))


def _norm_geo(row: dict[str, object]) -> str:
    return str(row.get("_geo") if row.get("_geo") is not None else row.get("geo"))


def _norm_kind(row: dict[str, object]) -> object:
    return row.get("_kind") or row.get("list_kind") or "top"


def _norm_refresh(row: dict[str, object], week: str) -> str:
    return str(row.get("_refresh") or row.get("refresh_date") or week)


def _norm_keys(row: dict[str, object]) -> tuple[object, str, str, object, object, str]:
    week = _norm_week(row)
    return _norm_table(row), week, _norm_geo(row), row.get("term"), _norm_kind(row), _norm_refresh(row, week)


def _cached_roundtrip(row: dict[str, object]) -> tuple[dict[str, object], list[object]] | None:
    if row.get("_cached") and isinstance(row.get("metrics"), dict):
        # Warehouse roundtrip: reuse stored metrics/evidence verbatim so a
        # recollect re-hashes identically (durable dedup, first known_at kept).
        return (dict(as_dict(row.get("metrics"), what="metrics")), list(as_list(row.get("evidence"), what="evidence")))
    return None


def _apply_subregion_counts(metrics: dict[str, object], row: dict[str, object]) -> None:
    if row.get("dma_count") is not None:
        metrics["dma_count"] = row["dma_count"]
    if row.get("region_count") is not None:
        metrics["region_count"] = row["region_count"]
    if row.get("dma_count") is not None or row.get("region_count") is not None:
        metrics["score_basis"] = "mean_list_score_where_listed"


def _fresh_metrics(row: dict[str, object], refresh: str, week: str) -> dict[str, object]:
    metrics: dict[str, object] = {
        "rank": row.get("rank"),
        "score": row.get("score"),
        "percent_gain": row.get("percent_gain"),
        "refresh_date": refresh,
        "week": week,
        "dma_id": row.get("dma_id"),
        "dma_name": row.get("dma_name"),
        "country_name": row.get("country_name"),
        "country_code": row.get("country_code"),
        "region_name": row.get("region_name"),
        "region_code": row.get("region_code"),
    }
    _apply_subregion_counts(metrics, row)
    return metrics


def _fresh_evidence(row: dict[str, object], table: object) -> list[object]:
    return [
        {
            "table": table,
            "row": {k: v for k, v in row.items() if not k.startswith("_")},
            "job_id": row.get("job_id"),
            "template": row.get("_template"),
        }
    ]


def _normalize_row(
    row: dict[str, object], retrieved_at: str, data_root: Path | str | None, features: object = None
) -> dict[str, object]:
    """Map one merged row to a persisted candidate observation."""
    from . import signals as _signals_n

    table, week, geo, term, kind, refresh = _norm_keys(row)
    roundtrip = _cached_roundtrip(row)
    if roundtrip is not None:
        metrics, evidence = roundtrip
    else:
        metrics = _fresh_metrics(row, refresh, week)
        evidence = _fresh_evidence(row, table)
    return _signals_n.normalize_candidate(
        table=table,
        period=week,
        geo=geo,
        term=term,
        list_kind=kind,
        rank=row.get("rank"),
        source=SOURCE,
        source_record_id=f"{table}|{refresh}|{geo}|{term}|{kind}",
        observed_at=week,
        entities=[],
        metrics=metrics,
        evidence=evidence,
        features=features,
        retrieved_at=row.get("retrieved_at") or retrieved_at,
        known_at=row.get("known_at"),
        # Warehouse is the durable store; JSONL stays a read-only legacy trail.
        data_root=None,
        persist=False,
    )
