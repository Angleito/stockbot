import json
import os
import sys
from datetime import datetime, timezone

os.environ["NEEDLE_TELEMETRY"] = "0"
os.environ["DO_NOT_TRACK"] = "1"

import needle  # noqa: E402


def load_catalog():
    env = os.environ.get("NEEDLE_CATALOG")
    if env:
        path = env
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.normpath(os.path.join(here, "..", "..", ".needle-catalog.json"))
    try:
        with open(path) as f:
            tools = json.load(f)
    except Exception as e:
        sys.stderr.write(f"needle server: cannot load tool catalog at {path}: {e}\n")
        sys.exit(1)
    if not isinstance(tools, list) or not tools:
        sys.stderr.write(f"needle server: tool catalog at {path} is empty or invalid\n")
        sys.exit(1)
    return tools


TOOLS = load_catalog()


def resolve_weights():
    env = os.environ.get("NEEDLE_WEIGHTS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    root_blob = os.path.normpath(os.path.join(here, "..", "..", "..", "needle3.cact"))
    if os.path.exists(root_blob):
        return root_blob
    return None


now = datetime.now(timezone.utc).strftime("%a %Y-%m-%d")
weights = resolve_weights()
kwargs = {
    "tools": TOOLS,
    # Binding args-only rule lives per-request in _arguments_prompt (kernel path); shared system stays legacy until loop.ts cutover removes start/step.
    "system": (
        f"date: {now} UTC; locale: en-US; "
        "Route retrieval only: call a tool only with entities/terms from the request or prior results. "
        "SEC questions: prefer find_sec_entities then search_sec_filings then get_sec_document chains. "
        "Return no call when evidence suffices."
    ),
    "buffer_size": 65536,
}
if weights is not None:
    kwargs["weights"] = weights
agent = needle.Needle(**kwargs)


def _decision(r):
    calls = r.get("function_calls") or []
    if r.get("type") == "call" and calls:
        tool = calls[0]["name"]
        args = calls[0].get("arguments") or {}
    else:
        tool = None
        args = {}
    return {
        "tool": tool,
        "arguments": args,
        "confidence": r.get("confidence"),
        "reasoning": r.get("reasoning") or "",
    }


def validate_needle_tool(jev_tool, needle_tool):
    """Runtime gate: Needle output must invoke the exact JEV-selected tool.

    JEV owns tool selection; Needle never selects, chains, or judges
    sufficiency. A mismatch (including None/escalation) raises — the caller
    treats it as retryable and returns to JEV with the full registry again.
    Never silently substitute.
    """
    if not isinstance(jev_tool, str) or not jev_tool:
        raise ValueError("jev_tool must be a nonempty tool name")
    if needle_tool != jev_tool:
        raise ValueError(f"needle tool mismatch: jev selected {jev_tool!r}, needle emitted {needle_tool!r}")
    return needle_tool


def _arguments_prompt(tool, schema, objective, node, context):
    """Single-shot constrained prompt: exactly one tool, grounded args only."""
    return json.dumps(
        {
            "instruction": (
                f"You must call exactly the tool {tool!r} once with valid grounded arguments, "
                "or return an error (must-call-or-error). Never call another tool, never chain "
                "tools, never judge sufficiency or completion. On insufficient grounding "
                "raise/return an error, never stay silent."
            ),
            "tool": tool,
            "schema": schema,
            "objective": objective,
            "node": node,
            "context": context,
        }
    )


def handle(line):
    try:
        req = json.loads(line)
        rid = req["id"]
        action = req.get("action", "")
        if not isinstance(rid, str):
            raise ValueError("bad types")
    except Exception:
        return {"id": "?", "error": "bad_request"}
    try:
        # Legacy non-kernel path: kernel path uses arguments.generate only; JEV owns transitions (start/step stay until loop.ts cutover).
        if action == "start":
            prompt = req["prompt"]
            if not isinstance(prompt, str):
                raise ValueError("bad prompt")
            agent.reset()
            r = agent.complete(prompt, max_new_tokens=256)
        elif action == "step":
            r = agent.complete(json.dumps(req.get("result")), max_new_tokens=256)
        elif action == "arguments.generate":
            # Narrow execution worker: exactly one JEV-selected tool. No
            # registry inspection, no tool choice, no chaining, no
            # sufficiency judgment. start/step (old Needle-owned loop) intact.
            tool = req.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValueError("bad tool")
            agent.reset()
            r = agent.complete(
                _arguments_prompt(tool, req.get("schema"), req.get("objective"), req.get("node"), req.get("context")),
                max_new_tokens=256,
            )
            validate_needle_tool(tool, _decision(r)["tool"])
        else:
            return {"id": rid, "error": "bad_action"}
        out = {"id": rid}
        out.update(_decision(r))
        return out
    except Exception as e:
        return {"id": rid, "error": str(e)}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        sys.stdout.write(json.dumps(handle(line)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
