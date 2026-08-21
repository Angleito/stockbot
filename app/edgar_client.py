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


def get_fundamentals(ticker: str, metric: str) -> dict:
    """Return a specific fundamental for ticker.

    metric: 'eps' | 'balance_sheet' | 'shares_float' | 'overview'
    """
    _ensure_init()
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
        if metric == "shares_float":
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
            }
        if metric == "eps":
            facts = company.get_facts()
            df = facts.to_dataframe()
            # Fetch diluted EPS
            eps_diluted = df[df["concept"].isin(["us-gaap:EarningsPerShareDiluted", "EarningsPerShareDiluted"])].copy()
            if eps_diluted.empty:
                return _no_data(ticker, "diluted EPS not found in company facts")
            eps_diluted["start_dt"] = eps_diluted["period_start"].astype(str)
            q_diluted = eps_diluted[eps_diluted["fiscal_period"] != "FY"].drop_duplicates(
                subset=["period_end"]
            ).sort_values("period_end")
            recent_diluted = q_diluted.tail(4)
            
            # Fetch basic (undiluted) EPS if available
            eps_basic = df[df["concept"].isin(["us-gaap:EarningsPerShareBasic", "EarningsPerShareBasic"])].copy()
            recent_basic = None
            if not eps_basic.empty:
                eps_basic["start_dt"] = eps_basic["period_start"].astype(str)
                q_basic = eps_basic[eps_basic["fiscal_period"] != "FY"].drop_duplicates(
                    subset=["period_end"]
                ).sort_values("period_end")
                recent_basic = q_basic.tail(4)
            
            # Merge diluted and basic into single quarterly list
            quarterly_eps = []
            for _, r_diluted in recent_diluted.iterrows():
                q_entry = {
                    "fiscal_year": str(r_diluted["fiscal_year"]),
                    "fiscal_period": str(r_diluted["fiscal_period"]),
                    "eps_diluted": float(r_diluted["value"]),
                    "period_end": str(r_diluted["period_end"]),
                }
                # Find matching basic EPS for same period
                if recent_basic is not None:
                    matching = recent_basic[recent_basic["period_end"] == r_diluted["period_end"]]
                    if not matching.empty:
                        q_entry["eps_basic"] = float(matching.iloc[0]["value"])
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
            if "2.02" not in items:
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
