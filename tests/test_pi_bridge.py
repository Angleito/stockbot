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


def _bridge_result(resp: dict[str, object]) -> dict[str, object]:
    r = resp.get("result")
    assert isinstance(r, dict)
    return r


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


def test_describe_direct_tool_names_parity():
    from app.tools import TOOL_DISCOVERY_REGISTRY, dynamically_activatable_tool_names
    describe = pi_bridge._describe()
    direct = describe["direct_tool_names"]
    assert isinstance(direct, list)
    tools = describe["tools"]
    assert isinstance(tools, list)
    assert set(direct) <= {pi_bridge._tool_name(t) for t in tools if isinstance(t, dict)}
    assert "thesis_show" in direct
    assert set(TOOL_DISCOVERY_REGISTRY) - set(direct) == {
        "thesis_create", "thesis_refine", "thesis_watch", "thesis_journal", "thesis_status",
        "research_start", "research_resume", "research_status", "research_cancel", "research_read",
        "research_add_evidence", "research_add_analysis", "research_finalize",
    }


def _routing_event_roundtrip(run_id: str, event: str, extra: dict[str, object]) -> tuple[str, dict[str, object]]:
    stub = _StubRecorder()
    pi_bridge._recorders[run_id] = stub
    try:
        response = pi_bridge._pi_event({"run_id": run_id, "event": event, **extra})
    finally:
        pi_bridge._recorders.pop(run_id, None)
    assert response == {"ok": True}
    assert stub.failed_events
    return stub.failed_events[-1]


def test_routing_continuation_persists():
    run_id = _run_id("routing-cont")
    _start_session(run_id)
    try:
        event_type, kwargs = _routing_event_roundtrip(
            run_id, "routing_continuation",
            {"reason": "discovery_without_research", "discovered_tools": ["get_fundamentals"], "continuation_number": 1},
        )
        assert event_type == "routing_continuation"
        metadata = kwargs.get("metadata")
        assert isinstance(metadata, dict)
        assert metadata["reason"] == "discovery_without_research"
        assert metadata["discovered_tools"] == ["get_fundamentals"]
    finally:
        _teardown_run(run_id)


def test_routing_continuation_failed_persists():
    run_id = _run_id("routing-cont-failed")
    _start_session(run_id)
    try:
        event_type, _ = _routing_event_roundtrip(run_id, "routing_continuation_failed", {})
        assert event_type == "routing_continuation_failed"
    finally:
        _teardown_run(run_id)


def test_routing_metrics_persists():
    run_id = _run_id("routing-metrics")
    _start_session(run_id)
    try:
        event_type, kwargs = _routing_event_roundtrip(
            run_id, "routing_metrics",
            {"discovery_calls": 1, "discovered_tool_count": 2, "research_calls": 1,
             "failed_research_calls": 0, "call_tool_count": 0, "direct_tool_calls": 1,
             "invalid_tool_calls": 0, "premature_stop_detected": False,
             "continuation_injected": False, "continuation_succeeded": False,
             "unrelated_research_calls": 0, "final_answer_after_evidence": True},
        )
        assert event_type == "routing_metrics"
        metadata = kwargs.get("metadata")
        assert isinstance(metadata, dict)
        assert metadata["discovery_calls"] == 1
        assert metadata["final_answer_after_evidence"] is True
    finally:
        _teardown_run(run_id)

def test_research_create_inspect_add_evidence_uses_initial_running_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_DB_PATH", raising=False)
    root = str(tmp_path / "bridge-data")
    created = pi_bridge._handle(
        json.dumps({"id": "r-create", "op": "research.session.create",
                    "question": "NVDA demand?", "objective": "o", "data_root": root})
    )
    assert created is not None and "result" in created
    sid_val = _bridge_result(created).get("session_id")
    assert isinstance(sid_val, str)
    sid = sid_val
    inspected = pi_bridge._handle(
        json.dumps({"id": "r-inspect", "op": "research.session.inspect",
                    "session_id": sid, "data_root": root})
    )
    assert inspected is not None and "result" in inspected
    jobs = _bridge_result(inspected).get("jobs")
    assert isinstance(jobs, list)
    src = [j for j in jobs if isinstance(j, dict) and j.get("job_type") == "source_agent"]
    assert len(src) == 1
    assert src[0].get("status") == "running"
    jid = str(src[0].get("job_id"))
    added = pi_bridge._handle(
        json.dumps({"id": "r-add", "op": "research.evidence.add", "session_id": sid,
                    "job_id": jid, "data_root": root,
                    "item": {"content": "c-ev-1", "claim_text": "c", "subject": "NVDA",
                             "source_name": "SEC", "source_uri": "https://sec.gov/x",
                             "source_record_id": "r",
                             "known_at": "2025-06-29T00:00:00+00:00"}})
    )
    assert added is not None and "result" in added, added
    reinspected = pi_bridge._handle(
        json.dumps({"id": "r-reinspect", "op": "research.session.inspect",
                    "session_id": sid, "data_root": root})
    )
    assert reinspected is not None and "result" in reinspected
    jobs2 = _bridge_result(reinspected).get("jobs")
    assert isinstance(jobs2, list)
    src2 = [j for j in jobs2 if isinstance(j, dict) and j.get("job_type") == "source_agent"]
    assert len(src2) == 1


def test_staged_context_no_crosstalk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Barrier-interleaved staged calls keep their own job pair; args canonical."""
    run_id = _run_id("staged")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    barrier = threading.Barrier(2)
    seen: dict[str, tuple[object, object, dict[str, object]]] = {}
    seen_lock = threading.Lock()

    def fake_execute(
        name: str, arguments: dict[str, object], session: PiSessionContext, **kwargs: object
    ) -> dict[str, str]:
        barrier.wait(timeout=30)
        snapshot = dict(arguments)
        with seen_lock:
            tool_id = kwargs.get("tool_call_id")
            assert isinstance(tool_id, str)
            seen[tool_id] = (kwargs.get("active_research_session_id"), kwargs.get("active_research_job_id"), snapshot)
        return {"content": "ok"}

    monkeypatch.setattr(pi_bridge, "execute_pi_tool", fake_execute)
    try:
        p1 = _tool_payload(run_id, "call-1")
        p1["active_research_session_id"] = "sess-A"
        p1["active_research_job_id"] = "job-A"
        p2 = _tool_payload(run_id, "call-2")
        p2["active_research_session_id"] = "sess-A"
        p2["active_research_job_id"] = "job-B"
        assert pi_bridge._handle(json.dumps(p1)) is None
        assert pi_bridge._handle(json.dumps(p2)) is None
        _wait_run(run_id)
        assert seen["call-1"][:2] == ("sess-A", "job-A")
        assert seen["call-2"][:2] == ("sess-A", "job-B")
        for _, (_, _, args) in seen.items():
            assert args == {"query": "overlap"}
            assert "active_research_session_id" not in args
            assert "active_research_job_id" not in args
        assert len(responses) == 2
    finally:
        _teardown_run(run_id)


def test_staged_context_validation() -> None:
    run_id = _run_id("staged-validate")
    _start_session(run_id)
    try:
        base = _tool_payload(run_id, "call-v")
        bad_job_only = dict(base)
        bad_job_only["active_research_job_id"] = "job-X"
        assert pi_bridge._validate_tool_call(bad_job_only) == {"error": "invalid_research_context"}
        bad_empty = dict(base)
        bad_empty["active_research_session_id"] = ""
        assert pi_bridge._validate_tool_call(bad_empty) == {"error": "invalid_research_context"}
        good = dict(base)
        good["active_research_session_id"] = "sess-A"
        good["active_research_job_id"] = "job-A"
        assert pi_bridge._validate_tool_call(good) is None
    finally:
        _teardown_run(run_id)

def test_tool_call_dispatch_consumes_data_root_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway bills <data_root>/research.sqlite; the directory is never opened as SQLite."""
    import dataclasses
    import json
    import app.pi_gateway as _gw
    from app.research import service as _svc
    from app.research.repository import ResearchRepository
    monkeypatch.delenv("RESEARCH_DB_PATH", raising=False)
    root = tmp_path / "bridge-data"
    repo = ResearchRepository(data_root=root)
    sid = _svc.create_research("NVDA demand?", "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    jid = repo.list_jobs(sid)[0].job_id
    repo.save_job(dataclasses.replace(repo.get_job(jid), tool_budget=1))
    calls: list[str] = []
    def _fake_execute(name: str, arguments: dict[str, object], model: str, context: object = None) -> dict[str, object]:
        calls.append(name)
        return {"result_type": "web_search", "query": arguments.get("query"), "results": [], "source": "exa"}
    monkeypatch.setattr(_gw, "execute_tool", _fake_execute)
    run_id = _run_id("dataroot")
    _start_session(run_id)
    responses = _capture_writes(monkeypatch)
    try:
        payload = {
            "id": "tc-data-1",
            "op": "tool_call",
            "run_id": run_id,
            "tool_call_id": "call-data-1",
            "name": "search_web",
            "arguments": {"query": "NVDA demand"},
            "data_root": str(root),
            "active_research_session_id": sid,
            "active_research_job_id": jid,
        }
        assert pi_bridge._handle(json.dumps(payload)) is None
        _wait_run(run_id)
        assert len(responses) == 1
        assert calls == ["search_web"]
        assert (root / "research.sqlite").exists()
        fresh = ResearchRepository(data_root=root)
        assert fresh.get_job(jid).tool_budget == 0
        assert fresh.get_session(sid).budget.get("tool_calls_used") == 1
    finally:
        _teardown_run(run_id)


def test_call_tool_research_finalize_completes_trio_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical research_finalize via call_tool completes a trio-complete session."""
    import app.pi_gateway as _gw
    from app.research import service as _svc
    from app.research.repository import ResearchRepository
    monkeypatch.delenv("RESEARCH_DB_PATH", raising=False)
    root = tmp_path / "finalize-data"
    repo = ResearchRepository(data_root=root)
    sid = _svc.create_research("NVDA demand?", "o", as_of="2025-06-30T00:00:00+00:00", repo=repo)
    src = repo.list_jobs(sid)[0].job_id
    eid = f"{sid}:ev:1"
    _svc.record_evidence(sid, src, {
        "evidence_id": eid, "wave_id": 1, "content": "c-" + eid, "claim_text": "c",
        "subject": "NVDA", "source_name": "SEC", "source_uri": "https://sec.gov/x",
        "source_record_id": "r", "known_at": "2025-06-29T00:00:00+00:00",
    }, repo=repo)
    _svc.complete_job(src, {}, repo=repo)
    fid = str(_svc.freeze_session(sid, 1, repo=repo)["freeze_id"])
    for role in ("stockbot", "bullbot", "bearbot"):
        jid = str(_svc.start_job(sid, role, repo=repo, wave_id=1)["job_id"])
        _svc.record_committee_analysis(sid, jid, role, {
            "claims": [{"text": "finding", "evidence_ids": [eid]}], "follow_ups": [],
        }, repo=repo)
    assert not [j for j in repo.list_jobs(sid) if j.status in ("queued", "running")]
    bound = PiSessionContext(session_id="pi-finalize", active_research_session_id=sid)
    out = _gw.execute_pi_tool("call_tool", {
        "name": "research_finalize",
        "arguments": {
            "session_id": sid,
            "answer": "NVDA demand is supported by the filed evidence.",
            "claims": [{"text": "finding", "evidence_ids": [eid]}],
        },
    }, bound, data_root=str(root))
    meta = out.get("meta")
    assert isinstance(meta, dict)
    assert meta.get("status") == "completed", out
    fresh = ResearchRepository(data_root=root)
    final = fresh.get_session(sid).final_result
    assert isinstance(final, dict)
    assert final.get("freeze_id") == fid
    raw_frozen = fresh.get_freeze(fid).get("evidence_ids", [])
    assert isinstance(raw_frozen, list)
    frozen = set(raw_frozen)
    claims = final.get("claims", [])
    assert isinstance(claims, list) and claims
    for claim in claims:
        assert isinstance(claim, dict)
        refs = claim.get("evidence_ids", [])
        assert isinstance(refs, list) and set(refs) <= frozen
