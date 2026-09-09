"""Reserved official Google Trends API (alpha-pending): no HTTP yet.

``get_interest_over_time`` reserves the future normalization surface —
multi-term comparison, daily/weekly/monthly/yearly intervals, country and
subregion scoping — behind ``GOOGLE_TRENDS_API_ENABLED``. Until alpha
credentials arrive this returns ``disabled`` (flag off) or ``unavailable``
with ``GOOGLE_TRENDS_API_PENDING_ACCESS`` without touching the network. No
endpoint, credential field, or auth is invented here; no ``pytrends`` and no
scraping, ever. When access lands, results normalize into TrendObservation
rows like the BigQuery path.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Optional

try:
    from .. import config as _config
except ImportError:  # pragma: no cover
    try:
        from app import config as _config  # type: ignore
    except ImportError:
        _config = None  # type: ignore

SOURCE = "google_trends_api"
_INTERVALS = ("daily", "weekly", "monthly", "yearly")


def _data_enabled() -> bool:
    fn = getattr(_config, "google_data_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return os.getenv("GOOGLE_DATA_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _api_enabled() -> bool:
    fn = getattr(_config, "google_trends_api_enabled", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:
            return False
    return os.getenv("GOOGLE_TRENDS_API_ENABLED", "").strip().lower() in ("1", "true", "yes")


def get_interest_over_time(*, terms: str | Sequence[str] | None, interval: str = "weekly",
                           start_date: Optional[str] = None,
                           end_date: Optional[str] = None, country: str = "US",
                           subregion: Optional[str] = None) -> dict[str, object]:
    """Reserved interest-over-time lookup; pending alpha access, never networked."""
    if isinstance(terms, str):
        terms = [terms]
    terms = [t for t in (terms or []) if t and t.strip()]
    if not _data_enabled():
        return {"status": "disabled", "source": SOURCE,
                "reason": "google data disabled", "error": "GOOGLE_DATA_DISABLED"}
    if not terms:
        return {"status": "error", "source": SOURCE,
                "error": "at least one term is required", "error_type": "invalid_params"}
    if interval not in _INTERVALS:
        return {"status": "error", "source": SOURCE,
                "error": f"invalid interval: {interval!r} (one of {list(_INTERVALS)})",
                "error_type": "invalid_params"}
    if not _api_enabled():
        return {"status": "disabled", "source": SOURCE,
                "error": "GOOGLE_TRENDS_API_DISABLED"}
    return {"status": "unavailable", "source": SOURCE,
            "error": "GOOGLE_TRENDS_API_PENDING_ACCESS",
            "error_type": "pending_access",
            "coverage": {"terms": [t for t in terms], "interval": interval,
                         "country": country, "subregion": subregion,
                         "start_date": start_date, "end_date": end_date}}
