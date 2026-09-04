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
