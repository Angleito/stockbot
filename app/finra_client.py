"""FINRA Query API client with filing-cabinet catalog + metadata discovery.

tools.py never talks to FINRA HTTP endpoints directly.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from . import cache
from .config import (
    FINRA_API_BASE,
    FINRA_TOKEN_URL,
    finra_use_mock,
    get_finra_client_id,
    get_finra_client_secret,
)
from .finra_analysis import analyze_and_brief

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
MAX_OFFSET = 500_000
CACHE_TTL_SECONDS = 3600
DISCOVERY_TTL_SECONDS = 86400
TOKEN_SKEW_SECONDS = 60

DATAPOINTS_DEFAULT_LIMIT = 10
DATAPOINTS_MAX_LIMIT = 25
DATAPOINTS_MAX_FIELDS = 10

# FINRA record-pagination headers (requests lowercases header names).
_RECORD_HEADERS = (
    "record-total",
    "record-offset",
    "record-limit",
    "record-max-limit",
)

_ALLOWED_COMPARE = {
    "EQUAL",
    "GREATER",
    "LESSER",
    "GTE",
    "LTE",
    "NOT_EQUAL",
    "BEGINS_WITH",
}

# Prefer these exact field names when detecting a ticker/symbol column.
_PREFERRED_SYMBOL_FIELDS = (
    "symbolCode",
    "issueSymbolIdentifier",
    "securitiesInformationProcessorSymbolIdentifier",
    "oldSymbolCode",
)

_WEEKLY_SUMMARY_TYPES = (
    "OTC_W_FIRM",
    "OTC_W_SMBL",
    "OTC_W_SMBL_FIRM",
    "OTC_W_VOL_STATS",
    "ATS_W_FIRM",
    "ATS_W_SMBL",
    "ATS_W_SMBL_FIRM",
    "ATS_W_VOL_STATS",
)

# Corrections applied on top of live catalog/metadata before exposure.
# Keys are lowercase (group, name).
_METADATA_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("fixedincomemarket", "treasurydailyaggregates"): {
        "market_aggregate": True,
        "symbol_field": None,
        "date_field": "tradeDate",
    },
    ("fixedincomemarket", "treasurymonthlyaggregates"): {
        "market_aggregate": True,
        "symbol_field": None,
        "date_field": "beginningOfTheMonthDate",
    },
    ("otcmarket", "weeklysummary"): {
        "symbol_field": "issueSymbolIdentifier",
        "date_field": "summaryStartDate",
        "default_filters": (("summaryTypeCode", "OTC_W_SMBL"),),
        "valid_filter_values": {"summaryTypeCode": _WEEKLY_SUMMARY_TYPES},
    },
    ("otcmarket", "weeklysummaryhistoric"): {
        "symbol_field": "issueSymbolIdentifier",
        "date_field": "summaryStartDate",
        "default_filters": (("summaryTypeCode", "OTC_W_SMBL"),),
        "valid_filter_values": {"summaryTypeCode": _WEEKLY_SUMMARY_TYPES},
    },
    ("otcmarket", "monthlysummary"): {
        "symbol_field": "issueSymbolIdentifier",
        "date_field": "summaryStartDate",
    },
    ("otcmarket", "consolidatedshortinterest"): {
        "symbol_field": "symbolCode",
        "date_field": "settlementDate",
    },
    ("otcmarket", "regshodaily"): {
        "symbol_field": "securitiesInformationProcessorSymbolIdentifier",
        "date_field": "tradeReportDate",
    },
    ("otcmarket", "thresholdlist"): {
        "symbol_field": "issueSymbolIdentifier",
        "date_field": "tradeDate",
    },
    ("otcmarket", "otcdailylist"): {
        "symbol_field": "oldSymbolCode",
        "date_field": "dailyListDatetime",
    },
}

# Legacy bare names kept only for resolution hints / error messages.
DATASET_NAMES = (
    "consolidatedShortInterest",
    "regShoDaily",
    "thresholdList",
    "weeklySummary",
    "weeklySummaryHistoric",
    "monthlySummary",
    "blocksSummary",
    "otcBlocksSummary",
    "otcDailyList",
    "agencyTbaPricing",
    "agencyCmoPricing",
    "agencyMarketBreadth",
    "agencyMarketSentiment",
    "agencyMbsTradingActivity",
    "agencyMbsArmHybridPricing",
    "agencyMbsPricing",
    "collateralizedObligationPricing",
    "corporate144AMarketBreadth",
    "corporate144AMarketSentiment",
    "corporatesAndAgenciesCappedVolume",
    "corporateMarketBreadth",
    "corporateMarketSentiment",
    "dailyCmbsPricing",
    "weeklyCmbsPricing",
    "nonAgencyCmoAbsPricing",
    "nonAgencyCmoVintagePricing",
    "securitizedProductsCappedVolume",
    "securitizedProductErrata",
    "securitizedProductTradingActivity",
    "treasuryDailyAggregates",
    "treasuryMonthlyAggregates",
    "industrySnapshotFirmsByRegistrationType",
)


@dataclass(frozen=True)
class CatalogEntry:
    group: str
    name: str
    description: str
    methods: tuple[str, ...] = ()
    supports_query: bool = True
    status: str = ""
    access: str = "unknown"
    supports_record_offset: Optional[bool] = None

    @property
    def dataset_id(self) -> str:
        return f"{self.group}/{self.name}"


@dataclass(frozen=True)
class DatasetSpec:
    group: str
    name: str
    description: str
    fields: tuple[dict[str, Any], ...] = ()
    partition_fields: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()
    symbol_field: Optional[str] = None
    date_field: Optional[str] = None
    market_aggregate: bool = False
    default_filters: tuple[tuple[str, str], ...] = ()
    valid_filter_values: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def dataset_id(self) -> str:
        return f"{self.group}/{self.name}"

    @property
    def field_names(self) -> frozenset[str]:
        return frozenset(f["name"] for f in self.fields if f.get("name"))


_token_lock = threading.Lock()
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0

_discovery_lock = threading.Lock()
_catalog_mem: Optional[list[CatalogEntry]] = None
_metadata_mem: dict[str, DatasetSpec] = {}


def list_datasets(
    group: Optional[str] = None, search: Optional[str] = None
) -> dict:
    """Concise filing-cabinet catalog. Never returns data rows."""
    try:
        entries = _get_catalog()
    except Exception as e:
        return {"error": _catalog_error_message(e)}

    group_filter = (group or "").strip().lower() or None
    search_filter = (search or "").strip().lower() or None

    datasets = []
    for entry in entries:
        if group_filter and entry.group.lower() != group_filter:
            continue
        if search_filter:
            hay = f"{entry.group} {entry.name} {entry.description}".lower()
            if search_filter not in hay:
                continue
        # Report capabilities only when verified (override corrections or
        # catalog flags). Unknown stays null until describe_finra_dataset
        # fetches real metadata — never guessed from the dataset name.
        override = _METADATA_OVERRIDES.get((entry.group.lower(), entry.name.lower()), {})
        supports_ticker: Optional[bool] = None
        supports_date: Optional[bool] = None
        if "symbol_field" in override:
            supports_ticker = bool(override["symbol_field"]) and not override.get(
                "market_aggregate"
            )
        if "date_field" in override:
            supports_date = bool(override["date_field"])
        if override.get("market_aggregate"):
            supports_ticker = False

        datasets.append(
            {
                "dataset": entry.dataset_id,
                "group": entry.group,
                "name": entry.name,
                "description": entry.description,
                "supports_ticker": supports_ticker,
                "supports_date": supports_date,
                "supports_record_offset": entry.supports_record_offset,
                "access": entry.access,
            }
        )

    return {
        "source": "FINRA Query API catalog",
        "count": len(datasets),
        "datasets": datasets,
    }


def describe_dataset(dataset_id: str) -> dict:
    """Full field metadata for one dataset (filing-cabinet describe step)."""
    try:
        entry = _resolve_dataset(dataset_id)
        spec = _get_dataset_spec(entry)
    except ValueError as e:
        return {"error": str(e)}
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status in (401, 403):
            return {
                "error": (
                    f"FINRA returned {status} for metadata of '{dataset_id}'. "
                    "It may not be public or the configured credentials lack "
                    "the required entitlement. Use list_finra_datasets to see "
                    "available datasets."
                )
            }
        return {"error": _catalog_error_message(e)}
    except Exception as e:
        return {"error": _catalog_error_message(e)}

    fields_out = [
        {
            "name": f.get("name"),
            "type": f.get("type"),
            "description": f.get("description") or "",
            **({"format": f["format"]} if f.get("format") else {}),
        }
        for f in spec.fields
        if f.get("name")
    ]

    result: dict[str, Any] = {
        "dataset": spec.dataset_id,
        "group": spec.group,
        "name": spec.name,
        "description": spec.description,
        "fields": fields_out,
        "partition_fields": list(spec.partition_fields),
        "ticker_field": spec.symbol_field,
        "date_field": spec.date_field,
        "supports_ticker": bool(spec.symbol_field) and not spec.market_aggregate,
        "supports_date": bool(spec.date_field),
        "market_aggregate": spec.market_aggregate,
        "supported_methods": list(spec.methods),
        "supports_record_offset": entry.supports_record_offset,
        "access": entry.access,
        "source": f"FINRA metadata {spec.group}/{spec.name}",
    }
    if spec.valid_filter_values:
        result["valid_filter_values"] = {
            k: list(v) for k, v in spec.valid_filter_values.items()
        }
    if spec.default_filters:
        result["default_filters"] = [
            {"field": f, "value": v} for f, v in spec.default_filters
        ]
    return result


def get_short_interest(ticker: str, settlement_date: Optional[str] = None) -> dict:
    return query_dataset(
        "otcMarket/consolidatedShortInterest",
        ticker=ticker,
        start_date=settlement_date,
        end_date=settlement_date,
        limit=50,
    )


def get_reg_sho_volume(ticker: str, trade_date: Optional[str] = None) -> dict:
    return query_dataset(
        "otcMarket/regShoDaily",
        ticker=ticker,
        start_date=trade_date,
        end_date=trade_date,
        limit=100,
    )


def get_threshold_securities(
    ticker: Optional[str] = None, trade_date: Optional[str] = None
) -> dict:
    return query_dataset(
        "otcMarket/thresholdList",
        ticker=ticker,
        start_date=trade_date,
        end_date=trade_date,
        limit=200 if not ticker else 50,
    )


def query_dataset(
    dataset: str,
    ticker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    filters: Optional[list[dict]] = None,
    analysis_goal: Optional[str] = None,
) -> dict:
    """Query a FINRA dataset and return an analyzed briefing.

    The main model receives deterministic metrics, trends, warnings, and
    (when configured) validated prose — never the raw records.
    """
    try:
        entry = _resolve_dataset(dataset)
        spec = _get_dataset_spec(entry)
        payload = _build_payload(
            spec, entry, ticker, start_date, end_date, limit, filters, offset
        )
        records, headers = _cached_query(spec, payload)
    except ValueError as e:
        return {"error": str(e)}
    except requests.HTTPError as e:
        return _http_error_result(dataset, e)
    except Exception as e:
        logger.exception("FINRA query failed for dataset %s", dataset)
        msg = str(e)
        if "FINRA_CLIENT" in msg or "catalog" in msg.lower():
            return {"error": _catalog_error_message(e)}
        return {"error": f"FINRA query failed: {e}"}

    if not records:
        what = ticker or dataset
        return {"error": f"No data found for {what}: {spec.name}"}

    effective_limit = int(payload.get("limit", DEFAULT_LIMIT))
    effective_offset = int(payload.get("offset", 0))
    returned_count = len(records)
    pagination = _parse_pagination(headers, effective_offset, effective_limit, returned_count)

    analysis = analyze_and_brief(
        spec,
        records,
        analysis_goal,
        _query_cache_key(spec, payload),
        pagination=pagination,
    )
    return {
        "dataset": spec.name,
        "group": spec.group,
        "dataset_id": spec.dataset_id,
        "source": f"FINRA Query API {spec.group}/{spec.name}",
        "query": {
            "ticker": (ticker or "").strip().upper() or None,
            "start_date": start_date,
            "end_date": end_date,
            "limit": effective_limit,
            "offset": effective_offset,
            "filters": filters,
        },
        "coverage": analysis["coverage"],
        "metrics": analysis["metrics"],
        "trends": analysis["trends"],
        "warnings": analysis["warnings"],
        "briefing": analysis["briefing"],
        "briefing_source": analysis["briefing_source"],
        "analysis_model": analysis["analysis_model"],
        "returned_count": returned_count,
        "limit": effective_limit,
        "offset": effective_offset,
        "next_offset": effective_offset + returned_count,
        "may_have_more": pagination["may_have_more"],
        "total_records": pagination["total_records"],
        "pagination_source": pagination["source"],
    }


def get_finra_datapoints(
    dataset: str,
    fields: Optional[list[str]] = None,
    ticker: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: Optional[int] = None,
    filters: Optional[list[dict]] = None,
    sort_fields: Optional[list[str]] = None,
    sort_order: Optional[str] = None,
) -> dict:
    """Exact requested fields from FINRA source records (explicit data asks).

    Requires a non-empty, metadata-validated fields list (at most
    DATAPOINTS_MAX_FIELDS) and at least one narrowing condition (ticker,
    date/date range, or filter). Returns only the selected fields per row,
    capped at DATAPOINTS_MAX_LIMIT. Exact values are guaranteed for normal
    scalar data; oversized text fields may be shown as a marked excerpt by
    the rendering layer.

    sort_fields uses FINRA sortFields syntax: '+field' ascending, '-field'
    descending (e.g. ["-settlementDate"] for newest first). sort_order is a
    convenience that sorts by the dataset's date field when one exists. When
    exactly one sort field is requested, rows are re-ordered deterministically
    before returning so 'latest five' style requests are stable regardless of
    source ordering.
    """
    try:
        entry = _resolve_dataset(dataset)
        spec = _get_dataset_spec(entry)
        selected = _validate_datapoint_fields(spec, fields)
        _require_datapoint_narrowing(ticker, start_date, end_date, filters)
        sort = _validate_sort(spec, sort_fields, sort_order)
        payload = _build_payload(
            spec,
            entry,
            ticker,
            start_date,
            end_date,
            _clamp_datapoint_limit(limit),
            filters,
            fields=selected,
            sort_fields=sort,
        )
        records, headers = _cached_query(spec, payload)
    except ValueError as e:
        return {"error": str(e)}
    except requests.HTTPError as e:
        return _http_error_result(dataset, e)
    except Exception as e:
        logger.exception("FINRA query failed for dataset %s", dataset)
        msg = str(e)
        if "FINRA_CLIENT" in msg or "catalog" in msg.lower():
            return {"error": _catalog_error_message(e)}
        return {"error": f"FINRA query failed: {e}"}

    if not records:
        what = ticker or dataset
        return {"error": f"No data found for {what}: {spec.name}"}

    effective_limit = int(payload.get("limit", DATAPOINTS_DEFAULT_LIMIT))
    effective_offset = int(payload.get("offset", 0))
    # Defensive cap: never surface more rows than the effective limit.
    reduced = [_select_fields(row, selected) for row in records[:effective_limit]]
    reduced = _apply_local_sort(reduced, sort)
    pagination = _parse_pagination(headers, effective_offset, effective_limit, len(reduced))
    result: dict[str, Any] = {
        "dataset": spec.name,
        "group": spec.group,
        "dataset_id": spec.dataset_id,
        "source": f"FINRA Query API {spec.group}/{spec.name}",
        "fields": list(selected),
        "records": reduced,
        "returned_count": len(reduced),
        "limit": effective_limit,
        "offset": effective_offset,
        "next_offset": effective_offset + len(reduced),
        "may_have_more": pagination["may_have_more"],
        "total_records": pagination["total_records"],
        "pagination_source": pagination["source"],
    }
    if sort:
        result["sort_fields"] = list(sort)
    return result


def _http_error_result(dataset: str, exc: requests.HTTPError) -> dict:
    status = exc.response.status_code if exc.response is not None else None
    body = exc.response.text[:500] if exc.response is not None else ""
    logger.exception("FINRA HTTP error for dataset %s", dataset)
    if status in (401, 403):
        return {
            "error": (
                f"FINRA returned {status} for dataset '{dataset}'. "
                "It may not be public or the configured credentials lack "
                "the required entitlement. Use list_finra_datasets to see "
                "available datasets, or omit this request."
            )
        }
    return {
        "error": f"FINRA request failed ({status if status is not None else '?'}): {body}"
    }


def _validate_datapoint_fields(spec: DatasetSpec, fields: Optional[list[str]]) -> list[str]:
    if not fields or not isinstance(fields, list):
        raise ValueError(
            "get_finra_datapoints requires a non-empty 'fields' list. "
            "Call describe_finra_dataset first to see available fields."
        )
    normalized: list[str] = []
    for index, f in enumerate(fields):
        if not isinstance(f, str) or not f.strip():
            raise ValueError(
                f"fields #{index} is malformed: a non-empty field name is required."
            )
        name = f.strip()
        if spec.field_names and name not in spec.field_names:
            known = ", ".join(sorted(spec.field_names)[:30])
            raise ValueError(
                f"Dataset '{spec.dataset_id}' has no field '{name}'. "
                f"Known fields include: {known}"
            )
        if name not in normalized:
            normalized.append(name)
    if len(normalized) > DATAPOINTS_MAX_FIELDS:
        raise ValueError(
            f"get_finra_datapoints accepts at most {DATAPOINTS_MAX_FIELDS} "
            f"fields, got {len(normalized)}. Select the specific fields you "
            "need instead."
        )
    return normalized


def _validate_sort(
    spec: DatasetSpec,
    sort_fields: Optional[list[str]],
    sort_order: Optional[str],
) -> list[str]:
    """Normalize FINRA sortFields entries ('+field' / '-field').

    sort_order is a convenience mapping onto the dataset's date field; it is
    rejected when the dataset has no date field or when sort_fields is also
    given. Raises ValueError before any data request on invalid input.
    """
    if sort_order is not None:
        if sort_fields:
            raise ValueError(
                "Provide either 'sort_fields' or 'sort_order', not both."
            )
        order = str(sort_order).strip().lower()
        if order not in ("asc", "desc"):
            raise ValueError("sort_order must be 'asc' or 'desc'.")
        if not spec.date_field:
            raise ValueError(
                f"Dataset '{spec.dataset_id}' has no date field, so "
                "'sort_order' cannot be applied; use 'sort_fields' instead."
            )
        return [("-" if order == "desc" else "+") + spec.date_field]
    if not sort_fields:
        return []
    normalized: list[str] = []
    for index, entry in enumerate(sort_fields):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"sort_fields #{index} is malformed: expected '+field' "
                "(ascending) or '-field' (descending)."
            )
        raw = entry.strip()
        sign, name = (raw[0], raw[1:]) if raw[0] in "+-" else ("+", raw)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(
                f"sort_fields #{index} is malformed: '{raw}' is not a valid "
                "FINRA sort field. Use '+field' (ascending) or '-field' "
                "(descending)."
            )
        if spec.field_names and name not in spec.field_names:
            known = ", ".join(sorted(spec.field_names)[:30])
            raise ValueError(
                f"Dataset '{spec.dataset_id}' has no sortable field "
                f"'{name}'. Known fields include: {known}"
            )
        normalized.append(sign + name)
    return normalized


def _apply_local_sort(rows: list[dict], sort_fields: list[str]) -> list[dict]:
    """Deterministic local ordering for a single FINRA sort field.

    Guarantees 'latest five' style requests return the requested order even
    when the response ordering is not honored. Missing values always sort
    last. Multi-field sorts rely on FINRA's server-side sortFields ordering.
    """
    if len(sort_fields) != 1:
        return rows
    sign, name = sort_fields[0][0], sort_fields[0][1:]
    descending = sign == "-"

    def _sortable(value: Any) -> Any:
        if value is None or value == "":
            return None
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return str(value)

    present = [r for r in rows if _sortable(r.get(name)) is not None]
    missing = [r for r in rows if _sortable(r.get(name)) is None]
    try:
        present.sort(key=lambda r: _sortable(r.get(name)), reverse=descending)
    except TypeError:
        # Mixed numeric/string values in one column: fall back to string order.
        present.sort(key=lambda r: str(r.get(name)), reverse=descending)
    return present + missing


def _require_datapoint_narrowing(
    ticker: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    filters: Optional[list[dict]],
) -> None:
    has_ticker = bool((ticker or "").strip())
    has_date = bool((start_date or "").strip()) or bool((end_date or "").strip())
    has_filter = bool(filters)
    if not (has_ticker or has_date or has_filter):
        raise ValueError(
            "get_finra_datapoints requires at least one narrowing condition: "
            "ticker, date/date range, or filters. Unbounded raw-data requests "
            "are not allowed."
        )


def _clamp_datapoint_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DATAPOINTS_DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DATAPOINTS_DEFAULT_LIMIT
    return max(1, min(n, DATAPOINTS_MAX_LIMIT))


def _select_fields(row: dict, fields: list[str]) -> dict:
    return {f: row.get(f) for f in fields}


def _parse_pagination(headers: dict, offset: int, limit: int, returned_count: int) -> dict:
    """Header-driven pagination; explicit estimate only when FINRA omits
    Record-Total. Self-contained metadata: offset/limit/returned_count are
    included so the analysis layer can prove full-query coverage."""
    total_raw = headers.get("record-total")
    total: Optional[int] = None
    if total_raw is not None:
        try:
            total = int(total_raw)
        except (TypeError, ValueError):
            total = None
    base = {
        "offset": offset,
        "limit": limit,
        "returned_count": returned_count,
    }
    if total is not None:
        base.update(
            {
                "total_records": total,
                "may_have_more": (offset + returned_count) < total,
                "source": "finra_header",
            }
        )
        return base
    base.update(
        {
            "total_records": None,
            "may_have_more": returned_count >= limit,
            "source": "estimate",
        }
    )
    return base


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _catalog_error_message(exc: BaseException) -> str:
    msg = str(exc)
    if "FINRA_CLIENT" in msg:
        return msg
    return (
        f"FINRA catalog unavailable: {msg}. "
        "Configure FINRA_CLIENT_ID / FINRA_CLIENT_SECRET and ensure network "
        "access to api.finra.org, then retry list_finra_datasets."
    )


def _get_catalog() -> list[CatalogEntry]:
    global _catalog_mem
    with _discovery_lock:
        if _catalog_mem is not None:
            return _catalog_mem

    cache_key = "finra:catalog:v1"
    hit = cache.get(cache_key, ttl=DISCOVERY_TTL_SECONDS)
    if isinstance(hit, list) and hit:
        entries = [_entry_from_dict(d) for d in hit]
        with _discovery_lock:
            _catalog_mem = entries
        return entries

    raw = _fetch_catalog_http()
    entries = [_normalize_catalog_item(item) for item in raw]
    entries = [e for e in entries if e is not None]
    # Drop clearly non-query / retired entries, and entries explicitly
    # confirmed non-public (entitled-only). Unknown access is kept and
    # reported honestly; 401/403 at query time is the runtime signal.
    entries = [
        e
        for e in entries
        if e.supports_query
        and e.access != "entitled"
        and (not e.status or e.status.lower() not in (
            "retired", "deprecated", "inactive", "terminated"
        ))
    ]
    cache.set(cache_key, [_entry_to_dict(e) for e in entries])
    with _discovery_lock:
        _catalog_mem = entries
    return entries


def _fetch_catalog_http() -> list[dict]:
    url = f"{FINRA_API_BASE}/datasets"
    resp = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Accept": "application/json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("datasets", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    raise ValueError("FINRA /datasets response did not contain a dataset list")


def _normalize_catalog_item(item: dict) -> Optional[CatalogEntry]:
    if not isinstance(item, dict):
        return None
    group = (
        item.get("group")
        or item.get("datasetGroup")
        or item.get("datasetGroupName")
        or ""
    )
    name = (
        item.get("name")
        or item.get("datasetName")
        or item.get("dataset")
        or ""
    )
    group = str(group).strip()
    name = str(name).strip()
    if not group or not name:
        return None
    # Prefer camelCase path segments when the catalog returns shouting-case.
    group = _prefer_camel(group)
    name = _prefer_camel(name)
    description = (
        str(item.get("description") or "").strip()
        or f"{group}/{name}"
    )
    methods_raw = item.get("supportedMethods") or item.get("methods") or []
    if isinstance(methods_raw, str):
        methods = tuple(m.strip() for m in methods_raw.split(",") if m.strip())
    else:
        methods = tuple(str(m) for m in methods_raw)
    supports_query = item.get("supportsQuery")
    if supports_query is None:
        supports_query = (not methods) or ("POST" in {m.upper() for m in methods}) or (
            "GET" in {m.upper() for m in methods}
        )
    status = str(item.get("status") or "").strip()
    return CatalogEntry(
        group=group,
        name=name,
        description=description,
        methods=methods,
        supports_query=bool(supports_query),
        status=status,
        access=_parse_access(item),
        supports_record_offset=_parse_optional_bool(
            item.get("supportsRecordOffset"), item.get("supports_record_offset")
        ),
    )


def _parse_access(item: dict) -> str:
    """Map any FINRA-provided access/credential metadata to a label.

    FINRA's /datasets response does not document an access field today, so
    real entries resolve to "unknown". We still parse the plausible shapes
    defensively and only claim "public" when explicitly confirmed.
    """
    raw = item.get("access") or item.get("accessType") or item.get("credentialType")
    if raw is not None:
        s = str(raw).strip().lower()
        if s in ("public", "open"):
            return "public"
        if s in ("firm", "organization", "entitled", "restricted", "private"):
            return "entitled"
    is_public = item.get("isPublic")
    if is_public is None:
        is_public = item.get("public")
    if isinstance(is_public, bool):
        return "public" if is_public else "entitled"
    return "unknown"


def _parse_optional_bool(*values: Any) -> Optional[bool]:
    for v in values:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no"):
                return False
    return None


def _prefer_camel(value: str) -> str:
    """If value is ALLCAPS/closed-up, keep as-is; otherwise return stripped."""
    return value.strip()


def _entry_to_dict(e: CatalogEntry) -> dict:
    return {
        "group": e.group,
        "name": e.name,
        "description": e.description,
        "methods": list(e.methods),
        "supports_query": e.supports_query,
        "status": e.status,
        "access": e.access,
        "supports_record_offset": e.supports_record_offset,
    }


def _entry_from_dict(d: dict) -> CatalogEntry:
    return CatalogEntry(
        group=d["group"],
        name=d["name"],
        description=d.get("description") or "",
        methods=tuple(d.get("methods") or ()),
        supports_query=bool(d.get("supports_query", True)),
        status=d.get("status") or "",
        access=d.get("access") or "unknown",
        supports_record_offset=d.get("supports_record_offset"),
    )


def _resolve_dataset(dataset_id: str) -> CatalogEntry:
    raw = (dataset_id or "").strip()
    if not raw:
        raise ValueError(
            "Dataset id is required. Use list_finra_datasets to browse the catalog."
        )

    entries = _get_catalog()
    by_id = {e.dataset_id.lower(): e for e in entries}
    by_name: dict[str, list[CatalogEntry]] = {}
    for e in entries:
        by_name.setdefault(e.name.lower(), []).append(e)

    if "/" in raw:
        key = raw.lower()
        # Also try after normalizing whitespace
        if key in by_id:
            return by_id[key]
        # Case-insensitive group/name match even if catalog casing differs
        parts = raw.split("/", 1)
        if len(parts) == 2:
            g, n = parts[0].strip().lower(), parts[1].strip().lower()
            for e in entries:
                if e.group.lower() == g and e.name.lower() == n:
                    return e
        raise ValueError(
            f"Unknown FINRA dataset '{dataset_id}'. "
            "Call list_finra_datasets to browse available datasets."
        )

    # Legacy bare name
    matches = by_name.get(raw.lower(), [])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        ids = ", ".join(m.dataset_id for m in matches)
        raise ValueError(
            f"Ambiguous FINRA dataset name '{dataset_id}'. "
            f"Specify one of: {ids}"
        )

    # Close matches for bare name
    close = [e for e in entries if raw.lower() in e.name.lower()]
    if close:
        suggestions = ", ".join(e.dataset_id for e in close[:8])
        raise ValueError(
            f"Unknown FINRA dataset '{dataset_id}'. "
            f"Did you mean: {suggestions}? "
            "Call list_finra_datasets to browse available datasets."
        )
    # Mention a few well-known legacy names in the error for agent guidance.
    known_hint = ", ".join(DATASET_NAMES[:6])
    raise ValueError(
        f"Unknown FINRA dataset '{dataset_id}'. "
        f"Examples: {known_hint}. "
        "Call list_finra_datasets to browse available datasets."
    )


def _get_dataset_spec(entry: CatalogEntry) -> DatasetSpec:
    key = entry.dataset_id.lower()
    with _discovery_lock:
        if key in _metadata_mem:
            return _metadata_mem[key]

    cache_key = f"finra:metadata:v1:{entry.group}/{entry.name}"
    hit = cache.get(cache_key, ttl=DISCOVERY_TTL_SECONDS)
    if isinstance(hit, dict) and hit.get("fields") is not None:
        spec = _spec_from_cached(entry, hit)
        with _discovery_lock:
            _metadata_mem[key] = spec
        return spec

    raw = _fetch_metadata_http(entry.group, entry.name)
    spec = _build_spec_from_metadata(entry, raw)
    cache.set(
        cache_key,
        {
            "fields": list(spec.fields),
            "partition_fields": list(spec.partition_fields),
            "description": spec.description,
            "methods": list(spec.methods),
            "symbol_field": spec.symbol_field,
            "date_field": spec.date_field,
            "market_aggregate": spec.market_aggregate,
            "default_filters": [list(p) for p in spec.default_filters],
            "valid_filter_values": {
                k: list(v) for k, v in spec.valid_filter_values.items()
            },
        },
    )
    with _discovery_lock:
        _metadata_mem[key] = spec
    return spec


def _fetch_metadata_http(group: str, name: str) -> dict:
    # Metadata is public; mock-mode data uses a Mock suffix but metadata
    # is fetched for the base dataset name (fields match).
    url = f"{FINRA_API_BASE}/metadata/group/{group}/name/{name}"
    resp = requests.get(
        url,
        headers={"Accept": "application/json"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError(f"Unexpected metadata response for {group}/{name}")
    return data


def _build_spec_from_metadata(entry: CatalogEntry, raw: dict) -> DatasetSpec:
    fields_raw = raw.get("fields") or raw.get("datasetFields") or []
    fields: list[dict[str, Any]] = []
    for f in fields_raw:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        fields.append(
            {
                "name": f["name"],
                "type": f.get("type") or f.get("dataType") or "",
                "description": f.get("description") or "",
                **({"format": f["format"]} if f.get("format") else {}),
            }
        )
    partition_raw = raw.get("partitionFields") or raw.get("partitions") or []
    partition_fields = tuple(str(p) for p in partition_raw)

    description = (
        str(raw.get("description") or "").strip()
        or entry.description
        or entry.dataset_id
    )
    methods = entry.methods
    if not methods:
        sm = raw.get("supportedMethods") or []
        methods = tuple(str(m) for m in sm)

    symbol_field = _detect_symbol_field(fields)
    date_field = _detect_date_field(fields, partition_fields)
    market_aggregate = False
    default_filters: tuple[tuple[str, str], ...] = ()
    valid_filter_values: dict[str, tuple[str, ...]] = {}

    override = _METADATA_OVERRIDES.get((entry.group.lower(), entry.name.lower()), {})
    if "symbol_field" in override:
        symbol_field = override["symbol_field"]
    if "date_field" in override:
        date_field = override["date_field"]
    if override.get("market_aggregate"):
        market_aggregate = True
        symbol_field = None
    if "default_filters" in override:
        default_filters = tuple(override["default_filters"])
    if "valid_filter_values" in override:
        valid_filter_values = {
            k: tuple(v) for k, v in override["valid_filter_values"].items()
        }

    # Validate override field names against metadata when present.
    field_names = {f["name"] for f in fields}
    if symbol_field and field_names and symbol_field not in field_names:
        # Keep override if metadata is incomplete; otherwise drop.
        if field_names:
            symbol_field = _detect_symbol_field(fields)
    if date_field and field_names and date_field not in field_names:
        date_field = _detect_date_field(fields, partition_fields)

    return DatasetSpec(
        group=entry.group,
        name=entry.name,
        description=description,
        fields=tuple(fields),
        partition_fields=partition_fields,
        methods=methods,
        symbol_field=symbol_field,
        date_field=date_field,
        market_aggregate=market_aggregate,
        default_filters=default_filters,
        valid_filter_values=valid_filter_values,
    )


def _spec_from_cached(entry: CatalogEntry, hit: dict) -> DatasetSpec:
    default_filters = tuple(
        (pair[0], pair[1]) for pair in (hit.get("default_filters") or [])
    )
    valid = {
        k: tuple(v) for k, v in (hit.get("valid_filter_values") or {}).items()
    }
    return DatasetSpec(
        group=entry.group,
        name=entry.name,
        description=hit.get("description") or entry.description,
        fields=tuple(hit.get("fields") or ()),
        partition_fields=tuple(hit.get("partition_fields") or ()),
        methods=tuple(hit.get("methods") or entry.methods),
        symbol_field=hit.get("symbol_field"),
        date_field=hit.get("date_field"),
        market_aggregate=bool(hit.get("market_aggregate")),
        default_filters=default_filters,
        valid_filter_values=valid,
    )


def _detect_symbol_field(fields: list[dict]) -> Optional[str]:
    names = {f["name"]: f for f in fields if f.get("name")}
    for preferred in _PREFERRED_SYMBOL_FIELDS:
        if preferred in names:
            return preferred
    for f in fields:
        n = f.get("name") or ""
        if "symbol" in n.lower() and (f.get("type") or "").lower() in (
            "string", "text", ""
        ):
            return n
    return None


def _detect_date_field(
    fields: list[dict], partition_fields: tuple[str, ...]
) -> Optional[str]:
    by_name = {f["name"]: f for f in fields if f.get("name")}
    for p in partition_fields:
        f = by_name.get(p)
        if f and _is_date_type(f.get("type")):
            return p
        # Partition key may be a date even without type annotation
        if f and re.search(r"date|datetime|time", p, re.I):
            return p
    for f in fields:
        if _is_date_type(f.get("type")):
            return f["name"]
    for f in fields:
        n = f.get("name") or ""
        if re.search(r"(^|.*)(date|datetime)$", n, re.I) or "Date" in n:
            return n
    return None


def _is_date_type(t: Any) -> bool:
    s = str(t or "").lower()
    return s in ("date", "datetime", "timestamp") or "date" in s


# ---------------------------------------------------------------------------
# Query payload + HTTP
# ---------------------------------------------------------------------------


def _build_payload(
    spec: DatasetSpec,
    entry: CatalogEntry,
    ticker: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    limit: Optional[int],
    filters: Optional[list[dict]],
    offset: Optional[int] = None,
    fields: Optional[list[str]] = None,
    sort_fields: Optional[list[str]] = None,
) -> dict:
    payload: dict[str, Any] = {"limit": _clamp_limit(limit)}
    if offset is not None:
        payload["offset"] = _validate_offset(entry, offset)
    if fields:
        payload["fields"] = list(fields)
    if sort_fields:
        payload["sortFields"] = list(sort_fields)
    compare: list[dict] = []

    explicit_fields: set[str] = set()
    normalized_extras: list[dict] = []
    for index, extra in enumerate(filters or []):
        if not isinstance(extra, dict):
            raise ValueError(
                f"Filter #{index} must be an object with 'field' and 'value'."
            )
        field_name = extra.get("field")
        value = extra.get("value")
        if not isinstance(field_name, str) or not field_name.strip():
            raise ValueError(
                f"Filter #{index} is malformed: a non-empty 'field' is required."
            )
        field_name = field_name.strip()
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(
                f"Filter for field '{field_name}' is malformed: 'value' is required."
            )
        op = str(extra.get("op") or "EQUAL").upper()
        if op not in _ALLOWED_COMPARE:
            raise ValueError(
                f"Unsupported compare op '{op}'. Use one of: {sorted(_ALLOWED_COMPARE)}"
            )
        if spec.field_names and field_name not in spec.field_names:
            known = ", ".join(sorted(spec.field_names)[:30])
            raise ValueError(
                f"Dataset '{spec.dataset_id}' has no filterable field "
                f"'{field_name}'. Known fields include: {known}"
            )
        allowed = spec.valid_filter_values.get(field_name)
        if allowed is not None and str(value) not in allowed:
            raise ValueError(
                f"Invalid value '{value}' for field '{field_name}' on "
                f"'{spec.dataset_id}'. Allowed values: {', '.join(allowed)}"
            )
        explicit_fields.add(field_name)
        normalized_extras.append(
            {"compareType": op, "fieldName": field_name, "fieldValue": str(value)}
        )

    symbol = (ticker or "").strip().upper() or None
    if symbol:
        if spec.market_aggregate or not spec.symbol_field:
            raise ValueError(
                f"Dataset '{spec.dataset_id}' is a market-wide aggregate with no "
                "ticker/symbol field. Omit ticker (and ISIN) filters; filter by "
                "date or other fields instead."
            )
        compare.append(
            {
                "compareType": "EQUAL",
                "fieldName": spec.symbol_field,
                "fieldValue": symbol,
            }
        )

    # Default filters (e.g. weeklySummary summaryTypeCode) only when the
    # caller did not already filter that field — avoids conflicting filters.
    for field_name, value in spec.default_filters:
        if field_name in explicit_fields:
            continue
        # Apply defaults when querying by ticker, or always for typed summaries
        # so bare weekly queries get symbol-level rows rather than mixed types.
        compare.append(
            {
                "compareType": "EQUAL",
                "fieldName": field_name,
                "fieldValue": value,
            }
        )

    start = (start_date or "").strip() or None
    end = (end_date or "").strip() or None
    if start or end:
        if not spec.date_field:
            raise ValueError(
                f"Dataset '{spec.dataset_id}' has no date field to filter on."
            )
        if start and end and start != end:
            payload["dateRangeFilters"] = [
                {
                    "fieldName": spec.date_field,
                    "startDate": start,
                    "endDate": end,
                }
            ]
        else:
            compare.append(
                {
                    "compareType": "EQUAL",
                    "fieldName": spec.date_field,
                    "fieldValue": start or end,
                }
            )

    compare.extend(normalized_extras)

    if compare:
        payload["compareFilters"] = compare
    return payload


def _clamp_limit(limit: Optional[int]) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _validate_offset(entry: CatalogEntry, offset: Any) -> int:
    try:
        n = int(offset)
    except (TypeError, ValueError):
        raise ValueError(f"offset must be an integer, got {offset!r}.")
    if n < 0:
        raise ValueError("offset must be >= 0.")
    if n > MAX_OFFSET:
        raise ValueError(
            f"offset {n} exceeds FINRA's maximum of {MAX_OFFSET}. Use filters "
            "to narrow the result set instead."
        )
    if entry.supports_record_offset is False:
        raise ValueError(
            f"Dataset '{entry.dataset_id}' does not support record offset "
            "pagination (supportsRecordOffset=false in the FINRA catalog)."
        )
    return n


def _dataset_path_name(spec: DatasetSpec) -> str:
    if finra_use_mock():
        return spec.name + "Mock"
    return spec.name


def _query_cache_key(spec: DatasetSpec, payload: dict) -> str:
    path_name = _dataset_path_name(spec)
    return f"finra:{spec.group}:{path_name}:{json.dumps(payload, sort_keys=True)}"


def _cached_query(spec: DatasetSpec, payload: dict) -> tuple[list, dict]:
    cache_key = _query_cache_key(spec, payload)
    hit = cache.get(cache_key, ttl=CACHE_TTL_SECONDS)
    if hit is not None:
        if isinstance(hit, dict) and "records" in hit:
            return hit["records"], hit.get("headers") or {}
        # Legacy cached plain list (pre-analysis layer): no headers.
        return hit, {}
    records, headers = _post_query(spec.group, _dataset_path_name(spec), payload)
    cache.set(cache_key, {"records": records, "headers": headers})
    return records, headers


def _post_query(group: str, dataset_name: str, payload: dict) -> tuple[list, dict]:
    url = f"{FINRA_API_BASE}/data/group/{group}/name/{dataset_name}"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    headers = {
        name.lower(): value
        for name, value in resp.headers.items()
        if name.lower() in _RECORD_HEADERS
    }
    return _extract_records(data), headers


def _extract_records(data: Any) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "records", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _access_token() -> str:
    global _cached_token, _token_expires_at
    now = time.time()
    with _token_lock:
        if _cached_token and now < _token_expires_at:
            return _cached_token
        token, expires_in = _fetch_token()
        _cached_token = token
        _token_expires_at = now + max(expires_in - TOKEN_SKEW_SECONDS, 30)
        return token


def reset_token_cache() -> None:
    """Test helper."""
    global _cached_token, _token_expires_at
    with _token_lock:
        _cached_token = None
        _token_expires_at = 0.0


def reset_discovery_cache() -> None:
    """Test helper — clears in-memory catalog/metadata caches."""
    global _catalog_mem, _metadata_mem
    with _discovery_lock:
        _catalog_mem = None
        _metadata_mem = {}


def _fetch_token() -> tuple[str, int]:
    client_id = get_finra_client_id()
    client_secret = get_finra_client_secret()
    resp = requests.post(
        FINRA_TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("FINRA token response missing access_token")
    expires_in = int(data.get("expires_in") or 3600)
    return token, expires_in
