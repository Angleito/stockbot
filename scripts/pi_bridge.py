"""Pi bridge: long-lived JSONL stdio between the Pi extension and Stockbot.

Protocol (one JSON object per line on stdin, one per line on stdout).
Every request carries a client-generated correlation ``id``; every response
echoes it. Responses may arrive out of request order; clients correlate by
``id``, never by arrival order. There is no ID-less path: requests without a
string ``id`` get ``{"error": "missing_arg"}``.

  {"op": "tool_call", "id": str, "run_id": str, "tool_call_id": str,
   "name": str, "arguments": dict, "bridge_queue_ms": float}
    -> {"id": str, "result": {...}}
  {"op": "tool.invoke", "id": str, "name": str, "arguments": dict,
   "session_id": str | null, "tool_call_id": str, "bridge_queue_ms": float}
    -> {"id": str, "result": {...}} (dumb tool passthrough, no session state)
  {"op": "research.session.create", "id": str, "question": str,
   "objective": str | null, "as_of": str | null} -> {"id": str, "result": {"session_id": str}}
  {"op": "research.job.start", "id": str, "session_id": str, "type": str,
   "source": str | null, "parent": str | null, "budget": dict | null}
    -> {"id": str, "result": {job}}
  {"op": "research.job.complete", "id": str, "job_id": str, "outcome": dict | null}
    -> {"id": str, "result": {job}}
  {"op": "research.evidence.add", "id": str, "session_id": str, "job_id": str,
   "item": dict} -> {"id": str, "result": {evidence record}}
  {"op": "research.session.inspect", "id": str, "session_id": str}
    -> {"id": str, "result": {session, jobs, pending_next_action}}
  {"op": "research.session.cancel", "id": str, "session_id": str}
    -> {"id": str, "result": {session}}

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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pi_gateway import PiSessionContext, _override_context, execute_pi_tool
from app.policy import Capability, RequestContext
from app.prompts import PI_RESEARCH_PROMPT, PROMPT_VERSION
from app.research import (
    service as _kernel,  # eventual rename target: kernel_rpc.py (name only; transport stays)
)
from app.research.models import JSONValue, default_policy
from app.research.repository import ResearchRepository
from app.runtime import EventType
from app.storage.runs import (
    RunRecorder,
    finalize_failed_run,
    reset_current_recorder,
    set_current_recorder,
)
from app.tools import (
    TOOL_REGISTRY_VERSION,
    dynamically_activatable_tool_names,
    tools_for_capabilities,
)

logger = logging.getLogger(__name__)


def _bridge_ctx(request: Mapping[str, object]) -> RequestContext:
    """Invocation context from wire data_root/as_of; untrusted shapes fall back."""
    data_root = request.get("data_root")
    as_of = request.get("as_of")
    return _override_context(
        data_root if isinstance(data_root, (str, Path)) else None,
        as_of if isinstance(as_of, str) else None,
    )


_sessions: dict[str, PiSessionContext] = {}
_recorders: dict[str, RunRecorder] = {}
_inflight: dict[str, set[concurrent.futures.Future[None]]] = {}

_state_lock = threading.Lock()
_stdout_lock = threading.Lock()
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
TOOL_DRAIN_TIMEOUT_SECONDS = 5.0


def _as_int(value: object) -> int | None:
    """Coerce a JSON number-ish telemetry field; None when absent or malformed."""
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _tool_name(tool: dict[str, object]) -> str:
    fn = tool["function"]
    assert isinstance(fn, dict)
    name = fn["name"]
    assert isinstance(name, str)
    return name


def _write(response: dict[str, object]) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def _track(run_id: str, fut: concurrent.futures.Future[None]) -> None:
    with _state_lock:
        _inflight.setdefault(run_id, set()).add(fut)
    fut.add_done_callback(lambda f, rid=run_id: _untrack(rid, f))


def _untrack(run_id: str, fut: concurrent.futures.Future[None]) -> None:
    with _state_lock:
        pending = _inflight.get(run_id)
        if pending is None:
            return
        pending.discard(fut)
        if not pending:
            _inflight.pop(run_id, None)


def _run_futures(run_id: str) -> list[concurrent.futures.Future[None]]:
    with _state_lock:
        return list(_inflight.get(run_id, ()))


def _all_futures() -> list[concurrent.futures.Future[None]]:
    with _state_lock:
        return [fut for futs in _inflight.values() for fut in list(futs)]


def _drain_futures(futures: list[concurrent.futures.Future[None]]) -> int:
    if not futures:
        return 0
    _, not_done = concurrent.futures.wait(futures, timeout=TOOL_DRAIN_TIMEOUT_SECONDS)
    for fut in not_done:
        fut.cancel()
    return len(not_done)


def _describe() -> dict[str, object]:
    if not PI_RESEARCH_PROMPT:
        return {"error": "prompt_missing"}
    return {
        "system_prompt": PI_RESEARCH_PROMPT,
        "tools": tools_for_capabilities(frozenset({Capability.RESEARCH})),
        "direct_tool_names": dynamically_activatable_tool_names(),
    }


def _doctor() -> dict[str, object]:
    tools = tools_for_capabilities(frozenset({Capability.RESEARCH}))
    return {
        "bridge_ok": True,
        "prompt_chars": len(PI_RESEARCH_PROMPT),
        "tool_count": len(tools),
        "tool_names": [_tool_name(t) for t in tools],
        "registry_version": TOOL_REGISTRY_VERSION,
        "python": sys.version,
        "cwd": str(Path.cwd()),
    }


def _validate_tool_call_shape(request: Mapping[str, object]) -> dict[str, object] | None:
    """Name/arguments/run_id shape; error response or None."""
    name = request.get("name")
    if not isinstance(name, str):
        return {"error": "missing_arg"}
    if not name:
        return {"error": "missing_arg"}
    if not isinstance(request.get("arguments", {}), dict):
        return {"error": "missing_arg"}
    run_id = request.get("run_id")
    if not isinstance(run_id, str):
        return {"error": "missing_arg"}
    if not run_id:
        return {"error": "missing_arg"}
    return None


def _validate_staged_sid(raw_sid: object) -> dict[str, object] | None:
    """Staged session id shape; error response or None."""
    if raw_sid is None:
        return None
    if not isinstance(raw_sid, str):
        return {"error": "invalid_research_context"}
    if not raw_sid:
        return {"error": "invalid_research_context"}
    return None


def _validate_staged_jid(raw_jid: object) -> dict[str, object] | None:
    """Staged job id shape; error response or None."""
    if raw_jid is None:
        return None
    if not isinstance(raw_jid, str):
        return {"error": "invalid_research_context"}
    if not raw_jid:
        return {"error": "invalid_research_context"}
    return None


def _validate_tool_call_staged(request: Mapping[str, object]) -> dict[str, object] | None:
    """Staged session/job consistency; error response or None."""
    error = _validate_staged_sid(request.get("active_research_session_id"))
    if error is not None:
        return error
    error = _validate_staged_jid(request.get("active_research_job_id"))
    if error is not None:
        return error
    if request.get("active_research_session_id") is None and request.get("active_research_job_id") is not None:
        return {"error": "invalid_research_context"}
    return None


def _validate_known_run(request: Mapping[str, object]) -> dict[str, object] | None:
    """Known-run check; error response or None."""
    with _state_lock:
        known = request.get("run_id") in _sessions
    if known:
        return None
    return {"error": "unknown_run"}


def _validate_tool_call(request: Mapping[str, object]) -> dict[str, object] | None:
    """Return an error response (without id) or None when submittable."""
    error = _validate_tool_call_shape(request)
    if error is not None:
        return error
    error = _validate_tool_call_staged(request)
    if error is not None:
        return error
    return _validate_known_run(request)


def _coerce_opt_str(value: object) -> str | None:
    """Wire string-or-absent field; non-strings become None."""
    if not isinstance(value, str):
        return None
    return value


def _coerce_data_root(value: object) -> str | None:
    """Wire data_root field; non-strings and empties become None."""
    if not isinstance(value, str):
        return None
    if not value:
        return None
    return value


def _parse_tool_call_shape(request: Mapping[str, object]) -> tuple[str, dict[str, object], str] | dict[str, object]:
    """Name/arguments/run_id shape; parsed triple or an error body (without id)."""
    name = request.get("name")
    arguments = request.get("arguments", {})
    raw_run_id = request.get("run_id")
    if not isinstance(name, str) or not name:
        return {"error": "missing_arg"}
    if not isinstance(arguments, dict):
        return {"error": "missing_arg"}
    if not isinstance(raw_run_id, str) or not raw_run_id:
        return {"error": "unknown_run"}
    return (name, arguments, raw_run_id)


def _tool_call_wire(request: Mapping[str, object]) -> tuple[str | None, str | None, str | None, float]:
    """Client telemetry + routing fields; never fails."""
    tool_call_id = _coerce_opt_str(request.get("tool_call_id"))
    data_root = _coerce_data_root(request.get("data_root"))
    raw_as_of = request.get("as_of")
    as_of = raw_as_of if isinstance(raw_as_of, str) and raw_as_of else None
    raw_queue_ms = request.get("bridge_queue_ms")
    try:
        queue_ms = float(raw_queue_ms if isinstance(raw_queue_ms, (int, float, str)) else 0.0)
    except TypeError, ValueError:
        queue_ms = 0.0
    return (tool_call_id, data_root, as_of, queue_ms)


def _lookup_tool_session(raw_run_id: str) -> tuple[PiSessionContext | None, RunRecorder | None]:
    """Session + recorder snapshot under the state lock."""
    with _state_lock:
        return (_sessions.get(raw_run_id), _recorders.get(raw_run_id))


def _stage_tool_session(session: PiSessionContext, raw_sid: object, raw_jid: object) -> tuple[str | None, str | None]:
    """Apply staged research ids; return the captured pair for this call."""
    with session._lock:
        if raw_sid is not None:
            session.active_research_session_id = raw_sid if isinstance(raw_sid, str) else None
            if raw_jid is not None:
                session.active_research_job_id = raw_jid if isinstance(raw_jid, str) else None
            else:
                session.active_research_job_id = None
        elif raw_jid is not None:
            session.active_research_job_id = raw_jid if isinstance(raw_jid, str) else None
        return (session.active_research_session_id, session.active_research_job_id)


def _invoke_tool_call(
    session: PiSessionContext,
    recorder: RunRecorder | None,
    name: str,
    arguments: dict[str, object],
    tool_call_id: str | None,
    protocol_id: object,
    queue_ms: float,
    data_root: str | None,
    as_of: str | None,
    captured_sid: str | None,
    captured_jid: str | None,
) -> object:
    """Run one tool under its recorder scope; return the gateway result."""
    token = set_current_recorder(recorder) if recorder is not None else None
    try:
        return execute_pi_tool(
            name,
            arguments,
            session,
            tool_call_id=tool_call_id,
            protocol_id=protocol_id if isinstance(protocol_id, str) else None,
            bridge_queue_ms=queue_ms,
            data_root=data_root,
            as_of=as_of,
            active_research_session_id=captured_sid,
            active_research_job_id=captured_jid,
        )
    finally:
        if token is not None:
            reset_current_recorder(token)


def _run_tool_call(request: Mapping[str, object]) -> None:
    """Executor worker: run one tool call, then write its correlated response."""
    protocol_id = request.get("id")
    parsed = _parse_tool_call_shape(request)
    if isinstance(parsed, dict):
        _write({"id": protocol_id, **parsed})
        return
    name, arguments, raw_run_id = parsed
    tool_call_id, data_root, as_of, queue_ms = _tool_call_wire(request)
    try:
        session, recorder = _lookup_tool_session(raw_run_id)
        if session is None:
            _write({"id": protocol_id, "error": "unknown_run"})
            return
        captured_sid, captured_jid = _stage_tool_session(
            session, request.get("active_research_session_id"), request.get("active_research_job_id")
        )
        result = _invoke_tool_call(
            session,
            recorder,
            name,
            arguments,
            tool_call_id,
            protocol_id,
            queue_ms,
            data_root,
            as_of,
            captured_sid,
            captured_jid,
        )
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
                run_id=run_id,
                request_id=run_id,
                question=question,
                as_of=None,
                model="pi",
                provider="pi",
                model_parameters={},
                agent_version="pi",
                prompt_version=PROMPT_VERSION,
                tool_registry_version=TOOL_REGISTRY_VERSION,
                git_sha="",
            )
            recorder.__enter__()
        except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            logger.warning("pi_event: recorder unavailable (%s); dropping events", exc)
            return None
        with _state_lock:
            _recorders[run_id] = recorder
    return recorder


def _teardown_failed_run(run_id: str, *, error_type: str, error_message: str, answer: str = "") -> bool:
    """Shared fail-stop teardown: live recorder -> failed, RUN_FAILED, close/pop, durable fallback.

    Returns True only when the idempotent fallback verifies durable
    terminalization (repaired the row or found it already terminal).
    A live recorder's non-raising complete() is never trusted on its own
    because it swallows storage errors by disabling itself.
    """
    with _state_lock:
        recorder = _recorders.get(run_id)
    try:
        if recorder is not None and recorder.enabled:
            recorder.complete(
                status="failed",
                answer=answer,
                error_type=error_type,
                error_message=error_message,
            )
            recorder.record_event(
                EventType.RUN_FAILED,
                metadata={"error_type": error_type, "error_message": error_message},
            )
    except (
        Exception  # noqa: BLE001 - intentional best-effort boundary, never aborts
    ) as exc:  # observability never breaks research
        logger.warning("abort_run: dropped (%s: %s)", type(exc).__name__, exc)
    finally:
        try:
            if recorder is not None:
                recorder.__exit__(None, None, None)
        except Exception as exc:  # teardown never breaks the response contract  # noqa: BLE001 - intentional best-effort boundary, never aborts
            logger.warning("abort_run: dropped (%s: %s)", type(exc).__name__, exc)
        with _state_lock:
            _sessions.pop(run_id, None)
            _recorders.pop(run_id, None)
    return finalize_failed_run(run_id, error_type=error_type, error_message=error_message)


def _pi_event_shape(request: Mapping[str, object]) -> tuple[str, str] | dict[str, object]:
    """run_id/event shape; parsed pair or an error body."""
    run_id = request.get("run_id")
    event = request.get("event")
    if not isinstance(run_id, str) or not run_id:
        return {"error": "missing_arg"}
    if not isinstance(event, str) or not event:
        return {"error": "missing_arg"}
    return (run_id, event)


def _is_explicit_agent_failure(request: Mapping[str, object]) -> tuple[str, str] | None:
    """Supplied error_type/message when agent_end is an explicit failure."""
    raw_type = request.get("error_type")
    raw_msg = request.get("error_message")
    if request.get("status") != "failed":
        return None
    if not isinstance(raw_type, str) or not raw_type:
        return None
    if not isinstance(raw_msg, str) or not raw_msg:
        return None
    return (raw_type, raw_msg)


def _fail_agent_end(run_id: str, error_type: str, error_message: str, answer: str) -> None:
    """Drain then fail-stop teardown for an agent_end failure."""
    _drain_futures(_run_futures(run_id))
    _teardown_failed_run(run_id, error_type=error_type, error_message=error_message, answer=answer)


def _lookup_agent_end_recorder(run_id: str) -> RunRecorder | None:
    """Recorder snapshot for agent_end completion."""
    with _state_lock:
        return _recorders.get(run_id)


def _complete_agent_end_run(recorder: RunRecorder, request: Mapping[str, object]) -> None:
    """Complete + RUN_FAILED bookkeeping for agent_end."""
    status = request.get("status") or "completed"
    recorder.complete(status=str(status), answer=str(request.get("answer") or ""))
    if status != "failed":
        return
    meta = {k: v for k, v in request.items() if k not in ("op", "run_id", "event")}
    recorder.record_event(EventType.RUN_FAILED, metadata=meta or None)


def _close_agent_end_run(run_id: str, recorder: RunRecorder | None) -> None:
    """Close + evict run state; teardown never breaks the response contract."""
    try:
        if recorder is not None:
            recorder.__exit__(None, None, None)
    except Exception as exc:  # teardown never breaks the response contract  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
    with _state_lock:
        _sessions.pop(run_id, None)
        _recorders.pop(run_id, None)


def _complete_agent_end(run_id: str, request: Mapping[str, object]) -> None:
    """Complete the run recorder for agent_end; observability never raises."""
    recorder = _lookup_agent_end_recorder(run_id)
    try:
        if recorder is not None and recorder.enabled:
            _complete_agent_end_run(recorder, request)
    except (
        Exception  # noqa: BLE001 - intentional best-effort boundary, never aborts
    ) as exc:  # observability never breaks research
        logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
    finally:
        _close_agent_end_run(run_id, recorder)


def _pi_event_agent_end(run_id: str, request: Mapping[str, object]) -> dict[str, object]:
    """agent_end: explicit-failure, drain-timeout, or normal completion."""
    failure = _is_explicit_agent_failure(request)
    if failure is not None:
        error_type, error_message = failure
        _fail_agent_end(run_id, error_type, error_message, str(request.get("answer") or ""))
        return {"ok": True}
    if _drain_futures(_run_futures(run_id)) > 0:
        _fail_agent_end(
            run_id,
            "tool_drain_timeout",
            "Tool calls did not finish before the bridge drain timeout",
            str(request.get("answer") or ""),
        )
        return {"error": "tool_drain_timeout"}
    _complete_agent_end(run_id, request)
    return {"ok": True}


def _pi_event_record_tool_start(
    recorder: RunRecorder, request: Mapping[str, object], meta: dict[str, object] | None
) -> None:
    """Tool-start observability; dropped failures stay with the caller."""
    recorder.record_event(
        EventType.TOOL_STARTED,
        tool_name=str(request.get("tool") or ""),
        arguments=request.get("arguments"),
        metadata=meta,
    )


def _pi_event_record_tool_end(
    recorder: RunRecorder, request: Mapping[str, object], meta: dict[str, object] | None
) -> None:
    """Tool-end observability; dropped failures stay with the caller."""
    recorder.record_event(
        EventType.TOOL_FAILED if request.get("is_error") else EventType.TOOL_COMPLETED,
        tool_name=str(request.get("tool") or ""),
        success=not bool(request.get("is_error")),
        metadata=meta,
    )


def _pi_event_record_model_call(recorder: RunRecorder, request: Mapping[str, object]) -> None:
    """Assistant message_end model-call observability."""
    usage = request.get("usage")
    now = datetime.now(UTC).isoformat()
    recorder.record_model_call(
        round=_as_int(request.get("turn")) or 0,
        provider="pi",
        model=str(request.get("model") or "pi"),
        started_at=str(request.get("started_at") or now),
        completed_at=str(request.get("completed_at") or now),
        usage=usage if isinstance(usage, dict) else {},
        tool_call_count=_as_int(request.get("tool_call_count")) or 0,
    )


def _pi_event_record_security(recorder: RunRecorder, request: Mapping[str, object]) -> None:
    """security_block observability; dropped failures stay with the caller."""
    raw = json.dumps([request.get("tool"), request.get("arguments")], sort_keys=True)
    recorder.record_security_event(
        source="pi",
        sha256=hashlib.sha256(raw.encode()).hexdigest(),
        score=None,
        verdict=None,
        rule_ids=None,
        decision="denied",
        reason=str(request.get("reason") or "pi tool_call gate"),
    )


def _pi_event_record_routing(recorder: RunRecorder, event: str, meta: dict[str, object]) -> None:
    """Routing observability events; dropped failures stay with the caller."""
    recorder.record_event(event, metadata=meta)


def _pi_event_record_turn(
    recorder: RunRecorder, event: str, request: Mapping[str, object], meta: dict[str, object] | None
) -> None:
    """Turn lifecycle observability; dropped failures stay with the caller."""
    recorder.record_event(event, round=_as_int(request.get("turn")), metadata=meta)


def _pi_event_record_core(
    recorder: RunRecorder, event: str, request: Mapping[str, object], meta: dict[str, object] | None
) -> bool:
    """Agent/tool/security events; True when handled."""
    if event == "agent_start":
        recorder.record_event(EventType.RUN_STARTED, metadata=meta)
        return True
    if event == "tool_execution_start":
        _pi_event_record_tool_start(recorder, request, meta)
        return True
    if event == "tool_execution_end":
        _pi_event_record_tool_end(recorder, request, meta)
        return True
    if event == "message_end" and request.get("role") == "assistant":
        _pi_event_record_model_call(recorder, request)
        return True
    if event == "security_block":
        _pi_event_record_security(recorder, request)
        return True
    return False


def _pi_event_record_aux(
    recorder: RunRecorder, event: str, request: Mapping[str, object], meta: dict[str, object] | None
) -> bool:
    """Routing/turn events; True when handled."""
    if event in (
        "routing_continuation",
        "routing_continuation_failed",
        "routing_metrics",
        "task_planned",
        "task_result",
        "subagent_started",
        "subagent_finished",
    ):
        _pi_event_record_routing(recorder, event, meta or {})
        return True
    if event in ("turn_start", "turn_end", "message_end"):
        _pi_event_record_turn(recorder, event, request, meta)
        return True
    return False


def _pi_event_record(
    recorder: RunRecorder, event: str, request: Mapping[str, object], meta: dict[str, object] | None
) -> None:
    """One non-lifecycle event; unknown names only warn."""
    if _pi_event_record_core(recorder, event, request, meta):
        return
    if _pi_event_record_aux(recorder, event, request, meta):
        return
    logger.warning("pi_event: unknown event %r ignored", event)


def _ensure_observe_session(run_id: str, event: str) -> None:
    """agent_start creates the Pi session; other events leave state alone."""
    if event != "agent_start":
        return
    with _state_lock:
        _sessions[run_id] = PiSessionContext(session_id=run_id)


def _observe_recorder(run_id: str, request: Mapping[str, object]) -> RunRecorder | None:
    """Recorder for observe events; question wires the run question once."""
    question = request.get("question")
    return _recorder_for(run_id, question if isinstance(question, str) else "")


def _observe_meta(request: Mapping[str, object]) -> dict[str, object]:
    """Event metadata: wire fields minus routing keys."""
    return {k: v for k, v in request.items() if k not in ("op", "run_id", "event")}


def _pi_event_observe(run_id: str, event: str, request: Mapping[str, object]) -> dict[str, object]:
    """Non-lifecycle event: session setup, recorder lookup, one record call."""
    try:
        _ensure_observe_session(run_id, event)
        recorder = _observe_recorder(run_id, request)
        if recorder is None or not recorder.enabled:
            return {"ok": True}  # dropped, research continues
        meta = _observe_meta(request)
        _pi_event_record(recorder, event, request, meta or None)
        return {"ok": True}
    except (
        Exception  # noqa: BLE001 - intentional best-effort boundary, never aborts
    ) as exc:  # observability never breaks research
        logger.warning("pi_event: dropped (%s: %s)", type(exc).__name__, exc)
        return {"ok": True}


def _pi_event(request: Mapping[str, object]) -> dict[str, object]:
    parsed = _pi_event_shape(request)
    if isinstance(parsed, dict):
        return parsed
    run_id, event = parsed
    if event == "agent_end":
        return _pi_event_agent_end(run_id, request)
    return _pi_event_observe(run_id, event, request)


def _abort_run(request: Mapping[str, object]) -> dict[str, object]:
    run_id = request.get("run_id")
    error_type = request.get("error_type")
    error_message = request.get("error_message")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(error_type, str)
        or not error_type
        or not isinstance(error_message, str)
        or not error_message
    ):
        return {"error": "missing_arg"}
    _drain_futures(_run_futures(run_id))
    finalized = _teardown_failed_run(run_id, error_type=error_type, error_message=error_message, answer="")
    return {"ok": True, "finalized": finalized}


def _op_research_session_create(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.session.create -> service.create_research."""
    question = request.get("question")
    objective = request.get("objective")
    as_of = request.get("as_of")
    ctx = _bridge_ctx(request)
    policy = _session_create_policy(request)
    try:
        session_id = _kernel.create_research(
            question if isinstance(question, str) else "",
            objective if isinstance(objective, str) and objective.strip() else None,
            as_of=as_of if isinstance(as_of, str) else None,
            policy=policy if policy is not None else default_policy(),
            repo=ResearchRepository(data_root=ctx.data_root),
        )
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": {"session_id": session_id}}


def _session_create_policy(request: Mapping[str, object]) -> dict[str, JSONValue] | None:
    """Optional session policy from the wire; None stays the SEC-only default."""
    policy = request.get("policy")
    if isinstance(policy, dict):
        return {str(k): v for k, v in policy.items()}  # type: ignore[misc]
    research_sources = request.get("research_sources")
    if isinstance(research_sources, dict):
        return {"research_sources": dict(research_sources)}  # type: ignore[misc]
    return None


def _job_start_session_id(request: Mapping[str, object], protocol_id: str) -> str | dict[str, object]:
    """session_id shape for job.start; value or an error response."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    return session_id


def _job_start_wave_id(request: Mapping[str, object], protocol_id: str) -> int | dict[str, object]:
    """wave_id shape for job.start; value or an error response."""
    raw_wave = request.get("wave_id", 1)
    wave_id = raw_wave if raw_wave is not None else 1
    if isinstance(wave_id, bool) or not isinstance(wave_id, int) or wave_id < 1:
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'wave_id' must be an int >= 1"}
    return wave_id


def _job_start_optional(value: object) -> str | None:
    """Optional non-empty wire string; anything else becomes None."""
    if not isinstance(value, str):
        return None
    if not value:
        return None
    return value


def _job_start_budget(request: Mapping[str, object]) -> dict[str, object] | None:
    """Validated budget mapping for start_job; None when absent or malformed."""
    budget = request.get("budget")
    if not isinstance(budget, dict):
        return None
    return {k: v for k, v in budget.items() if isinstance(k, str)}


def _call_job_start(
    session_id: str,
    request: Mapping[str, object],
    wave_id: int,
    ctx: RequestContext,
) -> dict[str, JSONValue]:
    """service.start_job call; optional fields fall back to gateway defaults."""
    return _kernel.start_job(
        session_id,
        _job_start_optional(request.get("type")) or "source_agent",
        _job_start_optional(request.get("source")),
        _job_start_optional(request.get("parent")),
        _job_start_budget(request),
        repo=ResearchRepository(data_root=ctx.data_root),
        wave_id=wave_id,
    )


def _op_research_job_start(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.job.start -> service.start_job."""
    session_id = _job_start_session_id(request, protocol_id)
    if isinstance(session_id, dict):
        return session_id
    wave_id = _job_start_wave_id(request, protocol_id)
    if isinstance(wave_id, dict):
        return wave_id
    ctx = _bridge_ctx(request)
    try:
        job = _call_job_start(session_id, request, wave_id, ctx)
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": job}


def _op_research_job_complete(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.job.complete -> service.complete_job."""
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    outcome = request.get("outcome")
    ctx = _bridge_ctx(request)
    try:
        job = _kernel.complete_job(
            job_id,
            outcome if isinstance(outcome, dict) else None,
            repo=ResearchRepository(data_root=ctx.data_root),
        )
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": job}


def _evidence_add_ids(request: Mapping[str, object], protocol_id: str) -> tuple[str, str] | dict[str, object]:
    """session/job id shapes for evidence.add; pair or an error response."""
    session_id = request.get("session_id")
    job_id = request.get("job_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    return (session_id, job_id)


def _evidence_add_error(exc: Exception, protocol_id: str, session_id: str, job_id: str) -> dict[str, object]:
    """Map record_evidence ResearchNotFound to the unknown job/session body."""
    if "unknown job_id" in str(exc):
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}


def _op_research_evidence_add(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.evidence.add -> service.record_evidence."""
    ids = _evidence_add_ids(request, protocol_id)
    if isinstance(ids, dict):
        return ids
    session_id, job_id = ids
    item = request.get("item")
    if not isinstance(item, dict):
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'item' must be a mapping"}
    ctx = _bridge_ctx(request)
    try:
        record = _kernel.record_evidence(session_id, job_id, item, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound as exc:
        return _evidence_add_error(exc, protocol_id, session_id, job_id)
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": record}


def _op_research_session_inspect(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.session.inspect -> service.inspect_research."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        state = _kernel.inspect_research(session_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    return {"id": protocol_id, "result": state}


def _op_research_session_resume(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.session.resume -> service.resume_research."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        state = _kernel.resume_research(session_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    return {"id": protocol_id, "result": state}


def _op_research_session_cancel(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.session.cancel -> service.cancel_research."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        session = _kernel.cancel_research(session_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    return {"id": protocol_id, "result": session}


def _freeze_session_id(request: Mapping[str, object], protocol_id: str) -> str | dict[str, object]:
    """session_id shape for freeze.create; value or an error response."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    return session_id


def _freeze_wave_id(request: Mapping[str, object], protocol_id: str) -> int | dict[str, object]:
    """wave_id shape for wave-scoped ops (freeze.create, committee.create); value or an error response."""
    wave_id = request.get("wave_id", 1)
    wave_id = wave_id if wave_id is not None else 1
    if isinstance(wave_id, bool) or not isinstance(wave_id, int) or wave_id < 1:
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'wave_id' must be an int >= 1"}
    return wave_id


def _op_research_freeze_create(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.freeze.create -> service.freeze_session (internal-only)."""
    session_id = _freeze_session_id(request, protocol_id)
    if isinstance(session_id, dict):
        return session_id
    wave_id = _freeze_wave_id(request, protocol_id)
    if isinstance(wave_id, dict):
        return wave_id
    ctx = _bridge_ctx(request)
    try:
        freeze = _kernel.freeze_session(session_id, wave_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": freeze}


def _op_research_wave_decide(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.wave.decide (alias research.wave2.decide) -> the director's next-wave gate."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    # Single defensive resolution: decide_next_wave lands with the director
    # rename, so fall back to the legacy name until it does (same wiring, never
    # a behavior switch).
    decide = getattr(_kernel, "decide_next_wave", None) or _kernel.decide_wave2
    try:
        decision = decide(session_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": decision}


def _op_research_committee_create(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.committee.create -> service.create_committee_jobs (atomic trio)."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    wave_id = _freeze_wave_id(request, protocol_id)
    if isinstance(wave_id, dict):
        return wave_id
    ctx = _bridge_ctx(request)
    try:
        jobs = _kernel.create_committee_jobs(session_id, wave_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": jobs}


COMMITTEE_ROLES: tuple[str, ...] = ("stockbot", "bullbot", "bearbot")


def _required_arg(request: Mapping[str, object], key: str) -> str | None:
    """One non-empty string argument, or None when missing/mistyped."""
    value = request.get(key)
    return value if isinstance(value, str) and value else None


def _op_research_analysis_record(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.analysis.record -> service.record_committee_analysis."""
    session_id = _required_arg(request, "session_id")
    job_id = _required_arg(request, "job_id")
    role = _required_arg(request, "role")
    if session_id is None or job_id is None or role is None:
        return {"id": protocol_id, "error": "missing_arg"}
    if role not in COMMITTEE_ROLES:
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'role' must be " + "|".join(COMMITTEE_ROLES)}
    analysis = request.get("analysis")
    if not isinstance(analysis, dict):
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'analysis' must be a mapping"}
    ctx = _bridge_ctx(request)
    try:
        out = _kernel.record_committee_analysis(
            session_id, job_id, role, analysis, repo=ResearchRepository(data_root=ctx.data_root)
        )
    except _kernel.ResearchNotFound as exc:
        return _evidence_add_error(exc, protocol_id, session_id, job_id)
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": out}


def _op_research_session_finalize(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.session.finalize -> service.finalize_session (internal-only)."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    answer = request.get("answer")
    if not isinstance(answer, str):
        return {"id": protocol_id, "error": "missing_arg"}
    claims = request.get("claims")
    if not isinstance(claims, list):
        return {"id": protocol_id, "error": "invalid_arg", "detail": "'claims' must be a list"}
    if not claims:
        return {"id": protocol_id, "error": "claims_required"}
    ctx = _bridge_ctx(request)
    try:
        final = _kernel.finalize_session(session_id, answer, claims, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": final}


def _source_coverage(request: Mapping[str, object]) -> dict[str, object]:
    """Coverage mapping for submit; empty when absent, narrowed when present."""
    coverage = request.get("coverage")
    if coverage is None:
        return {}
    if not isinstance(coverage, dict):
        return {}
    return {k: v for k, v in coverage.items() if isinstance(k, str)}


def _source_str_list(request: Mapping[str, object], key: str) -> list[str]:
    """Wire string list for submit; empty when absent or malformed."""
    raw = request.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    return [v for v in raw if isinstance(v, str)]


def _op_research_source_submit(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.source.submit -> service.submit_source_result."""
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        out = _kernel.submit_source_result(
            job_id,
            _source_coverage(request),
            _source_str_list(request, "evidence_ids"),
            _source_str_list(request, "unresolved_questions"),
            repo=ResearchRepository(data_root=ctx.data_root),
        )
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": out}


def _op_research_job_heartbeat(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.job.heartbeat -> service.heartbeat_job."""
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        out = _kernel.heartbeat_job(job_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": out}


def _op_research_job_runtime(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.job.runtime -> service.attach_job_runtime (accepted keys filtered there)."""
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        out = _kernel.attach_job_runtime(
            job_id, dict(request), repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": out}


def _op_research_job_fail(
    request: Mapping[str, object], protocol_id: str
) -> dict[str, object]:
    """Dumb dispatch: research.job.fail -> service.fail_job."""
    job_id = _required_arg(request, "job_id")
    category = _required_arg(request, "category")
    message = _required_arg(request, "message")
    if job_id is None or category is None or message is None:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        job = _kernel.fail_job(
            job_id, category, message, repo=ResearchRepository(data_root=ctx.data_root),
        )
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": job}


def _op_research_job_cancel(
    request: Mapping[str, object], protocol_id: str
) -> dict[str, object]:
    """Dumb dispatch: research.job.cancel -> service.cancel_job."""
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    try:
        job = _kernel.cancel_job(job_id, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_job", "job_id": job_id}
    except ValueError as exc:
        return {"id": protocol_id, "error": "invalid_arg", "detail": str(exc)[:500]}
    return {"id": protocol_id, "result": job}


def _events_paging(request: Mapping[str, object]) -> tuple[str | None, int, int]:
    """job/limit/cursor shapes for research.events; untrusted shapes fall back."""
    raw_job = request.get("job_id")
    raw_limit = request.get("limit", 100)
    raw_cursor = request.get("cursor", 0)
    job = raw_job if isinstance(raw_job, str) else None
    limit = raw_limit if isinstance(raw_limit, int) else 100
    cursor = raw_cursor if isinstance(raw_cursor, int) else 0
    return (job, limit, cursor)


def _op_research_events(request: Mapping[str, object], protocol_id: str) -> dict[str, object]:
    """Dumb dispatch: research.events -> service.research_events."""
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return {"id": protocol_id, "error": "missing_arg"}
    ctx = _bridge_ctx(request)
    job, limit, cursor = _events_paging(request)
    try:
        out = _kernel.research_events(session_id, job, limit, cursor, repo=ResearchRepository(data_root=ctx.data_root))
    except _kernel.ResearchNotFound:
        return {"id": protocol_id, "error": "unknown_session", "session_id": session_id}
    return {"id": protocol_id, "result": out}


def _parse_invoke_shape(request: Mapping[str, object]) -> tuple[str, dict[str, object]] | dict[str, object]:
    """name/arguments shape for tool.invoke; parsed pair or an error body (without id)."""
    name = request.get("name")
    arguments = request.get("arguments", {})
    if not isinstance(name, str) or not name:
        return {"error": "missing_arg"}
    if not isinstance(arguments, dict):
        return {"error": "missing_arg"}
    return (name, arguments)


def _invoke_sid(request: Mapping[str, object], protocol_id: object) -> str:
    """Ephemeral session id: explicit session/run id or a bridge-scoped fallback."""
    raw_sid = request.get("session_id", request.get("run_id"))
    if isinstance(raw_sid, str) and raw_sid:
        return raw_sid
    return f"bridge:{protocol_id}"


def _call_tool_invoke(
    name: str,
    arguments: dict[str, object],
    sid: str,
    request: Mapping[str, object],
    protocol_id: object,
    queue_ms: float,
    data_root: str | None,
    as_of: str | None,
) -> object:
    """execute_pi_tool with an ephemeral session; routing fields coerced."""
    tool_call_id = _coerce_opt_str(request.get("tool_call_id"))
    return execute_pi_tool(
        name,
        arguments,
        PiSessionContext(session_id=sid),
        tool_call_id=tool_call_id,
        protocol_id=protocol_id if isinstance(protocol_id, str) else None,
        bridge_queue_ms=queue_ms,
        data_root=data_root,
        as_of=as_of,
    )


def _run_tool_invoke(request: Mapping[str, object]) -> None:
    """Executor worker: dumb tool passthrough with an ephemeral session context."""
    protocol_id = request.get("id")
    parsed = _parse_invoke_shape(request)
    if isinstance(parsed, dict):
        _write({"id": protocol_id, **parsed})
        return
    name, arguments = parsed
    _, data_root, as_of, queue_ms = _tool_call_wire(request)
    sid = _invoke_sid(request, protocol_id)
    try:
        result = _call_tool_invoke(name, arguments, sid, request, protocol_id, queue_ms, data_root, as_of)
        _write({"id": protocol_id, "result": result})
    except Exception:  # per-request failure never breaks the loop
        logger.exception("tool.invoke failed")
        _write({"id": protocol_id, "error": "bridge_failed"})


def _decode_handle_line(line: str) -> tuple[dict[str, object], str] | dict[str, object]:
    """Parse + id-shape one input line; pair or an error body."""
    try:
        request = json.loads(line)
    except json.JSONDecodeError, ValueError:
        return {"error": "bad_request"}
    if not isinstance(request, dict):
        return {"error": "bad_request"}
    protocol_id = request.get("id")
    if not isinstance(protocol_id, str) or not protocol_id:
        return {"error": "missing_arg"}
    return (request, protocol_id)


def _handle_tool_call(request: dict[str, object]) -> dict[str, object] | None:
    """tool_call: validate then submit to the worker pool."""
    error = _validate_tool_call(request)
    if error is not None:
        protocol_id = request.get("id")
        return {"id": protocol_id, **error}
    fut = _executor.submit(_run_tool_call, dict(request))
    raw_run_id = request.get("run_id")
    if isinstance(raw_run_id, str) and raw_run_id:
        _track(raw_run_id, fut)
    return None


def _handle_local_op(op: object, request: dict[str, object], protocol_id: str) -> dict[str, object] | None:
    """describe/doctor/tool_call/abort/pi_event/tool.invoke; None when unhandled."""
    if op == "describe":
        return {"id": protocol_id, **_describe()}
    if op == "doctor":
        return {"id": protocol_id, **_doctor()}
    if op == "tool_call":
        return _handle_tool_call(request)
    if op == "abort_run":
        return {"id": protocol_id, **_abort_run(request)}
    if op == "pi_event":
        return {"id": protocol_id, **_pi_event(request)}
    if op == "tool.invoke":
        _executor.submit(_run_tool_invoke, dict(request))
        return None
    return None


_RESEARCH_OPS: tuple[str, ...] = (
    "research.session.create",
    "research.job.start",
    "research.job.complete",
    "research.evidence.add",
    "research.session.inspect",
    "research.session.resume",
    "research.session.cancel",
    "research.freeze.create",
    "research.committee.create",
    "research.analysis.record",
    "research.wave.decide",
    "research.session.finalize",
    "research.source.submit",
    "research.job.heartbeat",
    "research.job.runtime",
    "research.job.fail",
    "research.job.cancel",
    "research.events",
)


def _handle_research_lifecycle(op: object, request: dict[str, object], protocol_id: str) -> dict[str, object] | None:
    """Session/job/evidence lifecycle ops; None when op is outside this group."""
    if op == "research.session.create":
        return _op_research_session_create(request, protocol_id)
    if op == "research.job.start":
        return _op_research_job_start(request, protocol_id)
    if op == "research.job.complete":
        return _op_research_job_complete(request, protocol_id)
    if op == "research.evidence.add":
        return _op_research_evidence_add(request, protocol_id)
    if op == "research.session.inspect":
        return _op_research_session_inspect(request, protocol_id)
    if op == "research.session.resume":
        return _op_research_session_resume(request, protocol_id)
    if op == "research.session.cancel":
        return _op_research_session_cancel(request, protocol_id)
    return None


def _handle_research_committee(op: object, request: dict[str, object], protocol_id: str) -> dict[str, object] | None:
    """Freeze/committee/gate/finalize/submit/heartbeat/events ops; None when outside this group."""
    if op == "research.freeze.create":
        return _op_research_freeze_create(request, protocol_id)
    if op == "research.committee.create":
        return _op_research_committee_create(request, protocol_id)
    if op == "research.analysis.record":
        return _op_research_analysis_record(request, protocol_id)
    if op in ("research.wave.decide", "research.wave2.decide"):
        return _op_research_wave_decide(request, protocol_id)
    if op == "research.session.finalize":
        return _op_research_session_finalize(request, protocol_id)
    if op == "research.source.submit":
        return _op_research_source_submit(request, protocol_id)
    if op == "research.job.heartbeat":
        return _op_research_job_heartbeat(request, protocol_id)
    if op == "research.job.runtime":
        return _op_research_job_runtime(request, protocol_id)
    if op == "research.job.fail":
        return _op_research_job_fail(request, protocol_id)
    if op == "research.job.cancel":
        return _op_research_job_cancel(request, protocol_id)
    if op == "research.events":
        return _op_research_events(request, protocol_id)
    return None


def _handle_research_op(op: object, request: dict[str, object], protocol_id: str) -> dict[str, object] | None:
    """research.* ops; None when op is not a research op."""
    lifecycle = _handle_research_lifecycle(op, request, protocol_id)
    if lifecycle is not None:
        return lifecycle
    return _handle_research_committee(op, request, protocol_id)


def _handle(line: str) -> dict[str, object] | None:
    """Route one input line. Returns a response dict, or None when the
    response will be written asynchronously by a worker (tool_call)."""
    decoded = _decode_handle_line(line)
    if isinstance(decoded, dict):
        return decoded
    request, protocol_id = decoded
    op = request.get("op")
    local = _handle_local_op(op, request, protocol_id)
    if local is not None:
        return local
    if op in ("describe", "doctor", "tool_call", "abort_run", "pi_event", "tool.invoke"):
        return None
    researched = _handle_research_op(op, request, protocol_id)
    if researched is not None:
        return researched
    return {"id": protocol_id, "error": "unknown_op"}


def main() -> bool:
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            response: dict[str, object] | None
            try:
                response = _handle(line)
            except Exception:  # process never exits on a single request  # noqa: BLE001 - intentional best-effort boundary, never aborts
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
