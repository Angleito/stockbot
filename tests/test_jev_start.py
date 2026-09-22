"""JevClient.start() startup behavior: ping handshake + single sidecar reuse."""

from __future__ import annotations

import asyncio
import json
import select
import shutil
import subprocess
from pathlib import Path

import pytest

from app.decision_client import JevClient


class _FakeStdin:
    def __init__(self) -> None:
        self.last = ""
        self.writes: list[dict[str, object]] = []

    def write(self, s: str) -> None:
        self.last = s
        self.writes.append(json.loads(s))

    def flush(self) -> None:
        pass


class _FakeStdout:
    def __init__(self, stdin: _FakeStdin, *, bad_ack: bool = False) -> None:
        self._stdin = stdin
        self._bad_ack = bad_ack

    def readline(self) -> str:
        payload = json.loads(self._stdin.last)
        if self._bad_ack:
            return json.dumps({"id": "wrong", "ready": True}) + "\n"
        if payload.get("op") == "ping":
            return json.dumps({"id": payload["id"], "ready": True}) + "\n"
        return (
            json.dumps({"id": payload["id"], "raw": {}, "decisions": {"q": {"kind": "noul", "probability": 0.5}}})
            + "\n"
        )


class _FakeProc:
    def __init__(self, *, bad_ack: bool = False) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(self.stdin, bad_ack=bad_ack)

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bad_ack: bool = False) -> tuple[JevClient, _FakeProc]:
    root = Path(__file__).resolve().parent.parent
    client = JevClient(data_root=tmp_path, runtime_path=root / "decision" / "runtime.ts")
    proc = _FakeProc(bad_ack=bad_ack)
    monkeypatch.setattr(client, "_proc", proc)
    return client, proc


def test_start_pings_and_reuses_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, proc = _client(tmp_path, monkeypatch)
    client.start()
    client.start()
    assert client._proc is proc
    assert [w.get("op") for w in proc.stdin.writes] == ["ping", "ping"]
    assert proc.stdin.writes[0]["id"] != proc.stdin.writes[1]["id"]


def test_start_rejects_bad_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch, bad_ack=True)
    with pytest.raises(RuntimeError, match="ping failed"):
        client.start()


def test_start_then_decide_uses_one_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, proc = _client(tmp_path, monkeypatch)

    def _noop(**kwargs: object) -> None:
        return None

    monkeypatch.setattr(client, "_persist", _noop)
    client.start()
    out = asyncio.run(
        client.decide({"s": 1}, {"q": {"type": "noul", "instructions": "x"}}, decision_type="t", session_id="s")
    )
    assert out == {"q": {"kind": "noul", "probability": 0.5}}
    assert client._proc is proc


def test_runtime_ping_over_bun() -> None:
    bun = shutil.which("bun")
    if bun is None:
        pytest.skip("bun unavailable")
    root = Path(__file__).resolve().parent.parent
    rt = root / "decision" / "runtime.ts"
    if not rt.exists():
        pytest.skip("runtime.ts missing")
    proc = subprocess.Popen(
        [bun, str(rt)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        cwd=str(root),
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps({"id": "ping:t1", "op": "ping"}) + "\n")
        proc.stdin.flush()
        no_fds: list[int] = []
        ready, _, _ = select.select([proc.stdout], no_fds, no_fds, 30)
        assert ready, "sidecar did not answer ping"
        assert json.loads(proc.stdout.readline()) == {"id": "ping:t1", "ready": True}
        assert proc.poll() is None
    finally:
        proc.terminate()
