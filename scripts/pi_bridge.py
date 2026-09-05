"""Pi bridge: long-lived JSONL stdio between the Pi extension and Stockbot.

Protocol (one JSON object per line on stdin, one per line on stdout):
  {"op": "describe"} -> {"system_prompt": ..., "tools": [...RESEARCH-only...]}
  {"op": "doctor"} -> {"bridge_ok": true, "prompt_chars": int,
    "tool_count": int, "tool_names": [str], "registry_version": str,
    "python": str, "cwd": str}
  {"op": "tool_call", "name": str, "arguments": dict, "session_id": str}
    -> {"result": {...}}
  {"op": "pi_event", "run_id": str, "event": str, ...} -> {"ok": true}
  (recorded into the existing runs DB via RunRecorder; unknown events or a
  disabled recorder are ignored without breaking research)

Errors never raise: malformed lines -> {"error": "bad_request"}, unknown
ops/missing keys -> {"error": "unknown_op"|"missing_arg"}, and any unhandled
per-request exception -> {"error": "bridge_failed"}. The process never exits
on a single request. Calls are serialized (no threads); parallel Pi calls
queue and resolve in order.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pi_gateway import PiSessionContext, execute_pi_tool
from app.policy import Capability
from app.prompts import PI_RESEARCH_PROMPT, PROMPT_VERSION
from app.runtime import EventType
from app.storage.runs import RunRecorder, reset_current_recorder, set_current_recorder
from app.tools import TOOL_REGISTRY_VERSION, tools_for_capabilities

logger = logging.getLogger(__name__)

_sessions: dict[str, PiSessionContext] = {}


def _describe() -> dict:
    if not PI_RESEARCH_PROMPT:
        return {"error": "prompt_missing"}
    return {
        "system_prompt": PI_RESEARCH_PROMPT,
        "tools": tools_for_capabilities(frozenset({Capability.RESEARCH})),
    }


def _doctor() -> dict:
    tools = tools_for_capabilities(frozenset({Capability.RESEARCH}))
    return {
        "bridge_ok": True,
        "prompt_chars": len(PI_RESEARCH_PROMPT),
        "tool_count": len(tools),
        "tool_names": [t["function"]["name"] for t in tools],
        "registry_version": TOOL_REGISTRY_VERSION,
        "python": sys.version,
        "cwd": str(Path.cwd()),
    }


def _tool_call(request: dict) -> dict:
    name = request.get("name")
    if not isinstance(name, str) or not name:
        return {"error": "missing_arg"}
    arguments = request.get("arguments", {})
    if not isinstance(arguments, dict):
        return {"error": "missing_arg"}
    run_id = request.get("run_id") or request.get("session_id")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "missing_arg"}
    session = _sessions.get(run_id)
    if session is None:
        return {"error": "unknown_run"}
    recorder = _recorders.get(run_id)
    if recorder is None:
        return {"result": execute_pi_tool(name, arguments, session)}
    token = set_current_recorder(recorder)
    try:
        return {"result": execute_pi_tool(name, arguments, session)}
    finally:
        reset_current_recorder(token)


_recorders: dict[str, RunRecorder] = {}


def _recorder_for(run_id: str) -> RunRecorder | None:
    recorder = _recorders.get(run_id)
    if recorder is None:
        try:
            recorder = RunRecorder(
                run_id=run_id, request_id=run_id, question="", as_of=None,
                model="pi", provider="pi", model_parameters={},
                agent_version="pi", prompt_version=PROMPT_VERSION,
                tool_registry_version=TOOL_REGISTRY_VERSION, git_sha="",
            )
            recorder.__enter__()
        except Exception as exc:
            logger.warning("pi_event: recorder unavailable (%s); dropping events", exc)
            return None
        _recorders[run_id] = recorder
    return recorder


def _pi_event(request: dict) -> dict:
    run_id = request.get("run_id")
    event = request.get("event")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "missing_arg"}
    if not isinstance(event, str) or not event:
        return {"error": "missing_arg"}
    if event == "agent_end":
        recorder = _recorders.get(run_id)
        try:
            if recorder is not None and recorder.enabled:
                status = request.get("status") or "completed"
                recorder.complete(status=str(status), answer=str(request.get("answer") or ""))
                if status == "failed":
                    meta = {k: v for k, v in request.items() if k not in ("op", "run_id", "event")}
                    recorder.record_event(EventType.RUN_FAILED, metadata=meta or None)
        except Exception as exc:  # observability never breaks research
            logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
        finally:
            try:
                if recorder is not None:
                    recorder.__exit__(None, None, None)
            except Exception as exc:  # teardown never breaks the response contract
                logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
            _sessions.pop(run_id, None)
            _recorders.pop(run_id, None)
        return {"ok": True}
    try:
        if event == "agent_start":
            _sessions[run_id] = PiSessionContext(session_id=run_id)
        recorder = _recorder_for(run_id)
        if recorder is None or not recorder.enabled:
            return {"ok": True}  # dropped, research continues
        meta = {k: v for k, v in request.items() if k not in ("op", "run_id", "event")}
        if event == "agent_start":
            recorder.record_event(EventType.RUN_STARTED, metadata=meta or None)
        elif event == "tool_execution_start":
            recorder.record_event(
                EventType.TOOL_STARTED, tool_name=str(request.get("tool") or ""),
                arguments=request.get("arguments"), metadata=meta or None,
            )
        elif event == "tool_execution_end":
            recorder.record_event(
                EventType.TOOL_FAILED if request.get("is_error") else EventType.TOOL_COMPLETED,
                tool_name=str(request.get("tool") or ""),
                success=not bool(request.get("is_error")), metadata=meta or None,
            )
        elif event == "message_end" and request.get("role") == "assistant":
            usage = request.get("usage")
            now = datetime.now(timezone.utc).isoformat()
            recorder.record_model_call(
                round=int(request.get("turn") or 0), provider="pi",
                model=str(request.get("model") or "pi"),
                started_at=str(request.get("started_at") or now),
                completed_at=str(request.get("completed_at") or now),
                usage=usage if isinstance(usage, dict) else {},
                tool_call_count=int(request.get("tool_call_count") or 0),
            )
        elif event == "security_block":
            raw = json.dumps([request.get("tool"), request.get("arguments")], sort_keys=True)
            recorder.record_security_event(
                source="pi", sha256=hashlib.sha256(raw.encode()).hexdigest(),
                score=None, verdict=None, rule_ids=None,
                decision="denied", reason=str(request.get("reason") or "pi tool_call gate"),
            )
        elif event in ("turn_start", "turn_end", "message_end"):
            recorder.record_event(event, round=request.get("turn"), metadata=meta or None)
        else:
            logger.warning("pi_event: unknown event %r ignored", event)
        return {"ok": True}
    except Exception as exc:  # observability never breaks research
        logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
        return {"ok": True}


def _handle(line: str) -> dict:
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return {"error": "bad_request"}
    if not isinstance(request, dict):
        return {"error": "bad_request"}
    op = request.get("op")
    if op == "describe":
        return _describe()
    if op == "doctor":
        return _doctor()
    if op == "tool_call":
        return _tool_call(request)
    if op == "pi_event":
        return _pi_event(request)
    return {"error": "unknown_op"}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            response = _handle(line)
        except Exception:  # process never exits on a single request
            response = {"error": "bridge_failed"}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
