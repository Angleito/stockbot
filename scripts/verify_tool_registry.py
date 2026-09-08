#!/usr/bin/env python3
"""Static tool registry gate: schemas == handlers == capabilities == domains == envelopes.

Derives all five sets from source (no hand-maintained list). Exit 0 pass, 1 fail.
Also diffs schemas against tests/contracts/tool_inventory.json.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.action_policy import TOOL_DOMAINS  # noqa: E402
from app.security.context_gateway import TOOL_ENVELOPES  # noqa: E402
from app.tools import (  # noqa: E402
    TOOLS,
    TOOL_CAPABILITIES,
    _DIRECT_HANDLERS,
    _FINRA_HANDLERS,
    _ROBINHOOD_HANDLERS,
)

INVENTORY_PATH = Path(__file__).resolve().parent.parent / "tests" / "contracts" / "tool_inventory.json"


def tool_schema_function(tool: Mapping[str, object]) -> Mapping[str, object]:
    """Function object of an OpenAI-format schema. TOOLS entries are untyped app-side dicts, so validate at the boundary."""
    function = tool.get("function")
    if not isinstance(function, Mapping):
        raise RuntimeError("tool schema missing function object")
    return function


def tool_schema_name(tool: Mapping[str, object]) -> str:
    """Schema name, validated at the boundary (never defaulted)."""
    name = tool_schema_function(tool).get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError("tool schema missing function name")
    return name


def get_registry_sets() -> dict[str, set[str]]:
    schemas = {tool_schema_name(t) for t in TOOLS}
    handlers = set(_DIRECT_HANDLERS) | set(_FINRA_HANDLERS) | set(_ROBINHOOD_HANDLERS)
    return {
        "schemas": schemas,
        "handlers": handlers,
        "capabilities": set(TOOL_CAPABILITIES),
        "domains": set(TOOL_DOMAINS),
        "envelopes": set(TOOL_ENVELOPES),
    }


def registry_errors(sets: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
    s = sets or get_registry_sets()
    return {
        "Schemas without handlers:": sorted(s["schemas"] - s["handlers"]),
        "Handlers without schemas:": sorted(s["handlers"] - s["schemas"]),
        "Missing capability:": sorted(s["schemas"] - s["capabilities"]),
        "Missing security domain:": sorted(s["schemas"] - s["domains"]),
        "Missing context envelope:": sorted(s["schemas"] - s["envelopes"]),
    }


def inventory_errors(schemas: set[str]) -> tuple[list[str], list[str]]:
    try:
        committed = set(json.loads(INVENTORY_PATH.read_text()))
    except FileNotFoundError:
        return sorted(schemas), []
    return sorted(schemas - committed), sorted(committed - schemas)


def main() -> int:
    sets = get_registry_sets()
    errs = registry_errors(sets)
    failed = False
    for label, names in errs.items():
        if names:
            print(f"{label} {names}")
            failed = True
    added, removed = inventory_errors(sets["schemas"])
    if added or removed:
        print(f"TOOL INVENTORY CHANGED: added={added} removed={removed}")
        print("Run `bun run update-tool-inventory` to refresh tests/contracts/tool_inventory.json")
        failed = True
    if not failed:
        print(f"tool registry OK: {len(sets['schemas'])} tools")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
