"""Tool implementations + OpenAI-format JSON schemas for OpenRouter."""

import json
import logging
from datetime import date
from decimal import Decimal

import requests

from . import analytics
from . import edgar_client
from . import finra_client
from . import short_interest_screen
from .config import OPENROUTER_BASE_URL, get_openrouter_api_key
from .config import get_robinhood_mcp_url, robinhood_enabled
from .analytics.options import analyze_option, compare_options
from .prompts import READING_PROMPT_TEMPLATE
from .robinhood import RobinhoodClient
from .robinhood.auth import OAuthConfig
from .robinhood.options import OptionQuote, normalize_option_quote

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Returns a specific numeric fundamental (EPS, "
                "balance sheet line item, shares outstanding) for a ticker. "
                "Note: shares_outstanding is SEC-reported shares outstanding, "
                "not public float. Call this for any request for a specific "
                "numeric metric.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "metric": {"type": "string", "enum": [
                        "eps", "balance_sheet", "shares_outstanding", "overview"
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
    },
    {
        "type": "function",
        "function": {
            "name": "get_short_interest",
            "description": "Returns FINRA consolidated short interest for a ticker "
                "(current/previous short position, days to cover, average daily "
                "volume, percent change). Call for short interest, short float, "
                "or days-to-cover questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "settlementDate": {
                        "type": "string",
                        "description": "Optional settlement date YYYY-MM-DD. "
                        "Omit to return recent cycles."
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_short_interest_leaderboard",
            "description": "Returns the FINRA short-interest leaderboard: short interest as a percentage of SEC-reported shares outstanding for tickers that map 1:1 to an SEC CIK whose security is classified as common equity and that has a shares-outstanding fact knowable on or before the as-of date (default: today). Excludes symbols that cannot be mapped to a single SEC entity, are not classified as common equity (funds, ETFs, preferred issues), lack a usable shares-outstanding fact, or have invalid short-interest quantities; every exclusion is counted and returned in coverage. Use for questions such as 'which stock has the highest short interest', 'most shorted stock', or 'short interest as a percent of total shares'. This is a deterministic, complete FINRA settlement-date screen; it is NOT percent of public float and is not real-time short interest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of ranked stocks to return; default 10, maximum 25."},
                    "settlement_date": {"type": "string", "description": "Optional FINRA settlement date (YYYY-MM-DD). Omit for the latest published FINRA cycle."},
                    "as_of": {"type": "string", "description": "Optional knowledge horizon (YYYY-MM-DD). Only data knowable on or before this date is used. Defaults to today; pass an explicit date for a historical screen."}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reg_sho_volume",
            "description": "Returns FINRA daily Reg SHO short-sale volume for a "
                "ticker (short, short-exempt, and total share quantity by "
                "reporting facility). Rolling 12 months.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "tradeDate": {
                        "type": "string",
                        "description": "Optional trade date YYYY-MM-DD."
                    }
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_threshold_securities",
            "description": "Returns FINRA OTC Regulation SHO / Rule 4320 "
                "threshold securities. Optionally filter by ticker and date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "tradeDate": {
                        "type": "string",
                        "description": "Optional trade date YYYY-MM-DD."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_finra_datasets",
            "description": "Lists public FINRA Query API datasets (filing cabinet "
                "catalog): concise entries with canonical id group/name, group, "
                "description, and ticker/date support. Optional group or search "
                "filters. Call first when you are unsure which FINRA dataset to use; "
                "then describe_finra_dataset before query_finra.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group": {
                        "type": "string",
                        "description": "Optional dataset group filter "
                        "(e.g. otcMarket, fixedIncomeMarket, finra)."
                    },
                    "search": {
                        "type": "string",
                        "description": "Optional substring match on name/description."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_finra_dataset",
            "description": "Describes one FINRA dataset: fields with types and "
                "descriptions, ticker/date fields, documented filter values, and "
                "supported methods. Call after list_finra_datasets and before "
                "query_finra when the dataset is unfamiliar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {
                        "type": "string",
                        "description": "Canonical group/name "
                        "(e.g. otcMarket/consolidatedShortInterest). "
                        "Legacy bare names are accepted when unambiguous."
                    }
                },
                "required": ["dataset_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_finra_datapoints",
            "description": "Returns exact source values from a FINRA dataset "
                "for explicit data requests ONLY (e.g. 'show the last five "
                "settlement-date values'). Requires a 'fields' list and at "
                "least one narrowing condition (ticker, date/date range, or "
                "filters). IMPORTANT: when the user requests named datapoints "
                "with friendly labels (e.g. 'days to cover', 'average daily "
                "volume'), call describe_finra_dataset FIRST and use the "
                "metadata's exact field names (e.g. daysToCoverQuantity, "
                "averageDailyVolumeQuantity) in the fields list — never "
                "friendly labels. For 'latest five' / 'last five' / 'most "
                "recent' requests, add sort_fields [\"-<dateField>\"] or "
                "sort_order \"desc\" (or \"asc\" for oldest first); the "
                "client resolves the sort against the dataset's partitions "
                "automatically. Do NOT use for ordinary analysis — query_finra "
                "and the specific helper tools return analyzed briefings "
                "instead. Returns at most 25 rows containing only the "
                "requested fields. Exact source values are guaranteed for "
                "normal scalar data; oversized text fields are rendered as "
                "a marked excerpt (table cells are capped at 200 characters "
                "to keep the tool message compact).",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "description": "Canonical id group/name "
                        "(e.g. otcMarket/consolidatedShortInterest). "
                        "Legacy bare names accepted when unambiguous."
                    },
                    "fields": {
                        "type": "array",
                        "description": "Exact field names to return. Call "
                        "describe_finra_dataset first to see valid fields.",
                        "items": {"type": "string"},
                        "minItems": 1
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Issue symbol when the dataset is symbol-level."
                    },
                    "start_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. Combined with end_date as a range."
                    },
                    "end_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD."
                    },
                    "filters": {
                        "type": "array",
                        "description": "Extra compare filters (field names must "
                        "exist on the dataset — call describe_finra_dataset first).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "EQUAL", "GREATER", "LESSER",
                                        "GTE", "LTE", "NOT_EQUAL", "BEGINS_WITH"
                                    ]
                                },
                                "value": {"type": "string"}
                            },
                            "required": ["field", "value"]
                        }
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max rows to return (clamped to 1..25; "
                        "default 10)."
                    },
                    "sort_fields": {
                        "type": "array",
                        "description": "FINRA sortFields syntax: '+field' "
                        "ascending, '-field' descending, e.g. "
                        "[\"-settlementDate\"] returns newest first. Use for "
                        "'latest five' / 'last five' / 'most recent' data "
                        "requests. Fields must exist on the dataset.",
                        "items": {"type": "string"}
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Convenience: sort by the dataset's "
                        "date field ('desc' = newest first, for 'latest "
                        "five' requests). Rejected when the dataset has no "
                        "date field — use sort_fields instead."
                    }
                },
                "required": ["dataset", "fields"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_finra",
            "description": "Queries a FINRA dataset by canonical group/name "
                "(or legacy bare name) and returns an analyzed briefing: "
                "query provenance, coverage dates, deterministic metrics "
                "(min/max/mean/median/sum, latest-vs-prior change), derived "
                "trends, data-quality warnings, and a concise prose briefing. "
                "Raw source records are NOT returned. Prefer get_short_interest "
                "/ get_reg_sho_volume / get_threshold_securities for those "
                "specific questions. For unfamiliar datasets: list_finra_datasets "
                "→ describe_finra_dataset → query_finra with a bounded limit. "
                "Use get_finra_datapoints only when the user explicitly asks "
                "to see exact source values.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "description": "Canonical id group/name "
                        "(e.g. fixedIncomeMarket/treasuryDailyAggregates). "
                        "Legacy bare names accepted when unambiguous."
                    },
                    "ticker": {
                        "type": "string",
                        "description": "Issue symbol when the dataset is symbol-level."
                    },
                    "start_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. Combined with end_date as a range."
                    },
                    "end_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to return (clamped to 1..1000)."
                    },
                    "offset": {
                        "type": "integer",
                        "description": "0-based record offset for pagination "
                        "(FINRA max 500000). Rejected for datasets whose "
                        "catalog entry has supportsRecordOffset=false."
                    },
                    "filters": {
                        "type": "array",
                        "description": "Extra compare filters (field names must "
                        "exist on the dataset — call describe_finra_dataset first).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "EQUAL", "GREATER", "LESSER",
                                        "GTE", "LTE", "NOT_EQUAL", "BEGINS_WITH"
                                    ]
                                },
                                "value": {"type": "string"}
                            },
                            "required": ["field", "value"]
                        }
                    },
                    "analysis_goal": {
                        "type": "string",
                        "description": "Optional: what the user needs answered "
                        "(e.g. 'trend over the last 12 months'). Guides the "
                        "briefing; deterministic metrics are always computed."
                    }
                },
                "required": ["dataset"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_snapshot",
            "description": "Returns a read-only Robinhood MCP stock quote with last, bid, ask, and retrieval time.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_option_chain",
            "description": "Returns a bounded read-only Robinhood option chain filtered by type, DTE, and strike.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "option_type": {"type": "string", "enum": ["put", "call"]},
                    "min_dte": {"type": "integer", "minimum": 0},
                    "max_dte": {"type": "integer", "minimum": 0},
                    "strike_min": {"type": "number"},
                    "strike_max": {"type": "number"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["ticker", "option_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_option_contract",
            "description": "Analyzes one Robinhood option contract using observed quote fields and deterministic expiration payoff math.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "expiration": {"type": "string", "description": "YYYY-MM-DD"},
                    "strike": {"type": "number"},
                    "option_type": {"type": "string", "enum": ["put", "call"]},
                    "target_price": {"type": "number"},
                },
                "required": ["ticker", "expiration", "strike", "option_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_options",
            "description": "Compares bounded Robinhood option contracts at a target expiration price, including spreads, liquidity, Greeks, and deterministic payoff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "option_type": {"type": "string", "enum": ["put", "call"]},
                    "target_price": {"type": "number"},
                    "min_dte": {"type": "integer", "minimum": 0},
                    "max_dte": {"type": "integer", "minimum": 0},
                    "strike_min": {"type": "number"},
                    "strike_max": {"type": "number"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                "required": ["ticker", "option_type", "target_price"],
            },
        },
    },
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


_ROBINHOOD_PROVIDER_TOOLS = {
    "get_equity_quotes",
    "get_equity_fundamentals",
    "get_option_chains",
    "get_option_instruments",
    "get_option_quotes",
    "get_option_historicals",
}


def _robinhood_client() -> RobinhoodClient:
    if not robinhood_enabled():
        raise RuntimeError("Robinhood integration is disabled; set ROBINHOOD_ENABLED=true")
    url = get_robinhood_mcp_url()
    return RobinhoodClient(
        url,
        oauth=OAuthConfig(url),
        allowed_tools=_ROBINHOOD_PROVIDER_TOOLS,
    )


def _provider_payload(value):
    if isinstance(value, dict):
        structured = value.get("structured_content") or value.get("structuredContent")
        if structured is not None:
            return structured
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if text:
                    try:
                        return json.loads(text)
                    except (TypeError, ValueError):
                        return {"text": text}
        return value
    return value


def _rows(payload, *keys: str) -> list[dict]:
    payload = _provider_payload(payload)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    for key in ("data", "results", "items", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = _rows(value, *keys)
            if nested:
                return nested
    return [payload]


def _first(value, *keys):
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) is not None:
            return value[key]
    return None


def _quote_row(value):
    if isinstance(value, dict) and isinstance(value.get("quote"), dict):
        return value["quote"]
    return value


def get_market_snapshot(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    payload = _robinhood_client().call_tool("get_equity_quotes", {"symbols": [ticker]})
    rows = _rows(payload, "quotes", "equity_quotes")
    if not rows:
        return {"error": f"No Robinhood quote found for {ticker}", "source": "robinhood_mcp"}
    row = _quote_row(rows[0])
    return {
        "result_type": "market_snapshot",
        "ticker": ticker,
        "last": _first(
            row,
            "last",
            "last_price",
            "lastPrice",
            "last_trade_price",
            "last_non_reg_trade_price",
            "price",
        ),
        "bid": _first(row, "bid", "bid_price", "bidPrice"),
        "ask": _first(row, "ask", "ask_price", "askPrice"),
        "retrieved_at": _first(
            row,
            "retrieved_at",
            "retrievedAt",
            "venue_last_non_reg_trade_time",
            "venue_last_trade_time",
            "timestamp",
        ),
        "source": "robinhood_mcp",
    }


def _load_option_quotes(ticker: str, option_type: str, **filters) -> list[OptionQuote]:
    client = _robinhood_client()
    chain = _provider_payload(
        client.call_tool("get_option_chains", {"underlying_symbol": ticker})
    )
    chain_rows = _rows(chain, "chains", "option_chains")
    chain_id = _first(chain_rows[0], "chain_id", "chainId", "id") if chain_rows else None
    instrument_args = {"chain_symbol": ticker, "type": option_type}
    if chain_id:
        instrument_args["chain_id"] = chain_id
    if filters.get("expiration_date") is not None:
        instrument_args["expiration_dates"] = filters["expiration_date"]
    if filters.get("state") is not None:
        instrument_args["state"] = filters["state"]
    instruments = _rows(
        client.call_tool("get_option_instruments", instrument_args),
        "instruments",
        "option_instruments",
    )
    instruments = [
        row for row in instruments
        if str(_first(row, "type", "option_type", "optionType") or option_type).lower() in {option_type, option_type[0]}
    ]
    today = date.today()
    filtered_instruments = []
    for row in instruments:
        expiration = str(_first(row, "expiration", "expiration_date", "expirationDate") or "")[:10]
        try:
            dte = (date.fromisoformat(expiration) - today).days
        except ValueError:
            dte = None
        strike = _first(row, "strike", "strike_price", "strikePrice")
        try:
            strike_value = Decimal(str(strike))
        except (ValueError, TypeError):
            strike_value = None
        if filters.get("min_dte") is not None and (dte is None or dte < int(filters["min_dte"])):
            continue
        if filters.get("max_dte") is not None and (dte is None or dte > int(filters["max_dte"])):
            continue
        if filters.get("strike_min") is not None and (strike_value is None or strike_value < Decimal(str(filters["strike_min"]))):
            continue
        if filters.get("strike_max") is not None and (strike_value is None or strike_value > Decimal(str(filters["strike_max"]))):
            continue
        filtered_instruments.append(row)
    instruments = filtered_instruments
    ids = [_first(row, "id", "instrument_id", "contract_id") for row in instruments]
    ids = [str(value) for value in ids if value]
    quotes = _rows(
        client.call_tool("get_option_quotes", {"instrument_ids": ids}),
        "quotes",
        "option_quotes",
        "results",
    ) if ids else []
    quotes_by_id = {}
    for row in quotes:
        quote = _quote_row(row)
        quote_id = _first(quote, "id", "instrument_id", "contract_id")
        if quote_id:
            quotes_by_id[str(quote_id)] = quote
    normalized = []
    for instrument in instruments:
        instrument_id = str(_first(instrument, "id", "instrument_id", "contract_id") or "")
        merged = dict(instrument)
        merged.update(quotes_by_id.get(instrument_id, {}))
        merged["contract_id"] = instrument_id
        merged["ticker"] = ticker
        try:
            normalized.append(normalize_option_quote(merged, ticker=ticker))
        except ValueError:
            continue
    return normalized


def get_option_chain(ticker: str, option_type: str, min_dte=None, max_dte=None, strike_min=None, strike_max=None, limit=20) -> dict:
    ticker = ticker.strip().upper()
    option_type = option_type.lower()
    quotes = _load_option_quotes(
        ticker,
        option_type,
        min_dte=min_dte,
        max_dte=max_dte,
        strike_min=strike_min,
        strike_max=strike_max,
    )
    today = date.today()
    filtered = [
        quote for quote in quotes
        if (min_dte is None or (quote.expiration - today).days >= int(min_dte))
        and (max_dte is None or (quote.expiration - today).days <= int(max_dte))
        and (strike_min is None or quote.strike >= Decimal(str(strike_min)))
        and (strike_max is None or quote.strike <= Decimal(str(strike_max)))
    ]
    if not filtered:
        return {
            "error": f"No Robinhood {option_type} contracts matched the requested filters for {ticker}",
            "source": "robinhood_mcp",
        }
    bounded = max(1, min(int(limit or 20), 30))
    return {
        "result_type": "option_chain",
        "ticker": ticker,
        "option_type": option_type,
        "contracts": [analyze_option(quote) for quote in filtered[:bounded]],
        "matched": len(filtered),
        "returned": min(len(filtered), bounded),
        "filters": {"min_dte": min_dte, "max_dte": max_dte, "strike_min": strike_min, "strike_max": strike_max},
        "source": "robinhood_mcp",
    }


def analyze_option_contract(ticker: str, expiration: str, strike, option_type: str, target_price=None) -> dict:
    quotes = _load_option_quotes(ticker.strip().upper(), option_type.lower(), expiration_date=expiration)
    matches = [quote for quote in quotes if quote.expiration.isoformat() == expiration and quote.strike == Decimal(str(strike))]
    if not matches:
        return {"error": "No matching Robinhood option contract found", "source": "robinhood_mcp"}
    return {"result_type": "option_analysis", **analyze_option(matches[0], target_price=target_price), "source": "robinhood_mcp"}


def compare_robinhood_options(ticker: str, option_type: str, target_price, min_dte=None, max_dte=None, strike_min=None, strike_max=None, limit=20) -> dict:
    quotes = _load_option_quotes(
        ticker.strip().upper(), option_type.lower(), min_dte=min_dte, max_dte=max_dte, strike_min=strike_min, strike_max=strike_max
    )
    today = date.today()
    filtered = [
        quote for quote in quotes
        if (min_dte is None or (quote.expiration - today).days >= int(min_dte))
        and (max_dte is None or (quote.expiration - today).days <= int(max_dte))
        and (strike_min is None or quote.strike >= Decimal(str(strike_min)))
        and (strike_max is None or quote.strike <= Decimal(str(strike_max)))
    ]
    if not filtered:
        return {
            "error": f"No Robinhood {option_type} contracts matched the requested filters for {ticker}",
            "source": "robinhood_mcp",
        }
    return {"result_type": "option_comparison", "ticker": ticker.upper(), "source": "robinhood_mcp", **compare_options(filtered, target_price=target_price, limit=limit)}


# FINRA dispatch registry — kept next to the FINRA tool schemas above so the
# parity test can prove every FINRA schema has an executable dispatcher.
_FINRA_HANDLERS = {
    "get_short_interest_leaderboard": lambda args, model: analytics.screens.get_short_interest_leaderboard(
        limit=args.get("limit"), settlement_date=args.get("settlement_date"), as_of=args.get("as_of")
    ),
    "get_short_interest": lambda args, model: finra_client.get_short_interest(
        args["ticker"], args.get("settlementDate")
    ),
    "get_reg_sho_volume": lambda args, model: finra_client.get_reg_sho_volume(
        args["ticker"], args.get("tradeDate")
    ),
    "get_threshold_securities": lambda args, model: finra_client.get_threshold_securities(
        args.get("ticker"), args.get("tradeDate")
    ),
    "list_finra_datasets": lambda args, model: finra_client.list_datasets(
        group=args.get("group"), search=args.get("search")
    ),
    "describe_finra_dataset": lambda args, model: finra_client.describe_dataset(
        args.get("dataset_id") or args.get("dataset")
    ),
    "get_finra_datapoints": lambda args, model: finra_client.get_finra_datapoints(
        args["dataset"],
        fields=args.get("fields"),
        ticker=args.get("ticker") or args.get("symbol"),
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        limit=args.get("limit"),
        filters=args.get("filters"),
        sort_fields=args.get("sort_fields"),
        sort_order=args.get("sort_order"),
    ),
    "query_finra": lambda args, model: finra_client.query_dataset(
        args["dataset"],
        ticker=args.get("ticker") or args.get("symbol"),
        start_date=args.get("start_date"),
        end_date=args.get("end_date"),
        limit=args.get("limit"),
        offset=args.get("offset"),
        filters=args.get("filters"),
        analysis_goal=args.get("analysis_goal"),
    ),
}

_ROBINHOOD_HANDLERS = {
    "get_market_snapshot": lambda args, model: get_market_snapshot(args["ticker"]),
    "get_option_chain": lambda args, model: get_option_chain(
        args["ticker"], args["option_type"], args.get("min_dte"), args.get("max_dte"),
        args.get("strike_min"), args.get("strike_max"), args.get("limit", 20)
    ),
    "analyze_option_contract": lambda args, model: analyze_option_contract(
        args["ticker"], args["expiration"], args["strike"], args["option_type"], args.get("target_price")
    ),
    "compare_options": lambda args, model: compare_robinhood_options(
        args["ticker"], args["option_type"], args["target_price"], args.get("min_dte"), args.get("max_dte"),
        args.get("strike_min"), args.get("strike_max"), args.get("limit", 20)
    ),
}


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
        if name in _FINRA_HANDLERS:
            return _FINRA_HANDLERS[name](arguments, model)
        if name in _ROBINHOOD_HANDLERS:
            return _ROBINHOOD_HANDLERS[name](arguments, model)
        return {"error": f"Unknown tool '{name}'"}
    except KeyError as e:
        return {"error": f"Missing required argument {e} for tool '{name}'"}
    except Exception as e:
        logger.exception("Tool '%s' failed", name)
        return {"error": f"Tool '{name}' failed: {e}"}
