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
        if action == "start":
            prompt = req["prompt"]
            if not isinstance(prompt, str):
                raise ValueError("bad prompt")
            agent.reset()
            r = agent.complete(prompt, max_new_tokens=256)
        elif action == "step":
            r = agent.complete(json.dumps(req.get("result")), max_new_tokens=256)
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
