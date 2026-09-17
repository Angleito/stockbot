"""Filing-level and context-level SEC answers pending structured follow-ups.

13F holdings detail, FTD ingestion, and the Step-10 proxy/M&A parsers are
deferred; these functions expose what the generic layer can already prove
(filing pointers with accession-level provenance) and mark the rest
explicitly unknown instead of inventing it.
"""

from collections.abc import Mapping

from .models import Filing

INSTITUTIONAL_FORMS = ("13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A")

GOVERNANCE_FORMS = (
    "DEF 14A",
    "DEFA14A",
    "PREC14A",
    "DEFC14A",
    "DFAN14A",
    "PX14A6G",
    "PREM14A",
    "DEFM14A",
    "PRE 14C",
    "DEF 14C",
)

TRANSACTION_FORMS = (
    "SC TO-T",
    "SC TO-T/A",
    "SC TO-I",
    "SC TO-I/A",
    "SC 14D9",
    "SC 14D9/A",
    "SC 13E3",
    "SC 13E3/A",
    "S-4",
    "S-4/A",
    "F-4",
    "F-4/A",
    "DEFM14A",
    "PREM14A",
)


def _filing_pointer(filing: Filing | Mapping[str, object]) -> dict[str, object]:
    if isinstance(filing, Mapping):
        record: Mapping[str, object] = filing
    else:
        record = filing.to_dict()
    return {
        "form": record.get("form"),
        "accession_no": record.get("accession_no"),
        "filed_at": record.get("filed_at"),
        "known_at": record.get("known_at"),
        "report_period": record.get("report_period"),
        "source": record.get("source"),
    }


def _history(
    ticker_or_cik: str | int,
    forms: tuple[str, ...] | list[str],
    *,
    as_of: str | None = None,
    start_date: str | None = None,
    limit: int | None = 20,
) -> list[Filing]:
    from .filings import list_sec_filings

    try:
        return list_sec_filings(
            ticker_or_cik,
            forms=list(forms),
            start_date=start_date,
            as_of=as_of,
            limit=limit,
        )
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def get_institutional_ownership(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 10,
) -> dict[str, object]:
    """These are the issuer's own 13F-HR/13F-NT filings, not the set of managers holding the issuer; ticker-to-holders position lookup is deferred."""
    filings = _history(ticker_or_cik, INSTITUTIONAL_FORMS, as_of=as_of, limit=limit)
    return {
        "ticker": str(ticker_or_cik).upper(),
        "as_of": as_of,
        "filing_level_only": True,
        "count": len(filings),
        "filings": [_filing_pointer(f) for f in filings],
        "note": "These are the issuer's own 13F-HR/13F-NT filings, not the set of managers holding the issuer; ticker-to-holders position lookup is deferred.",
    }


_CONTESTED_PROXY_FORMS = frozenset({"DFAN14A", "DEFC14A", "PREC14A"})


def _count_contested(filings: list[Filing]) -> int:
    """Contested-proxy filings by form family (structured parsing deferred)."""
    return sum(1 for f in filings if f.form in _CONTESTED_PROXY_FORMS)


def get_governance_context(
    ticker_or_cik: str | int,
    *,
    since: str | None = None,
    as_of: str | None = None,
    limit: int | None = 10,
) -> dict[str, object]:
    """Proxy filing pointers (structured parsing lands with Step-10 parsers)."""
    filings = _history(
        ticker_or_cik,
        GOVERNANCE_FORMS,
        as_of=as_of,
        start_date=since,
        limit=limit,
    )
    return {
        "ticker": str(ticker_or_cik).upper(),
        "since": since,
        "as_of": as_of,
        "count": len(filings),
        "contested_filings": _count_contested(filings),
        "filings": [_filing_pointer(f) for f in filings],
        "status": "unknown",
        "note": "Retrieval-level proxy context; contested vs routine is by form family until structured parsers land.",
    }


def get_transaction_context(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 10,
) -> dict[str, object]:
    """M&A filing pointers; deal status unknown until Step-10 parsers."""
    filings = _history(ticker_or_cik, TRANSACTION_FORMS, as_of=as_of, limit=limit)
    return {
        "ticker": str(ticker_or_cik).upper(),
        "as_of": as_of,
        "count": len(filings),
        "filings": [_filing_pointer(f) for f in filings],
        "status": "unknown",
        "note": "Deal terms and status need the Step-10 transaction parser; use get_sec_document on a filing for its text.",
    }


_SHORT_POSITION_KEYS = (
    "short_position",
    "shortPosition",
    "short_interest",
    "current_short_position",
)


def _fetch_short_position(ticker: str) -> dict[str, object] | None:
    """FINRA short interest or None; transport failure is missing data."""
    try:
        from .. import finra_client

        short = finra_client.get_short_interest(ticker)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return short if isinstance(short, dict) else None


def _fetch_shares_outstanding(ticker: str) -> object:
    """SEC shares outstanding value or None; failure is missing data."""
    try:
        from ..services import sec_facts

        return sec_facts.get_fundamentals(ticker, "shares_outstanding").get("shares_outstanding")
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _extract_short_position(short: dict[str, object] | None) -> int | float | None:
    """First numeric short-position key; None when absent or non-numeric."""
    if not isinstance(short, dict):
        return None
    for key in _SHORT_POSITION_KEYS:
        value = short.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _short_ratio(
    short_position: float | None,
    shares: object,
) -> float | None:
    """Deterministic short/outstanding percent; None unless both quantify."""
    if not isinstance(short_position, (int, float)):
        return None
    if not isinstance(shares, (int, float)) or shares <= 0:
        return None
    return round(short_position / shares * 100, 2)


def get_short_pressure_context(ticker: str) -> dict[str, object]:
    """Short-interest context without manipulation claims (FTD deferred).

    Reports FINRA short interest and SEC shares outstanding and their
    deterministic ratio when both are available. Never asserts that short
    activity causes, or will cause, any price move.
    """
    short = _fetch_short_position(ticker)
    shares = _fetch_shares_outstanding(ticker)
    short_position = _extract_short_position(short)
    ratio = _short_ratio(short_position, shares)
    return {
        "ticker": ticker.upper(),
        "short_position": short_position if short_position is not None else "not_available",
        "shares_outstanding": shares if shares is not None else "not_available",
        "short_pct_of_outstanding": ratio if ratio is not None else "not_quantifiable",
        "does_not_assess_manipulation": True,
        "note": "Context only: short interest describes positioning, never manipulation or causation.",
    }
