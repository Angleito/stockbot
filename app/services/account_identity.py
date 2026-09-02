"""Provider-neutral local account identity mapping (raw broker ids -> opaque local ids)."""

from __future__ import annotations

import hashlib


def local_account_id(account_id: str) -> str:
    """Stable local opaque identifier for a broker account.

    The raw broker account id must never reach persisted data; this
    one-way deterministic mapping keeps per-account grouping and
    round-trip identity in the analytical tables without exposing the
    account to anyone who reads data/.
    """
    digest = hashlib.sha256(
        f"stockbot:local-account:v1:{account_id}".encode("utf-8")
    ).hexdigest()
    return f"local:{digest[:16]}"
