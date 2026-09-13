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
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.policy import Capability, RequestContext  # noqa: E402
from app.tools import (  # noqa: E402
    TOOLS,
    TOOL_CAPABILITIES,
    _FINRA_HANDLERS,
    _MODEL_HANDLERS,
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
