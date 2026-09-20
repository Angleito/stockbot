"""Needle arguments worker: grounded arguments for the exact JEV-selected tool.

JEV owns ALL selection; this worker never selects, chains, or judges
sufficiency. It shells out to needle-harness/lib/needle/server.py over JSONL
({id,action,tool,...} <-> {id,tool,arguments,error}) and returns
{"tool", "arguments"}. Missing server/weights raise; never stub.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

__all__ = ["generate_arguments", "validate_needle_tool"]

# Mirror of TOOL_TIMEOUT_MS in needle-harness/lib/needle/client.ts.
_TIMEOUT_S = 120.0


def validate_needle_tool(jev_tool: str, needle_tool: Any) -> str:
    """Exact-match gate: Needle must emit the JEV-selected tool (None rejects)."""
    if not isinstance(jev_tool, str) or not jev_tool:
        raise ValueError("jev_tool must be a nonempty tool name")
    if needle_tool != jev_tool:
        raise ValueError(f"needle tool mismatch: jev selected {jev_tool!r}, needle emitted {needle_tool!r}")
    return needle_tool  # type: ignore[return-value]


def _server() -> Path:
    return Path(__file__).resolve().parent.parent / "needle-harness" / "lib" / "needle" / "server.py"


def _python() -> str:
    # Mirror of VENV_PYTHON in needle-harness/lib/needle/client.ts.
    venv = Path(os.path.expanduser("~/.cache/needle-harness/.needle/bin/python"))
    if venv.exists():
        return str(venv)
    return sys.executable


def _ensure_weights() -> None:
    if os.environ.get("NEEDLE_WEIGHTS"):
        return
    blob = Path(__file__).resolve().parent.parent / "needle3.cact"
    if not blob.exists():
        raise RuntimeError(f"needle weights missing: {blob} (gap: set NEEDLE_WEIGHTS or provide needle3.cact)")


# ponytail: per-call spawn reloads weights each attempt; persist the child if tool attempts get slow.
def generate_arguments(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Fill arguments for one JEV-selected tool. Accepts kwargs or one request dict."""
    if args:
        if len(args) != 1 or not isinstance(args[0], dict) or kwargs:
            raise TypeError("generate_arguments: pass kwargs(tool, schema, ...) or a single request dict")
        req = args[0]
    else:
        req = kwargs
    tool, schema, objective, node, context = (req.get(k) for k in ("tool", "schema", "objective", "node", "context"))
    if not isinstance(tool, str) or not tool:
        raise ValueError("generate_arguments: tool must be a nonempty tool name")
    server = _server()
    if not server.exists():
        raise RuntimeError(f"needle arguments.generate unavailable (gap: server missing: {server})")
    _ensure_weights()
    rid = f"needle:{uuid.uuid4().hex[:12]}"
    payload = {
        "id": rid,
        "action": "arguments.generate",
        "tool": tool,
        "schema": schema,
        "objective": objective,
        "node": node,
        "context": context,
    }
    try:
        proc = subprocess.run(
            [_python(), str(server)],
            input=json.dumps(payload) + "\n",
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            env={**os.environ, "NEEDLE_TELEMETRY": "0", "DO_NOT_TRACK": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"needle arguments.generate timed out after {_TIMEOUT_S:g}s for {tool!r}") from exc
    except OSError as exc:
        raise RuntimeError(f"needle arguments.generate spawn failed for {tool!r}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"needle arguments.generate failed for {tool!r}: server exited {proc.returncode}; "
            f"stderr tail: {(proc.stderr or '')[-2000:] or '(empty)'}"
        )
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"needle arguments.generate failed for {tool!r}: empty server response")
    try:
        resp = json.loads(raw.splitlines()[-1])
    except ValueError as exc:
        raise RuntimeError(f"needle arguments.generate failed for {tool!r}: malformed server response") from exc
    if not isinstance(resp, dict) or resp.get("id") != rid:
        raise RuntimeError(f"needle arguments.generate failed for {tool!r}: malformed server response")
    if resp.get("error"):
        raise RuntimeError(f"needle arguments.generate failed for {tool!r}: {resp['error']}")
    arguments = resp.get("arguments", {})
    validate_needle_tool(tool, resp.get("tool"))
    if not isinstance(arguments, dict):
        raise TypeError(f"needle arguments for {tool!r} must be a mapping, got {type(arguments).__name__}")
    return {"tool": tool, "arguments": arguments}
