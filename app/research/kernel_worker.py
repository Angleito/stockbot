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
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.research.models import JSONValue


def _terminal(rid: str, category: str, message: str) -> dict[str, JSONValue]:
    out: dict[str, JSONValue] = {
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
    return out


def _evidence_text(rec: Mapping[str, JSONValue]) -> str:
    for key in ("content", "text", "fact", "passage"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val[:2000]
    return json.dumps(rec, default=str)[:2000]


def _evidence_id(rec: Mapping[str, JSONValue], fallback: str) -> str:
    eid = rec.get("evidence_id")
    if isinstance(eid, str) and eid:
        return eid
    return fallback


def _run(req: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    from app.research import scheduler, service
    from app.research.repository import ResearchRepository

    raw_id = req.get("id")
    rid = raw_id if isinstance(raw_id, str) else "?"
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _terminal(rid, "invalid_params", "prompt required")
    raw_as_of = req.get("asOf")
    as_of = raw_as_of if isinstance(raw_as_of, str) else None
    try:
        sid = service.create_research(prompt.strip(), prompt.strip(), as_of=as_of)
        node = service.create_node(sid, prompt.strip(), "Route question.")
    except Exception as exc:
        return _terminal(rid, "provider_error", f"session setup failed: {exc}")
    try:
        result: dict[str, JSONValue] = asyncio.run(scheduler.run_node(node, session_id=sid))
    except Exception as exc:
        return _terminal(rid, "provider_error", f"kernel run failed: {exc}")
    raw_attempts = result.get("attempts")
    attempts: list[JSONValue] = list(raw_attempts) if isinstance(raw_attempts, list) else []
    try:
        records: list[dict[str, JSONValue]] = ResearchRepository().list_evidence(sid)
    except Exception:
        records = []
    evidence: list[JSONValue] = []
    # ponytail: success linkage is FIFO over unclaimed admitted ids (scheduler
    # admits sequentially in attempt order); an explicit attempt evidence_id wins.
    unclaimed: list[str] = []
    for i, rec in enumerate(records):
        text = _evidence_text(rec)
        if not text:
            continue
        row_id = _evidence_id(rec, f"ev-{i}")
        row: dict[str, JSONValue] = {"id": row_id, "content": text}
        evidence.append(row)
        unclaimed.append(row_id)
    needle_decisions: list[JSONValue] = []
    tool_calls: list[JSONValue] = []
    failures: dict[str, int] = {}
    for step, attempt in enumerate(attempts):
        if not isinstance(attempt, Mapping):
            continue
        tool = attempt.get("tool")
        error = attempt.get("error")
        tool_name = str(tool) if isinstance(tool, str) and tool else "unknown"
        ok = not error
        args = attempt.get("arguments")
        conf = attempt.get("confidence")
        reasoning = attempt.get("reasoning")
        needle_decisions.append(
            {
                "step": step,
                "tool": tool_name,
                "arguments": dict(args) if isinstance(args, dict) else {},
                "confidence": float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None,
                "reasoning": str(reasoning) if isinstance(reasoning, str) else "",
            }
        )
        call: dict[str, JSONValue] = {"tool": tool_name, "ok": ok, "step": step}
        if ok:
            claimed = attempt.get("evidence_id", attempt.get("evidenceId"))
            eid = str(claimed) if isinstance(claimed, str) and claimed else None
            if eid is not None and eid in unclaimed:
                unclaimed.remove(eid)
            else:
                eid = unclaimed.pop(0) if unclaimed else None
            if eid is not None:
                call["evidenceId"] = eid
        else:
            raw_category = attempt.get("error_type", attempt.get("category"))
            category = str(raw_category) if isinstance(raw_category, str) and raw_category else "tool_error"
            call["error"] = str(error)[:500] if isinstance(error, str) else "tool failed"
            call["category"] = category
            failures[category] = failures.get(category, 0) + 1
        tool_calls.append(call)
    status = result.get("status")
    if status == "failed":
        err = result.get("error")
        return _terminal(rid, "provider_error", f"kernel failed: {err}")
    if status not in ("resolved", "blocked"):
        return _terminal(rid, "provider_error", f"kernel status {status!r}")
    failures_json: dict[str, JSONValue] = {k: v for k, v in failures.items()}
    out: dict[str, JSONValue] = {
        "id": rid,
        "evidence": evidence,
        "needleDecisions": needle_decisions,
        "toolCalls": tool_calls,
        "failures": failures_json,
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
            parsed: object = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps(_terminal("?", "invalid_params", "invalid JSON")) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(parsed, dict) or parsed.get("op") != "run":
            rid = parsed.get("id") if isinstance(parsed, dict) and isinstance(parsed.get("id"), str) else "?"
            sys.stdout.write(json.dumps(_terminal(rid, "invalid_params", "op must be 'run'")) + "\n")
            sys.stdout.flush()
            continue
        req: Mapping[str, JSONValue] = parsed
        try:
            resp = _run(req)
        except Exception as exc:  # never raise out of the worker
            raw_rid = req.get("id")
            rid = raw_rid if isinstance(raw_rid, str) else "?"
            resp = _terminal(rid, "provider_error", f"worker failed: {exc}")
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
