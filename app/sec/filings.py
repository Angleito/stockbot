"""Filing-level discovery. Arbitrary form strings pass straight through to
edgartools with no allowlist; as_of filtering lives here (never leak
filings the market couldn't know yet)."""

import re
from datetime import date, datetime

from . import documents
from .client import get_company
from .models import Filing, pit_of
from .normalization import filing_from_edgar

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_as_of(as_of: str | date | datetime | None) -> str | None:
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        as_of = as_of.date().isoformat()
    elif isinstance(as_of, date):
        as_of = as_of.isoformat()
    if not isinstance(as_of, str) or not _AS_OF_RE.match(as_of):
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(as_of)
    except ValueError:
        raise ValueError(f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)") from None
    return as_of


def _date_str(value: str | date | datetime) -> str:
    """edgartools filing-date ranges take YYYY-MM-DD text; normalize date/datetime inputs."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def list_sec_filings(
    ticker_or_cik: str | int,
    forms: str | list[str] | tuple[str, ...] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    as_of: str | date | datetime | None = None,
    limit: int | None = 50,
) -> list[Filing]:
    as_of = _check_as_of(as_of)
    form_arg: str | list[str] | None = None
    if forms is not None:
        form_arg = forms if isinstance(forms, str) else list(forms)
    filing_date_arg: str | None = None
    if start_date is not None or end_date is not None:
        # edgartools rejects open-ended ranges ("2026-09-07:"); close them:
        # missing start means archive beginning, missing end means as_of/today.
        end = _date_str(end_date or as_of or date.today().isoformat())
        filing_date_arg = f"{_date_str(start_date) if start_date else '1994-01-01'}:{end}"
    filings = get_company(ticker_or_cik).get_filings(
        form=form_arg, filing_date=filing_date_arg)
    out = [filing_from_edgar(f) for f in filings]
    if as_of is not None:
        kept: list[Filing] = []
        for x in out:
            value, _basis = pit_of(x)
            if value is not None and value[:10] <= as_of:
                kept.append(x)
        out = kept
    if limit is not None:
        out = out[:limit]
    return out


def get_sec_filing(
    accession_no: str,
    as_of: str | date | datetime | None = None,
) -> Filing:
    """Exact accession lookup; hydrates filing metadata first so a future
    accession cannot bypass PIT."""
    as_of = _check_as_of(as_of)
    try:
        filing = documents.get_by_accession_number(accession_no)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid accession number: {accession_no!r}") from exc
    if filing is None:
        raise ValueError(f"invalid accession number: {accession_no!r}")
    out = filing_from_edgar(filing)
    if as_of is not None:
        value, _basis = pit_of(out)
        if value is None or value[:10] > as_of:
            raise ValueError(
                f"filing {accession_no!r} not known as of {as_of!r}")
    return out
