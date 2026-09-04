"""Filing-level and context-level SEC answers pending structured follow-ups.

13F holdings detail, FTD ingestion, and the Step-10 proxy/M&A parsers are
deferred; these functions expose what the generic layer can already prove
(filing pointers with accession-level provenance) and mark the rest
explicitly unknown instead of inventing it.
"""

INSTITUTIONAL_FORMS = ("13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A")

GOVERNANCE_FORMS = (
    "DEF 14A", "DEFA14A", "PREC14A", "DEFC14A", "DFAN14A", "PX14A6G",
    "PREM14A", "DEFM14A", "PRE 14C", "DEF 14C",
)

TRANSACTION_FORMS = (
    "SC TO-T", "SC TO-T/A", "SC TO-I", "SC TO-I/A", "SC 14D9", "SC 14D9/A",
    "SC 13E3", "SC 13E3/A", "S-4", "S-4/A", "F-4", "F-4/A",
    "DEFM14A", "PREM14A",
)


def _filing_pointer(filing) -> dict:
    to_dict = getattr(filing, "to_dict", None)
    record = to_dict() if callable(to_dict) else dict(filing)
    return {
        "form": record.get("form"),
        "accession_no": record.get("accession_no"),
        "filed_at": record.get("filed_at"),
        "known_at": record.get("known_at"),
        "report_period": record.get("report_period"),
        "source": record.get("source"),
    }


def _history(ticker_or_cik, forms, *, as_of=None, start_date=None, limit=20) -> list:
    from .filings import list_sec_filings

    try:
        return list_sec_filings(
            ticker_or_cik, forms=list(forms), start_date=start_date,
            as_of=as_of, limit=limit,
        )
    except ValueError:
        raise
    except Exception:
        return []


def get_institutional_ownership(ticker_or_cik, *, as_of=None, limit=10) -> dict:
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


def get_governance_context(ticker_or_cik, *, since=None, as_of=None, limit=10) -> dict:
    """Proxy filing pointers (structured parsing lands with Step-10 parsers)."""
    filings = _history(
        ticker_or_cik, GOVERNANCE_FORMS, as_of=as_of, start_date=since, limit=limit,
    )
    contested = [f for f in filings if (f.to_dict() if hasattr(f, "to_dict") else f).get("form") in ("DFAN14A", "DEFC14A", "PREC14A")]
    return {
        "ticker": str(ticker_or_cik).upper(),
        "since": since,
        "as_of": as_of,
        "count": len(filings),
        "contested_filings": len(contested),
        "filings": [_filing_pointer(f) for f in filings],
        "status": "unknown",
        "note": "Retrieval-level proxy context; contested vs routine is by form family until structured parsers land.",
    }


def get_transaction_context(ticker_or_cik, *, as_of=None, limit=10) -> dict:
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


def get_short_pressure_context(ticker) -> dict:
    """Short-interest context without manipulation claims (FTD deferred).

    Reports FINRA short interest and SEC shares outstanding and their
    deterministic ratio when both are available. Never asserts that short
    activity caused, or will cause, any price move.
    """
    short = None
    try:
        from .. import finra_client

        short = finra_client.get_short_interest(ticker)
    except Exception:
        short = None
    shares = None
    try:
        from ..services import sec_facts

        facts = sec_facts.get_fundamentals(ticker, "shares_outstanding")
        shares = facts.get("shares_outstanding")
    except Exception:
        shares = None
    short_position = None
    if isinstance(short, dict):
        for key in ("short_position", "shortPosition", "short_interest", "current_short_position"):
            value = short.get(key)
            if isinstance(value, (int, float)):
                short_position = value
                break
    ratio = None
    if isinstance(short_position, (int, float)) and isinstance(shares, (int, float)) and shares > 0:
        ratio = round(short_position / shares * 100, 2)
    return {
        "ticker": str(ticker).upper(),
        "short_position": short_position if short_position is not None else "not_available",
        "shares_outstanding": shares if shares is not None else "not_available",
        "short_pct_of_outstanding": ratio if ratio is not None else "not_quantifiable",
        "does_not_assess_manipulation": True,
        "note": "Context only: short interest describes positioning, never manipulation or causation.",
    }
