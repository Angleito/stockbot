"""All SEC EDGAR access lives here. tools.py never imports edgartools directly."""

import datetime as _dt
import difflib
import logging
from typing import Any, Optional

from edgar import Company

from . import cache
from .config import get_sec_edgar_identity, init_config

logger = logging.getLogger(__name__)

_initialized = False


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        init_config()
        # edgartools identity lives in app/sec/client.py (the edgar boundary).
        from app.sec.client import ensure_identity

        ensure_identity()
        _initialized = True


def _no_data(ticker: str, what: str) -> dict:
    return {"error": f"No data found for {ticker}: {what}"}


def get_company(ticker: str) -> Company:
    """edgartools Company handle for a ticker (lazy init inside)."""
    _ensure_init()
    from app.sec.client import get_company as _sec_get_company

    return _sec_get_company(ticker)


def get_latest_report(ticker: str, form_type: str = "10-K"):
    """Latest filing object + parsed document for one form, or None.

    The single seam behind the obligations/valuation report reads; keeps the
    ``edgar`` import boundary inside this module."""
    filings = get_company(ticker).get_filings(form=[form_type])
    if not filings:
        return None
    filing = filings[0]
    return filing, filing.obj()


def _cached_or_fetch(key: str, fetch):
    """Check cache first; on miss, run fetch() and store the result."""
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = fetch()
    cache.set(key, value)
    return value


_QUARTER_DAYS = (60, 115)
_YTD_DAYS = (240, 300)
_FY_DAYS = (330, 400)
_MISSING_QUARTER_GAP_DAYS = 130
_DERIVED_Q4_OFFSET_DAYS = 91


def _fact_duration_days(frame) -> Any:
    """Duration in days between period_start and period_end (XBRL facts)."""
    import pandas as pd

    start = pd.to_datetime(frame["period_start"], errors="coerce")
    end = pd.to_datetime(frame["period_end"], errors="coerce")
    return (end - start).dt.days


def _dedup_latest(frame) -> Any:
    """Drop duplicate period_end facts, keeping the most recently filed one.

    XBRL company facts can carry restated values for the same period. The
    dataframe has no filing-date column, so the latest-filed fact is
    approximated by the largest fiscal_year (restatements are tagged with
    the year they were reported in).
    """
    return frame.sort_values(["period_end", "fiscal_year"]).drop_duplicates(
        subset=["period_end"], keep="last"
    )


def _quarters_with_derived_q4(quarterly, full_facts, concept) -> Any:
    """Return the last 4 quarterly facts, deriving a missing quarter end.

    XBRL company facts hold quarterly (~3-month), YTD (6-9 month), and
    full-year values for the same period_end. NVDA reports its Q4 diluted
    EPS only as a full-year fact, so the Q4 quarter is derived as
    FY_total - YTD_through_Q3 from the matching YTD and FY facts. Without
    this, the TTM window silently drops Q4 and double-counts a stale
    quarter (old behavior produced 8.13 instead of 6.53 for NVDA).
    """
    import pandas as pd

    quarter = quarterly.copy().sort_values("period_end")
    if len(quarter) < 2:
        return quarter.tail(4)
    ends = pd.to_datetime(quarter["period_end"], errors="coerce")
    gaps = ends.diff().dt.days
    if len(gaps) >= 2 and gaps.iloc[-1] is not None and float(gaps.iloc[-1]) > _MISSING_QUARTER_GAP_DAYS:
        # One quarter between the last two period_ends is missing (usually
        # Q4, reported only as a full-year fact). The missing quarter ends
        # ~91 days before the latest period_end.
        missing_end = ends.iloc[-1] - pd.Timedelta(days=_DERIVED_Q4_OFFSET_DAYS)
        derived = _derive_q4_from_facts(full_facts, concept, missing_end)
        if derived is not None:
            quarter = pd.concat([quarter, derived], ignore_index=True).sort_values("period_end")
    return quarter.tail(4)


def _derive_q4_from_facts(full_facts, concept, fy_end) -> Any:
    """Derive Q4 EPS = FY_total - YTD_through_Q3 for the fiscal year ending fy_end."""
    import pandas as pd

    facts = full_facts[
        full_facts["concept"].isin([concept, concept.split(":")[-1]])
    ].copy()
    facts["duration_days"] = _fact_duration_days(facts)
    fy_end = pd.Timestamp(fy_end)
    fy = facts[(facts["duration_days"].between(*_FY_DAYS)) & (pd.to_datetime(facts["period_end"]) == fy_end)]
    if fy.empty:
        return None
    fy_total = float(fy["value"].iloc[0])
    ytd = facts[
        (facts["duration_days"].between(*_YTD_DAYS))
        & (pd.to_datetime(facts["period_end"]) >= fy_end - pd.Timedelta(days=_MISSING_QUARTER_GAP_DAYS))
        & (pd.to_datetime(facts["period_end"]) < fy_end)
    ]
    if ytd.empty:
        return None
    ytd_q3 = float(ytd.sort_values("period_end")["value"].iloc[-1])
    q4 = fy_total - ytd_q3
    row = fy.iloc[0].copy()
    row["value"] = q4
    row["fiscal_period"] = "Q4"
    return pd.DataFrame([row])


_DIVIDEND_CONCEPT = "CommonStockDividendsPerShareDeclared"
_DIVIDEND_SOURCE = "SEC EDGAR company facts (Declared dividends per share)"

def _null_dividend_payload(ticker: str) -> dict:
    """Coverage uncertainty: concept absence is not proof of a nonpayer."""
    return {
        "ticker": ticker,
        "dividend_status": "insufficient_data",
        "ttm_dividend_per_share": None,
        "ttm_dividend_yield": None,
        "price": None,
        "price_source": None,
        "price_as_of": None,
        "growth_1y": None,
        "growth_3y_cagr": None,
        "growth_5y_cagr": None,
        "growth_10y_cagr": None,
        "annual_history": [],
        "source": _DIVIDEND_SOURCE,
    }


def _has_contiguous_quarters(period_ends: list[Any]) -> bool:
    """True only for exactly four parseable ends with quarterly gaps."""
    if len(period_ends) != 4:
        return False
    try:
        ends = sorted(_dt.date.fromisoformat(str(p)[:10]) for p in period_ends)
    except (TypeError, ValueError):
        return False
    return all(_QUARTER_DAYS[0] <= (b - a).days <= _QUARTER_DAYS[1] for a, b in zip(ends, ends[1:]))


def _dividend_growth(annual: dict) -> dict:
    """Exact-gap growth/CAGR over annual totals; missing gaps stay null."""
    out = {"growth_1y": None, "growth_3y_cagr": None, "growth_5y_cagr": None, "growth_10y_cagr": None}
    if not annual:
        return out
    latest_year = max(annual)
    latest = annual[latest_year]
    for key, n in (("growth_1y", 1), ("growth_3y_cagr", 3), ("growth_5y_cagr", 5), ("growth_10y_cagr", 10)):
        prev = annual.get(latest_year - n)
        if prev is None or prev <= 0:
            continue
        if n == 1:
            out[key] = round((latest - prev) / prev, 4)
        else:
            out[key] = round((latest / prev) ** (1.0 / n) - 1, 4)
    return out


def _dividend_valuation(ticker: str, ttm: Any, *, price_as_of: Optional[str]) -> dict[str, Any]:
    """Point-in-time valuation: historical requests expose no price metadata."""
    nulls = {"ttm_dividend_yield": None, "price": None, "price_source": None, "price_as_of": None}
    if price_as_of is None or ttm is None:
        return dict(nulls)
    try:
        ttm_f = float(ttm)
    except (TypeError, ValueError):
        return dict(nulls)
    try:
        from . import valuation as _valuation
        quote = _valuation.get_live_price(ticker)
    except Exception:
        return dict(nulls)
    try:
        price = float(quote) if quote is not None else None
    except (TypeError, ValueError):
        return dict(nulls)
    if price is None or price <= 0:
        return dict(nulls)
    return {"ttm_dividend_yield": round(ttm_f / price, 4), "price": price, "price_source": "yahoo", "price_as_of": price_as_of}


def _dividend_annual_history(rows: list[dict]) -> tuple[list[dict], dict]:
    """Full-year-duration facts keyed by calendar year of period_end."""
    by_year: dict[int, tuple[str, float]] = {}
    for r in rows:
        try:
            end = _dt.date.fromisoformat(str(r.get("period_end"))[:10])
            val = float(r.get("value"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        prev = by_year.get(end.year)
        if prev is None or str(r.get("period_end")) > prev[0]:
            by_year[end.year] = (str(r.get("period_end")), val)
    history: list[dict] = []
    annual: dict[int, float] = {}
    for fy in sorted(by_year, reverse=True):
        total = round(by_year[fy][1], 4)
        annual[fy] = total
        history.append({"fiscal_year": fy, "dividend_per_share": total})
    return history, annual


def get_fundamentals(ticker: str, metric: str, *, include_dividend_price: bool = True) -> dict:
    """Return a specific fundamental for ticker.

    metric: 'eps' | 'dividends' | 'balance_sheet' | 'shares_outstanding' | 'overview'

    'shares_float' is accepted as a deprecated alias for
    'shares_outstanding': it returns SEC-reported shares outstanding, not
    public float, and the response says so explicitly.
    """
    _ensure_init()
    if metric == "shares_float":
        metric = "shares_outstanding"
    key = f"fundamentals:{ticker}:{metric}"
    result = _cached_or_fetch(key, lambda: _fetch_fundamentals(ticker, metric))
    if metric == "dividends" and isinstance(result, dict) and "error" not in result:
        result = dict(result)
        result.update(_dividend_valuation(ticker, result.get("ttm_dividend_per_share"), price_as_of=_dt.date.today().isoformat() if include_dividend_price else None))
    return result


def _fetch_fundamentals(ticker: str, metric: str) -> dict:
    try:
        company = Company(ticker)
        if metric == "overview":
            return {
                "ticker": ticker,
                "name": company.name,
                "cik": company.cik,
                "industry": getattr(company, "sic_description", None),
            }
        if metric == "shares_outstanding":
            facts = company.get_facts()
            df = facts.to_dataframe()
            shares = df[df["concept"].isin(
                ["us-gaap:CommonStockSharesOutstanding", "CommonStockSharesOutstanding", "dei:EntityCommonStockSharesOutstanding"]
            )]
            if shares.empty:
                return _no_data(ticker, "shares outstanding not found in company facts")
            latest = shares.sort_values("period_end").iloc[-1]
            return {
                "ticker": ticker,
                "shares_outstanding": float(latest["value"]),
                "as_of": str(latest["period_end"]),
                "source": "SEC EDGAR company facts",
                "note": "SEC-reported shares outstanding, not public float",
            }
        if metric == "eps":
            facts = company.get_facts()
            df = facts.to_dataframe()
            # Fetch diluted EPS
            eps_diluted = df[df["concept"].isin(["us-gaap:EarningsPerShareDiluted", "EarningsPerShareDiluted"])].copy()
            if eps_diluted.empty:
                return _no_data(ticker, "diluted EPS not found in company facts")
            eps_diluted["duration_days"] = _fact_duration_days(eps_diluted)
            # Keep only true quarterly facts (~3 months). XBRL company facts
            # hold quarterly, YTD (6-9 month), and full-year values for the
            # same period_end; summing YTD rows double-counts (see TTM fix).
            q_diluted = eps_diluted[
                (eps_diluted["duration_days"] >= 60)
                & (eps_diluted["duration_days"] <= 115)
            ].copy()
            if q_diluted.empty:
                return _no_data(ticker, "no quarterly diluted EPS facts found")
            q_diluted = _dedup_latest(q_diluted).sort_values("period_end")
            recent_diluted = _quarters_with_derived_q4(
                q_diluted, df, "us-gaap:EarningsPerShareDiluted"
            )

            # Fetch basic (undiluted) EPS if available
            eps_basic = df[df["concept"].isin(["us-gaap:EarningsPerShareBasic", "EarningsPerShareBasic"])].copy()
            recent_basic = None
            if not eps_basic.empty:
                eps_basic["duration_days"] = _fact_duration_days(eps_basic)
                q_basic = eps_basic[
                    (eps_basic["duration_days"] >= 60)
                    & (eps_basic["duration_days"] <= 115)
                ].copy()
                if not q_basic.empty:
                    q_basic = _dedup_latest(q_basic).sort_values("period_end")
                    recent_basic = _quarters_with_derived_q4(
                        q_basic, df, "us-gaap:EarningsPerShareBasic"
                    )
            
            # Merge diluted and basic into single quarterly list
            quarterly_eps = []
            for _, r_diluted in recent_diluted.iterrows():
                q_entry = {
                    "fiscal_year": str(r_diluted["fiscal_year"]),
                    "fiscal_period": str(r_diluted["fiscal_period"]),
                    "eps_diluted": round(float(r_diluted["value"]), 2),
                    "period_end": str(r_diluted["period_end"]),
                }
                # Find matching basic EPS for same period
                if recent_basic is not None:
                    matching = recent_basic[recent_basic["period_end"] == r_diluted["period_end"]]
                    if not matching.empty:
                        q_entry["eps_basic"] = round(float(matching.iloc[0]["value"]), 2)
                quarterly_eps.append(q_entry)
            
            result = {
                "ticker": ticker,
                "quarterly_eps": quarterly_eps,
                "source": "SEC EDGAR company facts (Basic & Diluted EPS)",
            }
            if len(recent_diluted) == 4:
                result["ttm_eps_diluted"] = round(sum(float(r["value"]) for _, r in recent_diluted.iterrows()), 2)
            if recent_basic is not None and len(recent_basic) == 4:
                result["ttm_eps_basic"] = round(sum(float(r["value"]) for _, r in recent_basic.iterrows()), 2)
            return result
        if metric == "dividends":
            facts = company.get_facts()
            df = facts.to_dataframe()
            div = df[df["concept"].isin(
                ["us-gaap:" + _DIVIDEND_CONCEPT, _DIVIDEND_CONCEPT]
            )].copy()
            if div.empty:
                return _null_dividend_payload(ticker)
            div["duration_days"] = _fact_duration_days(div)
            q = div[
                (div["duration_days"] >= _QUARTER_DAYS[0])
                & (div["duration_days"] <= _QUARTER_DAYS[1])
            ].copy()
            if not q.empty:
                q = _dedup_latest(q).sort_values("period_end")
                recent = _quarters_with_derived_q4(
                    q, df, "us-gaap:" + _DIVIDEND_CONCEPT
                )
            else:
                recent = q
            if len(recent) == 4 and _has_contiguous_quarters(list(recent["period_end"])):
                ttm = round(sum(float(r["value"]) for _, r in recent.iterrows()), 4)
            else:
                ttm = None
            fy = div[
                (div["duration_days"] >= _FY_DAYS[0])
                & (div["duration_days"] <= _FY_DAYS[1])
            ].copy()
            if not fy.empty:
                fy = _dedup_latest(fy)
                fy_rows = [{"period_end": str(r["period_end"]), "value": float(r["value"])} for _, r in fy.iterrows()]
            else:
                fy_rows = []
            history, annual = _dividend_annual_history(fy_rows)
            return {
                "ticker": ticker,
                "dividend_status": "paying",
                "ttm_dividend_per_share": ttm,
                **_dividend_growth(annual),
                "annual_history": history,
                "source": _DIVIDEND_SOURCE,
            }
        if metric == "balance_sheet":
            financials = company.get_financials()
            bs = getattr(financials, "balance_sheet", None) or getattr(financials, "get_balance_sheet", lambda: None)()
            if bs is None:
                return _no_data(ticker, "balance sheet not available")
            try:
                latest = bs.get_latest() if hasattr(bs, "get_latest") else bs
                data = latest.to_dict() if hasattr(latest, "to_dict") else {"raw": str(latest)}
            except Exception:
                data = {"raw": str(bs)}
            return {"ticker": ticker, "balance_sheet": data, "source": "SEC EDGAR financials"}
        return {"error": f"Unknown metric '{metric}'"}
    except Exception as e:
        logger.warning("get_fundamentals(%s, %s) failed: %s", ticker, metric, e)
        return _no_data(ticker, f"error retrieving {metric}: {e}")



_OWNERSHIP_FEED_FORMS = ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A")
_OWNERSHIP_FEED_TTL_SECONDS = 3600  # SEC current-filings feed covers ~24h
_OWNERSHIP_TICKER_TTL_SECONDS = 7 * 86400  # ponytail: cached CIK->ticker map, refreshes weekly


def get_recent_ownership_filings(form_type: str = "both", limit: int = 10) -> dict:
    """Most recent SC 13D/G filings market-wide (SEC current-filings feed, ~24h window)."""
    _ensure_init()
    key = f"ownership_feed:{form_type}:{limit}"
    hit = cache.get(key, ttl=_OWNERSHIP_FEED_TTL_SECONDS)
    if hit is not None:
        return hit
    value = _fetch_recent_ownership_filings(form_type, limit)
    cache.set(key, value)
    return value


def _resolve_issuer_ticker(cik: int) -> str | None:
    """Best-effort CIK -> ticker for drill-down (cached; None when unresolvable)."""
    key = f"cik_ticker:{int(cik):010d}"
    hit = cache.get(key, ttl=_OWNERSHIP_TICKER_TTL_SECONDS)
    if hit is not None:
        return hit or None
    try:
        tickers = Company(int(cik)).tickers
        value = tickers[0] if tickers else ""
    except Exception:
        value = ""
    cache.set(key, value)
    return value or None


def _ownership_feed_row(filing) -> dict:
    """One feed row with issuer/filer detail; never raises (detail degrades to filer-only)."""
    row = {
        "form": str(getattr(filing, "form", "")),
        "filed": str(getattr(filing, "filing_date", "")),
        "accession_no": getattr(filing, "accession_no", None),
    }
    try:
        doc = filing.obj()
    except Exception:
        row["filer"] = str(getattr(filing, "company", ""))
        row["note"] = "filing detail unavailable"
        return row
    try:
        persons = getattr(doc, "reporting_persons", None) or []
        row["filers"] = [p.name for p in persons[:5]]
        issuer = getattr(doc, "issuer_info", None)
        if issuer is not None:
            row["issuer"] = getattr(issuer, "name", None)
            row["issuer_cik"] = getattr(issuer, "cik", None)
        if getattr(doc, "total_percent", None) is not None:
            row["percent"] = round(float(doc.total_percent), 2)
        if getattr(doc, "total_shares", None) is not None:
            row["shares"] = int(doc.total_shares)
        row["event_date"] = str(getattr(doc, "date_of_event", "") or "") or None
    except Exception:
        row["note"] = "ownership detail unavailable (pre-XML filing)"
    if not row.get("filers"):
        row["filers"] = [str(getattr(filing, "company", ""))]
    return row


def _fetch_recent_ownership_filings(form_type, limit) -> dict:
    label = str(form_type or "both").strip().upper()
    if label in ("BOTH", "13D/G", "13DG"):
        forms = list(_OWNERSHIP_FEED_FORMS)
    elif label in ("SC 13D", "13D"):
        forms = ["SC 13D", "SC 13D/A"]
    elif label in ("SC 13G", "13G"):
        forms = ["SC 13G", "SC 13G/A"]
    else:
        return {"error": f"Invalid form_type '{form_type}': use 'SC 13D', 'SC 13G', or 'both'"}
    try:
        limit = max(1, min(int(limit or 10), 25))
    except (TypeError, ValueError):
        return {"error": f"Invalid limit '{limit}': use 1-25"}
    try:
        from edgar import get_current_filings

        rows: list[dict] = []
        seen: set[str] = set()
        for form in forms:
            try:
                # ponytail: 10 filings per variant (40 merged max); raise page_size if daily 13D/G volume exceeds it
                feed = get_current_filings(form=form, page_size=10)
            except Exception:
                continue  # one variant failing must not sink the feed
            for filing in feed:
                accession = str(getattr(filing, "accession_no", ""))
                if accession in seen:
                    continue
                seen.add(accession)
                rows.append(_ownership_feed_row(filing))
        rows.sort(key=lambda r: r.get("filed", ""), reverse=True)
        rows = rows[:limit]
        for row in rows:
            if row.get("issuer_cik"):
                try:
                    row["ticker"] = _resolve_issuer_ticker(int(row["issuer_cik"]))
                except (TypeError, ValueError):
                    row["ticker"] = None
        return {
            "form_type": "both" if label in ("BOTH", "13D/G", "13DG") else label,
            "window": "SEC current-filings feed (~24h)",
            "count": len(rows),
            "filings": rows,
            "source": "SEC EDGAR current filings (SC 13D/G)",
        }
    except Exception as e:
        logger.warning("get_recent_ownership_filings(%s) failed: %s", form_type, e)
        return {"error": f"No data found: error retrieving recent {label} filings: {e}"}



def get_latest_earnings_release(ticker: str) -> dict:
    """Return the text of the latest 8-K Item 2.02 press release."""
    _ensure_init()
    key = f"earnings_release:{ticker}"
    return _cached_or_fetch(key, lambda: _fetch_latest_earnings_release(ticker))


def _fetch_latest_earnings_release(ticker: str) -> dict:
    try:
        company = Company(ticker)
        filings = company.get_filings(form=["8-K"])
        for filing in filings:
            eightk = filing.obj()
            items = getattr(eightk, "items", []) or []
            logger.debug("8-K %s items: %s", filing.accession_no, items)
            if not any("2.02" in item for item in items):
                continue
            press_releases = getattr(eightk, "press_releases", None) or []
            if not press_releases:
                logger.debug("8-K %s has Item 2.02 but no press_releases attr", filing.accession_no)
                continue
            text = press_releases[0].text()
            return {
                "ticker": ticker,
                "filed": str(filing.filing_date),
                "accession_no": filing.accession_no,
                "text": text,
                "source": f"8-K Item 2.02 filed {filing.filing_date} (accession {filing.accession_no})",
            }
        # Fallback: try latest 10-Q MD&A as earnings narrative source
        logger.debug("No 8-K Item 2.02 found for %s; falling back to 10-Q MD&A", ticker)
        tenq_filings = company.get_filings(form=["10-Q"])
        if tenq_filings:
            filing = tenq_filings[0]
            tenq = filing.obj()
            mda = getattr(tenq, "management_discussion", None)
            if mda is not None:
                text = mda if isinstance(mda, str) else getattr(mda, "text", lambda: str(mda))()
                return {
                    "ticker": ticker,
                    "filed": str(filing.filing_date),
                    "accession_no": filing.accession_no,
                    "text": text,
                    "source": f"10-Q MD&A filed {filing.filing_date} (accession {filing.accession_no})",
                }
        return _no_data(ticker, "no 8-K Item 2.02 or 10-Q filing found")
    except Exception as e:
        logger.warning("get_latest_earnings_release(%s) failed: %s", ticker, e)
        return _no_data(ticker, f"error retrieving earnings release: {e}")


def diff_risk_factors(ticker: str) -> dict:
    """Unified diff of risk factors between the last two 10-Qs."""
    _ensure_init()
    key = f"risk_diff:{ticker}"
    return _cached_or_fetch(key, lambda: _fetch_diff_risk_factors(ticker))


def _fetch_diff_risk_factors(ticker: str) -> dict:
    try:
        company = Company(ticker)
        filings = company.get_filings(form=["10-Q"])
        if len(filings) < 2:
            return _no_data(ticker, "fewer than two 10-Q filings found")
        latest, prior = filings[0], filings[1]
        texts = []
        for f in (latest, prior):
            tenq = f.obj()
            rf = getattr(tenq, "risk_factors", None)
            if rf is None:
                return _no_data(ticker, f"risk factors not present in 10-Q filed {f.filing_date}")
            texts.append(rf if isinstance(rf, str) else getattr(rf, "text", lambda: str(rf))())
        diff = "\n".join(difflib.unified_diff(
            texts[1].splitlines(), texts[0].splitlines(),
            fromfile=f"10-Q filed {prior.filing_date}", tofile=f"10-Q filed {latest.filing_date}",
            lineterm="",
        ))
        return {
            "ticker": ticker,
            "latest_filed": str(latest.filing_date),
            "prior_filed": str(prior.filing_date),
            "diff": diff if diff.strip() else "No changes in risk factors language between the two filings.",
            "source": f"10-Qs filed {prior.filing_date} and {latest.filing_date}",
        }
    except Exception as e:
        logger.warning("diff_risk_factors(%s) failed: %s", ticker, e)
        return _no_data(ticker, f"error diffing risk factors: {e}")


def get_financial_statements(ticker: str, statement_type: str) -> dict:
    """Return parsed financial statement (income, balance sheet, or cash flow)."""
    _ensure_init()
    key = f"financial_statements:{ticker}:{statement_type}"
    return _cached_or_fetch(key, lambda: _fetch_financial_statements(ticker, statement_type))


def _fetch_financial_statements(ticker: str, statement_type: str) -> dict:
    try:
        company = Company(ticker)
        financials = company.get_financials()
        
        if statement_type == "income_statement":
            stmt = financials.income if hasattr(financials, "income") else None
        elif statement_type == "balance_sheet":
            stmt = financials.balance if hasattr(financials, "balance") else None
        elif statement_type == "cash_flow":
            stmt = financials.cash_flow if hasattr(financials, "cash_flow") else None
        else:
            return {"error": f"Unknown statement type '{statement_type}'"}
        
        if stmt is None:
            return _no_data(ticker, f"{statement_type} not available")
        
        # Convert to readable text format
        try:
            if hasattr(stmt, "to_dataframe"):
                text = str(stmt.to_dataframe())
            elif hasattr(stmt, "to_string"):
                text = stmt.to_string()
            else:
                text = str(stmt)
        except Exception:
            text = str(stmt)
        
        return {
            "ticker": ticker,
            "statement_type": statement_type,
            "text": text,
            "source": f"SEC EDGAR {statement_type}",
        }
    except Exception as e:
        logger.warning("get_financial_statements(%s, %s) failed: %s", ticker, statement_type, e)
        return _no_data(ticker, f"error retrieving {statement_type}: {e}")


def get_xbrl_facts(ticker: str, concept: str) -> dict:
    """Return XBRL financial facts for any metric (Revenue, NetIncome, etc.)."""
    _ensure_init()
    key = f"xbrl_facts:{ticker}:{concept}"
    return _cached_or_fetch(key, lambda: _fetch_xbrl_facts(ticker, concept))


def _fetch_xbrl_facts(ticker: str, concept: str) -> dict:
    try:
        company = Company(ticker)
        facts = company.get_facts()
        df = facts.to_dataframe()
        
        # Search for concept (case-insensitive, partial match)
        concept_lower = concept.lower()
        matching = df[df["concept"].str.lower().str.contains(concept_lower, na=False)]
        
        if matching.empty:
            return _no_data(ticker, f"no XBRL facts found for concept '{concept}'")
        
        # Return recent values (most recent 5)
        recent = matching.sort_values("period_end").tail(5)
        result_list = [
            {
                "concept": str(r["concept"]),
                "value": float(r["value"]),
                "period_end": str(r["period_end"]),
                "fiscal_period": str(r.get("fiscal_period", "N/A")),
            }
            for _, r in recent.iterrows()
        ]
        
        return {
            "ticker": ticker,
            "concept_searched": concept,
            "matching_concepts": result_list,
            "count": len(result_list),
            "source": "SEC EDGAR XBRL facts",
        }
    except Exception as e:
        logger.warning("get_xbrl_facts(%s, %s) failed: %s", ticker, concept, e)
        return _no_data(ticker, f"error retrieving facts for '{concept}': {e}")
