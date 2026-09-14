"""Herdr socket shim: RAW LOGS + live events over the Unix JSON API.

Proven against Herdr 0.9.0 (protocol 22, generation 1) with zero Herdr edits:
``pane.read`` for RAW LOGS, ``events.subscribe`` streaming for
created/closed/output wakeups. Worker identity stays in :class:`WorkerMap`;
never sent to Herdr (generic ``pane.report_metadata`` only, when needed).
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from typing import Protocol

class _RecvSocket(Protocol):
    def recv(self, n: int, /) -> bytes: ...


DEFAULT_SOCKET = os.path.expanduser("~/.config/herdr/herdr.sock")


class WorkerMap:
    """Two-way WorkerId <-> pane_id map owned by the operator."""

    def __init__(self) -> None:
        self.worker_to_pane: dict[str, str] = {}
        self.pane_to_worker: dict[str, str] = {}

    def insert(self, worker: str, pane: str) -> None:
        old = self.worker_to_pane.get(worker)
        if old is not None:
            self.pane_to_worker.pop(old, None)
        self.worker_to_pane[worker] = pane
        self.pane_to_worker[pane] = worker

    def remove_by_pane(self, pane: str) -> str | None:
        worker = self.pane_to_worker.pop(pane, None)
        if worker is not None:
            self.worker_to_pane.pop(worker, None)
        return worker

    def pane_of(self, worker: str) -> str | None:
        return self.worker_to_pane.get(worker)


class HerdrClient:
    """Thin newline-JSON client over ``HERDR_SOCKET_PATH`` (0600, operator-held)."""

    def __init__(self, socket_path: str | None = None) -> None:
        self.socket_path = (
            socket_path or os.environ.get("HERDR_SOCKET_PATH") or DEFAULT_SOCKET
        )

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        return sock

    @staticmethod
    def _read_lines(sock: _RecvSocket) -> Iterator[dict[str, object]]:
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    yield json.loads(line)

    def request(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {"id": f"cli:{method}", "method": method, "params": params or {}}
        with self._connect() as sock:
            sock.sendall((json.dumps(payload) + "\n").encode())
            for message in self._read_lines(sock):
                return message
        raise ConnectionError("empty response from Herdr socket")

    def pane_read(
        self, pane_id: str, source: str = "recent", lines: int = 200
    ) -> str:
        """RAW LOGS tail for one pane (text, ANSI stripped)."""
        result = self.request(
            "pane.read",
            {"pane_id": pane_id, "source": source, "lines": lines,
             "format": "text", "strip_ansi": True},
        )["result"]
        assert isinstance(result, dict)
        payload = result.get("read", result)
        assert isinstance(payload, dict)
        return str(payload.get("text", ""))

    def subscribe(
        self, pane_ids: list[str] | None = None, match: str = ""
    ) -> Iterator[dict[str, object]]:
        """Yield narrow operator events; caller maps pane_id via WorkerMap."""
        subscriptions: list[dict[str, object]] = [{"type": "pane.created"}, {"type": "pane.closed"}]
        for pane_id in pane_ids or []:
            if match:
                subscriptions.append(
                    {"type": "pane.output_matched", "pane_id": pane_id,
                     "source": "recent",
                     "match": {"type": "substring", "value": match}})
            else:
                subscriptions.append(
                    {"type": "pane.agent_status_changed", "pane_id": pane_id})
        payload: dict[str, object] = {"id": "sub_operator", "method": "events.subscribe",
                    "params": {"subscriptions": subscriptions}}
        sock = self._connect()
        sock.sendall((json.dumps(payload) + "\n").encode())
        return self._read_lines(sock)


def demo() -> None:
    """Self-check framing + mapping with a fake socket (no live server)."""
    chunks = [b'{"event":"pane_cre', b'ated"}\n{"result":{"type":"ok"}}\n']
    received: list[bytes] = []

    class FakeSock:
        def recv(self, n: int) -> bytes:
            return chunks.pop(0) if chunks else b""

        def sendall(self, data: bytes) -> None:
            received.append(data)

    messages = list(HerdrClient._read_lines(FakeSock()))
    assert messages == [{"event": "pane_created"}, {"result": {"type": "ok"}}], messages
    assert received == [], received  # reader never writes

    mapping = WorkerMap()
    mapping.insert("worker-7", "wC:p3")
    assert mapping.pane_of("worker-7") == "wC:p3"
    assert mapping.remove_by_pane("wC:p3") == "worker-7"
    assert mapping.pane_of("worker-7") is None
    print("herdr_client demo: framing + WorkerMap OK")


if __name__ == "__main__":
    demo()
