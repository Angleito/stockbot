import json
import os
import sys
from datetime import datetime, timezone

os.environ["NEEDLE_TELEMETRY"] = "0"
os.environ["DO_NOT_TRACK"] = "1"

import needle  # noqa: E402


def _strip_descriptions(node):
    """Drop every `description` key from a tool-parameter schema tree, keep structure."""
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(v) for v in node]
    return node


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
    # ponytail: full describe text (~58KB catalog) exceeds the Needle init
    # budget (needle_init code -1); truncated top-level descriptions ground
    # routing (name-only misroutes, e.g. clock->insider), stripped params fit.
    slim = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        params = tool.get("parameters")
        raw_desc = tool.get("description")
        desc = raw_desc if isinstance(raw_desc, str) and raw_desc.strip() else tool["name"]
        slim.append(
            {
                "name": tool["name"],
                "description": desc[:150],
                "parameters": _strip_descriptions(params) if isinstance(params, dict) else {"type": "object"},
            }
        )
    if not slim:
        sys.stderr.write(f"needle server: tool catalog at {path} is empty or invalid\n")
        sys.exit(1)
    return slim


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


def _tool_triggers(tool):
    """Trigger gate: data tools must call; control tools must withhold when ungrounded."""
    # ponytail: universal [".+"] forces a call on any input — proven to
    # fabricate session_id from question text for research_resume
    # ({"session_id": "NVDA revenue last quarter"}). Control/session tools
    # carry caller sid via kernel_worker shortcut; Needle must withhold here.
    if tool.startswith(("research_", "thesis_")):
        return []
    return [".+"]


def _tool_entry(tool, schema):
    """Single-tool binding for one arguments.generate call: grammar admits exactly this tool."""
    params = schema if isinstance(schema, dict) else {}
    desc = next((t.get("description") for t in TOOLS if isinstance(t, dict) and t.get("name") == tool), tool)
    entry = {
        "name": tool,
        "description": desc if isinstance(desc, str) and desc.strip() else tool,
        "parameters": _strip_descriptions(params) if params else {"type": "object"},
    }
    triggers = _tool_triggers(tool)
    if triggers:
        entry["triggers"] = triggers
    return entry


def _bound_agent(tool, schema):
    """Fresh Needle bound to exactly the JEV-selected tool (docs: one tool per action)."""
    bound_kwargs = dict(kwargs)
    bound_kwargs["tools"] = [_tool_entry(tool, schema)]
    # ponytail: bound call is args-only — facts-only system. Instructions
    # ("route retrieval only", chain preferences, "no call when evidence
    # suffices") withhold the call on multi-hop objectives (docs: system =
    # facts never instructions). Shared agent keeps legacy system for start/step.
    bound_kwargs["system"] = f"date: {now} UTC; locale: en-US;"
    return needle.Needle(**bound_kwargs)


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
        # Ping-only gate: answers after imports load weights; never touches the model.
        if action == "ping":
            return {"id": rid, "ready": True}
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
            bound = _bound_agent(tool, req.get("schema"))
            try:
                r = bound.complete(
                    req.get("objective")
                    if isinstance(req.get("objective"), str) and req.get("objective").strip()
                    else json.dumps({"tool": tool, "schema": req.get("schema")}),
                    max_new_tokens=512,
                )
            finally:
                try:
                    bound.close()
                except Exception:
                    pass
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
