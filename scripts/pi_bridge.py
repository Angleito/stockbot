"""Pi bridge: long-lived JSONL stdio between the Pi extension and Stockbot.

Protocol (one JSON object per line on stdin, one per line on stdout).
Every request carries a client-generated correlation ``id``; every response
echoes it. Responses may arrive out of request order; clients correlate by
``id``, never by arrival order. There is no ID-less path: requests without a
string ``id`` get ``{"error": "missing_arg"}``.

  {"op": "tool_call", "id": str, "run_id": str, "tool_call_id": str,
   "name": str, "arguments": dict, "bridge_queue_ms": float}
    -> {"id": str, "result": {...}}
  {"op": "abort_run", "id": str, "run_id": str, "error_type": str, "error_message": str}
    -> {"id": str, "ok": true}
  {"op": "pi_event", "id": str, "run_id": str, "event": str, ...} -> {"id": str, "ok": true}
  (recorded into the existing runs DB via RunRecorder; unknown events or a
  disabled recorder are ignored without breaking research)

``id`` is protocol correlation only. ``tool_call_id`` is Pi's tool-call ID
used for run tracing (``{run_id}:tc:{tool_call_id}``). ``bridge_queue_ms``
is the client-side semaphore wait, recorded as telemetry, never shown to
the model.

Errors never raise: malformed lines -> {"error": "bad_request"}, unknown
ops/missing keys -> {"error": "unknown_op"|"missing_arg"}, and any unhandled
per-request exception -> {"error": "bridge_failed"}. The process never exits
on a single request. ``tool_call`` work runs on a 4-worker pool so
independent calls overlap; ``describe``/``doctor``/lifecycle stay on the
main thread. ``agent_end`` uses a bounded drain for its run's submitted
calls before completing/closing the recorder; EOF uses a bounded drain
of all submitted work and exits unsuccessfully if workers remain.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pi_gateway import PiSessionContext, execute_pi_tool
from app.policy import Capability
from app.prompts import PI_RESEARCH_PROMPT, PROMPT_VERSION
from app.runtime import EventType
from app.storage.runs import RunRecorder, finalize_failed_run, reset_current_recorder, set_current_recorder
from app.tools import TOOL_REGISTRY_VERSION, tools_for_capabilities

logger = logging.getLogger(__name__)

_sessions: dict[str, PiSessionContext] = {}
_recorders: dict[str, RunRecorder] = {}
_inflight: dict[str, set[concurrent.futures.Future]] = {}

_state_lock = threading.Lock()
_stdout_lock = threading.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
TOOL_DRAIN_TIMEOUT_SECONDS = 5.0


def _write(response: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def _track(run_id: str, fut: concurrent.futures.Future) -> None:
    with _state_lock:
        _inflight.setdefault(run_id, set()).add(fut)
    fut.add_done_callback(lambda f, rid=run_id: _untrack(rid, f))


def _untrack(run_id: str, fut: concurrent.futures.Future) -> None:
    with _state_lock:
        pending = _inflight.get(run_id)
        if pending is None:
            return
        pending.discard(fut)
        if not pending:
            _inflight.pop(run_id, None)


def _run_futures(run_id: str) -> list:
    with _state_lock:
        return list(_inflight.get(run_id, ()))


def _all_futures() -> list:
    with _state_lock:
        return [fut for futs in _inflight.values() for fut in list(futs)]


def _drain_futures(futures: list) -> int:
    if not futures:
        return 0
    _, not_done = concurrent.futures.wait(futures, timeout=TOOL_DRAIN_TIMEOUT_SECONDS)
    for fut in not_done:
        fut.cancel()
    return len(not_done)


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


def _validate_tool_call(request: dict) -> dict | None:
    """Return an error response (without id) or None when submittable."""
    name = request.get("name")
    if not isinstance(name, str) or not name:
        return {"error": "missing_arg"}
    if not isinstance(request.get("arguments", {}), dict):
        return {"error": "missing_arg"}
    run_id = request.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "missing_arg"}
    with _state_lock:
        known = run_id in _sessions
    if not known:
        return {"error": "unknown_run"}
    return None


def _run_tool_call(request: dict) -> None:
    """Executor worker: run one tool call, then write its correlated response."""
    protocol_id = request.get("id")
    name = request.get("name")
    arguments = request.get("arguments", {})
    run_id = request.get("run_id")
    tool_call_id = request.get("tool_call_id")
    try:
        queue_ms = float(request.get("bridge_queue_ms") or 0.0)
    except (TypeError, ValueError):
        queue_ms = 0.0
    try:
        with _state_lock:
            session = _sessions.get(run_id)
            recorder = _recorders.get(run_id)
        if session is None:
            _write({"id": protocol_id, "error": "unknown_run"})
            return
        token = set_current_recorder(recorder) if recorder is not None else None
        try:
            result = execute_pi_tool(
                name,
                arguments,
                session,
                tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
                protocol_id=protocol_id if isinstance(protocol_id, str) else None,
                bridge_queue_ms=queue_ms,
            )
        finally:
            if token is not None:
                reset_current_recorder(token)
        _write({"id": protocol_id, "result": result})
    except Exception:  # per-request failure never breaks the loop
        logger.exception("tool_call failed")
        _write({"id": protocol_id, "error": "bridge_failed"})


def _recorder_for(run_id: str, question: str = "") -> RunRecorder | None:
    with _state_lock:
        recorder = _recorders.get(run_id)
    if recorder is None:
        try:
            recorder = RunRecorder(
                run_id=run_id, request_id=run_id, question=question, as_of=None,
                model="pi", provider="pi", model_parameters={},
                agent_version="pi", prompt_version=PROMPT_VERSION,
                tool_registry_version=TOOL_REGISTRY_VERSION, git_sha="",
            )
            recorder.__enter__()
        except Exception as exc:
            logger.warning("pi_event: recorder unavailable (%s); dropping events", exc)
            return None
        with _state_lock:
            _recorders[run_id] = recorder
    return recorder


def _teardown_failed_run(run_id: str, *, error_type: str, error_message: str, answer: str = "") -> bool:
    """Shared fail-stop teardown: live recorder -> failed, RUN_FAILED, close/pop, durable fallback.

    Returns True when durable terminalization is confirmed (live recorder
    completed, or the orphan fallback updated/found the row).
    """
    with _state_lock:
        recorder = _recorders.get(run_id)
    live_completed = False
    try:
        if recorder is not None and recorder.enabled:
            recorder.complete(
                status="failed", answer=answer,
                error_type=error_type, error_message=error_message,
            )
            recorder.record_event(
                EventType.RUN_FAILED,
                metadata={"error_type": error_type, "error_message": error_message},
            )
            live_completed = True
    except Exception as exc:  # observability never breaks research
        logger.warning("abort_run: dropped (%s: %s)", type(exc).__name__, exc)
        live_completed = False
    finally:
        try:
            if recorder is not None:
                recorder.__exit__(None, None, None)
        except Exception as exc:  # teardown never breaks the response contract
            logger.warning("abort_run: dropped (%s: %s)", type(exc).__name__, exc)
        with _state_lock:
            _sessions.pop(run_id, None)
            _recorders.pop(run_id, None)
    if live_completed:
        return True
    return finalize_failed_run(run_id, error_type=error_type, error_message=error_message)


def _pi_event(request: dict) -> dict:
    run_id = request.get("run_id")
    event = request.get("event")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "missing_arg"}
    if not isinstance(event, str) or not event:
        return {"error": "missing_arg"}
    if event == "agent_end":
        raw_type = request.get("error_type")
        raw_msg = request.get("error_message")
        if (
            request.get("status") == "failed"
            and isinstance(raw_type, str) and raw_type
            and isinstance(raw_msg, str) and raw_msg
        ):
            # Explicitly failed request (e.g. terminated run forwarded to the
            # replacement bridge): preserve supplied fields even if drain also times out.
            _drain_futures(_run_futures(run_id))
            _teardown_failed_run(
                run_id, error_type=raw_type, error_message=raw_msg,
                answer=str(request.get("answer") or ""),
            )
            return {"ok": True}
        timed_out = _drain_futures(_run_futures(run_id)) > 0
        if timed_out:
            _teardown_failed_run(
                run_id, error_type="tool_drain_timeout",
                error_message="Tool calls did not finish before the bridge drain timeout",
                answer=str(request.get("answer") or ""),
            )
            return {"error": "tool_drain_timeout"}
        with _state_lock:
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
            with _state_lock:
                _sessions.pop(run_id, None)
                _recorders.pop(run_id, None)
        return {"ok": True}
    try:
        if event == "agent_start":
            with _state_lock:
                _sessions[run_id] = PiSessionContext(session_id=run_id)
        question = request.get("question")
        recorder = _recorder_for(run_id, question if isinstance(question, str) else "")
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

def _abort_run(request: dict) -> dict:
    run_id = request.get("run_id")
    error_type = request.get("error_type")
    error_message = request.get("error_message")
    if (
        not isinstance(run_id, str) or not run_id
        or not isinstance(error_type, str) or not error_type
        or not isinstance(error_message, str) or not error_message
    ):
        return {"error": "missing_arg"}
    _drain_futures(_run_futures(run_id))
    finalized = _teardown_failed_run(
        run_id, error_type=error_type, error_message=error_message, answer=""
    )
    return {"ok": True, "finalized": finalized}


def _handle(line: str) -> dict | None:
    """Route one input line. Returns a response dict, or None when the
    response will be written asynchronously by a worker (tool_call)."""
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return {"error": "bad_request"}
    if not isinstance(request, dict):
        return {"error": "bad_request"}
    protocol_id = request.get("id")
    if not isinstance(protocol_id, str) or not protocol_id:
        return {"error": "missing_arg"}
    op = request.get("op")
    if op == "describe":
        return {"id": protocol_id, **_describe()}
    if op == "doctor":
        return {"id": protocol_id, **_doctor()}
    if op == "tool_call":
        error = _validate_tool_call(request)
        if error is not None:
            return {"id": protocol_id, **error}
        fut = _executor.submit(_run_tool_call, dict(request))
        _track(request.get("run_id"), fut)
        return None
    if op == "abort_run":
        return {"id": protocol_id, **_abort_run(request)}
    if op == "pi_event":
        return {"id": protocol_id, **_pi_event(request)}
    return {"id": protocol_id, "error": "unknown_op"}


def main() -> bool:
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                response = _handle(line)
            except Exception:  # process never exits on a single request
                response = {"error": "bridge_failed"}
            if response is not None:
                _write(response)
    finally:
        unfinished = _drain_futures(_all_futures())
        _executor.shutdown(wait=False, cancel_futures=True)
    return unfinished == 0


if __name__ == "__main__":
    if not main():
        os._exit(1)
