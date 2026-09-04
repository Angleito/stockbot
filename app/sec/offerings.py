"""Deterministic offering history (no network, no invented terms)."""

import re

from .models import Offering

OFFERING_FORMS = (
    "S-1", "S-1/A", "S-3", "S-3/A", "S-8", "F-1", "F-1/A", "F-3", "F-3/A",
    "424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8",
    "EFFECT", "RW",
)
REGISTRATION_FORMS = (
    "S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A", "S-8",
)

_SHARE_ATTRS = ("shares", "shares_offered", "num_shares", "offered_shares",
                "share_count", "number_of_shares", "securities_registered",
                "shares_registered", "common_shares_offered")
_PRICE_ATTRS = ("price_per_share", "offer_price", "offering_price", "price",
                "price_to_public", "public_offering_price", "per_share_price")
_PROCEEDS_ATTRS = ("gross_proceeds", "gross_offering_proceeds", "proceeds",
                   "total_proceeds", "aggregate_proceeds",
                   "max_aggregate_price", "maximum_aggregate_offering_price")
_UNDERWRITER_ATTRS = ("underwriters", "underwriter", "managers",
                      "bookrunners", "book_runners", "agents")
_WARRANT_ATTRS = ("has_warrants", "warrants", "with_warrants",
                  "warrant_coverage")
_CONVERTIBLE_ATTRS = ("has_convertibles", "convertibles", "convertible",
                      "convertible_notes", "with_convertibles")
_ATM_ATTRS = ("is_atm", "atm", "at_the_market", "at_the_market_program",
              "is_at_the_market")
_TYPE_ATTRS = ("offering_type", "type", "offering_kind", "security_type",
               "securities_type")


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def _safe_int(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return int(float(text)) if "." in text else int(text)
    except (ValueError, TypeError):
        return None


def _safe_float(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip().replace(",", "").replace("$", "")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return float(text)
    except (ValueError, TypeError):
        return None


def _safe_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    try:
        text = str(value).strip().lower()
    except Exception:
        return None
    if text in ("true", "yes", "y", "1", "with"):
        return True
    if text in ("false", "no", "n", "0", "without", "none"):
        return False
    return True if text else None


def _str_or_none(value):
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _sweep(obj, names):
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _norm_underwriters(value):
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(s for s in (str(v).strip() for v in value) if s)
    text = _str_or_none(value)
    return (text,) if text else ()


def _type_says_atm(offering_type) -> bool:
    if not offering_type:
        return False
    text = re.sub(r"[-_]", " ", str(offering_type).lower())
    return "at the market" in text or re.search(r"\batm\b", text) is not None


def load_terms(accession_no: str) -> dict:
    """Live seam: best-effort edgar attr sweep; any failure -> {}."""
    try:
        from .documents import get_by_accession_number

        filing = get_by_accession_number(accession_no)
        try:
            obj = filing.obj()
        except Exception:
            obj = None
        terms: dict = {}
        for key, attrs in (("shares", _SHARE_ATTRS),
                           ("price_per_share", _PRICE_ATTRS),
                           ("gross_proceeds", _PROCEEDS_ATTRS),
                           ("underwriters", _UNDERWRITER_ATTRS),
                           ("has_warrants", _WARRANT_ATTRS),
                           ("has_convertibles", _CONVERTIBLE_ATTRS),
                           ("is_atm", _ATM_ATTRS),
                           ("offering_type", _TYPE_ATTRS)):
            value = _sweep(obj, attrs) if obj is not None else None
            if value is None:
                value = _sweep(filing, attrs)
            if value is not None:
                terms[key] = value
        return terms
    except Exception:
        return {}


def normalize_offering(accession_no, form, *, issuer, filed_at,
                       terms=None) -> Offering:
    """Pure: missing terms -> None fields, never invented."""
    terms = terms if isinstance(terms, dict) else {}
    offering_type = _str_or_none(terms.get("offering_type", terms.get("type")))
    atm_flag = _safe_bool(terms.get("is_atm", terms.get("atm",
                         terms.get("at_the_market"))))
    is_atm = bool(atm_flag) or _type_says_atm(offering_type)
    upper = str(form or "").strip().upper()
    status = "effective" if upper == "EFFECT" else "withdrawn" if upper == "RW" else "filed"
    return Offering(
        issuer=issuer, form=form, filed_at=filed_at,
        accession_no=accession_no, offering_type=offering_type,
        shares=_safe_int(terms.get("shares")),
        price_per_share=_safe_float(terms.get("price_per_share")),
        gross_proceeds=_safe_float(terms.get("gross_proceeds")),
        underwriters=_norm_underwriters(terms.get("underwriters")),
        has_warrants=_safe_bool(terms.get("has_warrants")),
        has_convertibles=_safe_bool(terms.get("has_convertibles")),
        is_atm=is_atm,
        source_registration=_str_or_none(terms.get("source_registration")),
        status=status,
    )


def get_offering_history(ticker_or_cik, *, as_of=None, limit=50,
                         forms=OFFERING_FORMS) -> list:
    filings = list_sec_filings(ticker_or_cik, forms=list(forms),
                               as_of=as_of, limit=limit)
    out: list[Offering] = []
    for filing in filings:
        try:
            accession = getattr(filing, "accession_no", "")
            form = getattr(filing, "form", "")
            filed_at = getattr(filing, "filed_at", None)
            issuer = getattr(filing, "company", None) or str(ticker_or_cik)
        except Exception:
            continue
        try:
            terms = load_terms(accession)
        except Exception:
            terms = None
        if not isinstance(terms, dict):
            terms = None
        try:
            out.append(normalize_offering(accession, form, issuer=issuer,
                                          filed_at=filed_at, terms=terms))
        except Exception:
            continue
    from dataclasses import replace

    regs = [o for o in out if o.form in REGISTRATION_FORMS]
    linked = []
    for offering in out:
        if offering.form.upper().startswith("424B") and offering.filed_at:
            best = None
            for reg in regs:
                if reg is offering or not reg.filed_at:
                    continue
                if reg.filed_at <= offering.filed_at and (
                        best is None or reg.filed_at > best.filed_at):
                    best = reg
            if best is not None:
                offering = replace(offering,
                                   source_registration=best.accession_no)
        linked.append(offering)
    return linked
