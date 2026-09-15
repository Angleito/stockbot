"""Restartable thesis monitor worker: a thin sleep loop around ``monitor.tick``.

The worker adds no monitoring logic of its own: every iteration calls
:func:`app.thesis.monitor.tick` (which owns pausing, dedup, checkpointing,
and pending-trigger resume) and then sleeps. Restart resumes solely from
``checkpoint.yaml`` + pending triggers; no in-memory state is carried.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Callable

from app.thesis import monitor
from app.thesis.monitor import SourceService, TickResult
from app.thesis.repository import ThesisRepository

log = logging.getLogger(__name__)


def run_monitor_once(repository: ThesisRepository, thesis_id: str, source_services: Mapping[str, SourceService] | None = None, *,
                     known_at: str | None = None) -> TickResult:
    """One monitor iteration: passthrough to :func:`monitor.tick`."""
    return monitor.tick(repository, thesis_id, source_services, known_at=known_at)


def _loop_known_at(known_at_fn: Callable[[], str] | None) -> str | None:
    """Cutoff for this iteration (None when the loop runs unpinned)."""
    return known_at_fn() if known_at_fn else None


def _tick_failed(thesis_id: str, exc: BaseException) -> bool:
    """Log one failed tick; True when the loop must stop (closed thesis)."""
    if isinstance(exc, ValueError) and "closed" in str(exc).lower():
        return True
    log.exception("<worker>: thesis %r tick failed: %s: %s",
                  thesis_id, type(exc).__name__, exc)
    return False


def _report_tick(thesis_id: str, outcome: TickResult,
                 on_tick: Callable[[TickResult], None] | None) -> None:
    """Deliver one tick outcome; reporting never breaks the loop."""
    if on_tick is None:
        return
    try:
        on_tick(outcome)
    except Exception as exc:  # noqa: BLE001 - reporting never breaks the loop
        log.exception("<worker>: thesis %r on_tick failed: %s: %s",
                      thesis_id, type(exc).__name__, exc)


def _stop_when_closed(repository: ThesisRepository, thesis_id: str, outcome: TickResult) -> bool:
    """True when this tick closed the thesis (marker or live status)."""
    if getattr(outcome, "no_op_reason", "") == "closed":
        return True
    try:
        return repository.load_thesis(thesis_id).status == "closed"
    except Exception:  # noqa: BLE001 - status re-check is best-effort only
        return False


def _run_one_tick(*, repository: ThesisRepository, thesis_id: str,
                  source_services: Mapping[str, SourceService] | None,
                  known_at_fn: Callable[[], str] | None,
                  on_tick: Callable[[TickResult], None] | None) -> bool:
    """One loop iteration; True when the loop must stop."""
    try:
        outcome = run_monitor_once(
            repository, thesis_id, source_services,
            known_at=_loop_known_at(known_at_fn))
    except Exception as exc:  # noqa: BLE001 - sleep + retry on any temp failure
        return _tick_failed(thesis_id, exc)
    _report_tick(thesis_id, outcome, on_tick)
    return _stop_when_closed(repository, thesis_id, outcome)


def monitor_loop(*, repository: ThesisRepository, thesis_id: str, interval_seconds: float = 900,
                 source_services: Mapping[str, SourceService] | None = None,
                 known_at_fn: Callable[[], str] | None = None,
                 stop_event: threading.Event | None = None,
                 on_tick: Callable[[TickResult], None] | None = None) -> int:
    if interval_seconds <= 0:
        raise ValueError(f"<worker>: interval_seconds must be > 0, got {interval_seconds!r}")
    stop = stop_event or threading.Event()
    # ponytail: fixed-interval sleep polling; upgrade to event-driven wakeups only
    # if sub-minute latency or many theses per process ever matter.
    while not stop.is_set():
        if _run_one_tick(repository=repository, thesis_id=thesis_id,
                         source_services=source_services, known_at_fn=known_at_fn,
                         on_tick=on_tick):
            return 0
        stop.wait(interval_seconds)
    return 0
