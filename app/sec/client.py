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


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hit_to_text_hit(search_id, attempt_id, query, hit, page_num):
    """EFTS hit -> SECTextHit; never infers identity beyond the filer."""
    from .models import SECTextHit

    raw_cik = getattr(hit, "cik", None)
    try:
        filer_cik: int | None = int(str(raw_cik).strip())
    except (TypeError, ValueError):
        filer_cik = None
    company = getattr(hit, "company", None)
    try:
        score = float(getattr(hit, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    items = getattr(hit, "items", None) or ()
    try:
        items = tuple(items)
    except TypeError:
        items = ()
    return SECTextHit(
        search_id=search_id,
        attempt_id=attempt_id,
        query=query,
        accession_no=str(getattr(hit, "accession_number", "")),
        form=str(getattr(hit, "form", "")),
        filed_at=str(getattr(hit, "filed", "") or ""),
        filer_cik=filer_cik,
        filer_name=None if company is None else str(company),
        matched_document=getattr(hit, "document_id", None),
        file_type=getattr(hit, "file_type", None),
        file_description=getattr(hit, "file_description", None),
        items=items,
        sic=getattr(hit, "sic", None),
        location=getattr(hit, "location", None),
        state=getattr(hit, "state", None),
        inc_state=getattr(hit, "inc_state", None),
        score=score,
        source_url=None,
        page=page_num,
    )


def search_sec_filings(
    query: str,
    forms: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 20,
    as_of: str | None = None,
):
    """EDGAR full-text search; text hits, never inferred identity.

    Paginates EFTS until the reported total, an empty page, the caller limit,
    a documented source cap, or failure. A caller bound yields ``partial``; a
    documented EFTS cap yields ``complete_within_source_limits``; transport
    failure never becomes zero matches.
    """
    from .models import (
        SECSearchRequest,
        SECSearchResult,
        SearchAttempt,
        SearchCoverage,
    )

    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"invalid query: {query!r}")
    if limit < 1:
        raise ValueError(f"invalid limit: {limit!r}")
    if as_of is not None:
        from .filings import _check_as_of

        as_of = _check_as_of(as_of)
    import uuid

    query = query.strip()
    search_id = uuid.uuid4().hex[:12]
    request = SECSearchRequest(
        query=query,
        forms=tuple(forms) if forms else None,
        start_date=start_date,
        end_date=end_date,
        as_of=as_of,
        max_results=limit,
    )
    filters = {
        key: value
        for key, value in (
            ("forms", list(forms) if forms else None),
            ("start_date", start_date),
            ("end_date", end_date),
            ("as_of", as_of),
        )
        if value is not None
    }
    attempts: list = []
    hits: list = []
    warnings: list = []
    errors: list = []
    seen: set = set()
    pit_gaps = 0

    def _attempt(page_num, status, reported, retrieved, **extra):
        now = _utcnow()
        attempts.append(SearchAttempt(
            attempt_id=f"{search_id}-p{page_num}",
            search_id=search_id,
            backend="efts",
            query=query,
            filters={**filters, "page": page_num},
            started_at=now,
            completed_at=now,
            status=status,
            results_reported=reported,
            results_retrieved=retrieved,
            pages_retrieved=1,
            truncated=status in ("partial", "source_limited"),
            pit_basis="filed_at",
            **extra,
        ))

    ensure_identity()
    from edgar.search.efts import search_filings

    try:
        page = search_filings(
            query, forms=forms, start_date=start_date, end_date=end_date,
            limit=min(limit, 100),
        )
    except Exception as exc:
        _attempt(0, "failed", 0, 0,
                 error_type=type(exc).__name__, error_message=str(exc))
        errors.append(f"efts page 1 failed: {exc}")
        return SECSearchResult(
            search_id=search_id, request=request,
            coverage=SearchCoverage(
                status="failed", sources_attempted=("efts",),
                sources_failed=("efts",),
                date_coverage=f"{start_date or ''}:{end_date or ''}" or None,
                forms_covered=tuple(forms) if forms else (),
            ),
            attempts=tuple(attempts), warnings=tuple(warnings),
            errors=tuple(errors), retrieval_order=("efts",),
        )
    reported = getattr(page, "total", None)
    if reported is None:
        reported = len(getattr(page, "results", None) or [])
    try:
        reported = int(reported)
    except (TypeError, ValueError):
        reported = 0
    page_num = 0
    source_capped = False
    failed_tail = False
    while page is not None and len(hits) < limit:
        page_num += 1
        results = getattr(page, "results", None) or []
        if not results:
            _attempt(page_num, "complete", reported, 0)
            break
        retrieved = 0
        for hit in results:
            if len(hits) >= limit:
                break
            text_hit = _hit_to_text_hit(
                search_id, f"{search_id}-p{page_num}", query, hit, page_num)
            filed = (text_hit.filed_at or "")[:10]
            if as_of is not None and (not filed or filed > as_of):
                pit_gaps += 1
                continue
            key = (query, text_hit.accession_no, text_hit.matched_document)
            if key in seen:
                continue
            seen.add(key)
            hits.append(text_hit)
            retrieved += 1
        _attempt(page_num, "complete", reported, retrieved)
        if len(hits) >= limit or len(hits) + pit_gaps >= reported:
            break
        nxt = getattr(page, "next", None)
        if not callable(nxt):
            break
        try:
            page = nxt()
        except Exception as exc:
            failed_tail = True
            _attempt(page_num + 1, "failed", reported, 0,
                     error_type=type(exc).__name__, error_message=str(exc))
            errors.append(f"efts page {page_num + 1} failed: {exc}")
            break
        if page is None:
            source_capped = True
            break
    if pit_gaps:
        warnings.append(
            f"{pit_gaps} EFTS hit(s) excluded by as_of {as_of} "
            "(no usable filed date or filed after as_of)")
    if failed_tail:
        status = "partial"
    elif len(hits) + pit_gaps >= reported and not source_capped:
        status = "complete"
    elif source_capped or reported > 10_000:
        status = "complete_within_source_limits"
    elif len(hits) >= limit and len(hits) + pit_gaps < reported:
        status = "partial"
    elif not hits and not reported:
        status = "complete"
    else:
        status = "partial"
    limits: tuple = ()
    if status == "complete_within_source_limits":
        limits = ("efts:deep-pagination-cap",)
        warnings.append(
            f"EFTS reports {reported} hits; retrieved {len(hits)} "
            "within source limits")
    completed = ("efts",) if status != "failed" else ()
    return SECSearchResult(
        search_id=search_id,
        request=request,
        text_hits=tuple(hits),
        coverage=SearchCoverage(
            status=status,
            sources_attempted=("efts",),
            sources_completed=completed,
            sources_failed=("efts",) if failed_tail else (),
            source_limits=limits,
            results_reported=reported,
            results_retrieved=len(hits),
            pages=page_num,
            date_coverage=f"{start_date or ''}:{end_date or ''}" or None,
            forms_covered=tuple(forms) if forms else (),
        ),
        attempts=tuple(attempts),
        warnings=tuple(warnings),
        errors=tuple(errors),
        retrieval_order=("efts",),
    )


def get_cik_lookup_candidates(query: str, limit: int = 10) -> list[dict[str, object]]:
    """General legal-name to CIK candidates via SEC ``cik-lookup-data.txt``.

    Unlike the ticker-company index behind :func:`find_sec_company`, this
    dataset includes no-ticker registrants. Matching is a deterministic
    normalized substring scan (exact, then prefix, then substring); network or
    parse failure returns ``[]`` and never raises. ``limit``/blank violations
    raise ``ValueError`` like the other discovery adapters.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError(f"invalid query: {query!r}")
    if limit < 1:
        raise ValueError(f"invalid limit: {limit!r}")
    try:
        ensure_identity()
        from edgar.entity.tickers import get_cik_lookup_data

        frame = get_cik_lookup_data()
    except Exception:
        return []
    try:
        import re
        import unicodedata

        def _norm(value: object) -> str:
            text = unicodedata.normalize("NFKD", str(value or "")).casefold()
            return re.sub(r"\s+", " ", re.sub(r"[\W_]+", " ", text)).strip()

        want = _norm(query)
        if not want:
            return []
        rows = []
        for row in frame.itertuples():
            try:
                cik = int(str(getattr(row, "cik")).strip())
            except (TypeError, ValueError):
                continue
            name = str(getattr(row, "name", ""))
            normed = _norm(name)
            if normed == want:
                rank = 0
            elif normed.startswith(want):
                rank = 1
            elif want in normed:
                rank = 2
            else:
                continue
            rows.append((rank, name, cik))
    except Exception:
        return []
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    return [
        {"name": name, "cik": cik, "tickers": [], "exchange": None}
        for _, name, cik in rows[:limit]
    ]


def _maybe_str(value: object) -> str | None:
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _address_dict(address: object) -> dict[str, object | None]:
    get = (lambda name: address.get(name)) if isinstance(address, dict) else (
        lambda name: getattr(address, name, None)
    )
    out: dict[str, object | None] = {}
    for key in ("street1", "street2", "city", "stateOrCountry",
                "stateOrCountryDescription", "zipCode"):
        try:
            out[key] = _maybe_str(get(key))
        except Exception:
            out[key] = None
    return out


def get_submissions_metadata(cik: int | str) -> dict[str, object] | None:
    """Authoritative per-CIK metadata from SEC submissions.

    Returns current name, ``formerNames`` (with SEC ``from``/``to`` dates),
    tickers, SIC, state of incorporation, addresses, and a light filing
    history. Unknown CIK, network, or parse failure returns ``None`` and
    never raises.
    """
    try:
        cik_int = int(str(cik).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        ensure_identity()
        from edgar.entity.submissions import get_entity_submissions

        data = get_entity_submissions(cik_int)
    except Exception:
        return None
    if data is None:
        return None
    try:
        raw_tickers = getattr(data, "tickers", None) or []
        try:
            tickers = [str(t).strip() for t in raw_tickers if str(t).strip()]
        except TypeError:
            tickers = []
        raw_exchanges = getattr(data, "exchanges", None) or []
        try:
            exchanges = [str(e).strip() for e in raw_exchanges if str(e).strip()]
        except TypeError:
            exchanges = []
        former: list[dict[str, object | None]] = []
        for entry in getattr(data, "former_names", None) or []:
            if isinstance(entry, dict):
                get = entry.get
                former.append({
                    "name": _maybe_str(get("name")),
                    "from": _maybe_str(get("from")),
                    "to": _maybe_str(get("to")),
                    "type": _maybe_str(get("type")),
                })
        history: list[dict[str, object | None]] = []
        try:
            filings = getattr(data, "filings", None)
            candidates = getattr(filings, "data", filings)
            for item in list(candidates)[:5]:
                history.append({
                    "form": _maybe_str(getattr(item, "form", None)),
                    "filed_at": _maybe_str(getattr(item, "filing_date", None)),
                    "accession_no": _maybe_str(getattr(item, "accession_number", None)),
                })
        except Exception:
            history = []
        sic = getattr(data, "sic", None)
        try:
            sic_s = None if sic is None else str(sic).strip() or None
        except Exception:
            sic_s = None
        return {
            "cik": cik_int,
            "name": _maybe_str(getattr(data, "name", None)),
            "tickers": tickers,
            "exchanges": exchanges,
            "sic": sic_s,
            "sic_description": _maybe_str(getattr(data, "sic_description", None)),
            "entity_type": _maybe_str(getattr(data, "entity_type", None)),
            "state_of_incorporation": _maybe_str(
                getattr(data, "state_of_incorporation", None)),
            "business_address": _address_dict(getattr(data, "business_address", None)),
            "mailing_address": _address_dict(getattr(data, "mailing_address", None)),
            "former_names": former,
            "filing_history": history,
        }
    except Exception:
        return None


def get_global_filings(year=None, quarter=None, form=None, filing_date=None,
                       *, amendments=True):
    """Global quarterly filing index -> normalized ``Filing`` list.

    Thin wrapper over installed ``edgar.get_filings`` for 1993+ quarterly
    partitions. Arbitrary form strings pass straight through (never an
    allowlist); ``None`` from edgar becomes ``[]``. Transport/parse failure
    raises so the caller records a failed attempt (never silent zero
    matches); per-row normalization failures are skipped.
    """
    ensure_identity()
    import edgar

    from .normalization import filing_from_edgar

    filings = edgar.get_filings(
        year, quarter, form=form, amendments=amendments,
        filing_date=filing_date)
    if filings is None:
        return []
    try:
        iterator = iter(filings)
    except TypeError:
        return []
    out = []
    for item in iterator:
        try:
            out.append(filing_from_edgar(item))
        except Exception:
            continue
    return out


def get_current_filings(form="", *, page_size=40, owner="include"):
    """Current-quarter SEC feed -> normalized ``Filing`` list.

    Thin wrapper over installed ``edgar.get_current_filings`` (near
    real-time, ~last 24h). Same failure contract as :func:`get_global_filings`.
    """
    ensure_identity()
    import edgar

    from .normalization import filing_from_edgar

    feed = edgar.get_current_filings(
        form=form or "", page_size=page_size, owner=owner)
    if feed is None:
        return []
    try:
        iterator = iter(feed)
    except TypeError:
        return []
    out = []
    for item in iterator:
        try:
            out.append(filing_from_edgar(item))
        except Exception:
            continue
    return out
