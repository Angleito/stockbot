"""All SEC EDGAR access lives here. tools.py never imports edgartools directly."""

import difflib
import logging
from typing import Any

from edgar import Company

from . import cache
from .config import init_config

logger = logging.getLogger(__name__)

_initialized = False


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        init_config()
        _initialized = True


def _no_data(ticker: str, what: str) -> dict:
    return {"error": f"No data found for {ticker}: {what}"}


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
    if len(gaps) >= 2 and gaps.iloc[-1] is not None and float(gaps.iloc[-1]) > 130:
        # One quarter between the last two period_ends is missing (usually
        # Q4, reported only as a full-year fact). The missing quarter ends
        # ~91 days before the latest period_end.
        missing_end = ends.iloc[-1] - pd.Timedelta(days=91)
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
        & (pd.to_datetime(facts["period_end"]) >= fy_end - pd.Timedelta(days=130))
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


def get_fundamentals(ticker: str, metric: str) -> dict:
    """Return a specific fundamental for ticker.

    metric: 'eps' | 'balance_sheet' | 'shares_outstanding' | 'overview'

    'shares_float' is accepted as a deprecated alias for
    'shares_outstanding': it returns SEC-reported shares outstanding, not
    public float, and the response says so explicitly.
    """
    _ensure_init()
    if metric == "shares_float":
        metric = "shares_outstanding"
    key = f"fundamentals:{ticker}:{metric}"
    return _cached_or_fetch(key, lambda: _fetch_fundamentals(ticker, metric))


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


_SECTION_ATTRS = {
    # 10-K / 10-Q sections
    "business": "business",
    "risk_factors": "risk_factors",
    "mda": "management_discussion",
    "financial_statements": "financials",
    # 8-K items
    "earnings": "earnings",  # Item 2.02
    "guidance": "guidance",  # Item 7.01
    "material_agreements": "material_agreements",  # Item 1.01
    "bankruptcy": "bankruptcy",  # Item 2.06
    "regulatory": "regulatory",  # Item 7.01
    "other_events": "other_events",  # Item 8.01
    # Form 4
    "transactions": "transactions",
    # DEF 14A (proxy)
    "proxy_summary": "proxy_summary",
    "executive_compensation": "executive_compensation",
    "ownership": "ownership",
}


def get_filing_section(ticker: str, form_type: str, item: str) -> dict:
    """Return the raw text of a section of the latest 10-K or 10-Q."""
    _ensure_init()
    key = f"filing_section:{ticker}:{form_type}:{item}"
    return _cached_or_fetch(key, lambda: _fetch_filing_section(ticker, form_type, item))


def _fetch_filing_section(ticker: str, form_type: str, item: str) -> dict:
    if item not in _SECTION_ATTRS:
        return {"error": f"Invalid item '{item}'"}
    if form_type not in ("10-K", "10-Q", "8-K", "4", "DEF 14A"):
        return {"error": f"Invalid form_type '{form_type}'"}
    
    try:
        company = Company(ticker)
        filings = company.get_filings(form=[form_type])
        if not filings:
            return _no_data(ticker, f"no {form_type} filings found")
        
        filing = filings[0]
        doc = filing.obj()
        
        # Map item name to attribute name for lookup
        attr_name = _SECTION_ATTRS.get(item, item)
        
        # Try to get the section from the document
        section = getattr(doc, attr_name, None)

        # 10-Q MD&A is not exposed as an attribute; retrieve the Part I
        # Item 2 section text via the filing's section map.
        if section is None and form_type == "10-Q" and item == "mda":
            try:
                mda = doc.get_item_with_part("part_i", "item 2")
                if mda:
                    return {
                        "ticker": ticker,
                        "form_type": "10-Q",
                        "item": "mda",
                        "filed": str(filing.filing_date),
                        "accession_no": filing.accession_no,
                        "text": str(mda),
                        "source": f"10-Q Part I Item 2 (MD&A) filed {filing.filing_date} (accession {filing.accession_no})",
                    }
            except Exception:
                pass

        # 8-K earnings/guidance: fall back to the Item 2.02 press release
        # text, which is where the outlook/guidance language actually lives.
        if section is None and form_type == "8-K" and item in ("earnings", "guidance"):
            release = _fetch_latest_earnings_release(ticker)
            if "error" not in release:
                release["item"] = item
                release["form_type"] = "8-K"
                release["note"] = (
                    f"'{item}' extracted from the 8-K Item 2.02 press "
                    "release text; edgartools exposes no separate "
                    f"'{item}' attribute for this filing."
                )
                return release

        # 8-K material agreements / other events: fall back to the raw
        # 8-K document text for the matching item section (e.g. Item 1.01
        # material agreements, Item 7.01/8.01 disclosure), which edgartools
        # does not expose as a dedicated attribute.
        if (
            section is None
            and form_type == "8-K"
            and item in ("material_agreements", "other_events", "regulatory")
        ):
            doc_text = getattr(doc, "document", None)
            if doc_text is not None:
                raw = str(doc_text)
                item_headings = {
                    "material_agreements": ["Item 1.01"],
                    "other_events": ["Item 8.01"],
                    "regulatory": ["Item 7.01"],
                }
                for heading in item_headings[item]:
                    idx = raw.find(heading)
                    if idx < 0:
                        continue
                    next_idx = len(raw)
                    for other in (
                        "Item 1.01", "Item 2.03", "Item 5.02", "Item 7.01",
                        "Item 8.01", "Item 9.01",
                    ):
                        pos = raw.find(other, idx + len(heading))
                        if 0 < pos < next_idx:
                            next_idx = pos
                    text = raw[idx:next_idx].strip()
                    if text:
                        return {
                            "ticker": ticker,
                            "form_type": "8-K",
                            "item": item,
                            "filed": str(filing.filing_date),
                            "accession_no": filing.accession_no,
                            "text": text,
                            "source": (
                                f"8-K {heading} filed {filing.filing_date} "
                                f"(accession {filing.accession_no})"
                            ),
                            "note": (
                                f"'{item}' extracted from the raw 8-K "
                                f"{heading} document text."
                            ),
                        }

        if section is None:
            return _no_data(ticker, f"section '{item}' not found in {form_type}")
        
        # Convert section to text
        if isinstance(section, str):
            text = section
        else:
            try:
                text = section.text() if callable(getattr(section, "text", None)) else str(section)
            except Exception:
                text = str(section)
        
        return {
            "ticker": ticker,
            "form_type": form_type,
            "item": item,
            "filed": str(filing.filing_date),
            "accession_no": filing.accession_no,
            "text": text,
            "source": f"{form_type} {item} filed {filing.filing_date} (accession {filing.accession_no})",
        }
    except Exception as e:
        logger.warning("get_filing_section(%s, %s, %s) failed: %s", ticker, form_type, item, e)
        return _no_data(ticker, f"error retrieving {form_type} {item}: {e}")


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
