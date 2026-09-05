"""Proxy/governance normalization: best-effort regex over filing text.

A text-less filing still yields a typed record from its form identity;
missing values are None/'unknown', never fabricated.
"""

import re

from .context import GOVERNANCE_FORMS
from .models import GovernanceEvent, ProxyProposal, ShareholderVote

CONTESTED_FORMS = ("DFAN14A", "DEFC14A", "PREC14A")

_PROPOSAL_SPLIT = re.compile(
    r"(?mi)^\s*(proposal\s+(no\.?\s*)?\d+[^\n]{0,120})")
_BOARD_REC = re.compile(
    r"board[^.]{0,120}recommend[^.]{0,120}(for|against|abstain)[^.]*",
    re.IGNORECASE)
_VOTES_FOR = re.compile(r"votes?\s+for[^\d]{0,20}([\d,]+)", re.IGNORECASE)
_VOTES_AGAINST = re.compile(
    r"votes?\s+against[^\d]{0,20}([\d,]+)", re.IGNORECASE)
_ABSTAIN = re.compile(r"absten[^\d]{0,20}([\d,]+)", re.IGNORECASE)
_OUTCOME = re.compile(r"(approved|rejected|passed|failed|adopted)",
                      re.IGNORECASE)


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def load_proxy_text(accession_no: str) -> str:
    """Live seam: raises on failure; callers fall back to form identity."""
    from . import documents

    return documents.get_sec_filing_text(accession_no)


def normalize_proxy(accession_no: str, form: str, *, issuer: str,
                    filed_at=None, text=None,
                    meeting_date=None, obj=None, filer_cik=None,
                    filer_name=None, subject_cik=None, subject_name=None,
                    document_name=None, known_at=None,
                    source_url=None) -> GovernanceEvent:
    if form in CONTESTED_FORMS:
        event_type = "proxy_contest"
    elif form in ("DEFM14A", "PREM14A"):
        event_type = "merger_vote"
    elif form == "PX14A6G":
        event_type = "shareholder_proposal"
    elif form in ("PRE 14C", "DEF 14C"):
        event_type = "information_statement"
    else:
        event_type = "annual_meeting"
    # Subject only from structured/explicit evidence, never a filer copy.
    structured_subject = None
    if obj is not None:
        try:
            for attr in ("subject_name", "subjectName", "registrant_name",
                         "company"):
                value = getattr(obj, attr, None)
                if value is not None and str(value).strip():
                    structured_subject = str(value).strip()
                    break
        except Exception:
            structured_subject = None
    method = ("structured-header" if obj is not None else "form-identity")
    return GovernanceEvent(
        event_id=f"{accession_no}:gov",
        issuer=issuer,
        event_type=event_type,
        meeting_date=meeting_date,
        filed_at=filed_at,
        accession_no=accession_no,
        contested=form in CONTESTED_FORMS,
        source=None,
        filer_cik=str(filer_cik).strip() if filer_cik is not None else None,
        filer_name=str(filer_name).strip() if filer_name is not None else None,
        subject_cik=str(subject_cik).strip()
        if subject_cik is not None else None,
        subject_name=(str(subject_name).strip()
                      if subject_name is not None else structured_subject),
        document_name=document_name,
        known_at=known_at or filed_at,
        source_url=source_url,
        extraction_method=method,
    )


def extract_proposals(text, *, issuer: str,
                      accession_no: str, document_name=None) -> list:
    if not text:
        return []
    body = str(text)
    headings = list(_PROPOSAL_SPLIT.finditer(body))
    if not headings:
        return []
    out = []
    for n, match in enumerate(headings, 1):
        end = headings[n].start() if n < len(headings) else len(body)
        window = body[match.start():end][:500]
        rec = _BOARD_REC.search(window)
        out.append(ProxyProposal(
            proposal_id=f"{accession_no}:p{n}",
            issuer=issuer,
            accession_no=accession_no,
            description=match.group(1).strip() or None,
            proposal_type=None,
            board_recommendation=rec.group(1).lower() if rec else None,
            status="unknown",
            source_span=f"{match.start()}:{match.start() + len(match.group(1))}",
            document_name=document_name,
        ))
    return out


def _num(raw) -> "int | None":
    try:
        return int(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def extract_votes(text, *, issuer: str, accession_no: str,
                  meeting_date=None, document_name=None) -> list:
    if not text:
        return []
    body = str(text)
    for_match = _VOTES_FOR.search(body)
    against_match = _VOTES_AGAINST.search(body)
    if not for_match and not against_match:
        return []
    abstain_match = _ABSTAIN.search(body)
    outcome_match = _OUTCOME.search(body)
    first = min(m.start() for m in (for_match, against_match)
                if m is not None)
    last = max(m.end() for m in (for_match, against_match, abstain_match,
                                  outcome_match) if m is not None)
    return [ShareholderVote(
        issuer=issuer,
        accession_no=accession_no,
        meeting_date=meeting_date,
        description=None,
        votes_for=_num(for_match.group(1)) if for_match else None,
        votes_against=_num(against_match.group(1)) if against_match else None,
        abstentions=_num(abstain_match.group(1)) if abstain_match else None,
        outcome=outcome_match.group(1).lower() if outcome_match else None,
        source_span=f"{first}:{last}",
        document_name=document_name,
    )]


def get_governance_events(ticker_or_cik, *, since=None, as_of=None,
                          limit=10) -> list:
    filings = list_sec_filings(ticker_or_cik, forms=list(GOVERNANCE_FORMS),
                               start_date=since, as_of=as_of, limit=limit)
    out = []
    for filing in filings:
        accession = getattr(filing, "accession_no", "")
        form = getattr(filing, "form", "")
        filed_at = getattr(filing, "filed_at", None)
        issuer = getattr(filing, "filer_name", None) or str(ticker_or_cik)
        try:
            text = load_proxy_text(accession)
        except Exception:
            text = None
        out.append(normalize_proxy(
            accession, form, issuer=issuer, filed_at=filed_at, text=text,
            filer_cik=getattr(filing, "filer_cik", None),
            filer_name=getattr(filing, "filer_name", None),
            subject_cik=getattr(filing, "subject_cik", None),
            subject_name=getattr(filing, "subject_name", None)))
    return out
