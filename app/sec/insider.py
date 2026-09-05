"""Deterministic Forms 3/4/5 + 144 insider normalization (no network)."""

from datetime import datetime, timezone
from pathlib import Path

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


def _owners_of(obj, fallback_name=None, fallback_cik=None):
    """Reporting owners: structured list first, legacy attrs as one owner."""
    try:
        container = getattr(obj, "reporting_owners", None)
        candidates = getattr(container, "owners", None) if container is not None else None
        if candidates is None and isinstance(container, (list, tuple)):
            candidates = container
        owners = list(candidates) if candidates else []
    except Exception:
        owners = []
    out = []
    for owner in owners:
        try:
            out.append({
                "name": _str_or_none(_first(
                    owner, "name_unreversed", "name", "owner_name")),
                "cik": _str_or_none(_first(
                    owner, "cik", "owner_cik", "reporting_owner_cik")),
                "is_director": _first(owner, "is_director"),
                "is_officer": _first(owner, "is_officer"),
                "is_ten_percent": _first(
                    owner, "is_ten_pct_owner", "is_ten_percent"),
                "is_other": _first(owner, "is_other"),
                "role_title": _str_or_none(_first(
                    owner, "officer_title", "role_title", "title")),
            })
        except Exception:
            continue
    if out:
        return out
    return [{
        "name": _str_or_none(fallback_name),
        "cik": _str_or_none(fallback_cik),
        "is_director": None,
        "is_officer": None,
        "is_ten_percent": None,
        "is_other": None,
        "role_title": None,
    }]


def _issuer_of(obj):
    """Authoritative issuer from structured ownership XML; never the filer."""
    try:
        issuer = getattr(obj, "issuer", None)
    except Exception:
        return None, None, None
    if issuer is None or isinstance(issuer, str):
        return None, None, None
    cik = _str_or_none(_first(issuer, "cik", "issuer_cik", "issuerCIK"))
    name = _str_or_none(_first(issuer, "name", "issuer_name", "issuerName"))
    ticker = _str_or_none(_first(issuer, "ticker", "symbol"))
    return cik, name, ticker


def _bool_or_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        text = str(value).strip().lower()
    except Exception:
        return None
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return None


def normalize_ownership_filing(obj, *, issuer, form, filed_at, accession_no,
                               issuer_cik=None, document_name=None,
                               known_at=None):
    """One InsiderTransaction per (owner, activity) row; never raises."""
    try:
        fallback_name = _str_or_none(_first(obj, "insider_name",
                                            "reporting_owner", "owner_name",
                                            "name"))
        fallback_cik = _insider_cik(obj)
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
    info_cik, info_name, _ticker = _issuer_of(obj)
    try:
        explicit_cik = str(issuer_cik).strip() if issuer_cik is not None else None
    except Exception:
        explicit_cik = None
    resolved_issuer_cik = explicit_cik or info_cik
    resolved_issuer = info_name or issuer
    try:
        known = str(known_at) if known_at is not None else filed_at
    except Exception:
        known = filed_at
    owners = _owners_of(obj, fallback_name, fallback_cik)
    out = []
    for activity in rows:
        for owner in owners:
            try:
                code = _str_or_none(_first(activity, "transaction_code",
                                                  "code", "transaction_type"))
                out.append(InsiderTransaction(
                    insider_name=owner["name"],
                    insider_cik=owner["cik"],
                    issuer=resolved_issuer,
                    form=form,
                    filed_at=filed_at,
                    accession_no=accession_no,
                    transaction_date=_str_or_none(_first(
                        activity, "transaction_date", "date",
                        "execution_date")),
                    security=_str_or_none(_first(
                        activity, "security", "security_title", "title",
                        "security_name")),
                    transaction_code=code,
                    transaction_kind=classify_transaction(code),
                    shares=_safe_int(_first(activity, "shares", "share_count",
                                                   "num_shares", "amount",
                                                   "shares_transacted",
                                                   "shares_numeric")),
                    price=_safe_float(_first(activity, "price",
                                                    "price_per_share",
                                                    "price_numeric",
                                                    "execution_price")),
                    acquired_disposed=_str_or_none(_first(
                        activity, "acquired_disposed",
                        "acquired_disposed_code", "acquired_or_disposed",
                        "action", "buy_or_sell")),
                    holdings_after=_safe_int(_first(
                        activity, "holdings_after", "shares_owned_after",
                        "holdings", "balance_after", "shares_held")),
                    issuer_cik=resolved_issuer_cik,
                    is_director=_bool_or_none(owner["is_director"]),
                    is_officer=_bool_or_none(owner["is_officer"]),
                    is_ten_percent=_bool_or_none(owner["is_ten_percent"]),
                    is_other=_bool_or_none(owner["is_other"]),
                    role_title=owner["role_title"],
                    document_name=document_name,
                    known_at=known,
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
            issuer = getattr(filing, "filer_name", None) or str(ticker_or_cik)
            obj = load_ownership(accession)
            try:
                _cik, _name, _t = _issuer_of(obj)
                issuer = _name or issuer
            except Exception:
                pass
            out.extend(normalize_ownership_filing(
                obj, issuer=issuer, form=form, filed_at=filed_at,
                accession_no=accession,
                document_name=getattr(filing, "primary_document", None),
                known_at=getattr(filing, "known_at", None) or filed_at))
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


def normalize_144(form144, *, issuer, filed_at, accession_no, issuer_cik=None,
                   form=None, document_name=None, known_at=None):
    """Build a ProposedInsiderSale; never raises."""
    try:
        seller = _str_or_none(_first(form144, "person_selling", "seller_name",
                                            "seller", "reporting_owner", "name"))
        seller_cik = _str_or_none(_first(form144, "seller_cik", "person_cik",
                                                "cik", "owner_cik"))
        resolved_form = form
        try:
            embedded = str(getattr(form144, "form", "") or "").strip()
            if embedded:
                resolved_form = resolved_form or embedded
        except Exception:
            pass
        try:
            df = getattr(form144, "securities_to_be_sold", None)
        except Exception:
            df = None
        shares = _sum_shares_column(df) if df is not None else None
        try:
            known = str(known_at) if known_at is not None else filed_at
        except Exception:
            known = filed_at
        return ProposedInsiderSale(
            seller_name=seller,
            seller_cik=seller_cik,
            issuer=issuer,
            filed_at=filed_at,
            accession_no=accession_no,
            shares_proposed=shares,
            issuer_cik=str(issuer_cik).strip() if issuer_cik is not None else None,
            form=resolved_form,
            document_name=document_name,
            known_at=known,
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
            issuer = getattr(filing, "filer_name", None) or str(ticker_or_cik)
            out.append(normalize_144(
                load_144(accession), issuer=issuer, filed_at=filed_at,
                accession_no=accession, form=getattr(filing, "form", None),
                document_name=getattr(filing, "primary_document", None),
                known_at=getattr(filing, "known_at", None) or filed_at))
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

_FORMS_13F = ("13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A")
_FORMS_13F_HOLDINGS = ("13F-HR", "13F-HR/A")
_FORMS_13F_NOTICE = ("13F-NT", "13F-NT/A")


def is_13f_notice(form) -> bool:
    """True for 13F-NT notice filings: manager filing with no holdings."""
    try:
        return str(form or "").strip().upper() in _FORMS_13F_NOTICE
    except Exception:
        return False


def _voting_label(sole=None, shared=None, non=None) -> "str | None":
    parts = []
    for label, value in (("sole", sole), ("shared", shared), ("none", non)):
        try:
            if value is None:
                continue
            parts.append(f"{label}={int(value)}")
        except Exception:
            continue
    return " ".join(parts) or None


def _holding_row_to_record(row, *, manager_name, manager_cik, accession_no,
                           report_period, filed_at, document_name, known_at,
                           source_url):
    """One information-table row -> InstitutionalHolding; raises on bad row."""
    from .models import InstitutionalHolding

    def _cell(*names):
        for name in names:
            try:
                if isinstance(row, dict):
                    value = row.get(name)
                else:
                    value = getattr(row, name, None)
                    if value is None and hasattr(row, "get"):
                        value = row.get(name)
            except Exception:
                continue
            if value is not None and str(value).strip() != "":
                return value
        return None

    cusip = _str_or_none(_cell("Cusip", "cusip", "CUSIP"))
    if cusip is not None:
        cusip = "".join(ch for ch in cusip if ch.isalnum()).upper() or None
    put_call = _str_or_none(_cell("PutCall", "put_call", "putCall"))
    if put_call is not None:
        put_call = put_call.strip().title() or None
    shares = _safe_int(_cell("SharesPrnAmount", "shares", "sshPrnamt",
                             "share_count"))
    value = _safe_int(_cell("Value", "value", "market_value"))
    sole = _safe_int(_cell("SoleVoting", "sole_voting", "Sole"))
    shared = _safe_int(_cell("SharedVoting", "shared_voting", "Shared"))
    non = _safe_int(_cell("NonVoting", "non_voting", "None"))
    try:
        known = str(known_at) if known_at is not None else filed_at
    except Exception:
        known = filed_at
    return InstitutionalHolding(
        manager_name=_str_or_none(manager_name),
        manager_cik=str(manager_cik).strip() if manager_cik is not None else None,
        accession_no=accession_no,
        report_period=_str_or_none(_cell("ReportPeriod", "report_period")) or _str_or_none(report_period),
        issuer_name=_str_or_none(_cell("Issuer", "issuer_name",
                                              "nameOfIssuer", "issuer")),
        entity_id=None,
        security_id=None,
        class_title=_str_or_none(_cell("Class", "class_title",
                                              "titleOfClass")),
        cusip=cusip,
        isin=_str_or_none(_cell("Isin", "isin", "ISIN")),
        shares=shares,
        value=value,
        put_call=put_call,
        discretion=_str_or_none(_cell("InvestmentDiscretion", "discretion",
                                             "investment_discretion",
                                             "OtherManager")),
        voting=_voting_label(sole, shared, non),
        filed_at=filed_at,
        known_at=known,
        document_name=document_name,
        source_url=source_url,
    )


def normalize_13f_holdings(infotable, *, manager_name=None, manager_cik=None,
                           accession_no, report_period=None, filed_at=None,
                           form=None, document_name=None, known_at=None,
                           source_url=None):
    """13F-HR/A information tables -> holdings; NT -> no holdings. Never raises."""
    from .models import InstitutionalHolding  # noqa: F401

    if is_13f_notice(form):
        return []
    try:
        rows = infotable.to_dict(orient="records") if hasattr(
            infotable, "to_dict") else list(infotable or [])
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    try:
        manager_cik = str(manager_cik).strip() if manager_cik is not None else None
    except Exception:
        manager_cik = None
    out = []
    for row in rows:
        try:
            out.append(_holding_row_to_record(
                row, manager_name=manager_name, manager_cik=manager_cik,
                accession_no=accession_no, report_period=report_period,
                filed_at=filed_at, document_name=document_name,
                known_at=known_at, source_url=source_url))
        except Exception:
            continue
    return out


def observe_13f_security(holding, *, accession_no=None, filed_at=None,
                         known_at=None, source_url=None, root=None) -> int:
    """Persist CUSIP/ISIN/ticker/class-title observations as Security + aliases.

    Identifier evidence only: never derives issuer ``entity_id`` identity.
    Returns rows written across the security/alias datasets.
    """
    from ..storage import parquet
    from ..storage.raw_archive import content_hash as _hash

    try:
        cusip = getattr(holding, "cusip", None)
        cusip = "".join(ch for ch in str(cusip) if ch.isalnum()).upper() if cusip else None
    except Exception:
        cusip = None
    try:
        isin = str(getattr(holding, "isin", None) or "").strip().upper() or None
    except Exception:
        isin = None
    try:
        class_title = str(getattr(holding, "class_title", None) or "").strip() or None
    except Exception:
        class_title = None
    if not cusip and not isin:
        return 0
    try:
        known = str(known_at or getattr(holding, "known_at", None)
                    or filed_at or getattr(holding, "filed_at", None))
    except Exception:
        known = None
    retrieved = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    security_id = f"cusip:{cusip}" if cusip else f"isin:{isin}"
    seed = "|".join(part or "" for part in (
        security_id, class_title or "", str(accession_no or "")))
    security_row = {
        "security_id": security_id,
        "entity_id": None,
        "security_type": "equity",
        "ticker": None,
        "exchange": None,
        "source": "sec-13f",
        "known_at": known,
        "retrieved_at": retrieved,
        "content_hash": _hash(seed.encode("utf-8")),
        "parser_version": "1",
        "cik": None,
        "accession": accession_no,
        "source_url": source_url or getattr(holding, "source_url", None),
        "raw_archive_path": None,
        "cusip": cusip,
        "isin": isin,
        "class_title": class_title,
    }
    written = parquet.write_rows(
        "securities", [security_row],
        root=Path(root) / "parquet" if root is not None else None)
    aliases = []
    if cusip:
        aliases.append(("cusip", cusip))
    if isin:
        aliases.append(("isin", isin))
    if class_title:
        aliases.append(("class_title", class_title))
    for alias_type, alias_value in aliases:
        aliases_seed = "|".join((alias_type, alias_value, security_id))
        written += parquet.write_rows(
            "entity_aliases", [{
                "alias_type": alias_type,
                "alias_value": alias_value,
                "entity_id": security_id,
                "security_id": security_id,
                "source": "sec-13f",
                "valid_from": None,
                "valid_to": None,
                "known_at": known,
                "retrieved_at": retrieved,
                "content_hash": _hash(aliases_seed.encode("utf-8")),
                "parser_version": "1",
                "cik": None,
                "accession": accession_no,
                "source_url": source_url or getattr(holding, "source_url", None),
            }],
            root=Path(root) / "parquet" if root is not None else None)
    return written


def query_issuer_insiders(issuer_cik, *, as_of=None, root=None, limit=200):
    """Issuer -> reporting owners over ``sec_insider_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_insider_transactions(
        issuer_cik=issuer_cik, as_of=as_of, root=root, limit=limit)


def query_person_transactions(owner_cik, *, as_of=None, root=None, limit=200):
    """Person -> transactions over ``sec_insider_transactions`` (PIT)."""
    from . import store as _store

    return _store.query_insider_transactions(
        owner_cik=owner_cik, as_of=as_of, root=root, limit=limit)


def query_manager_holdings(manager_cik, *, as_of=None, root=None, limit=200):
    """Manager -> holdings over ``sec_13f_holdings`` (PIT)."""
    from . import store as _store

    return _store.query_13f_holdings(
        manager_cik=manager_cik, as_of=as_of, root=root, limit=limit)


def query_security_managers(security, *, as_of=None, root=None, limit=200):
    """Security (CUSIP/ISIN/security_id) -> managers over ``sec_13f_holdings``."""
    from . import store as _store

    return _store.query_13f_holdings(
        security=security, as_of=as_of, root=root, limit=limit)
