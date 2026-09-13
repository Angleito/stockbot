#!/usr/bin/env python3
"""Deterministic per-research-tool health gate (no LLM, no Pi subprocess).

For every registered research tool (derived from source via
scripts.verify_tool_registry.get_registry_sets, never a hand list), checks:
  schema      - function name/description present, parameters is a
                type:object dict, required keys all have properties.
  fixture     - minimal required-only args pass _validate_tool_arguments.
  dispatch    - canonical execute_tool path invokes the registered handler
                (proven by sentinel-swapping the handler entry; the live
                network is never needed for this proof).
  handler     - real handler body via canonical execute_tool with provider
                boundaries replaced by deterministic doubles (no network);
                same dict/serializable/structured-error shape as live.
  live        - execute_tool with fixture args returns a dict that
                json.dumps accepts; a structured {"error": ...} counts as
                pass (data variance is not a plumbing failure), but a raise,
                non-dict, non-serializable, or malformed error fails.
  security    - capability is RESEARCH, permitted under a RESEARCH context,
                denied ("not permitted") under an empty-capability context.
  errors      - missing required args return error_type
                "invalid_tool_arguments"; required-less tools must still
                reject non-object args via the canonical validator.

All invocation is programmatic via app.tools.execute_tool, never via Pi LLM.
Exit 0 when every tool passes, 1 with per-tool failures listed otherwise.
Stdlib + repo venv only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Protocol


class _SendChannel(Protocol):
    """Pipe end the handler child sends its result dict through."""

    def send(self, obj: object) -> None: ...
    def close(self) -> None: ...

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tools as tools_mod  # noqa: E402
from app.policy import Capability, RequestContext  # noqa: E402
from app.sec.discovery import service as sec_discovery_service  # noqa: E402
from app.tools import (  # noqa: E402
    TOOLS,
    TOOL_CAPABILITIES,
    _FINRA_HANDLERS,
    _MODEL_HANDLERS,
    _RESEARCH_HANDLERS,
    _ROBINHOOD_HANDLERS,
    _THESIS_HANDLERS,
    _canonical_tool_schema,
    _validate_tool_arguments,
    execute_tool,
    tool_is_permitted,
)
from scripts.verify_tool_registry import (  # noqa: E402
    get_registry_sets,
    tool_schema_function,
    tool_schema_name,
)

MODEL = "verify-tool-health"
# Per-tool-call cliff, mirrors scripts/verify_pi_tools.py (handlers land in
# seconds; anything beyond this is hung, not slow).
LIVE_CALL_TIMEOUT_S = 120
THESIS_ID_TOOLS = frozenset({"thesis_show", "thesis_refine", "thesis_watch", "thesis_journal"})

_STRING_DEFAULTS: dict[str, object] = {
    "ticker": "AAPL",
    "symbol": "AAPL",
    "identifier": "AAPL",
    "entity": "AAPL",
    "cik": "0000320193",
    "query": "Apple",
    "company_name": "Apple Inc.",
    "company_id": "Apple Inc.",
    "accession_no": "0000320193-25-000079",
    "current_accession": "0000320193-25-000079",
    "previous_accession": "0000320193-24-000123",
    "dataset": "otcMarket/consolidatedShortInterest",
    "dataset_id": "otcMarket/consolidatedShortInterest",
    "user_thesis": "Health-check thesis: NVDA AI demand stays strong.",
    "clarification": "AI datacenter capex keeps growing.",
    "body": "Operator note: still watching NVDA datacenter demand.",
    "statement_type": "income_statement",
    "concept": "NetIncomeLoss",
    "term": "Stanley",
}
_ARRAY_DEFAULTS: dict[str, list[str]] = {
    "fields": ["settlementDate", "currentShortPositionQuantity"],
    "geos": ["geoId/06"],
    "variables": ["Count_Person"],
    "assignees": ["Apple Inc."],
}
_DATE_DEFAULT = "2024-01-01"
_DATE_NAMES = frozenset(
    {"since", "start_date", "end_date", "as_of", "week_start", "week_end", "start_published_date", "end_published_date"}
)


def research_names() -> list[str]:
    return sorted(get_registry_sets()["research"])


def _string_for(prop: str) -> str:
    if prop == "expiration":
        return "2026-01-16"
    if prop in _DATE_NAMES or prop.endswith("Date") or prop.endswith("_date"):
        return _DATE_DEFAULT
    default = _STRING_DEFAULTS.get(prop, "health-check")
    return default if isinstance(default, str) else "health-check"


def _clamp(value: float, spec: dict[str, object]) -> float:
    minimum = spec.get("minimum")
    maximum = spec.get("maximum")
    if isinstance(minimum, (int, float)) and value < minimum:
        value = minimum
    if isinstance(maximum, (int, float)) and value > maximum:
        value = maximum
    return value


def _value_for(prop: str, spec: object) -> object:
    if not isinstance(spec, dict):
        return "health-check"
    spec_map: dict[str, object] = {str(k): v for k, v in spec.items()}
    enum = spec_map.get("enum")
    if isinstance(enum, list) and enum:
        first: object = enum[0]
        return first
    kind = spec_map.get("type")
    if kind == "string":
        return _string_for(prop)
    if kind == "integer":
        return int(_clamp(5, spec_map))
    if kind == "number":
        base = 100.0 if prop in {"strike", "target_price", "strike_min", "strike_max"} else 1.5
        return _clamp(base, spec_map)
    if kind == "boolean":
        return True
    if kind == "array":
        if prop in _ARRAY_DEFAULTS:
            return list(_ARRAY_DEFAULTS[prop])
        items = spec_map.get("items")
        return [_value_for(prop, items)] if isinstance(items, dict) else ["health-check"]
    if kind == "object":
        empty: dict[str, object] = {}
        return empty
    properties = spec_map.get("properties")
    if isinstance(properties, dict):
        required = spec_map.get("required")
        subkeys: list[str] = (
            [k for k in required if isinstance(k, str) and k in properties] if isinstance(required, list) else []
        )
        return {k: _value_for(k, properties[k]) for k in subkeys}
    return "health-check"


def fixture_for(params: dict[str, object]) -> dict[str, object]:
    properties = params.get("properties")
    props: dict[str, object] = {str(k): v for k, v in properties.items()} if isinstance(properties, dict) else {}
    required = params.get("required")
    keys: list[str] = [k for k in required if isinstance(k, str)] if isinstance(required, list) else []
    return {k: _value_for(k, props.get(k)) for k in keys}


def check_schema(name: str, function: dict[str, object]) -> str | None:
    description = function.get("description")
    if not isinstance(description, str) or not description.strip():
        return "schema missing description"
    params = function.get("parameters")
    if not isinstance(params, dict) or params.get("type") != "object":
        return "parameters must be a type:object dict"
    if not isinstance(params.get("properties"), dict):
        return "parameters.properties must be a dict"
    _, required, optional = _canonical_tool_schema(name)
    missing = [k for k in required if k not in params["properties"]]
    if missing:
        return f"required without properties: {missing}"
    if set(optional) & set(required):
        return "required/optional overlap"
    return None


def handler_owner(name: str) -> object | None:
    if name in _THESIS_HANDLERS:
        return _THESIS_HANDLERS
    if name in _MODEL_HANDLERS:
        return _MODEL_HANDLERS
    if name in _FINRA_HANDLERS:
        return _FINRA_HANDLERS
    if name in _ROBINHOOD_HANDLERS:
        return _ROBINHOOD_HANDLERS
    if name in _RESEARCH_HANDLERS:
        return _RESEARCH_HANDLERS
    return None


def check_dispatch(name: str, fixture: dict[str, object], ctx: RequestContext) -> str | None:
    owner = handler_owner(name)
    if not isinstance(owner, dict):
        return "no handler registered (registry parity broken)"
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real = owner[name]

    def sentinel(*a: object, **k: object) -> dict[str, object]:
        calls.append((a, dict(k)))
        return {"ok": True, "tool": name}

    owner[name] = sentinel
    try:
        result = execute_tool(name, dict(fixture), MODEL, context=ctx)
    finally:
        owner[name] = real
    if not calls:
        return "canonical execute_tool did not invoke handler"
    if not isinstance(result, dict) or result.get("ok") is not True:
        return f"dispatch sentinel result not returned: {str(result)[:200]}"
    return None


# Handler-stage cliff: one missed network leg must not hang the default suite.
HANDLER_CALL_TIMEOUT_S = 60

# Tools whose required-only fixture dies in arg validation before the handler
# runs (found empirically by running each fixture through execute_tool).
EXTRA_FIXTURE_OVERRIDES: dict[str, dict[str, object]] = {
    "diff_sec_filings": {"ticker": "AAPL"},
    "search_sec_filings": {"query": "Apple"},
    "get_finra_datapoints": {"ticker": "AAPL"},
}

# Pure-local tools: no provider seam to double, call the handler directly.
_LOCAL_HANDLER_TOOLS = frozenset({
    "thesis_create",
    "thesis_show",
    "thesis_refine",
    "thesis_watch",
    "thesis_journal",
    "thesis_status",
    "get_sec_search_coverage",
    "find_alternative_signals",
    "get_macro_context",
    "get_trend_evidence",
    "search_company_patents",
}) | frozenset(_RESEARCH_HANDLERS)

# Google collectors check enabled flags first (trends.collect_trends:_bq_ready,
# datacommons.get_macro_context, patents.search_company_patents:_data_enabled,
# all via app/google_data/_lazy_config.py, read per call), so forcing the flag
# off makes them return fast disabled dicts with zero HTTP calls.
_GOOGLE_DISABLED_ENV_TOOLS = frozenset({
    "get_macro_context",
    "get_trend_evidence",
    "search_company_patents",
})


def _swap(target: object, attr: str, value: object, saved: list[tuple[object, str, object]]) -> None:
    saved.append((target, attr, getattr(target, attr)))
    setattr(target, attr, value)


def _swap_env(saved: list[tuple[str, str | None]], key: str, value: str) -> None:
    saved.append((key, os.environ.get(key)))
    os.environ[key] = value


def _fake_empty_list(*args: object, **kwargs: object) -> list[object]:
    return []


def _fake_empty_dict(*args: object, **kwargs: object) -> dict[str, object]:
    return {}


def _fake_search_envelope(*args: object, **kwargs: object) -> object:
    return SimpleNamespace(to_dict=lambda: {})


def _fake_entities_empty(*args: object, **kwargs: object) -> object:
    return SimpleNamespace(entities=[])


def _fake_exa_search(query: object = "", **kwargs: object) -> dict[str, object]:
    return {"result_type": "web_search", "query": query, "evidence": []}



class _FakeDiscoveryService:
    """Mirrors tests/test_sec_tools.py::_FakeService: serves one empty result."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def search(self, _request: object) -> object:
        return SimpleNamespace(to_dict=lambda: {})


# Provider seams (what each handler calls one level down), never handler
# entries: check_dispatch already proves entry wiring via sentinel. Empty
# fakes keep the real coercion/envelope code running; a structured {"error"}
# from the handler counts as pass, same as check_live.
_SEAM_MAP: dict[str, list[tuple[object, str, object]]] = {
    "list_sec_filings": [(tools_mod.sec, "list_sec_filings", _fake_empty_list)],
    "get_sec_filing": [(tools_mod.sec, "get_sec_filing", _fake_search_envelope)],
    "list_sec_documents": [(tools_mod.sec, "list_sec_documents", _fake_empty_list)],
    "get_sec_document": [(tools_mod.sec, "get_sec_document", _fake_empty_dict)],
    "diff_sec_filings": [
        (tools_mod.sec, "list_sec_filings", _fake_empty_list),
        (tools_mod.sec, "diff_filings", _fake_empty_dict),
    ],
    "find_sec_entities": [(tools_mod.sec, "find_sec_entities", _fake_search_envelope)],
    "search_sec_filings": [(tools_mod.sec, "SECDiscoveryService", _FakeDiscoveryService)],
    "search_sec_relationships": [(tools_mod.sec, "search_sec_relationships", _fake_empty_dict)],
    "get_material_events": [(tools_mod.sec, "get_material_events", _fake_empty_list)],
    "get_beneficial_ownership": [(tools_mod.sec, "get_beneficial_ownership", _fake_empty_list)],
    "get_ownership_changes": [(tools_mod.sec, "get_ownership_changes", _fake_empty_list)],
    "get_insider_activity": [(tools_mod.sec, "get_insider_activity", _fake_empty_list)],
    "get_planned_insider_sales": [(tools_mod.sec, "get_planned_insider_sales", _fake_empty_list)],
    "get_offering_history": [(tools_mod.sec, "get_offering_history", _fake_empty_list)],
    "get_dilution_profile": [(tools_mod.sec, "get_dilution_profile", _fake_empty_dict)],
    "get_governance_events": [(tools_mod.sec, "get_governance_events", _fake_empty_list)],
    "get_transaction_status": [(tools_mod.sec, "get_transaction_status", _fake_empty_list)],
    "get_short_pressure_profile": [(tools_mod.sec, "get_short_pressure_context", _fake_empty_dict)],
    "get_recent_ownership_filings": [
        (tools_mod.edgar_client, "get_recent_ownership_filings", _fake_empty_dict)
    ],
    "diff_risk_factors": [(tools_mod.edgar_client, "diff_risk_factors", _fake_empty_dict)],
    "get_financial_statements": [(tools_mod.edgar_client, "get_financial_statements", _fake_empty_dict)],
    "get_fundamentals": [(tools_mod.sec_facts, "get_fundamentals", _fake_empty_dict)],
    "get_xbrl_facts": [(tools_mod.sec_facts, "get_xbrl_facts", _fake_empty_dict)],
    "get_analyst_estimates": [(tools_mod.analyst_client, "get_analyst_estimates", _fake_empty_dict)],
    "get_sp500_weight": [(tools_mod.analyst_client, "get_sp500_weight", _fake_empty_dict)],
    "get_obligations": [(tools_mod.obligations, "get_obligations", _fake_empty_dict)],
    "get_valuation_metrics": [(tools_mod.valuation, "get_valuation_metrics", _fake_empty_dict)],
    "search_web": [(tools_mod.exa_client, "search", _fake_exa_search)],
    "query_finra": [(tools_mod.finra_client, "query_dataset", _fake_empty_dict)],
    "list_finra_datasets": [(tools_mod.finra_client, "list_datasets", _fake_empty_dict)],
    "describe_finra_dataset": [(tools_mod.finra_client, "describe_dataset", _fake_empty_dict)],
    "get_finra_datapoints": [(tools_mod.finra_client, "get_finra_datapoints", _fake_empty_dict)],
    "get_short_interest": [(tools_mod.finra_client, "get_short_interest", _fake_empty_dict)],
    "get_reg_sho_volume": [(tools_mod.finra_client, "get_reg_sho_volume", _fake_empty_dict)],
    "get_threshold_securities": [(tools_mod.finra_client, "get_threshold_securities", _fake_empty_dict)],
    "get_short_interest_leaderboard": [
        (tools_mod.screens, "get_short_interest_leaderboard", _fake_empty_dict)
    ],
    "investigate_social_arbitrage_candidate": [
        (sec_discovery_service, "find_sec_entities", _fake_entities_empty)
    ],
}


def _handler_swaps(name: str) -> list[tuple[object, str, object]] | None:
    """Provider seams to double for `name`; [] when purely local, None when unknown."""
    if name in _LOCAL_HANDLER_TOOLS:
        return []
    return _SEAM_MAP.get(name)


def _handler_worker(
    conn: _SendChannel,
    name: str,
    args: dict[str, object],
    principal_id: str,
    capability_names: list[str],
    data_root_str: str,
) -> None:
    """Child-side handler run: install the doubles here, never in the parent."""
    try:
        try:
            ctx = RequestContext(
                principal_id,
                frozenset({Capability(c) for c in capability_names}),
                data_root=Path(data_root_str),
            )
            swaps = _handler_swaps(name)
            if swaps is None:
                try:
                    conn.send({"worker_ok": False, "reason": "no deterministic provider seam"})
                except Exception:
                    pass
                return
            saved: list[tuple[object, str, object]] = []
            for target, attr, fake in swaps:
                _swap(target, attr, fake, saved)
            saved_env: list[tuple[str, str | None]] = []
            if name in _GOOGLE_DISABLED_ENV_TOOLS:
                _swap_env(saved_env, "GOOGLE_DATA_ENABLED", "")
        except Exception as e:
            try:
                conn.send({"worker_ok": False, "reason": f"handler setup failed: {type(e).__name__}: {e}"})
            except Exception:
                pass
            return
        try:
            result = execute_tool(name, args, MODEL, context=ctx)
        except Exception as e:
            try:
                conn.send({"worker_ok": False, "reason": f"execute_tool raised {type(e).__name__}: {e}"})
            except Exception:
                pass
            return
        try:
            conn.send({"worker_ok": True, "result": result})
        except Exception as e:
            try:
                conn.send({"worker_ok": False, "reason": f"handler result not sendable: {type(e).__name__}: {e}"})
            except Exception:
                pass
    except Exception as e:
        try:
            conn.send({"worker_ok": False, "reason": f"handler child failed: {type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _evaluate_envelope(message: object) -> str | None:
    if not isinstance(message, dict):
        return f"handler worker message not a dict: {type(message).__name__}"
    if message.get("worker_ok") is not True:
        reason = message.get("reason")
        if isinstance(reason, str):
            return f"handler worker failed: {reason}"
        return "handler worker failed: unknown reason"
    result = message.get("result")
    if not isinstance(result, dict):
        return f"handler result not a dict: {type(result).__name__}"
    try:
        json.dumps(result)
    except (TypeError, ValueError) as e:
        return f"handler result not JSON-serializable: {e}"
    if "error" in result:
        if not isinstance(result["error"], str) or not result["error"].strip():
            return "handler error is not a non-empty string"
        error_type = result.get("error_type")
        if error_type is not None and (not isinstance(error_type, str) or not error_type):
            return "handler error_type is not a string"
    return None


def check_handler(name: str, fixture: dict[str, object], ctx: RequestContext) -> str | None:
    """Execute the REAL handler via canonical execute_tool with provider doubles.

    The handler runs in a spawned child that installs the doubles; the parent
    installs nothing, so a hanging handler dies with terminate() and there is
    no parent-side seam to restore on timeout.
    """
    if _handler_swaps(name) is None:
        return "no deterministic provider seam"
    args = dict(fixture)
    extra = EXTRA_FIXTURE_OVERRIDES.get(name)
    if extra:
        args.update(extra)
    mp_ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = mp_ctx.Pipe(duplex=False)
    proc = mp_ctx.Process(
        target=_handler_worker,
        args=(child_conn, name, args, ctx.principal_id, [c.value for c in ctx.capabilities], str(ctx.data_root)),
    )
    proc.start()
    child_conn.close()
    try:
        deadline = time.monotonic() + HANDLER_CALL_TIMEOUT_S
        envelope: object = None
        got_envelope = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if parent_conn.poll(min(1.0, remaining)):
                envelope = parent_conn.recv()
                got_envelope = True
                break
            if not proc.is_alive():
                break
        if not got_envelope and parent_conn.poll():
            envelope = parent_conn.recv()
            got_envelope = True
        if not got_envelope:
            if proc.is_alive():
                proc.terminate()
                proc.join()
                return f"timed out after {HANDLER_CALL_TIMEOUT_S}s"
            return "handler child produced no result"
        proc.join(10)
        if proc.is_alive():
            proc.terminate()
            proc.join()
        return _evaluate_envelope(envelope)
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
            proc.join()
        proc.close()



def check_live(
    name: str,
    fixture: dict[str, object],
    ctx: RequestContext,
    pool: concurrent.futures.ThreadPoolExecutor,
) -> str | None:
    try:
        future = pool.submit(execute_tool, name, dict(fixture), MODEL, context=ctx)
        result = future.result(timeout=LIVE_CALL_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        return f"live call exceeded {LIVE_CALL_TIMEOUT_S}s"
    except Exception as e:  # execute_tool contract: never raises
        return f"live call raised {type(e).__name__}: {e}"
    if not isinstance(result, dict):
        return f"live result not a dict: {type(result).__name__}"
    try:
        json.dumps(result)
    except (TypeError, ValueError) as e:
        return f"live result not JSON-serializable: {e}"
    if "error" in result:
        if not isinstance(result["error"], str) or not result["error"].strip():
            return "live error is not a non-empty string"
        error_type = result.get("error_type")
        if error_type is not None and (not isinstance(error_type, str) or not error_type):
            return "live error_type is not a string"
    return None


def check_security(name: str, fixture: dict[str, object], ctx: RequestContext, deny: RequestContext) -> str | None:
    if TOOL_CAPABILITIES.get(name) is not Capability.RESEARCH:
        return f"capability is {TOOL_CAPABILITIES.get(name)!r}, want RESEARCH"
    if not tool_is_permitted(name, ctx):
        return "denied under RESEARCH context"
    denied = execute_tool(name, dict(fixture), MODEL, context=deny)
    if not isinstance(denied, dict) or "not permitted" not in str(denied.get("error", "")):
        return f"empty-capability call not denied: {str(denied)[:200]}"
    return None


def check_errors(name: str, params: dict[str, object], ctx: RequestContext) -> str | None:
    required = params.get("required")
    keys: list[str] = [k for k in required if isinstance(k, str)] if isinstance(required, list) else []
    if keys:
        bad = execute_tool(name, {}, MODEL, context=ctx)
        if not isinstance(bad, dict) or not isinstance(bad.get("error"), str):
            return "missing-args call has no structured error"
        if bad.get("error_type") != "invalid_tool_arguments":
            return f"missing-args error_type={bad.get('error_type')!r}"
        if bad.get("tool") != name:
            return "missing-args error missing tool name"
        return None
    message = _validate_tool_arguments(name, ["not-a-dict"])
    if not isinstance(message, str) or not message.strip():
        return "non-dict args not rejected by validator"
    return None

def bootstrap_thesis_id(ctx: RequestContext) -> str | None:
    thesis_text = _STRING_DEFAULTS["user_thesis"]
    try:
        result = execute_tool(
            "thesis_create",
            {"user_thesis": thesis_text if isinstance(thesis_text, str) else "health-check thesis"},
            MODEL,
            context=ctx,
        )
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    for key in ("thesis_id", "id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    nested = result.get("thesis")
    if isinstance(nested, dict):
        for key in ("thesis_id", "id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def verify_one(
    name: str,
    function: dict[str, object],
    ctx: RequestContext,
    deny: RequestContext,
    pool: concurrent.futures.ThreadPoolExecutor,
    thesis_id: str | None,
    live: bool = False,
) -> list[str]:
    failures: list[str] = []
    params = function.get("parameters")
    params_dict = params if isinstance(params, dict) else {}
    if (problem := check_schema(name, function)) is not None:
        return [f"schema: {problem}"]
    fixture = fixture_for(params_dict)
    if name in THESIS_ID_TOOLS and thesis_id:
        fixture["id"] = thesis_id
    if (invalid := _validate_tool_arguments(name, fixture)) is not None:
        failures.append(f"fixture: {invalid}")
    if (problem := check_dispatch(name, fixture, ctx)) is not None:
        failures.append(f"dispatch: {problem}")
    if (problem := check_handler(name, fixture, ctx)) is not None:
        failures.append(f"handler: {problem}")
    if live and (problem := check_live(name, fixture, ctx, pool)) is not None:
        failures.append(f"live: {problem}")
    if (problem := check_security(name, fixture, ctx, deny)) is not None:
        failures.append(f"security: {problem}")
    if (problem := check_errors(name, params_dict, ctx)) is not None:
        failures.append(f"errors: {problem}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic per-research-tool health gate (fast default; --live runs real handlers).")
    parser.add_argument("--tool", action="append", default=None, help="check one tool (repeatable)")
    parser.add_argument("--list", action="store_true", help="list research tools and exit 0")
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    parser.add_argument("--live", action="store_true", help="also execute real handlers (integration, may be slow/flaky)")
    args = parser.parse_args(argv)
    names = research_names()
    if args.list:
        for name in names:
            print(name)
        return 0
    selected = sorted(set(args.tool)) if args.tool else names
    unknown = [n for n in selected if n not in set(names)]
    if unknown:
        print(f"unknown tool(s): {unknown}", file=sys.stderr)
        return 2
    by_name: dict[str, dict[str, object]] = {}
    for raw in TOOLS:
        try:
            by_name[tool_schema_name(raw)] = dict(tool_schema_function(raw))
        except RuntimeError as e:
            print(f"FAIL schema for {raw!r}: {e}")
            return 1
    results: dict[str, list[str]] = {}
    with TemporaryDirectory(prefix="tool-health-") as tmp:
        ctx = RequestContext("verify-tool-health", frozenset({Capability.RESEARCH}), data_root=Path(tmp))
        deny = RequestContext("verify-tool-health-deny", frozenset(), data_root=Path(tmp))
        thesis_id = bootstrap_thesis_id(ctx) if any(n in THESIS_ID_TOOLS for n in selected) else None
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            for name in selected:
                results[name] = verify_one(name, by_name[name], ctx, deny, pool, thesis_id, args.live)
    confused = execute_tool("verify-health-no-such-tool", {}, MODEL, context=ctx)
    # Unknown names are denied before dispatch, so the contract (mirroring
    # tests/test_tool_routing.py::test_unknown_tool_executes_nothing) is a
    # structured error naming the tool — never a raise, never a dispatch.
    if not isinstance(confused, dict) or "verify-health-no-such-tool" not in str(confused.get("error", "")):
        print(f"FAIL unknown-tool error surface: {str(confused)[:200]}")
        return 1
    failed = {name: problems for name, problems in results.items() if problems}
    if args.json:
        print(json.dumps({"tools": results, "passed": len(results) - len(failed), "total": len(results)}, indent=2))
    else:
        for name in selected:
            if name in failed:
                for problem in failed[name]:
                    print(f"FAIL {name} [{problem.split(':')[0]}] {problem}")
            else:
                print(f"PASS {name}")
        print(f"tool health: {len(results) - len(failed)}/{len(results)} pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
