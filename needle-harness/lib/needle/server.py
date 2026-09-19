import json
import os
import sys
from datetime import datetime, timezone

os.environ["NEEDLE_TELEMETRY"] = "0"
os.environ["DO_NOT_TRACK"] = "1"

import needle  # noqa: E402

TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web for current information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetch a URL and extract readable text",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "get_sec_filings",
        "description": "List recent SEC EDGAR filings for a US stock ticker",
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Stock ticker symbol, e.g. NVDA (not the company name)"},
                "forms": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "get_current_time",
        "description": "Get the current UTC date and time",
        "parameters": {"type": "object", "properties": {}},
    },
]


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
    "system": f"date: {now} UTC; locale: en-US",
    "buffer_size": 65536,
}
if weights is not None:
    kwargs["weights"] = weights
agent = needle.Needle(**kwargs)


def handle(line):
    try:
        req = json.loads(line)
        rid = req["id"]
        prompt = req["prompt"]
        context = req.get("context", "")
        if not isinstance(rid, str) or not isinstance(prompt, str):
            raise ValueError("bad types")
        if context is None:
            context = ""
        if not isinstance(context, str):
            raise ValueError("bad context")
    except Exception:
        return {"id": "?", "error": "bad_request"}
    try:
        agent.reset()
        r = agent.complete(prompt + chr(10) + context, max_new_tokens=256)
        calls = r.get("function_calls") or []
        if r.get("type") == "call" and calls:
            tool = calls[0]["name"]
            args = calls[0].get("arguments") or {}
        else:
            tool = None
            args = {}
        return {
            "id": rid,
            "tool": tool,
            "arguments": args,
            "confidence": r.get("confidence"),
            "reasoning": r.get("reasoning") or "",
        }
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
