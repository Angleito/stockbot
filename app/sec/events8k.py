"""Item-aware 8-K parser. Pure functions; no network, no LLM."""

from __future__ import annotations

import re

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


def _normalize_key(key: str) -> str | None:
    s = key.lower().strip()
    if s.startswith("item"):
        s = s[4:]
    return _NORM.get(re.sub(r"[\s._\-]+", "", s))


def _exhibit_refs(text: str) -> tuple:
    seen, out = set(), []
    for m in _EXHIBIT_RE.findall(text):
        ref = m.upper()
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return tuple(out)


def parse_8k_events(accession_no, items: dict, *, event_date=None) -> list[CurrentReportEvent]:
    events = []
    for key, text in items.items():
        number = _normalize_key(str(key))
        if number is None or not text:
            continue
        events.append(
            CurrentReportEvent(
                accession_no=accession_no,
                item_number=number,
                item_name=KNOWN_8K_ITEMS[number],
                event_date=event_date,
                text=text,
                exhibit_refs=_exhibit_refs(text),
            )
        )
    return events


def extract_8k_events(report, accession_no, *, event_date=None) -> list[CurrentReportEvent]:
    names = report.items() if callable(getattr(report, "items", None)) else (report.items or [])
    items = {}
    for name in names:
        try:
            text = report[name]
        except Exception:
            continue
        if text:
            items[name] = text
    return parse_8k_events(accession_no, items, event_date=event_date)
