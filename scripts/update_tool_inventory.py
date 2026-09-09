#!/usr/bin/env python3
"""Write sorted schema-name list to tests/contracts/tool_inventory.json. Only writer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import TOOLS  # noqa: E402
from scripts.verify_tool_registry import INVENTORY_PATH, tool_schema_name  # noqa: E402


def main() -> int:
    names = sorted(tool_schema_name(t) for t in TOOLS)
    INVENTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_PATH.write_text(json.dumps(names, indent=2) + "\n")
    print(f"wrote {len(names)} tools to {INVENTORY_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
