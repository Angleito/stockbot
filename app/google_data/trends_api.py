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

from ._lazy_config import google_data_enabled

SOURCE = "google_trends_api"
_INTERVALS = ("daily", "weekly", "monthly", "yearly")


def _data_enabled() -> bool:
    return google_data_enabled()


def _api_enabled() -> bool:
    try:
        from .. import config as _cfg
    except ImportError:
        _cfg = None
    if _cfg is not None:
        try:
            return _cfg.google_trends_api_enabled()
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return False
    return os.getenv("GOOGLE_TRENDS_API_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _normalize_terms(terms: str | Sequence[str] | None) -> list[str]:
    """Caller terms as a stripped non-empty list; never None."""
    if isinstance(terms, str):
        terms = [terms]
    return [t for t in (terms or []) if t and t.strip()]


def _terms_interval_error(terms: list[str], interval: str) -> dict[str, object] | None:
    """Invalid-params error for empty terms or unknown interval, else None."""
    if not terms:
        return {
            "status": "error",
            "source": SOURCE,
            "error": "at least one term is required",
            "error_type": "invalid_params",
        }
    if interval not in _INTERVALS:
        return {
            "status": "error",
            "source": SOURCE,
            "error": f"invalid interval: {interval!r} (one of {list(_INTERVALS)})",
            "error_type": "invalid_params",
        }
    return None


def _pending_coverage(
    terms: list[str], interval: str, start_date: str | None, end_date: str | None, country: str, subregion: str | None
) -> dict[str, object]:
    """Alpha-pending unavailable payload; no network, fixed shape."""
    return {
        "status": "unavailable",
        "source": SOURCE,
        "error": "GOOGLE_TRENDS_API_PENDING_ACCESS",
        "error_type": "pending_access",
        "coverage": {
            "terms": [t for t in terms],
            "interval": interval,
            "country": country,
            "subregion": subregion,
            "start_date": start_date,
            "end_date": end_date,
        },
    }


def get_interest_over_time(
    *,
    terms: str | Sequence[str] | None,
    interval: str = "weekly",
    start_date: str | None = None,
    end_date: str | None = None,
    country: str = "US",
    subregion: str | None = None,
) -> dict[str, object]:
    """Reserved interest-over-time lookup; pending alpha access, never networked."""
    norm = _normalize_terms(terms)
    if not _data_enabled():
        return {
            "status": "disabled",
            "source": SOURCE,
            "reason": "google data disabled",
            "error": "GOOGLE_DATA_DISABLED",
        }
    err = _terms_interval_error(norm, interval)
    if err is not None:
        return err
    if not _api_enabled():
        return {"status": "disabled", "source": SOURCE, "error": "GOOGLE_TRENDS_API_DISABLED"}
    return _pending_coverage(norm, interval, start_date, end_date, country, subregion)
