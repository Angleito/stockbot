"""Concurrency + ID-correlation tests for scripts/pi_bridge.py.

Uses a barrier-backed fake handler (never the real gateway) to prove:
four calls overlap, out-of-order completions keep their request/tool IDs,
agent_end waits for its run, and EOF drains workers.
"""

import concurrent.futures
import io
import json
import threading
import time
import uuid
from pathlib import Path
from typing import override

import pytest

import scripts.pi_bridge as pi_bridge
from app.pi_gateway import PiSessionContext
from app.storage.runs import RunRecorder


class _StubRecorder(RunRecorder):
    """Bridge-test double: captures completion calls without touching storage.

    Inherits RunRecorder.__exit__: the stub never opens a connection, so the
    inherited teardown is a lock-only no-op.
    """

    def __init__(self) -> None:
        super().__init__(
            run_id="stub", request_id="stub", question="", as_of=None,
            model="stub", provider="stub", model_parameters={},
            agent_version="stub", prompt_version="stub",
            tool_registry_version="stub", git_sha="",
        )
        self.enabled = True
        self.completed: list[dict[str, str | None]] = []
        self.failed_events: list[tuple[str, dict[str, object]]] = []

    @override
    def complete(
        self, *, status: str, answer: str,
        error_type: str | None = None, error_message: str | None = None,
    ) -> None:
        self.completed.append(
            {
                "status": status, "answer": answer,
                "error_type": error_type, "error_message": error_message,
            }
        )

    @override
    def record_event(self, event_type: str, **kwargs: object) -> None:
        self.failed_events.append((event_type, kwargs))


def _run_id(tag: str) -> str:
    return f"run-{tag}-{uuid.uuid4().hex[:8]}"


def _capture_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    lock = threading.Lock()

    def fake_write(response: dict[str, object]) -> None:
        with lock:
            responses.append(response)

    monkeypatch.setattr(pi_bridge, "_write", fake_write)
    return responses


def _start_session(run_id: str) -> None:
    pi_bridge._sessions[run_id] = PiSessionContext(session_id=run_id)


def _teardown_run(run_id: str) -> None:
    pi_bridge._sessions.pop(run_id, None)
    pi_bridge._recorders.pop(run_id, None)
    with pi_bridge._state_lock:
        pi_bridge._inflight.pop(run_id, None)


def _tool_payload(run_id: str, tool_call_id: str | None = None) -> dict[str, object]:
    return {
        "id": f"tc-{uuid.uuid4().hex[:8]}",
        "op": "tool_call",
        "run_id": run_id,
        "tool_call_id": tool_call_id or f"call-{uuid.uuid4().hex[:8]}",
        "name": "search_tools",
        "arguments": {"query": "overlap"},
        "bridge_queue_ms": 0.0,
    }


def _wait_run(run_id: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with pi_bridge._state_lock:
            pending = dict(pi_bridge._inflight).get(run_id)
            if not pending:
                return
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not drain")


def test_four_tool_calls_overlap(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("overlap")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    barrier = threading.Barrier(4)
    live = 0
    max_live = 0
    live_lock = threading.Lock()

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        nonlocal live, max_live
        with live_lock:
            live += 1
            max_live = max(max_live, live)
        try:
            barrier.wait(timeout=30)
        finally:
            with live_lock:
                live -= 1
        return {"content": "ok"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        payloads = [_tool_payload(run_id) for _ in range(4)]
        for payload in payloads:
            assert pi_bridge._handle(json.dumps(payload)) is None
        _wait_run(run_id)
        assert max_live == 4
        assert {r["id"] for r in responses} == {p["id"] for p in payloads}
    finally:
        _teardown_run(run_id)


def test_out_of_order_completions_keep_ids(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("ooo")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    release = threading.Event()

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        if arguments.get("query") == "slow":
            assert release.wait(timeout=30)
            return {"content": "slow-result"}
        return {"content": "fast-result"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        slow = _tool_payload(run_id, "call-slow")
        slow["arguments"] = {"query": "slow"}
        fast = _tool_payload(run_id, "call-fast")
        fast["arguments"] = {"query": "fast"}
        assert pi_bridge._handle(json.dumps(slow)) is None
        assert pi_bridge._handle(json.dumps(fast)) is None
        deadline = time.time() + 30
        while time.time() < deadline and len(responses) < 1:
            time.sleep(0.01)
        assert len(responses) == 1
        assert responses[0]["id"] == fast["id"]
        release.set()
        _wait_run(run_id)
        assert len(responses) == 2
        by_id = {r["id"]: r for r in responses}
        assert by_id[slow["id"]]["result"] == {"content": "slow-result"}
        assert by_id[fast["id"]]["result"] == {"content": "fast-result"}
    finally:
        release.set()
        _teardown_run(run_id)


def test_tool_call_forwards_tracing_ids(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("trace")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    seen: dict[str, object] = {}

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        seen.update(kwargs)
        return {"content": "ok"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        payload = _tool_payload(run_id, "call-7")
        assert pi_bridge._handle(json.dumps(payload)) is None
        _wait_run(run_id)
        assert seen["tool_call_id"] == "call-7"
        assert seen["protocol_id"] == payload["id"]
        assert seen["bridge_queue_ms"] == 0.0
        assert responses[0]["id"] == payload["id"]
    finally:
        _teardown_run(run_id)


def test_agent_end_waits_for_run_calls(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("drain")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    release = threading.Event()
    stub = _StubRecorder()
    completed = stub.completed
    pi_bridge._recorders[run_id] = stub

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        assert release.wait(timeout=30)
        return {"content": "late"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        finished: list[dict[str, object] | None] = []
        worker = threading.Thread(
            target=lambda: finished.append(
                pi_bridge._handle(
                    json.dumps({"id": "end-1", "op": "pi_event", "run_id": run_id, "event": "agent_end"})
                )
            )
        )
        worker.start()
        time.sleep(0.3)
        assert finished == []  # agent_end blocked on the in-flight call
        assert completed == []
        release.set()
        worker.join(timeout=30)
        assert finished and finished[0] == {"id": "end-1", "ok": True}
        assert len(completed) == 1
        assert any(r.get("result") == {"content": "late"} for r in responses)
    finally:
        release.set()
        _teardown_run(run_id)


def test_eof_drains_submitted_work(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("eof")
    pi_bridge._sessions[run_id] = PiSessionContext(session_id=run_id)
    responses = _capture_writes(monkeypatch)
    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        time.sleep(0.2)
        return {"content": "ok"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(pi_bridge, "_executor", worker_pool)
    payloads = [_tool_payload(run_id) for _ in range(2)]
    lines = "".join(json.dumps(p) + "\n" for p in payloads)
    monkeypatch.setattr(pi_bridge.sys, "stdin", io.StringIO(lines))
    try:
        assert pi_bridge.main() is True
        assert {r["id"] for r in responses} == {p["id"] for p in payloads}
    finally:
        _teardown_run(run_id)

def test_agent_end_drain_timeout(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("timeout")
    _start_session(run_id)
    _capture_writes(monkeypatch)
    release = threading.Event()
    stub = _StubRecorder()
    completed = stub.completed
    failed_events = stub.failed_events
    pi_bridge._recorders[run_id] = stub
    worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(pi_bridge, "_executor", worker_pool)
    monkeypatch.setattr(pi_bridge, "TOOL_DRAIN_TIMEOUT_SECONDS", 0.05)
    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        release.wait(timeout=30)
        return {"content": "late"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        futures = pi_bridge._run_futures(run_id)
        assert len(futures) == 2
        start = time.time()
        response = pi_bridge._handle(
            json.dumps({"id": "end-t", "op": "pi_event", "run_id": run_id, "event": "agent_end"})
        )
        assert time.time() - start < 5
        assert response == {"id": "end-t", "error": "tool_drain_timeout"}
        assert any(f.cancelled() for f in futures)
        assert len(completed) == 1
        assert completed[0]["status"] == "failed"
        assert completed[0]["error_type"] == "tool_drain_timeout"
        assert completed[0]["error_message"] == "Tool calls did not finish before the bridge drain timeout"
        assert len(failed_events) == 1
        assert failed_events[0][0] == "run_failed"
        assert failed_events[0][1]["metadata"] == {
            "error_type": "tool_drain_timeout",
            "error_message": "Tool calls did not finish before the bridge drain timeout",
        }
        assert run_id not in pi_bridge._sessions
        assert run_id not in pi_bridge._recorders
    finally:
        release.set()
        worker_pool.shutdown(wait=True)
        _teardown_run(run_id)


def test_eof_drain_timeout(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("eof-timeout")
    pi_bridge._sessions[run_id] = PiSessionContext(session_id=run_id)
    _capture_writes(monkeypatch)
    release = threading.Event()
    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        release.wait(timeout=30)
        return {"content": "late"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(pi_bridge, "_executor", worker_pool)
    monkeypatch.setattr(pi_bridge, "TOOL_DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(pi_bridge.sys, "stdin", io.StringIO(""))
    try:
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        start = time.time()
        assert pi_bridge.main() is False
        assert time.time() - start < 5
    finally:
        release.set()
        worker_pool.shutdown(wait=True)
        _teardown_run(run_id)

def test_abort_run_fails_live_run(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("abort")
    _start_session(run_id)
    _capture_writes(monkeypatch)
    # Live complete() is never trusted alone; the ack comes from the
    # idempotent fallback, which sees the already-terminal row here.
    def _fake_finalize_failed_run(run_id: str, *, error_type: str, error_message: str) -> bool:
        return True

    monkeypatch.setattr(pi_bridge, "finalize_failed_run", _fake_finalize_failed_run)
    release = threading.Event()
    stub = _StubRecorder()
    completed = stub.completed
    failed_events = stub.failed_events
    pi_bridge._recorders[run_id] = stub
    worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(pi_bridge, "_executor", worker_pool)
    monkeypatch.setattr(pi_bridge, "TOOL_DRAIN_TIMEOUT_SECONDS", 0.05)
    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: str | float | Path | None
    ) -> dict[str, str]:
        release.wait(timeout=30)
        return {"content": "late"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        assert pi_bridge._handle(json.dumps({"id": "bad-1", "op": "abort_run", "run_id": run_id})) == {"id": "bad-1", "error": "missing_arg"}
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        assert pi_bridge._handle(json.dumps(_tool_payload(run_id))) is None
        futures = pi_bridge._run_futures(run_id)
        assert len(futures) == 2
        start = time.time()
        message = "Tool call timed out after 50ms"
        response = pi_bridge._handle(
            json.dumps({"id": "abort-1", "op": "abort_run", "run_id": run_id, "error_type": "tool_timeout", "error_message": message})
        )
        assert time.time() - start < 5
        assert response == {"id": "abort-1", "ok": True, "finalized": True}
        assert any(f.cancelled() for f in futures)
        assert len(completed) == 1
        assert completed[0]["status"] == "failed"
        assert completed[0]["error_type"] == "tool_timeout"
        assert completed[0]["error_message"] == message
        assert len(failed_events) == 1
        assert failed_events[0][0] == "run_failed"
        assert failed_events[0][1]["metadata"] == {"error_type": "tool_timeout", "error_message": message}
        assert run_id not in pi_bridge._sessions
        assert run_id not in pi_bridge._recorders
    finally:
        release.set()
        worker_pool.shutdown(wait=True)
        _teardown_run(run_id)


def test_abort_run_repairs_silent_live_complete(monkeypatch: pytest.MonkeyPatch):
    from app.storage.runs import get_run
    run_id = _run_id("abort-silent")
    _start_session(run_id)
    recorder = pi_bridge._recorder_for(run_id, question="silent live failure?")
    assert recorder is not None and recorder.enabled
    def _silent_complete(*, status: str, answer: str, error_type: str | None = None, error_message: str | None = None) -> None:
        recorder._disable(Exception("silent sqlite failure"))
    monkeypatch.setattr(recorder, "complete", _silent_complete)
    try:
        response = pi_bridge._handle(
            json.dumps({"id": "abort-3", "op": "abort_run", "run_id": run_id,
                        "error_type": "tool_timeout", "error_message": "timed out"})
        )
        assert response == {"id": "abort-3", "ok": True, "finalized": True}
        row = get_run(run_id)
        assert row is not None
        assert row["status"] == "failed"
        assert row["completed_at"] is not None
        assert row["error_type"] == "tool_timeout"
        assert run_id not in pi_bridge._sessions
        assert run_id not in pi_bridge._recorders
    finally:
        _teardown_run(run_id)



def test_requests_without_id_are_rejected():
    assert pi_bridge._handle('{"op": "describe"}') == {"error": "missing_arg"}
    assert pi_bridge._handle('{"op": "doctor"}') == {"error": "missing_arg"}
    response = pi_bridge._handle(json.dumps({"id": "x-1", "op": "nope"}))
    assert response == {"id": "x-1", "error": "unknown_op"}


def test_abort_run_reports_unconfirmed_finalization(monkeypatch: pytest.MonkeyPatch):
    run_id = _run_id("abort-unconfirmed")
    _start_session(run_id)
    def _fake_finalize_failed_run(run_id: str, *, error_type: str, error_message: str) -> bool:
        return False

    monkeypatch.setattr(pi_bridge, "finalize_failed_run", _fake_finalize_failed_run)
    try:
        response = pi_bridge._handle(
            json.dumps({"id": "abort-2", "op": "abort_run", "run_id": run_id,
                        "error_type": "tool_timeout", "error_message": "timed out"})
        )
        assert response == {"id": "abort-2", "ok": True, "finalized": False}
        assert run_id not in pi_bridge._sessions
        assert run_id not in pi_bridge._recorders
    finally:
        _teardown_run(run_id)
