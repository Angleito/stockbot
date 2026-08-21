"""Tool implementations + OpenAI-format JSON schemas for OpenRouter."""

import json
import logging

import requests

from . import edgar_client
from . import finra_client
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


# FINRA dispatch registry — kept next to the FINRA tool schemas above so the
# parity test can prove every FINRA schema has an executable dispatcher.
_FINRA_HANDLERS = {
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
        return {"error": f"Unknown tool '{name}'"}
    except KeyError as e:
        return {"error": f"Missing required argument {e} for tool '{name}'"}
    except Exception as e:
        logger.exception("Tool '%s' failed", name)
        return {"error": f"Tool '{name}' failed: {e}"}
