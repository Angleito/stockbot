"""Deterministic offering history (no network, no invented terms).

Seam: live reads via SourceGateway + normalization + raw_archive (write-once) + write_bundle; NOTE: a future warehouse slots in behind these live readers, never inside normalization.
"""

import re
import threading
from datetime import date, datetime
from typing import TypedDict

# `object` marks the edgar SDK dynamic boundary (no stubs): attrs are read
# via _sweep and validated before building Offering objects.
from .models import Filing, Offering

OFFERING_FORMS = (
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "S-8",
    "F-1",
    "F-1/A",
    "F-3",
    "F-3/A",
    "424B1",
    "424B2",
    "424B3",
    "424B4",
    "424B5",
    "424B7",
    "424B8",
    "EFFECT",
    "RW",
)
REGISTRATION_FORMS = (
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "F-1",
    "F-1/A",
    "F-3",
    "F-3/A",
    "S-8",
)
_TERMS_LOCK = threading.Lock()

_SHARE_ATTRS = (
    "shares",
    "shares_offered",
    "num_shares",
    "offered_shares",
    "share_count",
    "number_of_shares",
    "securities_registered",
    "shares_registered",
    "common_shares_offered",
)
_PRICE_ATTRS = (
    "price_per_share",
    "offer_price",
    "offering_price",
    "price",
    "price_to_public",
    "public_offering_price",
    "per_share_price",
)
_PROCEEDS_ATTRS = (
    "gross_proceeds",
    "gross_offering_proceeds",
    "proceeds",
    "total_proceeds",
    "aggregate_proceeds",
    "max_aggregate_price",
    "maximum_aggregate_offering_price",
)
_UNDERWRITER_ATTRS = ("underwriters", "underwriter", "managers", "bookrunners", "book_runners", "agents")
_WARRANT_ATTRS = ("has_warrants", "warrants", "with_warrants", "warrant_coverage")
_CONVERTIBLE_ATTRS = ("has_convertibles", "convertibles", "convertible", "convertible_notes", "with_convertibles")
_ATM_ATTRS = ("is_atm", "atm", "at_the_market", "at_the_market_program", "is_at_the_market")
_TYPE_ATTRS = ("offering_type", "type", "offering_kind", "security_type", "securities_type")


def list_sec_filings(
    ticker_or_cik: str | int,
    forms: str | list[str] | tuple[str, ...] | None = None,
    start_date: str | date | datetime | None = None,
    end_date: str | date | datetime | None = None,
    as_of: str | date | datetime | None = None,
    limit: int | None = 50,
) -> list[Filing]:
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(ticker_or_cik, forms=forms, start_date=start_date, end_date=end_date, as_of=as_of, limit=limit)


_SENTINEL_TEXTS = frozenset({"none", "nan", "na", "n/a", "--"})


def _int_text_of(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in _SENTINEL_TEXTS:
        return None
    return text


def _parse_int_text(text: str) -> int | None:
    try:
        return int(float(text)) if "." in text else int(text)
    except ValueError, TypeError:
        return None


def _safe_int(value: object) -> int | None:
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = _int_text_of(value)
    return _parse_int_text(text) if text is not None else None


def _safe_float(value: object) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        text = str(value).strip().replace(",", "").replace("$", "")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return float(text)
    except ValueError, TypeError:
        return None


_TRUE_TOKENS = frozenset({"true", "yes", "y", "1", "with"})
_FALSE_TOKENS = frozenset({"false", "no", "n", "0", "without", "none"})


def _bool_of_text(text: str) -> bool | None:
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return True if text else None


def _bool_text_of(value: object) -> str | None:
    try:
        return str(value).strip().lower()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _safe_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = _bool_text_of(value)
    return _bool_of_text(text) if text is not None else None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return text or None


def _sweep(obj: object, names: tuple[str, ...]) -> object:
    for name in names:
        try:
            value: object = getattr(obj, name)
        except Exception:  # noqa: BLE001, S112 - intentional best-effort boundary, never aborts
            continue
        if value is not None:
            return value
    return None


def _norm_underwriters(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(s for s in (str(v).strip() for v in value) if s)
    text = _str_or_none(value)
    return (text,) if text else ()


def _type_says_atm(offering_type: object) -> bool:
    if not offering_type:
        return False
    text = re.sub(r"[-_]", " ", str(offering_type).lower())
    return "at the market" in text or re.search(r"\batm\b", text) is not None


# ponytail: fixed attr sweep + two span patterns; broader NLP is out of scope.
_SHARES_SPAN = re.compile(
    r"([\d,]+)\s+shares?\s+of\s+([A-Z][A-Za-z0-9&.,'’\- ]{1,60}?)"
    r"\s+(?:common\s+stock|preferred\s+stock|common\s+shares)",
    re.IGNORECASE,
)
_PRICE_SPAN = re.compile(r"\$\s?[\d,]+(?:\.\d+)?\s+per\s+share", re.IGNORECASE)


def resolve_offering_status(form: object, *, text: object = None) -> str:
    """EFFECT is effective, RW is a filed withdrawal request, else filed.

    Amendments (``/A``) never set status: a shelf amendment is still a
    filed registration, never issuance. The form is the only signal; text
    never upgrades a registration into an issuance.
    """
    try:
        upper = str(form or "").strip().upper()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return "filed"
    if upper == "EFFECT":
        return "effective"
    if upper == "RW":
        return "withdrawn"
    return "filed"


def resolve_amount_basis(form: object) -> str | None:
    """Registration statements register; prospectuses propose. Never issuance."""
    try:
        upper = str(form or "").strip().upper()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if upper in REGISTRATION_FORMS:
        return "registered"
    if upper.startswith("424B"):
        return "proposed"
    if upper in ("EFFECT", "RW"):
        return None
    return "proposed"


_FACT_ATTRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("shares", _SHARE_ATTRS),
    ("price_per_share", _PRICE_ATTRS),
    ("gross_proceeds", _PROCEEDS_ATTRS),
    ("security_title", ("security_title", "offering_type", "type")),
    ("underwriters", _UNDERWRITER_ATTRS),
)


def _structured_facts_of(obj: object) -> tuple[dict[str, object], bool]:
    facts: dict[str, object] = {
        "shares": None,
        "price_per_share": None,
        "gross_proceeds": None,
        "security_title": None,
        "underwriters": None,
    }
    structured = False
    for key, attrs in _FACT_ATTRS:
        value = _sweep(obj, attrs)
        if value is not None:
            facts[key] = value
            structured = True
    return facts, structured


def _shares_span_of(facts: dict[str, object], body: str) -> dict[str, str] | None:
    if facts["shares"] is not None and facts["security_title"] is not None:
        return None
    match = _SHARES_SPAN.search(body)
    if not match:
        return None
    if facts["shares"] is None:
        facts["shares"] = match.group(1)
    if facts["security_title"] is None:
        facts["security_title"] = match.group(2).strip()
    return {
        "fact": "shares/security_title",
        "text": match.group(0).strip(),
        "span": f"{match.start()}:{match.end()}",
        "method": "exact-span",
    }


def _price_span_of(facts: dict[str, object], body: str) -> dict[str, str] | None:
    if facts["price_per_share"] is not None:
        return None
    match = _PRICE_SPAN.search(body)
    if not match:
        return None
    facts["price_per_share"] = match.group(0)
    return {
        "fact": "price_per_share",
        "text": match.group(0).strip(),
        "span": f"{match.start()}:{match.end()}",
        "method": "exact-span",
    }


def _empty_facts() -> dict[str, object]:
    return {
        "shares": None,
        "price_per_share": None,
        "gross_proceeds": None,
        "security_title": None,
        "underwriters": None,
    }


def _span_facts_of(facts: dict[str, object], text: str | None) -> list[dict[str, str]]:
    if not text:
        return []
    spans: list[dict[str, str]] = []
    for span in (_shares_span_of(facts, text), _price_span_of(facts, text)):
        if span is not None:
            spans.append(span)
    return spans


def extract_offering_facts(
    obj: object | None = None, *, text: str | None = None, form: object = None
) -> dict[str, object]:
    """Structured terms first, then exact document spans; never raises.

    Quantities stay proposed/registered via ``amount_basis``: a registration
    is never issuance. Each span records fact, exact text, offsets, and
    method for store/service provenance.
    """
    try:
        facts, structured = _structured_facts_of(obj) if obj is not None else (_empty_facts(), False)
        spans = _span_facts_of(facts, text)
        method = "structured-header" if structured else "exact-span" if spans else "form-identity"
        return {**facts, "spans": spans, "method": method, "amount_basis": resolve_amount_basis(form)}
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return {"spans": [], "method": "form-identity", "amount_basis": None}


def load_terms(accession_no: str) -> dict[str, object]:
    """Live seam: best-effort edgar attr sweep; any failure -> {}."""
    # ponytail: one global fetch lock; concurrent term sweeps collapse SEC
    # throttle into multi-minute runs (50 filings x 2 parallel calls blew the
    # 120s tool cliff). Per-accession singleflight if serialized latency matters.
    with _TERMS_LOCK:
        return _load_terms_locked(accession_no)


_TERM_ATTRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("shares", _SHARE_ATTRS),
    ("price_per_share", _PRICE_ATTRS),
    ("gross_proceeds", _PROCEEDS_ATTRS),
    ("underwriters", _UNDERWRITER_ATTRS),
    ("has_warrants", _WARRANT_ATTRS),
    ("has_convertibles", _CONVERTIBLE_ATTRS),
    ("is_atm", _ATM_ATTRS),
    ("offering_type", _TYPE_ATTRS),
)


def _obj_of(filing: object) -> object:
    try:
        obj_of = getattr(filing, "obj")  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
        return obj_of() if callable(obj_of) else None
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _sweep_terms(obj: object, filing: object) -> dict[str, object]:
    terms: dict[str, object] = {}
    for key, attrs in _TERM_ATTRS:
        value = _sweep(obj, attrs) if obj is not None else None
        if value is None:
            value = _sweep(filing, attrs)
        if value is not None:
            terms[key] = value
    return terms


def _load_terms_locked(accession_no: str) -> dict[str, object]:
    try:
        from .documents import get_by_accession_number

        filing = get_by_accession_number(accession_no)
        return _sweep_terms(_obj_of(filing), filing)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return {}


def _merged_terms(terms: dict[str, object] | None, facts: dict[str, object]) -> dict[str, object]:
    merged = terms if isinstance(terms, dict) else {}
    for key in ("shares", "price_per_share", "gross_proceeds", "security_title", "underwriters"):
        if merged.get(key) is None and facts.get(key) is not None:
            merged[key] = facts[key]
    return merged


def _offering_flags(terms: dict[str, object]) -> tuple[str | None, bool]:
    offering_type = _str_or_none(terms.get("offering_type", terms.get("type")))
    atm_flag = _safe_bool(terms.get("is_atm", terms.get("atm", terms.get("at_the_market"))))
    return offering_type, bool(atm_flag) or _type_says_atm(offering_type)


class _OfferingAmounts(TypedDict):
    shares: int | None
    price_per_share: float | None
    gross_proceeds: float | None
    underwriters: tuple[str, ...]
    has_warrants: bool | None
    has_convertibles: bool | None


def _offering_amounts(terms: dict[str, object]) -> _OfferingAmounts:
    return {
        "shares": _safe_int(terms.get("shares")),
        "price_per_share": _safe_float(terms.get("price_per_share")),
        "gross_proceeds": _safe_float(terms.get("gross_proceeds")),
        "underwriters": _norm_underwriters(terms.get("underwriters")),
        "has_warrants": _safe_bool(terms.get("has_warrants")),
        "has_convertibles": _safe_bool(terms.get("has_convertibles")),
    }


def _offering_parties(
    *, filer_cik: object, filer_name: object, registrant_cik: object, registrant_name: object, issuer: str
) -> dict[str, str | None]:
    return {
        "filer_cik": _str_or_none(filer_cik),
        "filer_name": _str_or_none(filer_name),
        "registrant_cik": _str_or_none(registrant_cik),
        "registrant_name": (_str_or_none(registrant_name) or issuer),
    }


def normalize_offering(
    accession_no: str,
    form: str,
    *,
    issuer: str,
    filed_at: str | None,
    terms: dict[str, object] | None = None,
    obj: object | None = None,
    text: str | None = None,
    filer_cik: str | int | None = None,
    filer_name: str | None = None,
    registrant_cik: str | int | None = None,
    registrant_name: str | None = None,
    security_title: str | None = None,
    document_name: str | None = None,
    known_at: str | None = None,
    source_url: str | None = None,
) -> Offering:
    """Pure: missing terms -> None fields, never invented.

    Registration stays registration: quantities are proposed/registered via
    ``amount_basis``, never issuance; amendments keep ``filed`` status.
    Explicit registrant wins, else the issuer; the filer is never copied
    into the registrant.
    """
    facts = extract_offering_facts(obj, text=text, form=form)
    terms = _merged_terms(terms, facts)
    offering_type, is_atm = _offering_flags(terms)
    security = (
        _str_or_none(security_title)
        or _str_or_none(terms.get("security_title"))
        or _str_or_none(facts.get("security_title"))
        or offering_type
    )
    amounts = _offering_amounts(terms)
    parties = _offering_parties(
        filer_cik=filer_cik,
        filer_name=filer_name,
        registrant_cik=registrant_cik,
        registrant_name=registrant_name,
        issuer=issuer,
    )
    return Offering(
        issuer=issuer,
        form=form,
        filed_at=filed_at,
        accession_no=accession_no,
        offering_type=offering_type,
        shares=amounts["shares"],
        price_per_share=amounts["price_per_share"],
        gross_proceeds=amounts["gross_proceeds"],
        underwriters=amounts["underwriters"],
        has_warrants=amounts["has_warrants"],
        has_convertibles=amounts["has_convertibles"],
        is_atm=is_atm,
        source_registration=_str_or_none(terms.get("source_registration")),
        status=resolve_offering_status(form),
        filer_cik=parties["filer_cik"],
        filer_name=parties["filer_name"],
        registrant_cik=parties["registrant_cik"],
        registrant_name=parties["registrant_name"],
        security_title=security,
        amount_basis=_str_or_none(facts.get("amount_basis")),
        document_name=_str_or_none(document_name),
        known_at=_str_or_none(known_at) or filed_at,
        source_url=_str_or_none(source_url),
        extraction_method=_str_or_none(facts.get("method")),
    )


def _wanted_terms_forms(terms_forms: tuple[str, ...] | list[str] | frozenset[str] | set[str] | None) -> set[str] | None:
    if terms_forms is None:
        return None
    return {f.strip().upper() for f in terms_forms}


def _filing_terms_of(accession: str, form: str, wanted: set[str] | None) -> dict[str, object] | None:
    # Terms are live per-filing fetches: skip forms the caller never
    # consumes (registration accessions need no terms).
    norm = form.strip().upper() if isinstance(form, str) else ""
    if wanted is not None and norm not in wanted:
        return None
    try:
        terms = load_terms(accession)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return terms if isinstance(terms, dict) else None


def _history_offerings(ticker_or_cik: str | int, filings: list[Filing], *, wanted: set[str] | None) -> list[Offering]:
    out: list[Offering] = []
    for filing in filings:
        try:
            accession = getattr(filing, "accession_no", "")
            form = getattr(filing, "form", "")
            filed_at = getattr(filing, "filed_at", None)
            issuer = getattr(filing, "filer_name", None) or str(ticker_or_cik)
            filer_cik = getattr(filing, "filer_cik", None)
            filer_name = getattr(filing, "filer_name", None)
        except Exception:  # noqa: BLE001, S112 - intentional best-effort boundary, never aborts
            continue
        try:
            out.append(
                normalize_offering(
                    accession,
                    form,
                    issuer=issuer,
                    filed_at=filed_at,
                    terms=_filing_terms_of(accession, form, wanted),
                    filer_cik=filer_cik,
                    filer_name=filer_name,
                )
            )
        except Exception:  # noqa: BLE001, S112 - intentional best-effort boundary, never aborts
            continue
    return out


def _link_registrations(out: list[Offering]) -> list[Offering]:

    regs = [o for o in out if o.form in REGISTRATION_FORMS]
    linked: list[Offering] = []
    for offering in out:
        linked.append(_link_one_registration(offering, regs))
    return linked


def _is_newer_registration(best: Offering | None, reg: Offering) -> bool:
    return best is None or (best.filed_at or "") < (reg.filed_at or "")


def _is_link_candidate(offering: Offering, reg: Offering) -> bool:
    if reg is offering or not reg.filed_at or not offering.filed_at:
        return False
    return reg.filed_at <= offering.filed_at


def _link_one_registration(offering: Offering, regs: list[Offering]) -> Offering:
    from dataclasses import replace

    if not (offering.form.upper().startswith("424B") and offering.filed_at):
        return offering
    best: Offering | None = None
    for reg in regs:
        if _is_link_candidate(offering, reg) and _is_newer_registration(best, reg):
            best = reg
    if best is None:
        return offering
    return replace(offering, source_registration=best.accession_no)


def get_offering_history(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 50,
    forms: tuple[str, ...] | list[str] = OFFERING_FORMS,
    terms_forms: tuple[str, ...] | list[str] | frozenset[str] | set[str] | None = None,
) -> list[Offering]:
    filings = list_sec_filings(ticker_or_cik, forms=list(forms), as_of=as_of, limit=limit)
    return _link_registrations(_history_offerings(ticker_or_cik, filings, wanted=_wanted_terms_forms(terms_forms)))


# Seam: registrant-filtered reads normalize live per filing via SourceGateway + normalization + raw_archive + write_bundle; NOTE: warehouse slots behind live readers.
