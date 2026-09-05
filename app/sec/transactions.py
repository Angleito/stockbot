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

# ponytail: fixed small pattern set; broader NLP/LLM extraction is out of scope.
_OFFEROR_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'’\- ]{1,80}?)\s+has\s+commenced\s+a\s+"
    r"(tender|exchange)\s+offer")
_ACQUIRE_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.,'’\- ]{1,80}?)\s+agreed\s+to\s+acquire",
    re.IGNORECASE)
_MERGER_RE = re.compile(
    r"merger\s+(?:with|of)\s+([A-Z][A-Za-z0-9&.,'’\- ]{1,80})",
    re.IGNORECASE)

# Deterministic status evidence only: acceptance, withdrawal, closing
# disclosure, or a terminated offer. Anything else (amendments, expiries,
# commenced offers) stays "unknown".
_STATUS_PATTERNS = (
    ("accepted", re.compile(
        r"accept\w+\s+for\s+payment|acceptance\s+of\s+the\s+offer",
        re.IGNORECASE)),
    ("withdrawn", re.compile(
        r"withdraw\w+(\s+of)?\s+the\s+offer|offer\s+(\w+\s+){0,3}withdrawn",
        re.IGNORECASE)),
    ("terminated", re.compile(
        r"terminat\w+(\s+of)?\s+the\s+(offer|merger|transaction|agreement)",
        re.IGNORECASE)),
    ("completed", re.compile(
        r"merger\s+(\w+\s+){0,3}complet\w+|consummat\w+|closing\s+occurred|"
        r"transaction\s+(\w+\s+){0,3}closed",
        re.IGNORECASE)),
)
_STATUS_VOCABULARY = frozenset(
    {"unknown", "accepted", "completed", "withdrawn", "terminated"})


def _first(obj, *names):
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:
            continue
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:
            continue
        if text:
            return text
    return None


def _str_or_none(value):
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def resolve_transaction_status(*, text=None, obj=None) -> str:
    """Status from deterministic evidence only; default "unknown".

    A structured ``status`` attr wins when it lands in-vocabulary; otherwise
    exact text spans for acceptance/withdrawal/closing/termination decide.
    Amendments never set status, so the form is intentionally ignored.
    """
    try:
        if obj is not None:
            raw = _first(obj, "status", "offer_status", "transaction_status")
            if raw is not None and raw.strip().lower() in _STATUS_VOCABULARY:
                return raw.strip().lower()
        if text:
            body = str(text)
            for status, pattern in _STATUS_PATTERNS:
                if pattern.search(body):
                    return status
    except Exception:
        pass
    return "unknown"


def extract_transaction_parties(obj=None, *, text=None) -> dict:
    """Filer/subject/target/acquirer/offeror/security evidence; never raises.

    Structured header/XML attrs first, then exact document spans. The filer
    is never copied into subject/target: missing evidence stays None with
    method ``form-identity``. Each span records fact, exact text, offsets,
    and method for store/service provenance.
    """
    try:
        parties = {
            "filer_cik": None, "filer_name": None,
            "subject_cik": None, "subject_name": None,
            "target_name": None, "acquirer_cik": None,
            "acquirer_name": None, "offeror": None,
            "security_title": None,
        }
        spans: list = []
        structured = False
        if obj is not None:
            found = {
                "filer_cik": _first(obj, "filer_cik", "filerCIK", "cik"),
                "filer_name": _first(obj, "filer_name", "filerName",
                                     "company", "registrant_name"),
                "subject_cik": _first(obj, "subject_cik", "subjectCIK",
                                      "subject_company_cik", "target_cik"),
                "subject_name": _first(obj, "subject_name", "subjectName",
                                       "subject_company_name", "target_name"),
                "target_name": _first(obj, "target_name", "target",
                                      "subject_name", "subjectName"),
                "acquirer_cik": _first(obj, "acquirer_cik", "bidder_cik",
                                       "offeror_cik"),
                "acquirer_name": _first(obj, "acquirer_name", "acquirer",
                                        "bidder", "buyer", "offeror"),
                "offeror": _first(obj, "offeror", "offeror_name", "bidder"),
                "security_title": _first(obj, "security_title",
                                         "subject_security_title",
                                         "class_title"),
            }
            for key, value in found.items():
                if value is not None:
                    parties[key] = value
                    structured = True
        if text:
            body = str(text)
            for fact, pattern, group in (
                    ("offeror", _OFFEROR_RE, 1),
                    ("acquirer_name", _ACQUIRE_RE, 1),
                    ("target_name", _MERGER_RE, 1)):
                if parties[fact] is not None:
                    continue
                try:
                    match = pattern.search(body)
                except Exception:
                    continue
                if match:
                    value = (match.group(group) or "").strip()
                    if value:
                        parties[fact] = value
                        spans.append({
                            "fact": fact,
                            "text": match.group(0).strip(),
                            "span": f"{match.start()}:{match.end()}",
                            "method": "exact-span",
                        })
        method = ("structured-header" if structured
                  else "exact-span" if spans else "form-identity")
        return {**parties, "spans": spans, "method": method}
    except Exception:
        return {"spans": [], "method": "form-identity"}


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
                          text=None, obj=None, filer_cik=None,
                          filer_name=None, subject_cik=None,
                          subject_name=None, acquirer_cik=None, acquirer=None,
                          offeror=None, security_title=None,
                          document_name=None, known_at=None,
                          source_url=None) -> Transaction:
    parties = extract_transaction_parties(obj, text=text)
    # Explicit args win, structured evidence next, spans last; the filer is
    # never copied into subject/target/acquirer.
    acquirer_name = (_str_or_none(acquirer) or _str_or_none(buyer)
                     or parties.get("acquirer_name"))
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
    method = parties.get("method") or "form-identity"
    if (filer_cik is not None or filer_name is not None
            or subject_cik is not None or subject_name is not None
            or acquirer is not None or buyer is not None):
        method = "structured-header" if obj is not None else method
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
        status=resolve_transaction_status(text=text, obj=obj),
        accession_no=accession_no,
        source_accessions=(accession_no,),
        filer_cik=_str_or_none(filer_cik) or parties.get("filer_cik"),
        filer_name=_str_or_none(filer_name) or parties.get("filer_name"),
        subject_cik=_str_or_none(subject_cik) or parties.get("subject_cik"),
        subject_name=_str_or_none(subject_name) or parties.get("subject_name"),
        acquirer_cik=_str_or_none(acquirer_cik) or parties.get("acquirer_cik"),
        acquirer_name=acquirer_name,
        offeror=_str_or_none(offeror) or parties.get("offeror"),
        security_title=_str_or_none(security_title)
        or parties.get("security_title"),
        document_name=_str_or_none(document_name),
        known_at=_str_or_none(known_at) or filed_at,
        source_url=_str_or_none(source_url),
        extraction_method=method,
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
        filer_name = getattr(filing, "filer_name", None)
        filer_cik = getattr(filing, "filer_cik", None)
        subject_name = getattr(filing, "subject_name", None)
        subject_cik = getattr(filing, "subject_cik", None)
        # Subject (target) identity comes only from structured filing
        # metadata; the filer is never copied into it.
        issuer = subject_name or filer_name or str(ticker_or_cik)
        try:
            text = load_transaction_text(accession)
        except Exception:
            text = None
        out.append(normalize_transaction(
            accession, form, target=str(issuer).upper(), filed_at=filed_at,
            text=text, filer_cik=filer_cik, filer_name=filer_name,
            subject_cik=subject_cik, subject_name=subject_name))
    return out


def query_target_transactions(target, *, as_of=None, root=None, limit=200):
    """Target -> transactions over ``sec_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_transactions(target=target, as_of=as_of, root=root,
                                     limit=limit)


def query_acquirer_transactions(acquirer, *, as_of=None, root=None,
                                limit=200):
    """Acquirer -> transactions over ``sec_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_transactions(acquirer=acquirer, as_of=as_of,
                                     root=root, limit=limit)
