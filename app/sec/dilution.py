"""Deterministic dilution math (pure) + one wiring function."""

from collections.abc import Sequence
from typing import TypeGuard

from .models import Offering
from .offerings import OFFERING_FORMS, REGISTRATION_FORMS

FORMULAS = {
    "dilution_pct": "dilution_pct = new_shares / (existing_shares + new_shares) * 100",
    "atm_pct_of_market_cap": "atm_pct_of_market_cap = atm_size / market_cap * 100",
    "fully_diluted_shares": "fully_diluted_shares = existing + new + convertible + warrant",
}

_NQ = "not_quantifiable"

_OFFERING_424B_FORMS = frozenset(
    {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7", "424B8"})

_REGISTRATION_ACCESSION_FORMS = frozenset(
    {f.strip().upper() for f in REGISTRATION_FORMS} | {"EFFECT", "RW"})


def get_offering_history(
    ticker_or_cik: str | int,
    *,
    as_of: str | None = None,
    limit: int | None = 50,
    forms: tuple[str, ...] | list[str] = OFFERING_FORMS,
    terms_forms: tuple[str, ...] | list[str] | frozenset[str] | set[str] | None = None,
) -> list[Offering]:
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .offerings import get_offering_history as _real

    return _real(ticker_or_cik, as_of=as_of, limit=limit, forms=forms, terms_forms=terms_forms)


def get_fundamentals(
    ticker: str,
    metric: str,
    as_of: str | None = None,
) -> dict[str, object]:
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from app.services import sec_facts

    return sec_facts.get_fundamentals(ticker, metric, as_of=as_of)


def _positive(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and value > 0


def _ratio_pct(part: object, whole: object) -> float | str:
    if _positive(part) and _positive(whole):
        part_v: float = part
        whole_v: float = whole
        return float(part_v) / float(whole_v) * 100
    return _NQ


def _is_share_count(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _extra_shares(parts: tuple[object, ...]) -> tuple[int | float, bool]:
    extra: int | float = 0
    for part in parts:
        if part is None:
            continue
        if not _is_share_count(part) or part < 0:
            return 0, True
        extra += part
    return extra, False


def _fully_diluted_shares(existing_shares: object, parts: tuple[object, ...]) -> int | float | str:
    if not (_is_share_count(existing_shares) and existing_shares > 0):
        return _NQ
    extra, bad = _extra_shares(parts)
    return _NQ if bad else existing_shares + extra

def _cw_total(convertible_shares: object, warrant_shares: object) -> object:
    if convertible_shares is None and warrant_shares is None:
        return None
    conv: float = float(convertible_shares) if _is_share_count(convertible_shares) else 0.0
    warr: float = float(warrant_shares) if _is_share_count(warrant_shares) else 0.0
    return conv + warr

def dilution_profile(
    *,
    existing_shares: int | float | None = None,
    new_shares: int | float | None = None,
    price: int | float | None = None,
    market_cap: int | float | None = None,
    atm_size: int | float | None = None,
    convertible_shares: int | float | None = None,
    warrant_shares: int | float | None = None,
    source_accessions: Sequence[str] = (),
) -> dict[str, object]:
    dilution_pct = _ratio_pct(new_shares, existing_shares + new_shares
                              if _positive(existing_shares) and _positive(new_shares) else None)
    atm_pct = _ratio_pct(atm_size, market_cap)
    fully_diluted = _fully_diluted_shares(
        existing_shares, (new_shares, convertible_shares, warrant_shares))
    cw = _cw_total(convertible_shares, warrant_shares)
    if cw is not None and _positive(cw) and isinstance(
            fully_diluted, (int, float)) and fully_diluted > 0:
        cw_pct: float | str = float(cw) / float(fully_diluted) * 100
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


def _load_dilution_history(ticker_or_cik: str, as_of: str | None) -> list[Offering]:
    try:
        # Shares are only ever read off 424B rows; registration rows need
        # accessions alone, so skip their live term fetches.
        return get_offering_history(
            ticker_or_cik, as_of=as_of, terms_forms=_OFFERING_424B_FORMS) or []
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return []


def _existing_shares_of(facts: object) -> int | None:
    try:
        raw = facts.get("shares_outstanding") if isinstance(facts, dict) else None
        if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            return int(float(raw))
    except (ValueError, TypeError):
        return None
    return None


def _load_existing_shares(ticker_or_cik: str, as_of: str | None) -> int | None:
    try:
        facts = get_fundamentals(ticker_or_cik, "shares_outstanding", as_of=as_of)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    return _existing_shares_of(facts)


def _offering_share_value(offering: Offering) -> int | None:
    try:
        shares = getattr(offering, "shares", None)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if isinstance(shares, bool) or shares is None:
        return None
    try:
        value = int(shares)
    except (ValueError, TypeError):
        return None
    return value if value > 0 else None


def _offering_form_and_accession(offering: Offering) -> tuple[str, str | None]:
    try:
        form = getattr(offering, "form", None)
        accession = getattr(offering, "accession_no", None)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return "", None
    norm = form.strip().upper() if isinstance(form, str) else ""
    return norm, accession


def _summarize_offering_shares(history: list[Offering]) -> tuple[int, list[str], list[str], bool]:
    disclosed_total = 0
    offering_accessions: list[str] = []
    registration_accessions: list[str] = []
    disclosed_known = False
    for offering in history or []:
        value = _offering_share_value(offering)
        if value is None:
            continue
        norm, accession = _offering_form_and_accession(offering)
        if norm in _OFFERING_424B_FORMS:
            disclosed_total += value
            disclosed_known = True
            if accession:
                offering_accessions.append(accession)
        elif norm in _REGISTRATION_ACCESSION_FORMS and accession:
            registration_accessions.append(accession)
    return disclosed_total, offering_accessions, registration_accessions, disclosed_known


def get_dilution_profile(
    ticker_or_cik: str,
    *,
    as_of: str | None = None,
) -> dict[str, object]:
    history = _load_dilution_history(ticker_or_cik, as_of)
    existing = _load_existing_shares(ticker_or_cik, as_of)
    disclosed_total, offering_accessions, registration_accessions, disclosed_known = \
        _summarize_offering_shares(history)
    out = dilution_profile(existing_shares=existing,
                           new_shares=None,
                           source_accessions=tuple(offering_accessions + registration_accessions))
    out["fully_diluted_shares"] = _NQ
    out["sum_of_disclosed_share_counts"] = disclosed_total if disclosed_known else None
    out["offering_accessions"] = tuple(offering_accessions)
    out["registration_accessions"] = tuple(registration_accessions)
    out["registered_capacity"] = "not_quantifiable"
    out["note"] = ("Observed only: 424B share counts are summed across disclosures without deduplication. This is not confirmed issuance and must not be interpreted as incremental dilution. Registration filings (S-1/S-3/F-1/F-3/S-8/EFFECT/RW, including /A amendments) are listed as individual accessions, never summed into capacity; dilution_pct stays not_quantifiable.")
    return out
