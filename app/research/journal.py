"""Append-only journal: in-memory log with per-session 1-based sequences.

Persistence lives in repository.py (save_event/list_events); resume
rehydrates this log via hydrate() so sequences never restart (no-dup).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime

from .models import JournalEvent, JSONValue, new_event_id, utcnow, validate_json_mapping

__all__ = [
    "EVIDENCE_REJECTED",
    "append_event",
    "hydrate",
    "list_events",
    "next_sequence",
    "rejection_payload",
]

# Journal event type for point-in-time rejections (known_at > as_of).
EVIDENCE_REJECTED = "evidence.rejected"

_LOG: dict[str, list[JournalEvent]] = {}
_LOCK = threading.Lock()


def next_sequence(session_id: str) -> int:
    """Next 1-based sequence for this session (1 when the log is empty)."""
    with _LOCK:
        events = _LOG.get(session_id, [])
        return (max((e.sequence for e in events), default=0)) + 1


def append_event(
    session_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    payload: Mapping[str, object] | None = None,
    previous_state: str | None = None,
    new_state: str | None = None,
    *,
    event_id: str | None = None,
    timestamp: datetime | None = None,
) -> JournalEvent:
    """Build, validate, append, and return one event. Never mutates history."""
    if not session_id:
        raise ValueError("<journal>: 'session_id' must be a non-empty string")
    if not event_type:
        raise ValueError("<journal>: 'event_type' must be a non-empty string")
    if not actor_type:
        raise ValueError("<journal>: 'actor_type' must be a non-empty string")
    if not actor_id:
        raise ValueError("<journal>: 'actor_id' must be a non-empty string")
    with _LOCK:
        events = _LOG.setdefault(session_id, [])
        sequence = (max((e.sequence for e in events), default=0)) + 1
        event = JournalEvent(
            event_id=event_id or new_event_id(),
            session_id=session_id,
            sequence=sequence,
            event_type=event_type,
            timestamp=timestamp or utcnow(),
            actor_type=actor_type,
            actor_id=actor_id,
            payload=validate_json_mapping(payload or {}, "<journal>: 'payload'"),
            previous_state=previous_state,
            new_state=new_state,
        )
        event.validate("<journal>")
        if any(e.event_id == event.event_id for e in events):
            raise ValueError(f"<journal>: duplicate event_id {event.event_id!r}")
        events.append(event)
        return event


def list_events(session_id: str, *, event_type: str | None = None) -> list[JournalEvent]:
    """Session events in sequence order, optionally filtered by type."""
    with _LOCK:
        events = list(_LOG.get(session_id, []))
    if event_type is not None:
        events = [e for e in events if e.event_type == event_type]
    return events


def _seq(event: JournalEvent) -> int:
    return event.sequence


def hydrate(session_id: str, events: list[JournalEvent]) -> None:
    """Replace the in-memory log (resume path only); validates ordering."""
    ordered = sorted(events, key=_seq)
    for i, event in enumerate(ordered, start=1):
        if event.session_id != session_id:
            raise ValueError(f"<journal>: event {event.event_id!r} belongs to {event.session_id!r}")
        if event.sequence != i:
            raise ValueError(f"<journal>: gap in {session_id!r} log at sequence {i}")
        event.validate("<journal>")
    with _LOCK:
        _LOG[session_id] = ordered


def rejection_payload(evidence_id: str, reason: str, known_at: str | None, as_of: str | None) -> dict[str, JSONValue]:
    """Payload for evidence.rejected: {evidence_id, reason, known_at, as_of} (ISO strings)."""
    if not evidence_id:
        raise ValueError("<journal>: 'evidence_id' must be a non-empty string")
    if not reason:
        raise ValueError("<journal>: 'reason' must be a non-empty string")
    return {"evidence_id": evidence_id, "reason": reason, "known_at": known_at, "as_of": as_of}
