"""Stable run/request identity helpers.

Run and request identities are generated fresh per run; entity, security,
and document identity derivation lives in ``app/domain/market/ids.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone


def run_id() -> str:
    """New run identity for a research run."""
    return f"run:{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"


def request_id() -> str:
    """New request identity for a research request."""
    return f"req:{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
