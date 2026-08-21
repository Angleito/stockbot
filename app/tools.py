"""Tool implementations + OpenAI-format JSON schemas for OpenRouter."""

import json
import logging

import requests

from . import edgar_client
from .config import OPENROUTER_BASE_URL, get_openrouter_api_key
from .prompts import READING_PROMPT_TEMPLATE

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Returns a specific numeric fundamental (EPS, "
                "balance sheet line item, shares float) for a ticker. Call "
                "this for any request for a specific numeric metric.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "metric": {"type": "string", "enum": [
                        "eps", "balance_sheet", "shares_float", "overview"
                    ]}
                },
                "required": ["ticker", "metric"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_filing_section",
            "description": "Returns text from any SEC filing (10-K, 10-Q, 8-K, "
                "Form 4, DEF 14A). Specify form type and item.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "form_type": {"type": "string", "enum": [
                        "10-K", "10-Q", "8-K", "4", "DEF 14A"
                    ]},
                    "item": {"type": "string", "enum": [
                        "business", "risk_factors", "mda", "financial_statements",
                        "earnings", "guidance", "material_agreements",
                        "bankruptcy", "regulatory", "other_events",
                        "proxy_summary", "executive_compensation", "ownership",
                        "transactions"
                    ]}
                },
                "required": ["ticker", "form_type", "item"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_earnings_summary",
            "description": "Returns an analyst-style read of the most "
                "recent earnings data from SEC filings (8-K Item 2.02 or 10-Q MD&A). "
                "Call for 'how did [company] do' or 'summarize earnings' questions.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diff_risk_factors",
            "description": "Returns what changed in Risk Factors language "
                "vs. the prior filing. Call for 'what's new/changed' "
                "questions.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_financial_statements",
            "description": "Returns parsed financial statements (income statement, "
                "balance sheet, cash flow) from 10-K or 10-Q filings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "statement_type": {"type": "string", "enum": [
                        "income_statement", "balance_sheet", "cash_flow"
                    ]}
                },
                "required": ["ticker", "statement_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_xbrl_facts",
            "description": "Returns XBRL financial metrics (Revenue, Net Income, "
                "Cash, Debt, Equity, etc.) for any company.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "concept": {"type": "string"}
                },
                "required": ["ticker", "concept"]
            }
        }
    }
]


def _llm_complete(model: str, prompt: str, max_tokens: int = 2000) -> str:
    """Plain (tool-less) completion — used only by get_earnings_summary."""
    resp = requests.post(
        f"{OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {get_openrouter_api_key()}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def get_earnings_summary(ticker: str, model: str) -> dict:
    """Fetch the latest 8-K press release and summarize it with the reading prompt.

    The only tool that itself calls the LLM (nested call). The summary is
    cached per ticker+model so we don't re-run the reading prompt twice.
    """
    from . import cache

    release = edgar_client.get_latest_earnings_release(ticker)
    if "error" in release:
        return release

    cache_key = f"earnings_summary:{model}:{ticker}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    # Truncate very long press releases to keep the nested call bounded.
    text = release["text"][:60000]
    summary = _llm_complete(model, READING_PROMPT_TEMPLATE.format(section_text=text))
    result = {
        "ticker": ticker,
        "summary": summary,
        "source": release["source"],
    }
    cache.set(cache_key, result)
    return result


def execute_tool(name: str, arguments: dict, model: str) -> dict:
    """Dispatch a tool call by name. Always returns a JSON-serializable dict;
    never raises — errors are returned as {"error": ...} so the model can
    report them honestly (guardrail behavior)."""
    try:
        if name == "get_fundamentals":
            return edgar_client.get_fundamentals(
                arguments["ticker"], arguments["metric"]
            )
        if name == "get_filing_section":
            return edgar_client.get_filing_section(
                arguments["ticker"], arguments["form_type"], arguments["item"]
            )
        if name == "get_financial_statements":
            return edgar_client.get_financial_statements(
                arguments["ticker"], arguments["statement_type"]
            )
        if name == "get_xbrl_facts":
            return edgar_client.get_xbrl_facts(
                arguments["ticker"], arguments["concept"]
            )
        if name == "get_earnings_summary":
            return get_earnings_summary(arguments["ticker"], model)
        if name == "diff_risk_factors":
            return edgar_client.diff_risk_factors(arguments["ticker"])
        return {"error": f"Unknown tool '{name}'"}
    except KeyError as e:
        return {"error": f"Missing required argument {e} for tool '{name}'"}
    except Exception as e:
        logger.exception("Tool '%s' failed", name)
        return {"error": f"Tool '{name}' failed: {e}"}
