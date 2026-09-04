"""Filing-level discovery. Arbitrary form strings pass straight through to
edgartools with no allowlist; as_of filtering lives here (never leak
filings the market couldn't know yet)."""

import re
from datetime import date

from . import documents
from .client import get_company
from .normalization import filing_from_edgar

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_as_of(as_of):
    if as_of is None:
        return None
    if not isinstance(as_of, str) or not _AS_OF_RE.match(as_of):
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)") from None
    return as_of


def list_sec_filings(
    ticker_or_cik,
    forms=None,
    start_date=None,
    end_date=None,
    as_of=None,
    limit=50,
):
    as_of = _check_as_of(as_of)
    kwargs = {}
    if forms is not None:
        kwargs["form"] = forms
    if start_date is not None or end_date is not None:
        kwargs["filing_date"] = f"{start_date or ''}:{end_date or ''}"
    filings = get_company(ticker_or_cik).get_filings(**kwargs)
    out = [filing_from_edgar(f) for f in filings]
    if as_of is not None:
        out = [x for x in out if x.known_at[:10] <= as_of]
    if limit is not None:
        out = out[:limit]
    return out


def get_sec_filing(accession_no: str):
    try:
        filing = documents.get_by_accession_number(accession_no)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid accession number: {accession_no!r}") from exc
    if filing is None:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    return filing_from_edgar(filing)
