"""Reporting-regime detection from filing history (no parsers needed)."""


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def reporting_regime(ticker_or_cik, *, as_of=None) -> dict:
    try:
        filings = list_sec_filings(ticker_or_cik, limit=100, as_of=as_of)
        forms = sorted({getattr(f, "form", "") for f in filings
                        if getattr(f, "form", "")})
    except Exception:
        return {"ticker": str(ticker_or_cik), "regime": "unknown",
                "evidence_forms": []}
    if any(f in ("40-F", "40-F/A") for f in forms):
        regime = "foreign-40F"
    elif any(f in ("20-F", "20-F/A") for f in forms):
        regime = "foreign-20F"
    elif any(f in ("10-K", "10-K/A") for f in forms):
        regime = "domestic"
    else:
        regime = "unknown"
    return {"ticker": str(ticker_or_cik), "regime": regime,
            "evidence_forms": forms}
