"""Canonical CUSIP/ISIN contract for 13F ingestion and query paths.

Leaf module: depends on nothing in-repo. ``normalize_cusip`` used to live
in ``app.sec.models``; it now lives here and ``models`` re-exports it.
All helpers are never-raising (``None`` on failure).
"""

from __future__ import annotations


def normalize_cusip(value: object) -> str | None:
    """Canonical CUSIP: alphanumerics only, uppercased; None when empty."""
    if value is None:
        return None
    try:
        text = "".join(ch for ch in str(value) if ch.isalnum()).upper()
    except Exception:
        return None
    return text or None


def normalize_isin(value: object) -> str | None:
    """Canonical ISIN: stripped, uppercased; None when empty."""
    if value is None:
        return None
    try:
        text = str(value).strip().upper()
    except Exception:
        return None
    return text or None


def cusip_security_id(value: object) -> str | None:
    """Provisional ``cusip:<CUSIP>`` security id, or None when empty."""
    try:
        canonical = normalize_cusip(value)
    except Exception:
        return None
    return f"cusip:{canonical}" if canonical else None
