"""Tool implementations + OpenAI-format JSON schemas for OpenRouter."""

import hashlib
import json
import logging
from datetime import date
from pathlib import Path
from decimal import Decimal
from typing import Any

from . import analytics
from . import analyst_client
from . import edgar_client
from . import exa_client
from . import finra_client
from . import obligations
from . import valuation
from .config import broker_enabled, get_robinhood_mcp_url
from .policy import Capability, RequestContext
from .analytics.options import analyze_option, compare_options
from .analytics.portfolio import largest_positions, portfolio_concentration
from .robinhood import RobinhoodClient
from .robinhood import capabilities
from .robinhood.auth import DEFAULT_TOKEN_PATH, OAuthConfig, has_valid_tokens
from .robinhood.client import RobinhoodAuthRequired
from .robinhood.options import OptionQuote, normalize_option_quote
from .robinhood.portfolio import RobinhoodPortfolioProvider
from .services import risk as risk_service
from .services.portfolio_research import SEC_CONCEPTS, enrich_portfolio_research
from .services.portfolio_sync import read_latest_snapshot, sync_robinhood_portfolio
from .services import sec_facts
from . import sec
from .storage import duckdb

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
                    ]},
                    "as_of": {"type": "string", "description": "Point-in-time query date YYYY-MM-DD; store-backed for eps/shares_outstanding; live results are labeled data_source=live."}
                },
                "required": ["ticker", "metric"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sec_filings",
            "description": "Lists SEC EDGAR filings for a ticker or CIK. Any form string works (10-K, 8-K, SC 13D, Form 4, S-1, 20-F, 13F-HR, ...). Start here for filing discovery; then drill into get_sec_document or a deterministic analyzer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "forms": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."},
                    "limit": {"type": "integer"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_filing",
            "description": "Returns one filing's record (form, filed/accepted/known dates, period, primary document, amendment link, source URL) by accession number.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string"}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sec_documents",
            "description": "Lists the documents and exhibits attached to one filing by accession number.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string"}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_document",
            "description": "Returns the text of one filing document (default: primary document) by accession number. Load only the document relevant to the question, never full history.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string"}, "document_name": {"type": "string"}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diff_sec_filings",
            "description": "Deterministic diff between two filings by accession numbers (amendment vs prior, risk-factor changes). Numbers first; the LLM interprets only after deterministic output.",
            "parameters": {
                "type": "object",
                "properties": {"current_accession": {"type": "string"}, "previous_accession": {"type": "string"}, "section": {"type": "string"}},
                "required": ["current_accession", "previous_accession"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_material_events",
            "description": "What changed since a date: deterministic 8-K-derived events with accession citations. Call for 'what changed/what's new' questions before loading raw filings.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "since": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker", "since"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_beneficial_ownership",
            "description": "5%+ beneficial-ownership records (SC 13D/G): holder, shares, percent, voting/dispositive powers. Deterministic numbers, never web prose.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_ownership_changes",
            "description": "Deterministic diffs between a holder's consecutive 13D/G filings: share and percent changes plus voting/text changes.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_insider_activity",
            "description": "Insider transactions (Forms 3/4/5) with SEC transaction codes mapped to purchase/sale/exercise/grant/gift/conversion/withholding/other. Disposals are never defaulted to bearish selling.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_planned_insider_sales",
            "description": "Planned insider sales from Form 144 notices (proposed, not yet executed). Compare with get_insider_activity for follow-through.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_offering_history",
            "description": "Financing history (S-1/S-3/424B/EFFECT): offering terms with source-registration links. Unknown terms stay unknown, never estimated.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_dilution_profile",
            "description": "Deterministic dilution math: inputs, formula, and source accessions always shown. Unquantifiable terms return not_quantifiable.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_institutional_ownership",
            "description": "13F filing pointers (filing level only until the 13F holdings follow-up). For holder/entry questions when position detail is unavailable.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_governance_events",
            "description": "Proxy/governance filing context (DEF 14A, contested forms, information statements) with retrieval pointers.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "since": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_transaction_status",
            "description": "M&A filing context (tender offers, 14D-9, S-4, merger proxies). Deal status is unknown until structured parsers land; use get_sec_document for filing text.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "as_of": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_short_pressure_profile",
            "description": "Short-interest context (FINRA positioning plus SEC shares outstanding and their ratio). Describes positioning only; never assesses manipulation or causation.",
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
            "name": "search_tools",
            "description": "Find the right tool first: keyword search over tool names, descriptions, and domain tags. Call this when unsure which tool fits; it returns only matching schemas.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "domain": {"type": "string", "description": "Browse a domain pack: filings, ownership, insider, offerings, events, governance, transactions, market."}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_ownership_filings",
            "description": "Lists the most recent SC 13D/13G filings market-wide "
                "(SEC current-filings feed, ~24h window): issuer, filer, stake "
                "percent/shares, filed date, accession. Call for 'most recent' "
                "or 'latest' big-investor filings when no ticker is given; then "
                "drill into get_beneficial_ownership for detail. It lists filings, it "
                "does not establish that a filing caused a price move.",
            "parameters": {
                "type": "object",
                "properties": {
                    "form_type": {"type": "string", "enum": ["SC 13D", "SC 13G", "both"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25}
                },
                "required": []
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
            "name": "get_analyst_estimates",
            "description": "Returns sell-side consensus estimates for a ticker "
                "from Yahoo Finance: latest quote, analyst 12-month price "
                "targets (mean/median/high/low) and recommendation rating, "
                "forward EPS and revenue estimates per period (current quarter, "
                "next quarter, current fiscal year, next fiscal year) with "
                "growth rates, plus EPS estimate-revision trend (7/30/60 days "
                "ago). Call for analyst estimates, price targets, consensus "
                "expectations, forward growth, or valuation-vs-consensus "
                "questions. Consensus moves daily; the response includes the "
                "as-of timestamp.",
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
            "name": "get_sp500_weight",
            "description": "Returns a company's current weight in the S&P 500 "
                "index (rank, weight as percent of index market cap) from the "
                "Slickcharts constituent list. Call for 'what percent of the "
                "S&P 500 is [ticker]' or index-weight questions. To estimate "
                "total S&P 500 market cap, divide market_cap from "
                "get_analyst_estimates by weight_pct/100.",
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
            "name": "get_obligations",
            "description": "Returns quantified contractual obligations and "
                "commitments disclosed in the latest 10-Q/10-K notes: "
                "manufacturing/supply/capacity commitments, cloud service "
                "agreements, vendor commitments, operating leases, and "
                "facility lease guarantees, each with the amount, the "
                "filing's own certainty language (contractual = "
                "non-cancelable/firm; contingent = cancellable, reducible, "
                "terminable, or default-triggered), payment horizon, and "
                "source excerpt. Call for purchase obligations, supply "
                "commitments, cloud commitments, lease obligations, "
                "guarantees, or any 'what is the company obligated to pay "
                "in the future' question. Contingent items are NOT counted "
                "in adjusted EPS.",
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
            "name": "get_valuation_metrics",
            "description": "Returns valuation metrics anchored to the live "
                "price as of the query: trailing P/E (SEC GAAP TTM EPS), "
                "consensus forward P/E (Yahoo), plus three clearly separated "
                "EPS figures: consensus forward EPS; adjusted forward EPS "
                "(consensus minus only contractual obligations — "
                "non-cancelable/firm per the 10-Q/10-K notes — annualized "
                "per share); and a stress-scenario forward EPS (also "
                "subtracting contingent obligations: cancellable, reducible, "
                "terminable, or default-triggered). The per-share obligation "
                "drag is shown explicitly. Use for 'is the stock cheap', "
                "P/E, forward earnings, or obligation-adjusted valuation "
                "questions. Never present the stress scenario as 'adjusted'.",
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
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_snapshot",
            "description": "Returns the user's current Robinhood portfolio with deterministic valuation, weights, cash, concentration, and available SEC/FINRA research context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "refresh": {"type": "boolean", "description": "If true, refresh account and quote data from Robinhood before returning the snapshot."}
                },
                "required": []
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scanner_filter_specs",
            "description": "Lists every valid Robinhood scanner filter type and usage (read-only catalog).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_mandate",
            "description": "Deterministic risk/mandate evaluation of the latest portfolio snapshot against data/mandate.json: sector exposure, single-position weight, minimum cash, prohibited assets. Breaches are computed by Stockbot; explain them, do not recalculate.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scans",
            "description": "Lists the user's saved Robinhood scanners (screeners): id, title, active filters, configured columns, sort order, and whether the scan is Cortex-managed (read-only).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_scan",
            "description": "Executes a saved Robinhood scanner and returns live, real-time market results (bounded to limit rows). Requires a scan_id from get_scans.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scan_id": {"type": "string", "description": "The scan identifier to execute."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Maximum result rows to return (default 20)."},
                },
                "required": ["scan_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Current qualitative evidence from the web (news, announcements, competitive/industry developments, management commentary, publications, specialist commentary, counterevidence) with bounded highlights. NOT a source for exact financial facts, portfolio state, historical point-in-time facts, mandate calculations, or deterministic screens — use the canonical SEC/FINRA/Robinhood/local-warehouse tools for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query: ticker/company/industry plus the research question. Never include account, portfolio, or personal identifiers."},
                    "category": {"type": "string", "enum": ["news", "company", "publication", "financial report"], "description": "Optional category to narrow the search."},
                    "include_domains": {"type": "array", "items": {"type": "string"}, "description": "Optional domains to restrict results to."},
                    "exclude_domains": {"type": "array", "items": {"type": "string"}, "description": "Optional domains to exclude from results."},
                    "start_published_date": {"type": "string", "description": "Optional start publication date YYYY-MM-DD, inclusive."},
                    "end_published_date": {"type": "string", "description": "Optional end publication date YYYY-MM-DD, inclusive."},
                    "search_type": {"type": "string", "enum": ["auto", "fast", "deep-lite"], "description": "Optional search mode (default auto)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Maximum results, 1-10 (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
]
# Content-derived registry version for observability records.
TOOL_REGISTRY_VERSION = hashlib.sha256(json.dumps(TOOLS, sort_keys=True).encode()).hexdigest()[:12]


def _robinhood_client(
    *, account_tools: frozenset[str] = frozenset(),
) -> RobinhoodClient:
    """Construct a broker client with only the MCP reads this handler needs."""
    if not broker_enabled():
        raise RuntimeError("Robinhood integration is disabled; set BROKER_ENABLED=true")
    url = get_robinhood_mcp_url()
    oauth = OAuthConfig(url)
    if not has_valid_tokens(oauth.server_origin, DEFAULT_TOKEN_PATH):
        raise RobinhoodAuthRequired("Robinhood OAuth is not set up or has expired")
    return RobinhoodClient(
        url,
        oauth=oauth,
        market_tools=capabilities.MARKET_READ_TOOLS,
        account_tools=account_tools,
    )


def authorize_robinhood_browser() -> bool:
    """Run the full OAuth flow now (browser + loopback callback), persisting
    tokens. True on success. Works regardless of BROKER_ENABLED — this is
    the setup step."""
    try:
        url = get_robinhood_mcp_url()
        client = RobinhoodClient(url, oauth=OAuthConfig(url))
        client.list_tools()  # SDK performs discovery + OAuth when required
        return True
    except Exception:
        logger.warning("Robinhood authorization failed", exc_info=True)
        return False


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
    provider = RobinhoodPortfolioProvider(_robinhood_client())
    quote = provider.get_equity_quotes([ticker]).get(ticker)
    if quote is None:
        return {"error": f"No Robinhood quote found for {ticker}", "source": "robinhood_mcp"}
    return {
        "result_type": "market_snapshot",
        "ticker": ticker,
        "last": str(quote.last) if quote.last is not None else None,
        "bid": str(quote.bid) if quote.bid is not None else None,
        "ask": str(quote.ask) if quote.ask is not None else None,
        "retrieved_at": quote.retrieved_at.isoformat(),
        # *_local fields are the process host's local timezone, not the end user's.
        "retrieved_at_local": quote.retrieved_at.astimezone().isoformat(),
        "source": "robinhood_mcp",
    }


_PORTFOLIO_TOP_POSITIONS = 15
_PORTFOLIO_TOP_LARGEST = 5

def evaluate_mandate(data_root: Path | None = None, mandate_path: Path | None = None) -> dict:
    """Deterministic mandate evaluation over the latest persisted snapshot."""
    path = mandate_path or Path(duckdb.DEFAULT_DATA_ROOT) / "mandate.json"
    try:
        evaluation = risk_service.evaluate_latest_mandate(path, data_root=data_root)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}
    return {
        "result_type": "mandate_evaluation",
        "snapshot_id": evaluation.snapshot_id,
        "snapshot_created_at": evaluation.created_at.isoformat(),
        "snapshot_created_at_local": evaluation.created_at.astimezone().isoformat(),
        "breaches": [
            {
                "metric": breach.metric,
                "target": breach.target,
                "severity": breach.severity,
                "actual": str(breach.actual) if breach.actual is not None else None,
                "limit": str(breach.limit) if breach.limit is not None else None,
                "excess": str(breach.excess) if breach.excess is not None else None,
                "note": breach.note,
                "unit": breach.unit,
            }
            for breach in evaluation.breaches
        ],
        "sector_exposures": {sector: str(weight) for sector, weight in evaluation.sector_exposures.items()},
        "issues": [
            {
                "code": issue.code,
                "metric": issue.metric,
                "target": issue.target,
                "position_id": issue.position_id,
                "ticker": issue.ticker,
            }
            for issue in evaluation.issues
        ],
        "source": "mandate",
    }


def _str_or_none(value) -> str | None:
    return str(value) if value is not None else None


def _research_freshness(freshness_items: list[dict]) -> dict:
    """Aggregate per-position research freshness to one latest non-empty dict."""
    non_empty = [item for item in freshness_items if item]
    if not non_empty:
        return {}
    return max(
        non_empty,
        key=lambda item: (
            item.get("sec_latest_filed_at") or "0000-00-00",
            item.get("finra_settlement_date") or "0000-00-00",
            item.get("finra_known_at") or "",
        ),
    )


def _position_research_row(position, research_item) -> dict:
    row = {
        "ticker": position.ticker,
        "quantity": str(position.quantity),
        "market_price": _str_or_none(position.market_price),
        "price_type": position.price_type,
        "market_value": _str_or_none(position.market_value),
        "portfolio_weight": _str_or_none(position.portfolio_weight),
        "unrealized_gain": _str_or_none(position.unrealized_gain),
        "security_id": position.security_id,
        "entity_id": position.entity_id,
        "resolved": position.entity_id is not None,
    }
    if research_item is not None:
        sec = {}
        for concept in SEC_CONCEPTS:
            fact = research_item.latest_sec_metrics.get(concept)
            if fact:
                sec[concept] = {
                    "value": _str_or_none(fact.get("value")),
                    "period_end": fact.get("period_end") or None,
                }
        row["sec"] = sec
        finra = research_item.latest_finra_metrics
        if finra:
            row["finra"] = {
                "short_position": _str_or_none(finra.get("short_position")),
                "prev_position": _str_or_none(finra.get("prev_position")),
                "change": _str_or_none(finra.get("short_interest_change")),
                "change_pct": _str_or_none(finra.get("short_interest_change_pct")),
                "days_to_cover": _str_or_none(finra.get("days_to_cover")),
                "settlement_date": finra.get("settlement_date") or None,
            }
    return row


def _get_portfolio_snapshot(arguments: dict, model: str) -> dict:
    """Bounded, deterministic portfolio snapshot (spec §23)."""
    del model
    refresh = bool(arguments.get("refresh", False))
    provider = RobinhoodPortfolioProvider(_robinhood_client(
        account_tools=frozenset({
            "get_accounts", "get_portfolio", "get_equity_positions",
        })
    ))
    if refresh:
        snapshot = sync_robinhood_portfolio(provider, data_root=None)
    else:
        snapshot = read_latest_snapshot(data_root=None) or sync_robinhood_portfolio(provider, data_root=None)
    research = {
        item.position.position_id: item
        for item in enrich_portfolio_research(snapshot)
    }
    positions_by_id = {position.position_id: position for position in snapshot.positions}
    ranked = largest_positions(
        [(position.position_id, position.market_value) for position in snapshot.positions],
        limit=_PORTFOLIO_TOP_POSITIONS,
    )
    position_rows = [
        _position_research_row(positions_by_id[position_id], research.get(position_id))
        for position_id, _ in ranked
    ]
    omitted_count = max(0, len(snapshot.positions) - len(position_rows))
    return {
        "result_type": "portfolio_snapshot",
        # Persistent snapshot/account identifiers stay local. Tool results are
        # rendered into OpenRouter context, where they are not needed.
        "created_at": snapshot.created_at.isoformat(),
        "created_at_local": snapshot.created_at.astimezone().isoformat(),
        "broker": snapshot.broker,
        "account_count": len(snapshot.account_ids),
        "total_value": _str_or_none(snapshot.total_value),
        "cash": _str_or_none(snapshot.cash),
        "invested_value": _str_or_none(snapshot.invested_value),
        "position_count": len(snapshot.positions),
        "priced_position_count": sum(
            1 for position in snapshot.positions if position.market_value is not None
        ),
        "unresolved_position_count": sum(
            1 for position in snapshot.positions if position.entity_id is None
        ),
        "concentration": _str_or_none(
            portfolio_concentration(
                [position.portfolio_weight for position in snapshot.positions]
            )
        ),
        "positions": position_rows,
        "omitted_count": omitted_count,
        "largest_positions": [
            {"ticker": ticker, "market_value": _str_or_none(value)}
            for ticker, value in largest_positions(
                [(position.ticker, position.market_value) for position in snapshot.positions],
                limit=_PORTFOLIO_TOP_LARGEST,
            )
        ],
        "unresolved": [
            position.ticker
            for position in snapshot.positions
            if position.entity_id is None
        ],
        "freshness": {
            "snapshot_created_at": snapshot.created_at.isoformat(),
            "snapshot_created_at_local": snapshot.created_at.astimezone().isoformat(),
            **_research_freshness(
                [item.research_data_freshness for item in research.values()]
            ),
        },
        "source": "robinhood_mcp",
    }


_SCAN_SPECS_CAP = 60
_SCAN_LIST_CAP = 60
_SCAN_RESULTS_ROWS = 20
_SCAN_WRITE_PREVIEW_ROWS = 10


def _scan_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Instrument rows from a scan payload under any of the common keys."""
    rows = _rows(data, "results", "instruments", "rows", "items")
    return rows if rows is not None else []


def _get_scanner_filter_specs(arguments: dict, model: str) -> dict:
    del arguments, model
    data = RobinhoodPortfolioProvider(_robinhood_client()).get_scanner_filter_specs()
    specs = data.get("filter_specs")
    rows = [row for row in specs if isinstance(row, dict)] if isinstance(specs, list) else _scan_rows(data)
    return {
        "result_type": "scan_specs",
        "count": len(rows),
        "specs": rows[:_SCAN_SPECS_CAP],
        "omitted_count": max(0, len(rows) - _SCAN_SPECS_CAP),
        "source": "robinhood_mcp",
    }


def _get_scans(arguments: dict, model: str) -> dict:
    del arguments, model
    rows = RobinhoodPortfolioProvider(_robinhood_client(
        account_tools=frozenset({"get_scans"})
    )).get_scans()
    return {
        "result_type": "scan_list",
        "count": len(rows),
        "scans": rows[:_SCAN_LIST_CAP],
        "omitted_count": max(0, len(rows) - _SCAN_LIST_CAP),
        "source": "robinhood_mcp",
    }


def _run_scan(arguments: dict, model: str) -> dict:
    del model
    scan_id = str(arguments["scan_id"])
    limit = max(1, min(int(arguments.get("limit") or _SCAN_RESULTS_ROWS), 25))
    data = RobinhoodPortfolioProvider(_robinhood_client(
        account_tools=frozenset({"run_scan"})
    )).run_scan(scan_id)
    rows = _scan_rows(data)
    return {
        "result_type": "scan_results",
        "scan_id": scan_id,
        "title": str(_first(data, "title", "name") or ""),
        "total": _first(data, "total", "total_matches", "match_count", "count"),
        "rows": rows[:limit],
        "omitted": max(0, len(rows) - limit),
        "sort": _first(data, "sort", "sort_order"),
        "filters": _first(data, "filters", "active_filters"),
        "live": True,
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


def _search_web(args: dict, model: str) -> dict:
    """Exa-backed web search; harness-level soft failures must not stop the run."""
    result = exa_client.search(
        args["query"],
        category=args.get("category"),
        include_domains=args.get("include_domains"),
        exclude_domains=args.get("exclude_domains"),
        start_published_date=args.get("start_published_date"),
        end_published_date=args.get("end_published_date"),
        search_type=args.get("search_type") or "auto",
        limit=args.get("limit") or exa_client.EXA_DEFAULT_LIMIT,
    )
    if isinstance(result, dict) and "error" in result:
        result["soft"] = True  # harness-level: soft failures must not stop the run
    return result


def plan_public_search_queries(
    primary_name: str | None = None,
    primary_ticker: str | None = None,
    related_names: Any = (),
) -> list[dict]:
    """PUBLIC search targets → search_web args (planning only, no numbers).

    Targets come only from the user request, canonical/public
    relationships, public screens, or explicitly named companies.
    Queries carry names only — never quantities/weights/cost
    basis/account IDs (`authorize_egress` + `private_pattern_hit`
    remain the gate, unchanged).
    """
    # ponytail: names-only mapping, no query-builder lib for 3 strings
    targets: list[str] = []
    seen: set[str] = set()

    def _add(name: object) -> None:
        if not isinstance(name, str):
            return
        key = name.strip()
        if not key or key.casefold() in seen:
            return
        seen.add(key.casefold())
        targets.append(key)

    if isinstance(primary_name, str) and primary_name.strip():
        _add(primary_name.strip())
    elif isinstance(primary_ticker, str) and primary_ticker.strip():
        _add(primary_ticker.strip().upper())
    for name in related_names or ():
        _add(name)
    return [{"query": f"{name} recent announcements"} for name in targets[:3]]


def suggest_public_search_queries(
    primary_entity_id: str | None,
    primary_name: str | None,
    primary_ticker: str | None,
    relationships: Any = (),
    names_by_entity: Any = None,
) -> list[dict]:
    """Warehouse-aware wrapper: single-hop EntityRelationships."""
    related: list[str] = []
    if primary_entity_id:
        for rel in relationships or ():
            other = None
            try:
                if rel.from_entity_id == primary_entity_id:
                    other = rel.to_entity_id
                elif rel.to_entity_id == primary_entity_id:
                    other = rel.from_entity_id
            except AttributeError:
                continue
            if other and isinstance(names_by_entity, dict) and other in names_by_entity:
                related.append(names_by_entity[other])
    return plan_public_search_queries(primary_name, primary_ticker, related)


def _wrap_list(identifier, records, key: str) -> dict:
    """SEC list results: identifier echo, count, to_dict records, source."""
    items = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in records or []]
    return {"subject": identifier, "count": len(items), key: items, "source": "SEC EDGAR"}


# search_tools catalog: name -> (domain, keyword tags). New tools add one
# line here; prompts never change.
_SEC_TOOL_TAGS = {
    "list_sec_filings": ("filings", "list filings forms 10-K 10-Q 8-K discovery accession"),
    "get_sec_filing": ("filings", "filing record accession metadata filed known amendment"),
    "list_sec_documents": ("filings", "documents exhibits attachments list accession"),
    "get_sec_document": ("filings", "document text read MD&A risk factors business primary exhibit"),
    "diff_sec_filings": ("filings", "diff compare change amendment prior risk factors"),
    "get_material_events": ("events", "material events changed new earnings bankruptcy 8-K since"),
    "get_beneficial_ownership": ("ownership", "beneficial ownership 13D 13G holder stake percent activist passive"),
    "get_ownership_changes": ("ownership", "ownership change holder stake increase decrease activist"),
    "get_insider_activity": ("insider", "insider purchase sale transactions Form 4 open market"),
    "get_planned_insider_sales": ("insider", "planned insider sale Form 144 proposed notice"),
    "get_offering_history": ("offerings", "offering registration S-1 S-3 prospectus financing shelf"),
    "get_dilution_profile": ("offerings", "dilution shares offering ATM convertible warrant"),
    "get_institutional_ownership": ("ownership", "institutional 13F holdings fund manager"),
    "get_governance_events": ("governance", "governance proxy DEF 14A vote shareholder board compensation"),
    "get_transaction_status": ("transactions", "transaction merger tender offer acquisition S-4 status"),
    "get_short_pressure_profile": ("market", "short interest pressure squeeze positioning outstanding"),
}

_SEC_DOMAIN_PACKS = {
    "filings": ["list_sec_filings", "get_sec_filing", "list_sec_documents", "get_sec_document", "diff_sec_filings"],
    "events": ["get_material_events"],
    "ownership": ["get_beneficial_ownership", "get_ownership_changes"],
    "insider": ["get_insider_activity", "get_planned_insider_sales"],
    "offerings": ["get_offering_history", "get_dilution_profile"],
    "governance": ["get_governance_events"],
    "transactions": ["get_transaction_status"],
    "market": ["get_short_pressure_profile"],
}


def _search_tools(args: dict, model: str) -> dict:
    """Keyword search over tool names, descriptions, and domain tags."""
    del model
    query = (args.get("query") or "").strip().lower()
    domain = (args.get("domain") or "").strip().lower()
    by_name = {tool["function"]["name"]: tool for tool in TOOLS}
    if domain and not query:
        names = _SEC_DOMAIN_PACKS.get(domain, [])
        return {"domain": domain, "schemas": [by_name[n] for n in names if n in by_name]}
    tokens = query.split()
    matches = []
    for name, tool in by_name.items():
        if name == "search_tools":
            continue
        tags = _SEC_TOOL_TAGS.get(name, ("", ""))[1]
        haystack = f"{name} {tool['function'].get('description', '')} {tags}".lower().replace("_", " ")
        if tokens and all(token in haystack for token in tokens):
            matches.append(tool)
    return {"query": args.get("query"), "count": len(matches), "schemas": matches}
# Direct-dispatch tools (EDGAR/analyst/obligations/valuation) — same
# registry pattern as the FINRA/Robinhood handler maps below.
_DIRECT_HANDLERS = {
    "evaluate_mandate": lambda args, model: evaluate_mandate(),
    "get_fundamentals": lambda args, model: sec_facts.get_fundamentals(
        args["ticker"], args["metric"], as_of=args.get("as_of")
    ),
    "list_sec_filings": lambda args, model: _wrap_list(
        args.get("ticker"), sec.list_sec_filings(
            args["ticker"], forms=args.get("forms"), start_date=args.get("start_date"),
            end_date=args.get("end_date"), as_of=args.get("as_of"),
            limit=args.get("limit", 50),
        ), "filings",
    ),
    "get_sec_filing": lambda args, model: sec.get_sec_filing(args["accession_no"]).to_dict(),
    "list_sec_documents": lambda args, model: _wrap_list(
        args.get("accession_no"), sec.list_sec_documents(args["accession_no"]), "documents",
    ),
    "get_sec_document": lambda args, model: sec.get_sec_document(
        args["accession_no"], args.get("document_name"),
    ),
    "diff_sec_filings": lambda args, model: sec.diff_filings(
        args["current_accession"], args["previous_accession"], section=args.get("section"),
    ),
    "get_material_events": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_material_events(
            args["ticker"], args["since"], as_of=args.get("as_of"),
            limit=args.get("limit", 50),
        ), "events",
    ),
    "get_beneficial_ownership": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_beneficial_ownership(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 20),
        ), "records",
    ),
    "get_ownership_changes": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_ownership_changes(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 20),
        ), "changes",
    ),
    "get_insider_activity": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_insider_activity(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 50),
        ), "transactions",
    ),
    "get_planned_insider_sales": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_planned_insider_sales(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 20),
        ), "proposed_sales",
    ),
    "get_offering_history": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_offering_history(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 50),
        ), "offerings",
    ),
    "get_dilution_profile": lambda args, model: sec.get_dilution_profile(
        args["ticker"], as_of=args.get("as_of"),
    ),
    "get_institutional_ownership": lambda args, model: sec.get_institutional_ownership(
        args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 10),
    ),
    "get_governance_events": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_governance_events(
            args["ticker"], since=args.get("since"), as_of=args.get("as_of"),
            limit=args.get("limit", 10),
        ), "events",
    ),
    "get_transaction_status": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_transaction_status(
            args["ticker"], as_of=args.get("as_of"), limit=args.get("limit", 10),
        ), "transactions",
    ),
    "get_short_pressure_profile": lambda args, model: sec.get_short_pressure_context(
        args["ticker"],
    ),
    "search_tools": _search_tools,
    "get_financial_statements": lambda args, model: edgar_client.get_financial_statements(
        args["ticker"], args["statement_type"]
    ),
    "get_xbrl_facts": lambda args, model: sec_facts.get_xbrl_facts(
        args["ticker"], args["concept"]
    ),
    "diff_risk_factors": lambda args, model: edgar_client.diff_risk_factors(args["ticker"]),
    "get_analyst_estimates": lambda args, model: analyst_client.get_analyst_estimates(args["ticker"]),
    "get_sp500_weight": lambda args, model: analyst_client.get_sp500_weight(args["ticker"]),
    "get_obligations": lambda args, model: obligations.get_obligations(args["ticker"]),
    "get_valuation_metrics": lambda args, model: valuation.get_valuation_metrics(args["ticker"]),
    "search_web": _search_web,
}

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
    "get_portfolio_snapshot": lambda arguments, model: _get_portfolio_snapshot(arguments, model),
    "get_scanner_filter_specs": lambda arguments, model: _get_scanner_filter_specs(arguments, model),
    "get_scans": lambda arguments, model: _get_scans(arguments, model),
    "run_scan": lambda arguments, model: _run_scan(arguments, model),
}

# Every model-visible tool has one application-level capability. This is
# separate from the Robinhood MCP registry, which governs broker operations.
# Broker/account-connected market reads (Robinhood quotes/options/scans)
# require BROKER_MARKET_READ so generic research contexts never expose them;
# portfolio/account tools additionally require PORTFOLIO_READ.
TOOL_CAPABILITIES: dict[str, Capability] = {
    "evaluate_mandate": Capability.PORTFOLIO_READ,
    "get_fundamentals": Capability.RESEARCH,
    "list_sec_filings": Capability.RESEARCH,
    "get_sec_filing": Capability.RESEARCH,
    "list_sec_documents": Capability.RESEARCH,
    "get_sec_document": Capability.RESEARCH,
    "diff_sec_filings": Capability.RESEARCH,
    "get_material_events": Capability.RESEARCH,
    "get_beneficial_ownership": Capability.RESEARCH,
    "get_ownership_changes": Capability.RESEARCH,
    "get_insider_activity": Capability.RESEARCH,
    "get_planned_insider_sales": Capability.RESEARCH,
    "get_offering_history": Capability.RESEARCH,
    "get_dilution_profile": Capability.RESEARCH,
    "get_institutional_ownership": Capability.RESEARCH,
    "get_governance_events": Capability.RESEARCH,
    "get_transaction_status": Capability.RESEARCH,
    "get_short_pressure_profile": Capability.RESEARCH,
    "search_tools": Capability.RESEARCH,
    "get_recent_ownership_filings": Capability.RESEARCH,
    "diff_risk_factors": Capability.RESEARCH,
    "get_financial_statements": Capability.RESEARCH,
    "get_xbrl_facts": Capability.RESEARCH,
    "get_short_interest": Capability.RESEARCH,
    "get_short_interest_leaderboard": Capability.RESEARCH,
    "get_reg_sho_volume": Capability.RESEARCH,
    "get_threshold_securities": Capability.RESEARCH,
    "get_analyst_estimates": Capability.RESEARCH,
    "get_sp500_weight": Capability.RESEARCH,
    "get_obligations": Capability.RESEARCH,
    "get_valuation_metrics": Capability.RESEARCH,
    "search_web": Capability.RESEARCH,
    "list_finra_datasets": Capability.RESEARCH,
    "describe_finra_dataset": Capability.RESEARCH,
    "get_finra_datapoints": Capability.RESEARCH,
    "query_finra": Capability.RESEARCH,
    "get_market_snapshot": Capability.BROKER_MARKET_READ,
    "get_option_chain": Capability.BROKER_MARKET_READ,
    "analyze_option_contract": Capability.BROKER_MARKET_READ,
    "compare_options": Capability.BROKER_MARKET_READ,
    "get_scanner_filter_specs": Capability.BROKER_MARKET_READ,
    "get_portfolio_snapshot": Capability.PORTFOLIO_READ,
    "get_scans": Capability.PORTFOLIO_READ,
    "run_scan": Capability.PORTFOLIO_READ,
}
PORTFOLIO_AUTHORIZED_TOOLS: frozenset[str] = frozenset(
    name for name, capability in TOOL_CAPABILITIES.items()
    if capability is Capability.PORTFOLIO_READ
)


def tools_for_capabilities(capabilities: frozenset[Capability]) -> list[dict]:
    """Return only schemas whose application capability is granted."""
    return [
        tool for tool in TOOLS
        if TOOL_CAPABILITIES.get(tool["function"]["name"]) in capabilities
    ]


def tool_is_permitted(name: str, context: RequestContext) -> bool:
    capability = TOOL_CAPABILITIES.get(name)
    return capability is not None and capability in context.capabilities


def _validate_tool_arguments(name: str, arguments: Any) -> str | None:
    """Schema-level argument check: object-ness plus required keys. Returns
    an error message, or None when the arguments are acceptable. Type
    checking is intentionally out of scope; lenient handler coercions
    (int(...), ...) remain the source of truth for value shapes."""
    if not isinstance(arguments, dict):
        return f"Tool arguments must be a JSON object for tool '{name}'"
    tool = next((t for t in TOOLS if t["function"]["name"] == name), None)
    parameters = (tool["function"].get("parameters") or {}) if tool else {}
    missing = [key for key in (parameters.get("required") or []) if key not in arguments]
    if missing:
        return f"Missing required argument(s) for tool '{name}': {', '.join(missing)}"
    return None


def execute_tool(
    name: str,
    arguments: dict,
    model: str,
    *,
    context: RequestContext,
) -> dict:
    """Dispatch a tool call by name. Always returns a JSON-serializable dict;
    never raises — errors are returned as {"error": ...} so the model can
    report them honestly (guardrail behavior)."""
    try:
        if not tool_is_permitted(name, context):
            return {"error": f"Tool is not permitted: {name}"}
        invalid = _validate_tool_arguments(name, arguments)
        if invalid is not None:
            return {"error": invalid, "error_type": "invalid_tool_arguments"}
        handler = (
            _DIRECT_HANDLERS.get(name)
            or _FINRA_HANDLERS.get(name)
            or _ROBINHOOD_HANDLERS.get(name)
        )
        if handler is None:
            return {"error": f"Unknown tool '{name}'"}
        return handler(arguments, model)
    except KeyError as e:
        return {"error": f"Missing required argument {e} for tool '{name}'"}
    except RobinhoodAuthRequired:
        logger.info("Robinhood tool '%s' not authorized; soft failure", name)
        return {
            "error": "Robinhood data unavailable (not authorized). Run `cli.py robinhood-login` to authorize.",
            "error_type": "auth_required",
            "soft": True,
            "source": "robinhood_mcp",
        }
    except Exception as e:
        if name in _ROBINHOOD_HANDLERS:
            # Provider errors can echo request arguments. Do not log
            # exception details (no account identifiers in logs) or place
            # them in a tool message that is subsequently sent to the LLM.
            logger.warning("Robinhood tool '%s' failed; provider details withheld", name)
            return {"error": f"Robinhood tool '{name}' failed; provider details withheld."}
        logger.exception("Tool '%s' failed", name)
        return {"error": f"Tool '{name}' failed: {e}"}
