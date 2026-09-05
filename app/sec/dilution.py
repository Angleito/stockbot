"""Deterministic dilution math (pure) + one wiring function."""

FORMULAS = {
    "dilution_pct": "dilution_pct = new_shares / (existing_shares + new_shares) * 100",
    "atm_pct_of_market_cap": "atm_pct_of_market_cap = atm_size / market_cap * 100",
    "fully_diluted_shares": "fully_diluted_shares = existing + new + convertible + warrant",
}

_NQ = "not_quantifiable"

from .offerings import REGISTRATION_FORMS

_OFFERING_424B_FORMS = frozenset(
    {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8"})

_REGISTRATION_ACCESSION_FORMS = frozenset(
    {f.strip().upper() for f in REGISTRATION_FORMS} | {"EFFECT", "RW"})


def get_offering_history(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .offerings import get_offering_history as _real

    return _real(*args, **kwargs)


def get_fundamentals(ticker, metric, as_of=None):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from app.services import sec_facts

    return sec_facts.get_fundamentals(ticker, metric, as_of=as_of)


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0


def dilution_profile(*, existing_shares=None, new_shares=None, price=None,
                     market_cap=None, atm_size=None, convertible_shares=None,
                     warrant_shares=None, source_accessions=()) -> dict:
    if _positive(existing_shares) and _positive(new_shares):
        dilution_pct = float(new_shares) / float(existing_shares + new_shares) * 100
    else:
        dilution_pct = _NQ
    if _positive(atm_size) and _positive(market_cap):
        atm_pct = float(atm_size) / float(market_cap) * 100
    else:
        atm_pct = _NQ
    if isinstance(existing_shares, (int, float)) and not isinstance(
            existing_shares, bool) and existing_shares > 0:
        extra = 0
        bad = False
        for part in (new_shares, convertible_shares, warrant_shares):
            if part is None:
                continue
            if not isinstance(part, (int, float)) or isinstance(part, bool) \
                    or part < 0:
                bad = True
                break
            extra += part
        fully_diluted = _NQ if bad else existing_shares + extra
    else:
        fully_diluted = _NQ
    cw = (convertible_shares or 0) + (warrant_shares or 0) \
        if convertible_shares is not None or warrant_shares is not None else None
    if cw is not None and _positive(cw) and isinstance(
            fully_diluted, (int, float)) and fully_diluted > 0:
        cw_pct = float(cw) / float(fully_diluted) * 100
    else:
        cw_pct = _NQ
    return {
        "inputs": {
            "existing_shares": existing_shares, "new_shares": new_shares,
            "price": price, "market_cap": market_cap, "atm_size": atm_size,
            "convertible_shares": convertible_shares,
            "warrant_shares": warrant_shares,
            "source_accessions": tuple(source_accessions or ()),
        },
        "formulas": dict(FORMULAS),
        "dilution_pct": dilution_pct,
        "fully_diluted_shares": fully_diluted,
        "atm_pct_of_market_cap": atm_pct,
        "convertible_warrant_pct": cw_pct,
        "source_accessions": tuple(source_accessions or ()),
    }


def get_dilution_profile(ticker_or_cik, *, as_of=None) -> dict:
    try:
        history = get_offering_history(ticker_or_cik, as_of=as_of) or []
    except Exception:
        history = []
    try:
        facts = get_fundamentals(ticker_or_cik, "shares_outstanding",
                                 as_of=as_of)
    except Exception:
        facts = None
    existing = None
    try:
        raw = facts.get("shares_outstanding") if isinstance(facts, dict) else None
        if raw is not None and not isinstance(raw, bool):
            existing = int(float(raw))
    except (ValueError, TypeError):
        existing = None
    disclosed_total = 0
    offering_accessions = []
    registration_accessions = []
    disclosed_known = False
    for offering in history or []:
        try:
            form = getattr(offering, "form", None)
            shares = getattr(offering, "shares", None)
            accession = getattr(offering, "accession_no", None)
        except Exception:
            continue
        if isinstance(shares, bool) or shares is None:
            continue
        try:
            value = int(shares)
        except (ValueError, TypeError):
            continue
        if value <= 0:
            continue
        norm = form.strip().upper() if isinstance(form, str) else ""
        if norm in _OFFERING_424B_FORMS:
            disclosed_total += value
            disclosed_known = True
            if accession:
                offering_accessions.append(accession)
        elif norm in _REGISTRATION_ACCESSION_FORMS:
            if accession:
                registration_accessions.append(accession)
    out = dilution_profile(existing_shares=existing,
                           new_shares=None,
                           source_accessions=())
    out["fully_diluted_shares"] = _NQ
    out["sum_of_disclosed_share_counts"] = disclosed_total if disclosed_known else None
    out["offering_accessions"] = tuple(offering_accessions)
    out["registration_accessions"] = tuple(registration_accessions)
    out["registered_capacity"] = "not_quantifiable"
    out["note"] = ("Observed only: 424B share counts are summed across disclosures without deduplication. This is not confirmed issuance and must not be interpreted as incremental dilution. Registration filings (S-1/S-3/F-1/F-3/S-8/EFFECT/RW, including /A amendments) are listed as individual accessions, never summed into capacity; dilution_pct stays not_quantifiable.")
    return out
