"""8-K items -> RegulatoryEvent + 'what changed since <date>' query.

Pure mapping except for two live seams (`list_sec_filings`, `load_report`);
sibling imports stay inside functions so `app.sec` can re-export this module
without import cycles.
"""

from __future__ import annotations

import re
from datetime import date

EIGHT_K_ITEM_EVENTS = {
    "1.01": "material_agreement",
    "1.02": "material_agreement",
    "1.03": "bankruptcy",
    "1.05": "cybersecurity_incident",
    "2.01": "acquisition",
    "2.02": "earnings",
    "2.03": "debt_issuance",
    "2.04": "default",
    "2.05": "restructuring",
    "2.06": "impairment",
    "3.01": "delisting_notice",
    "3.02": "equity_issuance",
    "4.01": "auditor_change",
    "4.02": "restatement",
    "5.01": "change_of_control",
    "5.02": "management_change",
    "5.07": "shareholder_vote",
}

SEVERITY = {
    "bankruptcy": "critical",
    "default": "critical",
    "earnings": "notable",
    "acquisition": "notable",
    "change_of_control": "notable",
    "restatement": "notable",
    "delisting_notice": "notable",
    "debt_issuance": "notable",
}

_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def material_events_from_8k(accession_no, eight_k_events, *, issuer, event_date=None,
                             known_at=None):
    from .models import RegulatoryEvent

    out = []
    for event in eight_k_events:
        event_type = EIGHT_K_ITEM_EVENTS.get(event.item_number)
        if event_type is None:
            continue
        effective = event.event_date or event_date
        out.append(RegulatoryEvent(
            event_id=f"{accession_no}:{event.item_number}",
            issuer=issuer,
            event_type=event_type,
            effective_date=effective,
            known_at=known_at or effective or "unknown",
            source_accessions=(accession_no,),
            severity=SEVERITY.get(event_type, "routine"),
            structured_data={"item_number": event.item_number,
                             "item_name": event.item_name},
        ))
    return out


def load_report(accession_no):
    """Live seam: accession -> (report, event_date, known_at). Raises on failure."""
    from .documents import get_by_accession_number

    filing = get_by_accession_number(accession_no)
    report = filing.obj()
    raw_date = getattr(report, "date_of_report", None) or getattr(filing, "filing_date", None)
    event_date = str(raw_date) if raw_date is not None else None
    raw_known = (getattr(filing, "acceptance_datetime", None)
                 or getattr(filing, "accepted_at", None)
                 or getattr(filing, "filing_date", None))
    if raw_known is None:
        raise ValueError(f"no known_at for accession {accession_no!r}")
    return report, event_date, str(raw_known)


def get_material_events(ticker_or_cik, since, *, as_of=None, limit=50):
    if not isinstance(since, str) or not _SINCE_RE.match(since):
        raise ValueError(f"invalid since date: {since!r} (expected YYYY-MM-DD)")
    try:
        date.fromisoformat(since)
    except ValueError:
        raise ValueError(f"invalid since date: {since!r} (expected YYYY-MM-DD)") from None
    from .events8k import extract_8k_events
    from .filings import list_sec_filings

    out = []
    filings = list_sec_filings(ticker_or_cik, forms=["8-K", "8-K/A"],
                               start_date=since, as_of=as_of, limit=limit)
    for filing in filings:
        try:
            report, event_date, known_at = load_report(filing.accession_no)
        except Exception:
            continue
        out.extend(material_events_from_8k(
            filing.accession_no,
            extract_8k_events(report, filing.accession_no, event_date=event_date),
            issuer=filing.filer_name, event_date=event_date, known_at=known_at))
    out.sort(key=lambda e: (e.effective_date or e.known_at, e.event_id))
    return out
