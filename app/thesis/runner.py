"""Bounded Pi runs over a thesis trigger (stdlib + PyYAML only).

Automated monitoring launches one normal-Pi subprocess per pending trigger
(see app.thesis.pi_runner); Pi persists its own findings through the
canonical thesis tools. The runner only selects the trigger, builds a small
bounded prompt, launches Pi without holding any lock across the subprocess,
and acknowledges the stable trigger ID afterwards. A failed launch leaves
the trigger pending with valid partial tool writes intact (at-least-once
retry; no rollback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import get_data_root
from app.policy import Capability
from app.storage.ids import run_id as new_run_id
from app.thesis.context import build_context
from app.thesis.pi_runner import run_thesis_pi

_GRANTS: dict[str, Capability] = {
    "broker-market-read": Capability.BROKER_MARKET_READ,
    "portfolio-read": Capability.PORTFOLIO_READ,
}


def capabilities_for_grants(grants: list[str]) -> frozenset[Capability]:
    """Map explicit CLI grant strings to capabilities; reject anything else."""
    caps: set[Capability] = set()
    for g in grants or []:
        if g not in _GRANTS:
            raise ValueError(f"<runner>: unknown grant {g!r}; expected one of {sorted(_GRANTS)}")
        caps.add(_GRANTS[g])
    return frozenset(caps)


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    thesis_id: str
    trigger_id: str
    journal_path: str = ""
    evidence_ids: tuple = ()
    processed: bool = False
    tools_used: tuple = field(default_factory=tuple)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_prompt(*, thesis_id: str, trigger: Any, known_at: str) -> str:
    refs = ", ".join(trigger.canonical_refs) or "(none)"
    return "\n".join(
        [
            f"thesis_id: {thesis_id}",
            f"trigger_id: {trigger.trigger_id}",
            f"known_at: {known_at}",
            f"trigger summary: {(trigger.summary or '')[:500]}",
            f"trigger canonical refs: {refs[:500]}",
            "Only use evidence known at or before known_at. Inspect the thesis",
            "with the thesis_show tool and record material findings, supporting",
            "and counterevidence, with the thesis_journal tool. Unknown values",
            "stay unknown.",
        ]
    )


def _fail(run_id: str, exc: Exception) -> None:
    try:
        from app.storage.runs import finalize_failed_run

        finalize_failed_run(run_id, error_type=type(exc).__name__, error_message=str(exc))
    except Exception:
        pass


def run_trigger(
    repository: Any,
    thesis_id: str,
    trigger_id: str,
    *,
    known_at: str | None = None,
) -> RunOutcome:
    """Launch normal Pi for one pending trigger; ack that trigger ID only.

    Failure (bad state, over-budget context, Pi launch/timeout/nonzero)
    raises before acknowledgement, so the trigger stays pending and valid
    partial tool writes are retained for the retry.
    """
    known_at = known_at or _utcnow()
    thesis = repository.load_thesis(thesis_id)
    tid = thesis.thesis_id
    trigger = next(
        (t for t in repository.load_triggers(tid) if t.trigger_id == trigger_id), None
    )
    if trigger is None:
        raise KeyError(f"unknown trigger: {trigger_id!r}")
    if trigger.status != "pending":
        raise ValueError(f"<runner>: trigger {trigger_id!r} is {trigger.status}, not pending")
    # PIT/budget gate: raises before any Pi call when context is over budget.
    build_context(repository, tid, trigger, known_at=known_at)
    root = getattr(repository, "root", None)
    data_root = root.parent if root is not None else get_data_root()
    prompt = _build_prompt(thesis_id=tid, trigger=trigger, known_at=known_at)
    rid = new_run_id()
    try:
        run_thesis_pi(thesis_id=tid, trigger_id=trigger.trigger_id,
                       prompt=prompt, data_root=data_root)
        repository.load_triggers(tid)  # re-read: surface corrupt YAML instead of acking blind
        repository.mark_trigger_processed(tid, trigger.trigger_id, rid)
    except Exception as exc:
        _fail(rid, exc)
        raise
    return RunOutcome(run_id=rid, thesis_id=tid, trigger_id=trigger.trigger_id, processed=True)
