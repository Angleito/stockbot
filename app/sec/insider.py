"""Deterministic Forms 3/4/5 + 144 insider normalization (no network)."""

from .models import InsiderTransaction, ProposedInsiderSale

TRANSACTION_KINDS = {
    "P": "open_market_purchase",
    "S": "open_market_sale",
    "M": "exercise",
    "X": "exercise",
    "A": "grant_award",
    "G": "gift",
    "C": "conversion",
    "F": "tax_withholding",
}

_DEFAULT_FORMS = ("3", "3/A", "4", "4/A", "5", "5/A")
_DEFAULT_144_FORMS = ("144", "144/A")


def list_sec_filings(*args, **kwargs):
    """Lazy seam: tests monkeypatch this name; real path imports on call."""
    from .filings import list_sec_filings as _real

    return _real(*args, **kwargs)


def classify_transaction(code) -> str:
    try:
        key = str(code).strip().upper()
    except Exception:
        return "other"
    return TRANSACTION_KINDS.get(key, "other")


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
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in ("none", "nan", "na", "n/a", "--"):
            return None
        return float(text)
    except (ValueError, TypeError):
        return None


def _first(obj, *names):
    for name in names:
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if value is not None:
            return value
    return None


def _str_or_none(value):
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _insider_cik(obj):
    cik = _first(obj, "insider_cik", "reporting_owner_cik", "owner_cik", "cik")
    if cik is not None:
        try:
            return str(cik)
        except Exception:
            pass
    try:
        owners = getattr(obj, "reporting_owners", None) or []
        for owner in list(owners):
            cik = _first(owner, "cik", "owner_cik", "reporting_owner_cik")
            if cik is not None:
                return str(cik)
    except Exception:
        pass
    return None


def normalize_ownership_filing(obj, *, issuer, form, filed_at, accession_no):
    """One InsiderTransaction per activity row; never raises."""
    try:
        name = _str_or_none(_first(obj, "insider_name", "reporting_owner",
                                          "owner_name", "name"))
        cik = _insider_cik(obj)
    except Exception:
        return []
    try:
        activities = obj.get_transaction_activities()
    except Exception:
        return []
    if not activities:
        return []
    try:
        rows = list(activities)
    except Exception:
        return []
    out = []
    for activity in rows:
        try:
            code = _str_or_none(_first(activity, "transaction_code", "code",
                                              "transaction_type"))
            out.append(InsiderTransaction(
                insider_name=name,
                insider_cik=cik,
                issuer=issuer,
                form=form,
                filed_at=filed_at,
                accession_no=accession_no,
                transaction_date=_str_or_none(_first(
                    activity, "transaction_date", "date", "execution_date")),
                security=_str_or_none(_first(
                    activity, "security", "security_title", "title",
                    "security_name")),
                transaction_code=code,
                transaction_kind=classify_transaction(code),
                shares=_safe_int(_first(activity, "shares", "share_count",
                                               "num_shares", "amount",
                                               "shares_transacted")),
                price=_safe_float(_first(activity, "price", "price_per_share",
                                                "execution_price")),
                acquired_disposed=_str_or_none(_first(
                    activity, "acquired_disposed", "acquired_disposed_code",
                    "acquired_or_disposed", "action", "buy_or_sell")),
                holdings_after=_safe_int(_first(
                    activity, "holdings_after", "shares_owned_after",
                    "holdings", "balance_after", "shares_held")),
            ))
        except Exception:
            continue
    return out


def load_ownership(accession_no: str):
    """Live seam: filing.obj() stays here; raises on failure."""
    from .documents import get_by_accession_number

    return get_by_accession_number(accession_no).obj()


def get_insider_activity(ticker_or_cik, *, as_of=None, limit=50,
                         forms=_DEFAULT_FORMS):
    filings = list_sec_filings(ticker_or_cik, forms=list(forms),
                               as_of=as_of, limit=limit)
    out = []
    for filing in filings:
        try:
            accession = getattr(filing, "accession_no", "")
            form = getattr(filing, "form", "")
            filed_at = getattr(filing, "filed_at", None)
            issuer = getattr(filing, "company", None) or str(ticker_or_cik)
            out.extend(normalize_ownership_filing(
                load_ownership(accession), issuer=issuer, form=form,
                filed_at=filed_at, accession_no=accession))
        except Exception:
            continue
    if limit is not None:
        out = out[:limit]
    return out


def _sum_shares_column(df):
    """Sum the first 'share'-like column; None when nothing parses."""
    try:
        columns = list(getattr(df, "columns", None) or [])
    except Exception:
        columns = []
    if columns:
        target = next((c for c in columns if "share" in str(c).lower()), None)
        if target is None:
            return None
        try:
            values = df[target]
        except Exception:
            try:
                values = [row[target] for row in df]
            except Exception:
                return None
    else:
        try:
            rows = list(df)
        except Exception:
            return None
        if not rows:
            return None
        first = rows[0]
        if isinstance(first, dict):
            target = next((k for k in first if "share" in str(k).lower()), None)
            if target is None:
                return None
            values = [row.get(target) for row in rows]
        else:
            return None
    total = 0
    found = False
    try:
        iterator = list(values)
    except Exception:
        return None
    for value in iterator:
        parsed = _safe_int(value)
        if parsed is not None:
            total += parsed
            found = True
    return total if found else None


def normalize_144(form144, *, issuer, filed_at, accession_no):
    """Build a ProposedInsiderSale; never raises."""
    try:
        seller = _str_or_none(_first(form144, "person_selling", "seller_name",
                                            "seller", "reporting_owner", "name"))
        seller_cik = _str_or_none(_first(form144, "seller_cik", "person_cik",
                                                "cik", "owner_cik"))
        is_amend = bool(_first(form144, "is_amendment", "amendment") or False)
        if not is_amend:
            try:
                form = str(getattr(form144, "form", "") or "")
                is_amend = form.strip().upper().endswith("/A")
            except Exception:
                is_amend = False
        _ = is_amend
        try:
            df = getattr(form144, "securities_to_be_sold", None)
        except Exception:
            df = None
        shares = _sum_shares_column(df) if df is not None else None
        return ProposedInsiderSale(
            seller_name=seller,
            seller_cik=seller_cik,
            issuer=issuer,
            filed_at=filed_at,
            accession_no=accession_no,
            shares_proposed=shares,
        )
    except Exception:
        try:
            return ProposedInsiderSale(
                seller_name=None, seller_cik=None, issuer=issuer,
                filed_at=filed_at, accession_no=accession_no,
                shares_proposed=None)
        except Exception:
            raise


def load_144(accession_no: str):
    """Live seam: Form144 import stays here; raises on failure."""
    from .documents import get_by_accession_number

    filing = get_by_accession_number(accession_no)
    from edgar import Form144

    return Form144.from_filing(filing)


def get_planned_insider_sales(ticker_or_cik, *, as_of=None, limit=20,
                              forms=_DEFAULT_144_FORMS):
    filings = list_sec_filings(ticker_or_cik, forms=list(forms),
                               as_of=as_of, limit=limit)
    out = []
    for filing in filings:
        try:
            accession = getattr(filing, "accession_no", "")
            filed_at = getattr(filing, "filed_at", None)
            issuer = getattr(filing, "company", None) or str(ticker_or_cik)
            out.append(normalize_144(load_144(accession), issuer=issuer,
                                     filed_at=filed_at,
                                     accession_no=accession))
        except Exception:
            continue
    if limit is not None:
        out = out[:limit]
    return out


def compare_144_to_form4(proposed: ProposedInsiderSale, transactions) -> dict:
    """Match a 144 proposal against later open-market Form 4 sales."""
    try:
        rows = list(transactions or [])
    except Exception:
        rows = []
    seller = (proposed.seller_name or "")
    executed = 0
    for txn in rows:
        try:
            if getattr(txn, "transaction_kind", None) != "open_market_sale":
                continue
            name = getattr(txn, "insider_name", None) or ""
            if str(name).strip().lower() != seller.strip().lower():
                continue
            txn_date = getattr(txn, "transaction_date", None)
            if txn_date and proposed.filed_at and txn_date < proposed.filed_at:
                continue
            shares = getattr(txn, "shares", None)
            if shares is not None:
                executed += shares
        except Exception:
            continue
    matched = executed > 0
    note = (f"{seller or 'seller'} proposed {proposed.shares_proposed} shares; "
            f"{executed} executed in later open-market Form 4 sales"
            if proposed.shares_proposed is not None else
            f"{seller or 'seller'} proposed an unknown quantity; "
            f"{executed} executed in later open-market Form 4 sales")
    return {
        "seller_name": proposed.seller_name,
        "proposed_shares": proposed.shares_proposed,
        "executed_sale_shares": executed,
        "matched": matched,
        "note": note,
    }
