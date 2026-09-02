"""Streams formatted log records to a local log server over HTTP."""

import logging
import queue
import threading
import urllib.request

_LOGGER = logging.getLogger(__name__)


class LogStreamHandler(logging.Handler):
    """Fire-and-forget HTTP POST of each record to a local log server.

    Records are formatted, queued, and sent from a daemon worker thread so
    logging never blocks or fails the chat. When the server is unreachable
    records are dropped and one WARNING is emitted per outage (reset on the
    next successful send)."""

    def __init__(self, url: str, *, queue_size: int = 1000, timeout: float = 1.0):
        super().__init__()
        self.url = url
        self.timeout = timeout
        self._queue: queue.Queue[str] = queue.Queue(maxsize=queue_size)
        self._unreachable = False
        self._worker = threading.Thread(target=self._run, name="log-stream", daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._queue.put_nowait(self.format(record))
        except queue.Full:
            pass  # drop when full; never block the chat

    def _run(self) -> None:
        while True:
            line = self._queue.get()
            try:
                request = urllib.request.Request(
                    self.url, data=line.encode("utf-8"), method="POST"
                )
                with urllib.request.urlopen(request, timeout=self.timeout):
                    pass
                self._unreachable = False
            except Exception:
                if not self._unreachable:
                    self._unreachable = True
                    _LOGGER.warning(
                        "log server unreachable at %s; streaming disabled until it responds",
                        self.url,
                    )
