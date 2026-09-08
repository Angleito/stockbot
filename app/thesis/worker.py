"""Restartable thesis monitor worker: a thin sleep loop around ``monitor.tick``.

The worker adds no monitoring logic of its own: every iteration calls
:func:`app.thesis.monitor.tick` (which owns pausing, dedup, checkpointing,
and pending-trigger resume) and then sleeps. Restart resumes solely from
``checkpoint.yaml`` + pending triggers; no in-memory state is carried.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from app.thesis import monitor

log = logging.getLogger(__name__)


def run_monitor_once(repository: Any, thesis_id: str, source_services: Any = None, *,
                     known_at: str | None = None) -> Any:
    """One monitor iteration: passthrough to :func:`monitor.tick`."""
    return monitor.tick(repository, thesis_id, source_services, known_at=known_at)


def monitor_loop(*, repository: Any, thesis_id: str, interval_seconds: float = 900,
                 source_services: Any = None,
                 known_at_fn: Callable[[], str] | None = None,
                 stop_event: threading.Event | None = None,
                 on_tick: Callable[[Any], None] | None = None) -> int:
    if interval_seconds <= 0:
        raise ValueError(f"<worker>: interval_seconds must be > 0, got {interval_seconds!r}")
    stop = stop_event or threading.Event()
    # ponytail: fixed-interval sleep polling; upgrade to event-driven wakeups only
    # if sub-minute latency or many theses per process ever matter.
    while not stop.is_set():
        try:
            outcome = run_monitor_once(
                repository, thesis_id, source_services,
                known_at=known_at_fn() if known_at_fn else None)
        except ValueError as exc:
            if "closed" in str(exc).lower():
                return 0
            log.exception("<worker>: thesis %r tick failed: %s: %s",
                          thesis_id, type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001 - sleep + retry on any temp failure
            log.exception("<worker>: thesis %r tick failed: %s: %s",
                          thesis_id, type(exc).__name__, exc)
        else:
            if getattr(outcome, "no_op_reason", "") == "closed":
                return 0
            if on_tick is not None:
                try:
                    on_tick(outcome)
                except Exception as exc:  # noqa: BLE001 - reporting never breaks the loop
                    log.exception("<worker>: thesis %r on_tick failed: %s: %s",
                                  thesis_id, type(exc).__name__, exc)
            try:
                if repository.load_thesis(thesis_id).status == "closed":
                    return 0
            except Exception:  # noqa: BLE001 - status re-check is best-effort only
                pass
        stop.wait(interval_seconds)
    return 0
