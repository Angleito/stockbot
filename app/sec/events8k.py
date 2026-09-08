"""Item-aware 8-K parser. Pure functions; no network, no LLM."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from .models import CurrentReportEvent

KNOWN_8K_ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate or Increase a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure of Directors or Certain Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

_NORM = {re.sub(r"[\s._\-]+", "", k): k for k in KNOWN_8K_ITEMS}
_EXHIBIT_RE = re.compile(r"EX-\d+(?:\.\d+)?", re.IGNORECASE)


@runtime_checkable
class EightKReport(Protocol):
    """Structural edgartools 8-K report surface consumed here.

    External limitation: edgartools types ``Filing.obj()`` as plain
    ``object`` and exposes item text via ``__getitem__`` rather than a
    Mapping, so callers narrow with ``isinstance`` and item names are read
    with ``getattr`` instead of a static union.
    """

    def __getitem__(self, name: str) -> object: ...

def _normalize_key(key: object) -> str | None:
    s = str(key).lower().strip()
    if s.startswith("item"):
        s = s[4:]
    return _NORM.get(re.sub(r"[\s._\-]+", "", s))


def _exhibit_refs(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _EXHIBIT_RE.findall(text):
        ref = m.upper()
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return tuple(out)


def _event_date_str(event_date: str | date | None) -> str | None:
    """CurrentReportEvent keeps the filer-supplied date as YYYY-MM-DD text."""
    if isinstance(event_date, datetime):
        return event_date.date().isoformat()
    if isinstance(event_date, date):
        return event_date.isoformat()
    return event_date


def parse_8k_events(
    accession_no: str,
    items: Mapping[str, object],
    *,
    event_date: str | date | None = None,
) -> list[CurrentReportEvent]:
    """Raw 8-K item texts at the JSON boundary; non-text values are skipped."""
    as_of_text = _event_date_str(event_date)
    events: list[CurrentReportEvent] = []
    for key, text in items.items():
        number = _normalize_key(key)
        if number is None or not isinstance(text, str) or not text:
            continue
        events.append(
            CurrentReportEvent(
                accession_no=accession_no,
                item_number=number,
                item_name=KNOWN_8K_ITEMS[number],
                event_date=as_of_text,
                text=text,
                exhibit_refs=_exhibit_refs(text),
            )
        )
    return events


def extract_8k_events(
    report: EightKReport,
    accession_no: str,
    *,
    event_date: str | date | None = None,
) -> list[CurrentReportEvent]:
    # ``items`` is a plain list on the SDK object; getattr keeps alternate
    # doubles working without a type-level union.
    raw_items: object = getattr(report, "items", None)
    if callable(raw_items):
        try:
            resolved: object = raw_items()
        except Exception:
            resolved = None
        raw_items = resolved
    if isinstance(raw_items, str) or not isinstance(raw_items, Iterable):
        names: list[str] = []
    else:
        names = [item for item in raw_items if isinstance(item, str)]
    items: dict[str, object] = {}
    for name in names:
        try:
            text = report[name]
        except Exception:
            continue
        if text:
            items[name] = text
    return parse_8k_events(accession_no, items, event_date=event_date)
