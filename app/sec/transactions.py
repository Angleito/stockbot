"""M&A transaction normalization: best-effort regex over filing text.

One Transaction per filing; amendments update/diff the same transaction.
Missing values are None/'unknown', never fabricated.
"""

import re
from dataclasses import fields, replace
from datetime import date, datetime
from pathlib import Path

from .context import TRANSACTION_FORMS
from .models import Filing, Transaction

# `object` marks the edgar SDK dynamic boundary (no stubs): attrs are read
# via getattr/_first and validated before building Transaction objects.

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


def _first(obj: object, *names: str) -> str | None:
    for name in names:
        try:
            value = getattr(obj, name, None)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            continue
        if value is None:
            continue
        try:
            text = str(value).strip()
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            continue
        if text:
            return text
    return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return text or None


def _structured_status_of(obj: object) -> str | None:
    raw = _first(obj, "status", "offer_status", "transaction_status")
    if raw is not None and raw.strip().lower() in _STATUS_VOCABULARY:
        return raw.strip().lower()
    return None


def _text_status_of(text: str) -> str | None:
    for status, pattern in _STATUS_PATTERNS:
        if pattern.search(text):
            return status
    return None


def resolve_transaction_status(*, text: str | None = None, obj: object | None = None) -> str:
    """Status from deterministic evidence only; default "unknown".

    A structured ``status`` attr wins when it lands in-vocabulary; otherwise
    exact text spans for acceptance/withdrawal/closing/termination decide.
    Amendments never set status, so the form is intentionally ignored.
    """
    try:
        if obj is not None:
            found = _structured_status_of(obj)
            if found is not None:
                return found
        if text:
            found = _text_status_of(text)
            if found is not None:
                return found
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
        pass
    return "unknown"


_PARTY_ATTRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("filer_cik", ("filer_cik", "filerCIK", "cik")),
    ("filer_name", ("filer_name", "filerName", "company", "registrant_name")),
    ("subject_cik", ("subject_cik", "subjectCIK", "subject_company_cik", "target_cik")),
    ("subject_name", ("subject_name", "subjectName", "subject_company_name", "target_name")),
    ("target_name", ("target_name", "target", "subject_name", "subjectName")),
    ("acquirer_cik", ("acquirer_cik", "bidder_cik", "offeror_cik")),
    ("acquirer_name", ("acquirer_name", "acquirer", "bidder", "buyer", "offeror")),
    ("offeror", ("offeror", "offeror_name", "bidder")),
    ("security_title", ("security_title", "subject_security_title", "class_title")),
)

_PARTY_SPAN_PATTERNS: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("offeror", _OFFEROR_RE, 1),
    ("acquirer_name", _ACQUIRE_RE, 1),
    ("target_name", _MERGER_RE, 1),
)


def _structured_parties_of(obj: object) -> tuple[dict[str, object], bool]:
    parties: dict[str, object] = {
        "filer_cik": None, "filer_name": None,
        "subject_cik": None, "subject_name": None,
        "target_name": None, "acquirer_cik": None,
        "acquirer_name": None, "offeror": None,
        "security_title": None,
    }
    structured = False
    for key, attrs in _PARTY_ATTRS:
        value = _first(obj, *attrs)
        if value is not None:
            parties[key] = value
            structured = True
    return parties, structured


def _span_parties_of(parties: dict[str, object], text: str) -> list[dict[str, str]]:
    spans: list[dict[str, str]] = []
    for fact, pattern, group in _PARTY_SPAN_PATTERNS:
        if parties[fact] is not None:
            continue
        try:
            match = pattern.search(text)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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
    return spans


def _empty_parties() -> dict[str, object]:
    return {
        "filer_cik": None, "filer_name": None,
        "subject_cik": None, "subject_name": None,
        "target_name": None, "acquirer_cik": None,
        "acquirer_name": None, "offeror": None,
        "security_title": None,
    }


def extract_transaction_parties(obj: object | None = None, *, text: str | None = None) -> dict[str, object]:
    """Filer/subject/target/acquirer/offeror/security evidence; never raises.

    Structured header/XML attrs first, then exact document spans. The filer
    is never copied into subject/target: missing evidence stays None with
    method ``form-identity``. Each span records fact, exact text, offsets,
    and method for store/service provenance.
    """
    try:
        parties, structured = (_structured_parties_of(obj) if obj is not None
                               else (_empty_parties(), False))
        spans: list[dict[str, str]] = _span_parties_of(parties, text) if text else []
        method = ("structured-header" if structured
                  else "exact-span" if spans else "form-identity")
        return {**parties, "spans": spans, "method": method}
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return {"spans": [], "method": "form-identity"}
def list_sec_filings(ticker_or_cik: str | int,
                     forms: str | list[str] | tuple[str, ...] | None = None,
                     start_date: str | date | datetime | None = None,
                     end_date: str | date | datetime | None = None,
                     as_of: str | date | datetime | None = None,
                     limit: int | None = 50) -> list[Filing]:
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(ticker_or_cik, forms=forms, start_date=start_date,
                 end_date=end_date, as_of=as_of, limit=limit)


def load_transaction_text(accession_no: str) -> str:
    """Live seam: raises on failure; callers fall back to form identity."""
    from . import documents

    return documents.get_sec_filing_text(accession_no)


def _money_fee_gap(m: re.Match[str], fee_spans: list[tuple[int, int]]) -> int:
    gaps = [0 if m.start() < e and s < m.end()
            else (s - m.end() if m.end() <= s else m.start() - e)
            for s, e in fee_spans]
    return min(gaps)


def _nearest_money_to_fee(text: str, fee_spans: list[tuple[int, int]]) -> str | None:
    best: str | None = None
    best_dist: int | None = None
    for m in _MONEY.finditer(text):
        dist = _money_fee_gap(m, fee_spans)
        if best_dist is None or dist < best_dist:
            best, best_dist = m.group(0).strip(), dist
    if best is not None and best_dist is not None and best_dist <= 200:
        return best
    return None


def _termination_fee(text: str | None) -> str | None:
    if not text:
        return None
    fee_spans = [m.span() for m in _TERM_FEE.finditer(text)]
    if not fee_spans:
        return None
    return _nearest_money_to_fee(text, fee_spans)


def _fallback_target_of(parties: dict[str, object]) -> str | None:
    return (_str_or_none(parties.get("subject_name"))
            or _str_or_none(parties.get("target_name")))


def _resolve_deal_target(target: str, subject_name: str | None,
                         parties: dict[str, object]) -> str:
    return (_str_or_none(target) or _str_or_none(subject_name)
            or _fallback_target_of(parties) or "")


def _consideration_of(text: str | None) -> str | None:
    if not text:
        return None
    per_share = _PER_SHARE.search(text)
    if per_share:
        return per_share.group(0).strip()
    money = _MONEY.search(text)
    return money.group(0).strip() if money else None


def _extraction_method_of(parties: dict[str, object], obj: object | None, *,
                          filer_cik: str | int | None, filer_name: str | None,
                          subject_cik: str | int | None, subject_name: str | None,
                          acquirer: object, buyer: object) -> str:
    method = _str_or_none(parties.get("method")) or "form-identity"
    if (filer_cik is not None or filer_name is not None
            or subject_cik is not None or subject_name is not None
            or acquirer is not None or buyer is not None):
        method = "structured-header" if obj is not None else method
    return method


def _resolve_acquirer_name(acquirer: object, buyer: object,
                           parties: dict[str, object]) -> str | None:
    # Explicit args win, structured evidence next, spans last; the filer is
    # never copied into subject/target/acquirer.
    return (_str_or_none(acquirer) or _str_or_none(buyer)
            or _str_or_none(parties.get("acquirer_name")))


def _deal_text_spans(text: str | None) -> tuple[str | None, str | None]:
    exchange = _EXCHANGE.search(text) if text else None
    expiry = _TENDER_EXPIRY.search(text) if text else None
    return (exchange.group(0).strip() if exchange else None,
            expiry.group(0).strip() if expiry else None)


def _party_or_arg(explicit: object, parties: dict[str, object], key: str) -> str | None:
    return _str_or_none(explicit) or _str_or_none(parties.get(key))


def _identity_block(*, filer_cik: object, filer_name: object, subject_cik: object,
                    subject_name: object, acquirer_cik: object, acquirer_name: str | None,
                    offeror: object, security_title: object,
                    parties: dict[str, object]) -> dict[str, str | None]:
    return {
        "filer_cik": _party_or_arg(filer_cik, parties, "filer_cik"),
        "filer_name": _party_or_arg(filer_name, parties, "filer_name"),
        "subject_cik": _party_or_arg(subject_cik, parties, "subject_cik"),
        "subject_name": _party_or_arg(subject_name, parties, "subject_name"),
        "acquirer_cik": _party_or_arg(acquirer_cik, parties, "acquirer_cik"),
        "acquirer_name": acquirer_name,
        "offeror": _party_or_arg(offeror, parties, "offeror"),
        "security_title": _party_or_arg(security_title, parties, "security_title"),
    }


def normalize_transaction(accession_no: str, form: str, *, target: str,
                          buyer: object = None, announced_at: str | None = None,
                          filed_at: str | None = None, text: str | None = None,
                          obj: object | None = None,
                          filer_cik: str | int | None = None,
                          filer_name: str | None = None,
                          subject_cik: str | int | None = None,
                          subject_name: str | None = None,
                          acquirer_cik: str | int | None = None,
                          acquirer: object = None, offeror: object = None,
                          security_title: object = None,
                          document_name: str | None = None,
                          known_at: str | None = None,
                          source_url: str | None = None) -> Transaction:
    parties = extract_transaction_parties(obj, text=text)
    resolved_target = _resolve_deal_target(target, subject_name, parties)
    acquirer_name = _resolve_acquirer_name(acquirer, buyer, parties)
    deal_type = DEAL_TYPE_BY_FORM.get(form, "unknown")
    exchange_ratio, tender_expiry = _deal_text_spans(text)
    ids = _identity_block(filer_cik=filer_cik, filer_name=filer_name,
                          subject_cik=subject_cik, subject_name=subject_name,
                          acquirer_cik=acquirer_cik, acquirer_name=acquirer_name,
                          offeror=offeror, security_title=security_title,
                          parties=parties)
    return Transaction(
        event_id=f"{resolved_target.upper()}:{deal_type}:{accession_no}",
        target=resolved_target,
        buyer=_str_or_none(buyer),
        deal_type=deal_type,
        announced_at=announced_at or filed_at,
        consideration=_consideration_of(text),
        exchange_ratio=exchange_ratio,
        implied_value=None,
        financing=None,
        termination_fee=_termination_fee(text),
        reverse_termination_fee=None,
        vote_conditions=None,
        regulatory_conditions=None,
        tender_expiry=tender_expiry,
        expected_close=None,
        competing_offer=False,
        status=resolve_transaction_status(text=text, obj=obj),
        accession_no=accession_no,
        source_accessions=(accession_no,),
        filer_cik=ids["filer_cik"],
        filer_name=ids["filer_name"],
        subject_cik=ids["subject_cik"],
        subject_name=ids["subject_name"],
        acquirer_cik=ids["acquirer_cik"],
        acquirer_name=ids["acquirer_name"],
        offeror=ids["offeror"],
        security_title=ids["security_title"],
        document_name=_str_or_none(document_name),
        known_at=_str_or_none(known_at) or filed_at,
        source_url=_str_or_none(source_url),
        extraction_method=_extraction_method_of(
            parties, obj, filer_cik=filer_cik, filer_name=filer_name,
            subject_cik=subject_cik, subject_name=subject_name,
            acquirer=acquirer, buyer=buyer),
    )


def _merged_field_value(previous: Transaction, current: Transaction, name: str) -> object:
    if name == "buyer":
        return current.buyer if current.buyer is not None else previous.buyer
    if name == "source_accessions":
        return (tuple(previous.source_accessions)
                + tuple(a for a in current.source_accessions
                        if a not in previous.source_accessions))
    if name == "accession_no":
        return current.accession_no or previous.accession_no
    current_value: object = getattr(current, name)
    previous_value: object = getattr(previous, name)
    return current_value if current_value is not None else previous_value


def update_transaction(previous: Transaction,
                       current: Transaction) -> Transaction:
    updated = replace(previous)
    for f in fields(Transaction):
        if f.name in ("event_id", "target"):
            continue
        object.__setattr__(updated, f.name, _merged_field_value(previous, current, f.name))
    return updated


def diff_transaction(previous: Transaction, current: Transaction) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for f in fields(Transaction):
        name = f.name
        old, new = getattr(previous, name), getattr(current, name)
        if name == "source_accessions":
            if set(old) != set(new):
                out[name] = [old, new]
        elif old != new:
            out[name] = [old, new]
    return out


def get_transaction_status(ticker_or_cik: str | int, *, as_of: str | None = None,
                           limit: int | None = 10) -> list[Transaction]:
    filings = list_sec_filings(ticker_or_cik, forms=list(TRANSACTION_FORMS),
                               as_of=as_of, limit=limit)
    out: list[Transaction] = []
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
        try:
            text = load_transaction_text(accession)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            text = None
        out.append(normalize_transaction(
            accession, form, target=str(subject_name or ""), filed_at=filed_at,
            text=text, filer_cik=filer_cik, filer_name=filer_name,
            subject_cik=subject_cik, subject_name=subject_name))
    return out


def query_target_transactions(target: str, *, as_of: str | None = None,
                              root: Path | str | None = None,
                              limit: int = 200) -> list[dict[str, object]]:
    """Target -> transactions over ``sec_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_transactions(target=target, as_of=as_of, root=root,
                                     limit=limit)


def query_acquirer_transactions(acquirer: str, *, as_of: str | None = None,
                                root: Path | str | None = None,
                                limit: int = 200) -> list[dict[str, object]]:
    """Acquirer -> transactions over ``sec_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_transactions(acquirer=acquirer, as_of=as_of,
                                     root=root, limit=limit)
