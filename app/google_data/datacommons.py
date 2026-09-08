"""Data Commons macro context via bounded V2 REST observations.

Uses the official V2 observation API with DCIDs + statistical-variable IDs
(never the retired BigQuery mirror or trial keys). GOOGLE_DATA_ENABLED gates
the source and DATACOMMONS_API_KEY is required: without it the source reports
``DATACOMMONS_AUTH_REQUIRED`` with zero HTTP calls. Each facet stays a
distinct series with its original unit/provider — incompatible units or
providers are never spliced into one series. Date filtering is client-side so
the request stays one bounded call.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import requests

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

logger = logging.getLogger(__name__)

SOURCE = "datacommons"
_DC_URL = "https://api.datacommons.org/v2/observation"
_NODE_URL = "https://api.datacommons.org/v2/node"
_RESOLVE_URL = "https://api.datacommons.org/v2/resolve"
_TIMEOUT = 20
_MAX_LIMIT = 1000
_MAX_NODES = 50


def _data_enabled() -> bool:
    fn = getattr(_config, "google_data_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return os.getenv("GOOGLE_DATA_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _dc_key() -> str | None:
    fn = getattr(_config, "get_datacommons_api_key", None)
    if callable(fn):
        try:
            value = fn()
            if value:
                return str(value)
        except Exception:
            pass
    return (os.getenv("DATACOMMONS_API_KEY") or "").strip() or None


def _key_headers() -> dict[str, str] | None:
    key = _dc_key()
    if not key:
        return None
    return {"X-API-Key": key}


def _map_http_error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return {"status": "unavailable", "source": SOURCE,
                "error": f"data commons unreachable: {exc}",
                "error_type": "source_unavailable"}
    return {"status": "error", "source": SOURCE,
            "error": f"data commons request failed: {exc}", "error_type": "request_failed"}


def _map_status(response: requests.Response) -> dict[str, object] | None:
    code = response.status_code
    if code in (401, 403):
        return {"status": "unavailable", "source": SOURCE,
                "error": f"data commons refused (HTTP {code}): DATACOMMONS_AUTH_REQUIRED",
                "error_type": "auth_required"}
    if code == 429:
        return {"status": "unavailable", "source": SOURCE,
                "error": f"data commons refused (HTTP {code}): DATACOMMONS_RATE_LIMITED",
                "error_type": "rate_limited"}
    if code >= 400:
        return {"status": "error", "source": SOURCE,
                "error": f"data commons error (HTTP {code})",
                "error_type": "request_failed"}
    return None


def _obs_date(point: dict[str, object]) -> str:
    """Sort key for observation points: ISO date string, never missing here."""
    return str(point["date"])


def get_macro_context(geos: list[str] | str | None, variables: list[str] | str | None, *,
                      start_date: str | None = None, end_date: str | None = None,
                      limit: int = 100) -> dict[str, object]:
    """Bounded statistical observations for geos x variables, faceted series."""
    if isinstance(geos, str):
        geos = [geos]
    if isinstance(variables, str):
        variables = [variables]
    geos, variables = list(geos or []), list(variables or [])
    if not geos or not variables:
        return {"status": "error", "source": SOURCE,
                "error": "geos and variables are both required",
                "error_type": "invalid_params"}
    try:
        limit = max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return {"status": "error", "source": SOURCE,
                "error": f"invalid limit: {limit!r}", "error_type": "invalid_params"}
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled", "error": "DATACOMMONS_DISABLED"}
    headers = _key_headers()
    if headers is None:
        return {"status": "unavailable", "source": SOURCE,
                "error": "DATACOMMONS_AUTH_REQUIRED", "error_type": "auth_required"}
    payload = {"date": "", "entity": {"dcids": geos},
               "variable": {"dcids": variables},
               "select": ["date", "entity", "variable", "value", "facet"]}
    try:
        response = requests.post(_DC_URL, headers=headers, json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return _map_http_error(exc)
    mapped = _map_status(response)
    if mapped is not None:
        return mapped
    try:
        body = response.json()
    except ValueError:
        return {"status": "error", "source": SOURCE,
                "error": "data commons returned non-JSON", "error_type": "malformed_response"}
    retrieved_at = datetime.now(timezone.utc).isoformat()
    facets = body.get("facets", {}) or {}
    series: list[dict[str, object]] = []
    remaining = limit
    continuation = False
    by_variable = body.get("byVariable", {}) or {}
    variables_iter = by_variable.items() if isinstance(by_variable, dict) else []
    for variable, by_var in variables_iter:
        by_entity = (by_var or {}).get("byEntity", {}) or {}
        entities_iter = by_entity.items() if isinstance(by_entity, dict) else []
        for entity, by_ent in entities_iter:
            for facet in (by_ent or {}).get("orderedFacets", []) or []:
                facet_id = facet.get("facetId")
                meta = facets.get(facet_id, {}) if isinstance(facets, dict) else {}
                points: list[dict[str, object]] = []
                for obs in facet.get("observations", []) or []:
                    when, value = obs.get("date"), obs.get("value")
                    if when is None or value is None:
                        continue
                    if start_date and str(when) < str(start_date):
                        continue
                    if end_date and str(when) > str(end_date):
                        continue
                    points.append({"date": when, "value": value})
                if not points:
                    continue
                points.sort(key=_obs_date)
                if remaining <= 0:
                    continuation = True
                    continue
                series.append({
                    "geo": entity, "variable": variable,
                    "facet": facet_id,
                    "unit": facet.get("unit", meta.get("unit")),
                    "provider": (facet.get("provenanceName") or meta.get("importName")
                                 or facet.get("seriesSource") or meta.get("provenanceUrl")
                                 or facet.get("provenanceUrl")),
                    "provenanceUrl": meta.get("provenanceUrl") or facet.get("provenanceUrl"),
                    "importName": meta.get("importName"),
                    "measurementMethod": meta.get("measurementMethod"),
                    "observationPeriod": meta.get("observationPeriod"),
                    "scalingFactor": meta.get("scalingFactor"),
                    "isDcAggregate": meta.get("isDcAggregate"),
                    "observations": points[:remaining],
                    "retrieved_at": retrieved_at, "known_at": retrieved_at,
                })
                remaining -= len(points[:remaining])
                if remaining <= 0:
                    continuation = True
    out: dict[str, object] = {"status": "ok", "source": SOURCE, "series": series, "count": len(series),
                 "coverage": {"geos": geos, "variables": variables},
                 "continuation": continuation or bool(body.get("nextToken")),
                 "retrieved_at": retrieved_at, "known_at": retrieved_at}
    if body.get("nextToken"):
        out["nextToken"] = body["nextToken"]
    return out


def resolve_entities(nodes: list[str] | str | None, *, resolver: str | None = None,
                     property: str | None = None) -> dict[str, object]:
    """Resolve up to 50 nodes to DCIDs via the V2 resolve API (key header)."""
    nodes = [nodes] if isinstance(nodes, str) else list(nodes or [])
    if not nodes:
        return {"status": "error", "source": SOURCE,
                "error": "at least one node is required", "error_type": "invalid_params"}
    if len(nodes) > _MAX_NODES:
        return {"status": "error", "source": SOURCE,
                "error": f"{len(nodes)} nodes exceeds bound {_MAX_NODES}",
                "error_type": "invalid_params"}
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled", "error": "DATACOMMONS_DISABLED"}
    headers = _key_headers()
    if headers is None:
        return {"status": "unavailable", "source": SOURCE,
                "error": "DATACOMMONS_AUTH_REQUIRED", "error_type": "auth_required"}
    payload: dict[str, object] = {"nodes": nodes}
    if resolver:
        payload["resolver"] = resolver
    if property:
        payload["property"] = property
    try:
        response = requests.post(_RESOLVE_URL, headers=headers, json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return _map_http_error(exc)
    mapped = _map_status(response)
    if mapped is not None:
        return mapped
    try:
        body = response.json()
    except ValueError:
        return {"status": "error", "source": SOURCE,
                "error": "data commons returned non-JSON", "error_type": "malformed_response"}
    return {"status": "ok", "source": SOURCE, "entities": body.get("entities", body),
            "retrieved_at": datetime.now(timezone.utc).isoformat()}


def get_place_hierarchy(dcid: str) -> dict[str, object]:
    """Containing states for one place DCID via documented containedInPlace lookup."""
    if not dcid or not str(dcid).strip():
        return {"status": "error", "source": SOURCE,
                "error": "dcid is required", "error_type": "invalid_params"}
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled", "error": "DATACOMMONS_DISABLED"}
    headers = _key_headers()
    if headers is None:
        return {"status": "unavailable", "source": SOURCE,
                "error": "DATACOMMONS_AUTH_REQUIRED", "error_type": "auth_required"}
    payload = {"nodes": [dcid], "property": "<-containedInPlace+{typeOf:State}"}
    try:
        response = requests.post(_NODE_URL, headers=headers, json=payload, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        return _map_http_error(exc)
    mapped = _map_status(response)
    if mapped is not None:
        return mapped
    try:
        body = response.json()
    except ValueError:
        return {"status": "error", "source": SOURCE,
                "error": "data commons returned non-JSON", "error_type": "malformed_response"}
    return {"status": "ok", "source": SOURCE, "dcid": dcid, "hierarchy": body.get("data", body),
            "retrieved_at": datetime.now(timezone.utc).isoformat()}
