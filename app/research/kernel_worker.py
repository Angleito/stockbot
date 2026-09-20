"""Kernel worker: stdin-JSONL bridge from the TS route to the Python scheduler.

Request:  {"id", "op": "run", "prompt", "asOf"?, "deadlineMs"?}
Response: {"id", "evidence": [{id, content}], "needleDecisions": [...],
           "toolCalls": [...], "failures": {}, "escalations": n,
           "escalated": bool, "error"?, "terminal"?}
Never raises out of the worker: failures report as terminal provider_error.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _terminal(rid: str, category: str, message: str) -> dict[str, Any]:
    return {
        "id": rid,
        "evidence": [],
        "needleDecisions": [],
        "toolCalls": [],
        "failures": {category: 1},
        "escalations": 1,
        "escalated": True,
        "error": message[:500],
        "terminal": {"category": category, "message": message[:500]},
    }


def _evidence_text(rec: Any) -> str:
    if isinstance(rec, dict):
        for key in ("content", "text", "fact", "passage"):
            val = rec.get(key)
            if isinstance(val, str) and val.strip():
                return val[:2000]
        return json.dumps(rec, default=str)[:2000]
    text = getattr(rec, "content", None)
    return str(text)[:2000] if isinstance(text, str) else ""


def _evidence_id(rec: Any, fallback: str) -> str:
    if isinstance(rec, dict):
        eid = rec.get("evidence_id")
        if isinstance(eid, str) and eid:
            return eid
    eid = getattr(rec, "evidence_id", None)
    return str(eid) if isinstance(eid, str) and eid else fallback


def _run(req: dict[str, Any]) -> dict[str, Any]:
    from app.research import scheduler, service
    from app.research.repository import ResearchRepository

    rid = req.get("id") if isinstance(req.get("id"), str) else "?"
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _terminal(rid, "invalid_params", "prompt required")
    try:
        sid = service.create_research(prompt.strip(), prompt.strip(), as_of=req.get("asOf"))
        node = service.create_node(sid, prompt.strip(), "Route question.")
    except Exception as exc:
        return _terminal(rid, "provider_error", f"session setup failed: {exc}")
    try:
        result = asyncio.run(scheduler.run_node(node, session_id=sid))
    except Exception as exc:
        return _terminal(rid, "provider_error", f"kernel run failed: {exc}")
    attempts = result.get("attempts") if isinstance(result, dict) else None
    attempts = list(attempts) if isinstance(attempts, list) else []
    needle_decisions: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    failures: dict[str, int] = {}
    for step, attempt in enumerate(attempts):
        if not isinstance(attempt, dict):
            continue
        tool = attempt.get("tool")
        error = attempt.get("error")
        tool_name = str(tool) if isinstance(tool, str) and tool else "unknown"
        ok = not error
        needle_decisions.append({"step": step, "tool": tool_name, "arguments": {}, "confidence": None, "reasoning": ""})
        tool_calls.append({"tool": tool_name, "ok": ok})
        if not ok:
            failures["tool_error"] = failures.get("tool_error", 0) + 1
    try:
        records = ResearchRepository().list_evidence(sid)
    except Exception:
        records = []
    evidence = [
        {"id": _evidence_id(rec, f"ev-{i}"), "content": _evidence_text(rec)}
        for i, rec in enumerate(records)
        if _evidence_text(rec)
    ]
    status = result.get("status") if isinstance(result, dict) else None
    if status == "failed":
        err = result.get("error") if isinstance(result, dict) else None
        return _terminal(rid, "provider_error", f"kernel failed: {err}")
    if status not in ("resolved", "blocked"):
        return _terminal(rid, "provider_error", f"kernel status {status!r}")
    out: dict[str, Any] = {
        "id": rid,
        "evidence": evidence,
        "needleDecisions": needle_decisions,
        "toolCalls": tool_calls,
        "failures": failures,
        "escalations": 0,
        "escalated": status != "resolved",
    }
    if status != "resolved":
        out["terminal"] = {"category": "escalation", "message": f"node {status}"}
    return out


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps(_terminal("?", "invalid_params", "invalid JSON")) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(req, dict) or req.get("op") != "run":
            rid = req.get("id") if isinstance(req, dict) and isinstance(req.get("id"), str) else "?"
            sys.stdout.write(json.dumps(_terminal(rid, "invalid_params", "op must be 'run'")) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = _run(req)
        except Exception as exc:  # never raise out of the worker
            rid = req.get("id") if isinstance(req.get("id"), str) else "?"
            resp = _terminal(rid, "provider_error", f"worker failed: {exc}")
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
