"""edgartools-only access. `edgar` is imported lazily so module import
has no side effects and never touches the network."""

_initialized = False


def ensure_identity() -> None:
    """Set the SEC identity once; later calls are no-ops."""
    global _initialized
    if _initialized:
        return
    from edgar import set_identity

    from ..config import get_sec_edgar_identity

    set_identity(get_sec_edgar_identity())
    _initialized = True


def get_company(ticker_or_cik):
    """Company handle: digits/int go by CIK, anything else by ticker."""
    ensure_identity()
    from edgar import Company

    if isinstance(ticker_or_cik, int) or (
        isinstance(ticker_or_cik, str) and ticker_or_cik.strip().isdigit()
    ):
        return Company(int(str(ticker_or_cik).strip()))
    return Company(ticker_or_cik)


def resolve_cik(ticker_or_cik) -> int | None:
    """Ticker/CIK to int CIK; None on failure, never raises."""
    try:
        if isinstance(ticker_or_cik, int):
            return ticker_or_cik
        if isinstance(ticker_or_cik, str) and ticker_or_cik.strip().isdigit():
            return int(ticker_or_cik.strip())
        return int(get_company(ticker_or_cik).cik)
    except Exception:
        return None


def find_sec_company(query: str, limit: int = 10) -> list[dict[str, object]]:
    """Issuer name to candidate CIKs via the edgartools company index."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"invalid query: {query!r}")
    if limit < 1:
        raise ValueError(f"invalid limit: {limit!r}")
    ensure_identity()
    from edgar.entity.search import find_company

    results = find_company(query, top_n=limit)
    frame = getattr(results, "results", None)
    if frame is None or getattr(frame, "empty", False):
        return []
    out: list[dict[str, object]] = []
    for row in frame.itertuples():
        try:
            cik = int(str(getattr(row, "cik")).strip())
        except (TypeError, ValueError):
            continue
        ticker = getattr(row, "ticker", None)
        ticker_s = "" if ticker is None else str(ticker).strip()
        if ticker_s.lower() == "nan":
            ticker_s = ""
        out.append({
            "name": str(getattr(row, "company", "")),
            "cik": cik,
            "tickers": [ticker_s] if ticker_s else [],
            "exchange": None,
        })
        if len(out) >= limit:
            break
    return out


def search_sec_filings(
    query: str,
    forms: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
) -> list[dict[str, object]]:
    """EDGAR full-text search; text hits, never inferred identity."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"invalid query: {query!r}")
    if limit < 1:
        raise ValueError(f"invalid limit: {limit!r}")
    ensure_identity()
    from edgar.search.efts import search_filings

    found = search_filings(
        query, forms=forms, start_date=start_date, end_date=end_date, limit=limit
    )
    out: list[dict[str, object]] = []
    for hit in (getattr(found, "results", None) or [])[:limit]:
        raw_cik = getattr(hit, "cik", None)
        try:
            cik: int | None = int(str(raw_cik).strip())
        except (TypeError, ValueError):
            cik = None
        company = getattr(hit, "company", None)
        period = getattr(hit, "period", None)
        try:
            score = float(getattr(hit, "score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        out.append({
            "company": None if company is None else str(company),
            "cik": cik,
            "form": str(getattr(hit, "form", "")),
            "filed": str(getattr(hit, "filed", "")),
            "accession_no": str(getattr(hit, "accession_number", "")),
            "source_url": None,
            "score": score,
            "period": None if period is None else str(period),
        })
    return out
