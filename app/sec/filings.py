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


def _forms_arg(forms: str | list[str] | tuple[str, ...] | None) -> str | list[str] | None:
    if forms is None:
        return None
    return forms if isinstance(forms, str) else list(forms)


def _filing_date_arg(start_date: str | date | datetime | None,
                     end_date: str | date | datetime | None,
                     as_of: str | None) -> str | None:
    if start_date is None and end_date is None:
        return None
    # edgartools rejects open-ended ranges ("2026-09-07:"); close them:
    # missing start means archive beginning, missing end means as_of/today.
    end = _date_str(end_date or as_of or date.today().isoformat())
    return f"{_date_str(start_date) if start_date else '1994-01-01'}:{end}"


def _known_as_of(filing: Filing, as_of: str | None) -> bool:
    if as_of is None:
        return True
    value, _basis = pit_of(filing)
    return value is not None and value[:10] <= as_of


def resolve_latest_filing(
    filings: list[Filing],
    form: str = "10-K",
    as_of: str | date | datetime | None = None,
) -> Filing | None:
    """Latest PIT-eligible filing of one form family (10-K/10-Q/8-K + /A); None when absent.

    No-date questions pin this accession first, then check the latest 10-Q +
    relevant 8-Ks; historical as_of/range questions may use older filings.
    """
    bound = _check_as_of(as_of)
    want = form.strip().upper()
    family = {want, f"{want}/A"} if not want.endswith("/A") else {want}
    best: Filing | None = None
    best_day = ""
    for filing in filings:
        if filing.form.strip().upper() not in family:
            continue
        if not _known_as_of(filing, bound):
            continue
        value, _basis = pit_of(filing)
        day = value[:10] if value is not None else filing.filed_at
        if best is None or day > best_day:
            best, best_day = filing, day
    return best


def list_sec_filings(
    ticker_or_cik: str | int,
    forms: str | list[str] | tuple[str, ...] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    as_of: str | date | datetime | None = None,
    limit: int | None = 50,
) -> list[Filing]:
    as_of = _check_as_of(as_of)
    filings_raw = get_company(ticker_or_cik).get_filings(
        form=_forms_arg(forms), filing_date=_filing_date_arg(start_date, end_date, as_of))
    out: list[Filing] = []
    for f in filings_raw:
        if limit is not None and len(out) >= limit:
            break
        x = filing_from_edgar(f)
        if _known_as_of(x, as_of):
            out.append(x)
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
