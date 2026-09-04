"""M&A transaction normalization: best-effort regex over filing text.

One Transaction per filing; amendments update/diff the same transaction.
Missing values are None/'unknown', never fabricated.
"""

import re
from dataclasses import fields

from .context import TRANSACTION_FORMS
from .models import Transaction

DEAL_TYPE_BY_FORM = {
    "SC TO-T": "tender_offer",
    "SC TO-T/A": "tender_offer",
    "SC TO-I": "tender_offer",
    "SC TO-I/A": "tender_offer",
    "SC 14D9": "tender_offer",
    "SC 14D9/A": "tender_offer",
    "SC 13E3": "going_private",
    "SC 13E3/A": "going_private",
    "S-4": "merger",
    "S-4/A": "merger",
    "F-4": "merger",
    "F-4/A": "merger",
    "DEFM14A": "merger",
    "PREM14A": "merger",
}

_money_pat = r"\$\s?[\d,]+(?:\.\d+)?(?:\s?(?:million|billion))?"
_MONEY = re.compile(_money_pat, re.IGNORECASE)
_PER_SHARE = re.compile(r"\$\s?[\d,.]+\s*per\s+share", re.IGNORECASE)
_EXCHANGE = re.compile(
    r"[\d.]+\s*(?:shares?|for each|per)\s+[^.]{0,40}?shares?",
    re.IGNORECASE)
_TENDER_EXPIRY = re.compile(r"expir(?:ation|es)[^.]{0,120}", re.IGNORECASE)
_TERM_FEE = re.compile(r"termination fee", re.IGNORECASE)


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def load_transaction_text(accession_no: str) -> str:
    """Live seam: raises on failure; callers fall back to form identity."""
    from . import documents

    return documents.get_sec_filing_text(accession_no)


def _termination_fee(text) -> "str | None":
    if not text:
        return None
    fee_spans = [m.span() for m in _TERM_FEE.finditer(text)]
    if not fee_spans:
        return None
    best = None
    best_dist = None
    for m in _MONEY.finditer(text):
        gaps = [0 if m.start() < e and s < m.end()
                else (s - m.end() if m.end() <= s else m.start() - e)
                for s, e in fee_spans]
        dist = min(gaps)
        if best_dist is None or dist < best_dist:
            best, best_dist = m.group(0).strip(), dist
    if best is not None and best_dist is not None and best_dist <= 200:
        return best
    return None


def normalize_transaction(accession_no: str, form: str, *, target: str,
                          buyer=None, announced_at=None, filed_at=None,
                          text=None) -> Transaction:
    deal_type = DEAL_TYPE_BY_FORM.get(form, "unknown")
    consideration = None
    if text:
        per_share = _PER_SHARE.search(text)
        if per_share:
            consideration = per_share.group(0).strip()
        else:
            money = _MONEY.search(text)
            consideration = money.group(0).strip() if money else None
    exchange = _EXCHANGE.search(text) if text else None
    expiry = _TENDER_EXPIRY.search(text) if text else None
    return Transaction(
        event_id=f"{target.upper()}:{deal_type}:{accession_no}",
        target=target,
        buyer=buyer,
        deal_type=deal_type,
        announced_at=announced_at or filed_at,
        consideration=consideration,
        exchange_ratio=exchange.group(0).strip() if exchange else None,
        implied_value=None,
        financing=None,
        termination_fee=_termination_fee(text),
        reverse_termination_fee=None,
        vote_conditions=None,
        regulatory_conditions=None,
        tender_expiry=expiry.group(0).strip() if expiry else None,
        expected_close=None,
        competing_offer=False,
        status="unknown",
        accession_no=accession_no,
        source_accessions=(accession_no,),
    )


def update_transaction(previous: Transaction,
                       current: Transaction) -> Transaction:
    merged = {}
    for f in fields(Transaction):
        name = f.name
        if name == "event_id":
            merged[name] = previous.event_id
        elif name == "target":
            merged[name] = previous.target
        elif name == "buyer":
            merged[name] = (current.buyer if current.buyer is not None
                            else previous.buyer)
        elif name == "source_accessions":
            merged[name] = (tuple(previous.source_accessions)
                            + tuple(a for a in current.source_accessions
                                    if a not in previous.source_accessions))
        elif name == "accession_no":
            merged[name] = current.accession_no or previous.accession_no
        else:
            value = getattr(current, name)
            merged[name] = value if value is not None else getattr(
                previous, name)
    return Transaction(**merged)


def diff_transaction(previous: Transaction, current: Transaction) -> dict:
    out = {}
    for f in fields(Transaction):
        name = f.name
        old, new = getattr(previous, name), getattr(current, name)
        if name == "source_accessions":
            if set(old) != set(new):
                out[name] = [old, new]
        elif old != new:
            out[name] = [old, new]
    return out


def get_transaction_status(ticker_or_cik, *, as_of=None,
                           limit=10) -> list:
    filings = list_sec_filings(ticker_or_cik, forms=list(TRANSACTION_FORMS),
                               as_of=as_of, limit=limit)
    out = []
    for filing in filings:
        accession = getattr(filing, "accession_no", "")
        form = getattr(filing, "form", "")
        filed_at = getattr(filing, "filed_at", None)
        issuer = getattr(filing, "company", None) or str(ticker_or_cik)
        try:
            text = load_transaction_text(accession)
        except Exception:
            text = None
        out.append(normalize_transaction(accession, form,
                                         target=str(issuer).upper(),
                                         filed_at=filed_at, text=text))
    return out
