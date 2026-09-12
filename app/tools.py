"""Tool implementations + OpenAI-format JSON schemas for Pi."""

import hashlib
import json
import logging
from dataclasses import dataclass
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal
from typing import TYPE_CHECKING

from . import analyst_client
from . import edgar_client
from . import exa_client
from . import finra_client
from . import obligations
from . import valuation
from .config import broker_enabled, get_data_root, get_robinhood_mcp_url
from .policy import Capability, RequestContext
from .analytics import screens
from .analytics.options import analyze_option, compare_options
from .analytics.portfolio import largest_positions, portfolio_concentration
from .robinhood import RobinhoodClient
from .robinhood import capabilities
from .robinhood.auth import DEFAULT_TOKEN_PATH, OAuthConfig, has_valid_tokens
from .robinhood.client import RobinhoodAuthRequired
from .robinhood.options import OptionQuote, normalize_option_quote
from .robinhood.portfolio import RobinhoodPortfolioProvider
from .services import risk as risk_service
from .domain.market.entities import EntityRelationship
from .domain.portfolio.models import Position
from .services.portfolio_research import PortfolioResearchPosition, SEC_CONCEPTS, enrich_portfolio_research
from .services.portfolio_sync import read_latest_snapshot, sync_robinhood_portfolio
from .sec.models import SECSearchResult
from .services import sec_facts
from . import sec
from .storage import duckdb

if TYPE_CHECKING:
    from .thesis.intake import IntakeProposal
    from .thesis.models import Thesis
    from .thesis.repository import ThesisRepository

logger = logging.getLogger(__name__)

# Structured thesis-proposal delta fields shared by thesis_create/refine.
# IntakeProposal.from_dict remains the source of truth for value shapes.
_THESIS_DELTA_PROPERTIES = {
    "scope": {"type": "string", "description": "Ticker scope (e.g. NVDA) or 'unknown'."},
    "claims": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"statement": {"type": "string"}},
            "required": ["statement"],
        },
    },
    "assumptions": {"type": "array", "items": {"type": "string"}},
    "invalidators": {"type": "array", "items": {"type": "string"}},
    "unknowns": {"type": "array", "items": {"type": "string"}},
    "expressions": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "intent": {"type": "string"},
                "instrument": {"type": "string"},
                "direction": {"type": "string"},
                "structure": {"type": "string"},
                "horizon": {"type": "string"},
            },
            "required": list[str](),
        },
    },
    "questions": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "question_type": {"type": "string"}},
            "required": ["question"],
        },
    },
}

_THESIS_PROPOSAL_KEYS = tuple(_THESIS_DELTA_PROPERTIES)

TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "get_fundamentals",
            "description": "Single reported fundamental for one ticker: a specific numeric fundamental (EPS, "
                "dividends, balance sheet line item, shares outstanding) for a ticker. "
                "Note: shares_outstanding is SEC-reported shares outstanding, "
                "not public float. Call this for any request for a specific "
                "numeric metric. Do NOT use for full financial statements (get_financial_statements), XBRL-tagged facts by concept name (get_xbrl_facts), cheap-vs-expensive multiples (get_valuation_metrics), or forward consensus expectations (get_analyst_estimates). When presenting EPS, show basic and diluted EPS side by side in a markdown table "
                "with period, basic EPS, and diluted EPS columns, including TTM for both when available. Dividends responses include last paid and next "
                "SEC-declared (upcoming) dividends with filing provenance; undeclared estimates are never included. Render past, present, and future-declared dividends under separate headings; anything undeclared is an estimate and must never appear under NEXT DECLARED. Dividend metrics are tool-computed; interpret, never recalculate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "metric": {"type": "string", "enum": [
                        "eps", "dividends", "balance_sheet", "shares_outstanding", "overview"
                    ]},
                    "as_of": {"type": "string", "description": "Point-in-time query date YYYY-MM-DD; store-backed for eps/shares_outstanding/dividends; live results are labeled data_source=live."}
                },
                "required": ["ticker", "metric"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_sec_entities",
            "description": "Resolves a company name, ticker, or CIK to verified SEC entity candidates (CIK, tickers, verification status), including no-ticker registrants and former names. Ties and fuzzy-only matches stay ambiguous; verify identity before list_sec_filings. Default is non-exhaustive (capped at limit).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; former names apply only within their known/valid interval."},
                    "exhaustive": {"type": "boolean", "description": "Fan out over all entity routes (default false; non-exhaustive)."},
                    "limit": {"type": "integer", "description": "Max candidates to return (default 20); higher values probe deeper."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_sec_filings",
            "description": "EDGAR discovery over entity, full-text (EFTS), filer-submissions, global filing, and local routes (default non-exhaustive, capped at limit). Hits are text mentions: each names the filer (filer_name/filer_cik) and the exact matched document, never inferred subject identity. Returns coverage, attempts, counts, PIT basis, warnings/errors, auto-queued backfill jobs, and bounded evidence IDs.",
            "parameters": {
                "type": "object",
                "properties": {
                "query": {"type": "string"},
                "ticker": {"type": "string"},
                "cik": {"type": "string"},
                "company_name": {"type": "string"},
                "person_name": {"type": "string"},
                "domain": {"type": "string"},
                "security_identifier": {"type": "string", "description": "Ticker, CUSIP, ISIN, or class title; never treated as issuer identity."},
                "accession_no": {"type": "string"},
                "forms": {"type": "array", "items": {"type": "string"}},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; records known after it are excluded."},
                "exhaustive": {"type": "boolean", "description": "Fan out over all routes (default false; non-exhaustive)."},
                "limit": {"type": "integer"}
                },
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sec_filings",
            "description": "Lists SEC EDGAR filings for an exact ticker or CIK (Apple: AAPL); does NOT search company names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string"},
                    "forms": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."},
                    "limit": {"type": "integer"}
                },
                "required": ["identifier"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_sec_relationships",
            "description": "Ownership and transaction relationships for an entity (CIK, ticker, or entity id), both directions: 13D/G beneficial owners, 13F manager holdings (either direction), insider issuer/owner links, transaction filer/target/acquirer, offering filer/registrant, plus verified workflow links and observed mentions. Mentions never flatten into verified links; transaction status stays unknown without closing evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "description": "CIK, ticker, entity id, or candidate dict."},
                    "relationship_types": {"type": "array", "items": {"type": "string"}, "description": "Optional open-vocabulary type filter (e.g. beneficial_owner, holding_manager)."},
                    "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD."},
                    "limit": {"type": "integer"},
                    "exhaustive": {
                        "type": "boolean",
                        "description": (
                            "Exhaust all applicable relationship indexes and SEC routes; "
                            "the returned model context remains bounded."
                        ),
                        "default": True,
                    }
                },
                "required": ["entity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_search_coverage",
            "description": "Reads persisted SEC ingestion coverage and backfill jobs only (never infers completeness from rows). Use to check whether a form/source/date partition is covered or still queued/running/failed, or to revisit one search's ledger.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "form": {"type": "string"},
                    "search_id": {"type": "string"},
                    "limit": {"type": "integer"}
                },
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_filing",
            "description": "Returns one filing's record (filer, subject when known, form, filed/accepted/known dates, period, primary document, amendment link, source URL) by accession number. When the accession number is unknown, find it with list_sec_filings.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string", "description": "SEC accession number, e.g. 0000320193-25-000079. Named accession_no, not accession_number."}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sec_documents",
            "description": "Lists the documents and exhibits attached to one filing by accession number. When the accession number is unknown, find it with list_sec_filings.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string", "description": "SEC accession number, e.g. 0000320193-25-000079. Named accession_no, not accession_number."}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_document",
            "description": "Returns a bounded window of one filing document's text (default: primary document) by accession number. Defaults to the first 12000 characters; page with offset/max_chars. Load only the document relevant to the question, never full history. When the accession is already known, pass it; use get_material_events only to discover what changed.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string", "description": "SEC accession number, e.g. 0000320193-25-000079. Named accession_no, not accession_number."}, "document_name": {"type": "string"}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}, "offset": {"type": "integer", "description": "Character offset into the document text (default 0)."}, "max_chars": {"type": "integer", "description": "Characters to return, 1..32000 (default 12000)."}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diff_sec_filings",
            "description": "Self-contained full-filing diff for one ticker: pass ticker (plus optional forms hint like S-3/A) and the two most recent matching filings resolve internally; or pass two accession numbers directly. Returns added/removed language. Do NOT call list_sec_filings first. Do NOT use for risk-factor-only year-over-year diffs for one ticker (diff_risk_factors).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "forms": {"type": "array", "items": {"type": "string"}}, "current_accession": {"type": "string"}, "previous_accession": {"type": "string"}, "section": {"type": "string"}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_material_events",
            "description": "What changed since a date: deterministic 8-K-derived events with accession citations. Call for 'what changed/what's new' questions. Takes a ticker and date; never load full history.",
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
            "description": "5%+ beneficial-ownership records (SC 13D/G): holder, shares, percent, voting/dispositive powers. Deterministic numbers, never web prose. Use for current 5%+ stakes; use get_ownership_changes for stake changes. Takes a ticker.",
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
            "description": "Deterministic diffs between a holder's consecutive 13D/G filings: share and percent changes plus voting/text changes. Use for stake changes; use get_beneficial_ownership for current 5%+ stakes. Takes a ticker.",
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
            "description": "Executed insider buys/sells for one ticker: insider transactions (Forms 3/4/5) with SEC transaction codes mapped to purchase/sale/exercise/grant/gift/conversion/withholding/other. Disposals are never defaulted to bearish selling. Use for actual insider purchases and sales by executives and directors. Do NOT use for planned but unexecuted Form 144 sales (get_planned_insider_sales). Takes a ticker.",
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
            "description": "Planned Form 144 sale notices not yet executed for one ticker: proposed insider sales. Use for proposed or planned insider sales. Do NOT use for completed insider trades (get_insider_activity); compare with get_insider_activity for follow-through. Takes a ticker.",
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
            "description": "Financing history (S-1/S-3/424B/EFFECT): offering terms with source-registration links. Unknown terms stay unknown, never estimated. Pair with get_dilution_profile for financing/dilution work. Takes a ticker.",
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
            "description": "Deterministic dilution math: inputs, formula, and source accessions always shown. Unquantifiable terms return not_quantifiable. Pair with get_offering_history for financing/dilution work. Takes a ticker.",
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
            "name": "get_governance_events",
            "description": "Proxy/governance filing context (DEF 14A, contested forms, information statements) with retrieval pointers. Takes a ticker.",
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
            "description": "M&A filing context (tender offers, 14D-9, S-4, merger proxies). Deal status is unknown until structured parsers land; use get_sec_document for filing text. Takes a ticker.",
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
            "description": "Short pressure vs shares outstanding for one ticker: FINRA positioning plus SEC shares outstanding and their ratio. Do NOT use for one ticker's biweekly short position alone (get_short_interest) or daily short-sale volume (get_reg_sho_volume). Describes positioning only; never assesses manipulation or causation. Takes a ticker.",
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
            "description": "Search for relevant Stockbot tools when the active tools cannot perform the task. Returns compact routing cards with ambiguity groups; browse_tools remains the hierarchical catalog path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Non-empty capability description (not a company or ticker); never call with an empty query."},
                    "domain": {"type": "string"},
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tool_domains",
            "description": "List the tool-domain catalog: domain names with one-line descriptions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_tool",
            "description": "Show full metadata for one named Stockbot tool: domain, family, intent, output kind, source, entity scope, time mode, choose-when and reject-when notes, conflicts, related tools, prerequisites, required and optional arguments. Call with the exact tool name, or describe several tools in one call with names. Use for 'what can X do' questions; do not run the subject tool instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "names": {"type": "array", "items": {"type": "string"}, "description": "Describe several tools in one call, in order."},
                },
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "browse_tools",
            "description": "Browse the hierarchical Stockbot tool catalog: root domains, one domain's families, one family's tools with contrast, or one tool's full metadata with canonical parameters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "family": {"type": "string"},
                    "name": {"type": "string"},
                },
                "additionalProperties": False
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_tool",
            "description": "Call one catalog tool by its exact canonical name with arguments; validates against the canonical schema and executes through the standard gateway path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
                "required": ["name", "arguments"],
                "additionalProperties": False
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
                "or 'latest' big-investor filings when no ticker is given. Do not drill into get_beneficial_ownership unless asked. It lists filings, it "
                "does not establish that a filing caused a price move.",
            "parameters": {
                "type": "object",
                "properties": {
                    "form_type": {"type": "string", "enum": ["SC 13D", "SC 13G", "both"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25}
                },
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "diff_risk_factors",
            "description": "Self-contained Risk Factors year-over-year diff for one ticker: what changed in Risk Factors language "
                "vs. the prior filing. Takes a ticker alone; filings resolve internally so do NOT call list_sec_filings first. Call for risk-disclosure change framing (what is new/changed). Do NOT use for full-filing diffs between two accessions (diff_sec_filings). "
                "Do not use for disclosure or mention questions without change framing; use search_sec_filings instead.",
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
            "description": "Full parsed statements for one ticker: income statement, "
                "balance sheet, and cash flow from 10-K or 10-Q filings. Do NOT use for a single metric like EPS (get_fundamentals) or a single XBRL-tagged fact (get_xbrl_facts). Takes a ticker.",
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
            "description": "Single XBRL-tagged fact by concept name for one ticker: XBRL financial metrics (Revenue, Net Income, "
                "Cash, Debt, Equity, etc.) for any company. Do not use this for EPS; for EPS use get_fundamentals(metric=\"eps\"). Use exact XBRL tag names (e.g. NetIncomeLoss for net income), never friendly labels. Do NOT use for full statements (get_financial_statements). Takes a ticker and concept.",
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
            "description": "Biweekly short position for one ticker: FINRA consolidated short interest "
                "(current/previous short position, days to cover, average daily "
                "volume, percent change). Call for short interest, short float, "
                "or days-to-cover questions. Do NOT use for daily short-sale volume by venue (get_reg_sho_volume), market-wide most-shorted screens (get_short_interest_leaderboard), or short-vs-shares-outstanding context (get_short_pressure_profile). For change-over-time or trend "
                "questions, prefer query_finra. When the user asks to show "
                "figures or values, or names exact fields, prefer "
                "get_finra_datapoints. Takes a ticker.",
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
            "description": "Market-wide most-shorted screen: FINRA short-interest leaderboard, short interest as a percentage of SEC-reported shares outstanding for tickers that map 1:1 to an SEC CIK whose security is classified as common equity and that has a shares-outstanding fact knowable on or before the as-of date (default: today). Excludes symbols that cannot be mapped to a single SEC entity, are not classified as common equity (funds, ETFs, preferred issues), lack a usable shares-outstanding fact, or have invalid short-interest quantities; every exclusion is counted and returned in coverage. Use for questions such as 'which stock has the highest short interest', 'most shorted stock', or 'short interest as a percent of total shares'. Do NOT use for one ticker's short interest (get_short_interest). This is a deterministic, complete FINRA settlement-date screen; it is NOT percent of public float and is not real-time short interest.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of ranked stocks to return; default 10, maximum 25."},
                    "settlement_date": {"type": "string", "description": "Optional FINRA settlement date (YYYY-MM-DD). Omit for the latest published FINRA cycle."},
                    "as_of": {"type": "string", "description": "Optional knowledge horizon (YYYY-MM-DD). Only data knowable on or before this date is used. Defaults to today; pass an explicit date for a historical screen."}
                },
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reg_sho_volume",
            "description": "Self-contained daily short-sale volume by venue for one ticker: FINRA daily Reg SHO short-sale volume "
                "ticker (short, short-exempt, and total share quantity by "
                "reporting facility). Rolling 12 months. Takes a ticker alone; dataset and fields resolve internally so do NOT call describe_finra_dataset or get_finra_datapoints. Do NOT use for biweekly short interest positions (get_short_interest). Takes a ticker.",
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
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_analyst_estimates",
            "description": "Forward sell-side consensus expectations for one ticker: sell-side consensus estimates "
                "from Yahoo Finance: latest quote, analyst 12-month price "
                "targets (mean/median/high/low) and recommendation rating, "
                "forward EPS and revenue estimates per period (current quarter, "
                "next quarter, current fiscal year, next fiscal year) with "
                "growth rates, plus EPS estimate-revision trend (7/30/60 days "
                "ago). Call for analyst estimates, price targets, consensus "
                "expectations, forward growth, or valuation-vs-consensus "
                "questions. Do NOT use for reported historical EPS (get_fundamentals). Consensus moves daily; the response includes the "
                "as-of timestamp. Always state the as-of date.",
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
            "description": "Future cash obligations from 10-K/10-Q notes for one ticker: quantified contractual obligations and "
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
                "in the future' question. Do NOT use for cheap-vs-expensive multiples (get_valuation_metrics). Contingent items are NOT counted "
                "in adjusted EPS. Treat on-balance-sheet (already accrued) items as informational and never double-count them; never present contingent or off-balance-sheet obligations as certain. Takes a ticker.",
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
            "description": "Cheap-vs-expensive earnings multiples at live price for one ticker: valuation metrics anchored to the live "
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
                "questions. Do NOT use for reported EPS alone (get_fundamentals) or forward consensus alone (get_analyst_estimates). Never present the stress scenario as 'adjusted'. Always state which ledger tier you are citing plus the live price and its timestamp. Takes a ticker.",
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
                "filters. Prefer calling the analysis tool directly with a known dataset ID; use this listing only to resolve an ID search did not surface.",
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
                "required": list[str]()
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "describe_finra_dataset",
            "description": "Describes one FINRA dataset: fields with types and "
                "descriptions, ticker/date fields, documented filter values, and "
                "supported methods. Takes the dataset ID directly.",
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
            "description": "Exact raw rows from any named FINRA dataset: returns exact source values "
                "for explicit data requests ONLY (e.g. 'show the last five "
                "settlement-date values' or 'show recent position figures'). "
                "Requires a 'fields' list and at least one narrowing "
                "condition (ticker, date/date range, or filters). IMPORTANT: "
                "when the user requests named datapoints with friendly "
                "labels (e.g. 'days to cover', 'average daily volume'), "
                "call describe_finra_dataset FIRST and use the "
                "metadata's exact field names (e.g. daysToCoverQuantity, "
                "averageDailyVolumeQuantity) in the fields list — never "
                "friendly labels. For 'latest five' / 'last five' / 'most "
                "recent' requests, add sort_fields [\"-<dateField>\"] or "
                "sort_order \"desc\" (or \"asc\" for oldest first); the "
                "client resolves the sort against the dataset's partitions "
                "automatically. Do NOT use for ordinary analysis — query_finra "
                "and the specific helper tools return analyzed briefings "
                "instead. Common short-interest fields: settlementDate, symbolCode, "
                "currentShortPositionQuantity, previousShortPositionQuantity, "
                "averageDailyVolumeQuantity, daysToCoverQuantity. For other datasets, "
                "call describe_finra_dataset first for exact field names — ticker "
                "plus dataset is enough to begin. Returns at "
                "most 25 rows containing only the requested fields. Exact "
                "source values are guaranteed for normal scalar data; "
                "oversized text fields are rendered as a marked excerpt "
                "(table cells are capped at 200 characters to keep the tool "
                "message compact). Takes the dataset ID directly.",
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
                        "description": "Exact field names to return (e.g. settlementDate, symbolCode, currentShortPositionQuantity for short interest).",
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
                        "description": "Extra compare filters (field names must exist on the dataset — when unknown, call describe_finra_dataset first).",
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
            "description": "Analyzed FINRA briefing with trends and metrics over any named dataset: queries a FINRA dataset by canonical group/name "
                "(or legacy bare name) and returns an analyzed briefing: "
                "query provenance, coverage dates, deterministic metrics "
                "(min/max/mean/median/sum, latest-vs-prior change), derived "
                "trends, data-quality warnings, and a concise prose briefing. "
                "Raw source records are NOT returned. Prefer get_short_interest "
                "/ get_reg_sho_volume / get_threshold_securities for those "
                "specific questions. Takes the dataset ID directly with a bounded limit. "
                "Use get_finra_datapoints only when the user explicitly asks "
                "to see exact source values. For more records, paginate with offset using the returned next_offset/may_have_more indicators. If a result is flagged stale or historical (newest date older than 90 days), say so explicitly and never present it as current market data.",
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
                        "description": "Extra compare filters (field names must exist on the dataset — when unknown, call describe_finra_dataset first).",
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
                "required": list[str]()
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scanner_filter_specs",
            "description": "Lists every valid Robinhood scanner filter type and usage (read-only catalog).",
            "parameters": {"type": "object", "properties": dict[str, object](), "required": list[str]()},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evaluate_mandate",
            "description": "Deterministic risk/mandate evaluation of the latest portfolio snapshot against data/mandate.json: sector exposure, single-position weight, minimum cash, prohibited assets. Breaches are computed by Stockbot; explain them, do not recalculate.",
            "parameters": {"type": "object", "properties": dict[str, object](), "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scans",
            "description": "Lists the user's saved Robinhood scanners (screeners): id, title, active filters, configured columns, sort order, and whether the scan is Cortex-managed (read-only).",
            "parameters": {"type": "object", "properties": dict[str, object](), "required": list[str]()},
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
            "description": "Current qualitative evidence from the web (news, announcements, competitive/industry developments, management commentary, publications, specialist commentary, counterevidence) with bounded highlights. NOT a source for exact financial facts, portfolio state, historical point-in-time facts, mandate calculations, or deterministic screens — use the canonical SEC/FINRA/Robinhood/local-warehouse tools for those. Distinguish published_at from retrieved_at and never claim historical completeness. Never use for market-wide screening; deterministic screens generate candidates first.",
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
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Maximum results, 1-25 (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thesis_create",
            "description": "Creates a thesis from a structured proposal. Returns thesis_id, scope, initial supported watch rules, or setup-needed state with missing questions when no target resolves. Never invents thresholds. Takes a bare investment view with no existing thesis ID (thesis_refine needs an existing ID).",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_thesis": {"type": "string", "description": "The user's investment thesis in their own words."},
                    **_THESIS_DELTA_PROPERTIES,
                },
                "required": ["user_thesis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thesis_show",
            "description": "Reads one thesis with its assessment, watch rules, and open questions. Nonmutating.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Thesis ID or slug."},
                    "as_of": {"type": "string", "description": "Point-in-time cutoff (ISO-8601); omit for current state."},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thesis_refine",
            "description": "Refines a thesis with a clarification plus optional structured deltas. Adds claims/expressions and supported watch rules; never overwrites user-disabled rules. Refuses paused/closed theses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Thesis ID or slug."},
                    "clarification": {"type": "string", "description": "New information or correction in the user's own words."},
                    **_THESIS_DELTA_PROPERTIES,
                },
                "required": ["id", "clarification"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thesis_watch",
            "description": "Lists a thesis's watch rules, or appends one validated supported rule (IDs and domain input only). Never modifies existing rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Thesis ID or slug."},
                    "rule_type": {"type": "string", "description": "Semantic monitor name to add (omit to only list rules)."},
                    "claim_ids": {"type": "array", "items": {"type": "string"}},
                    "expression_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thesis_journal",
            "description": "Appends one operator note to a thesis journal (active theses only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Thesis ID or slug."},
                    "title": {"type": "string"},
                    "body": {"type": "string", "description": "Note body (Markdown)."},
                    "trigger_id": {"type": "string", "description": "Trigger this entry completes (omit for ordinary notes)."},
                    "run_id": {"type": "string", "description": "Live run this entry completes (trigger-linked only)."},
                    "known_at": {"type": "string", "description": "PIT cutoff this entry is known at (ISO-8601)."},
                },
                "required": ["id", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_alternative_signals",
            "description": "Reads locally collected Google public-data discovery candidates (top/rising lists) with persistence/diffusion features only when exactly one PIT-valid v2 feature scope matches; otherwise features are null with available_feature_scopes listed. Candidates only, never materiality or investment claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring filter over candidate terms."},
                    "geo": {"type": "string", "description": "Geography filter, e.g. US."},
                    "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; candidates known after it are excluded."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max candidates (default 20)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend_evidence",
            "description": "Bounded Google Trends discovery collection over public top/rising lists with stable source identity and retrieval timestamps. List membership only, never search-volume claims. Takes a named trend.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Range start YYYY-MM-DD. Optional; both omitted defaults to trailing 7 days ending today UTC."},
                    "end_date": {"type": "string", "description": "Range end YYYY-MM-DD. Optional; both omitted defaults to trailing 7 days ending today UTC."},
                    "geos": {"type": "array", "items": {"type": "string"}, "description": "Geographies, e.g. [US]."},
                    "geo": {"type": "string", "description": "Single geography shorthand for geos."},
                    "term": {"type": "string", "description": "Optional substring filter over collected terms."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "Max rows (default 100)."},
                    "week_start": {"type": "string", "description": "Interest-week start YYYY-MM-DD; omitted defaults to the trailing 14-day week window ending at end_date."},
                    "week_end": {"type": "string", "description": "Interest-week end YYYY-MM-DD; omitted defaults to the trailing 14-day week window ending at end_date."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "investigate_social_arbitrage_candidate",
            "description": "Bounded enrichment for one discovery term: local signal evidence and SEC-confirmed/unresolved entity mappings plus a pointer to transient YouTube corroboration. Returns evidence and explicit gaps; never fabricates causality and never trades.",
            "parameters": {
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "Discovery term to investigate."},
                    "geo": {"type": "string", "description": "Geography, e.g. US."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Max evidence rows per source (default 5; YouTube never above 5)."},
                },
                "required": ["term"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macro_context",
            "description": "Bounded Data Commons statistical observations for explicit geography/variable IDs with unit/facet/provider provenance. Distinct facets stay distinct; never splices incompatible series. Takes explicit geography/variable IDs; do not use search_web for macro statistics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "geos": {"type": "array", "items": {"type": "string"}, "description": "Geography DCIDs, e.g. [geoId/06]."},
                    "variables": {"type": "array", "items": {"type": "string"}, "description": "Statistical variable IDs, e.g. [Count_Person]."},
                    "start_date": {"type": "string", "description": "Range start YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "Range end YYYY-MM-DD."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max observations (default 100)."},
                },
                "required": ["geos", "variables"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_company_patents",
            "description": "Bounded patent-publication search for documented company assignees via checked-in BigQuery templates. Counts publications explicitly; never labels counts as inventions or bullish signals. Patent records are authoritative here; never use search_web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_id": {"type": "string", "description": "Documented assignee name from existing company evidence."},
                    "assignees": {"type": "array", "items": {"type": "string"}, "description": "Documented assignee aliases (verified, never inferred from matching text)."},
                    "start_date": {"type": "string", "description": "Range start YYYY-MM-DD."},
                    "end_date": {"type": "string", "description": "Range end YYYY-MM-DD."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max publications (default 20)."},
                },
                "required": ["company_id", "assignees"],
            },
        },
    },
]
# TOOL_REGISTRY_VERSION moved below TOOL_DISCOVERY_REGISTRY (hashes schemas + routing metadata).


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


def _provider_payload(value: object) -> object:
    """Unwrap MCP envelope (genuinely dynamic provider JSON)."""
    if isinstance(value, dict):
        structured = value.get("structured_content") or value.get("structuredContent")
        if structured is not None:
            payload: object = structured
            return payload
        content = value.get("content")
        if isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if text:
                    try:
                        parsed: object = json.loads(text)
                        return parsed
                    except (TypeError, ValueError):
                        return {"text": text}
        return value
    return value


def _rows(payload: object, *keys: str) -> list[dict[str, object]]:
    unwrapped = _provider_payload(payload)
    if isinstance(unwrapped, list):
        return [{str(k): v for k, v in row.items()} for row in unwrapped if isinstance(row, dict)]
    if not isinstance(unwrapped, dict):
        return []
    for key in keys:
        value = unwrapped.get(key)
        if isinstance(value, list):
            return [{str(k): v for k, v in row.items()} for row in value if isinstance(row, dict)]
    for key in ("data", "results", "items", "records"):
        value = unwrapped.get(key)
        if isinstance(value, list):
            return [{str(k): v for k, v in row.items()} for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            nested = _rows(value, *keys)
            if nested:
                return nested
    return [{str(k): v for k, v in unwrapped.items()}]


def _first(value: object, *keys: str) -> object:
    if not isinstance(value, dict):
        return None
    for key in keys:
        if value.get(key) is not None:
            found: object = value[key]
            return found
    return None


def _quote_row(value: object) -> object:
    if isinstance(value, dict) and isinstance(value.get("quote"), dict):
        return value["quote"]
    return value


def get_market_snapshot(ticker: str) -> dict[str, object]:
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

def evaluate_mandate(data_root: Path | None = None, mandate_path: Path | None = None) -> dict[str, object]:
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


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    """Lenient tool-JSON int coercion (None stays None, garbage raises)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    return int(str(value))


def _tool_function(tool: dict[str, object]) -> dict[str, object]:
    """OpenAI schema function dict (TOOLS entries are untyped app-side JSON)."""
    function = tool.get("function")
    if isinstance(function, dict):
        return {str(k): v for k, v in function.items()}
    return {}


ModelHandler = Callable[[dict[str, object], str], dict[str, object]]
ContextHandler = Callable[[dict[str, object], RequestContext], dict[str, object]]


def _freshness_key(item: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(item.get("sec_latest_filed_at") or "0000-00-00"),
        str(item.get("finra_settlement_date") or "0000-00-00"),
        str(item.get("finra_retrieved_at") or ""),
    )


def _bases_count_key(bases: list[object]) -> Callable[[str], int]:
    def _count(s: str) -> int:
        return bases.count(s)
    return _count


def _research_freshness(freshness_items: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate per-position research freshness to one latest non-empty dict."""
    non_empty = [item for item in freshness_items if item]
    if not non_empty:
        return {}
    return max(non_empty, key=_freshness_key)


def _position_research_row(position: Position, research_item: PortfolioResearchPosition | None) -> dict[str, object]:
    row: dict[str, object] = {
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
        sec: dict[str, object] = {}
        for concept in SEC_CONCEPTS:
            fact = research_item.latest_sec_metrics.get(concept)
            if isinstance(fact, dict) and fact:
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


def _get_portfolio_snapshot(arguments: dict[str, object], model: str) -> dict[str, object]:
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
        # rendered into model context, where they are not needed.
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


def _scan_rows(data: dict[str, object]) -> list[dict[str, object]]:
    """Instrument rows from a scan payload under any of the common keys."""
    rows = _rows(data, "results", "instruments", "rows", "items")
    return rows if rows is not None else []


def _get_scanner_filter_specs(arguments: dict[str, object], model: str) -> dict[str, object]:
    del arguments, model
    data = RobinhoodPortfolioProvider(_robinhood_client()).get_scanner_filter_specs()
    specs = data.get("filter_specs")
    if isinstance(specs, list):
        rows = [{str(k): v for k, v in row.items()} for row in specs if isinstance(row, dict)]
    else:
        rows = [{k: v for k, v in row.items()} for row in _scan_rows(data)]
    result: dict[str, object] = {
        "result_type": "scan_specs",
        "count": len(rows),
        "specs": rows[:_SCAN_SPECS_CAP],
        "omitted_count": max(0, len(rows) - _SCAN_SPECS_CAP),
        "source": "robinhood_mcp",
    }
    return result


def _get_scans(arguments: dict[str, object], model: str) -> dict[str, object]:
    del arguments, model
    rows = RobinhoodPortfolioProvider(_robinhood_client(
        account_tools=frozenset({"get_scans"})
    )).get_scans()
    result: dict[str, object] = {
        "result_type": "scan_list",
        "count": len(rows),
        "scans": rows[:_SCAN_LIST_CAP],
        "omitted_count": max(0, len(rows) - _SCAN_LIST_CAP),
        "source": "robinhood_mcp",
    }
    return result


def _run_scan(arguments: dict[str, object], model: str) -> dict[str, object]:
    del model
    scan_id = str(arguments["scan_id"])
    limit = max(1, min(int(str(arguments.get("limit") or _SCAN_RESULTS_ROWS)), 25))
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


def _load_option_quotes(ticker: str, option_type: str, **filters: object) -> list[OptionQuote]:
    client = _robinhood_client()
    chain = _provider_payload(
        client.call_tool("get_option_chains", {"underlying_symbol": ticker})
    )
    chain_rows = _rows(chain, "chains", "option_chains")
    chain_id = _first(chain_rows[0], "chain_id", "chainId", "id") if chain_rows else None
    instrument_args: dict[str, object] = {"chain_symbol": ticker, "type": option_type}
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
    filtered_instruments: list[dict[str, object]] = []
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
        if filters.get("min_dte") is not None and (dte is None or dte < int(str(filters["min_dte"]))):
            continue
        if filters.get("max_dte") is not None and (dte is None or dte > int(str(filters["max_dte"]))):
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
    quotes_by_id: dict[str, object] = {}
    for row in quotes:
        quote = _quote_row(row)
        quote_id = _first(quote, "id", "instrument_id", "contract_id")
        if quote_id:
            quotes_by_id[str(quote_id)] = quote
    normalized: list[OptionQuote] = []
    for instrument in instruments:
        instrument_id = str(_first(instrument, "id", "instrument_id", "contract_id") or "")
        merged: dict[str, object] = dict(instrument)
        raw_quote = quotes_by_id.get(instrument_id)
        if isinstance(raw_quote, dict):
            merged.update(raw_quote)
        merged["contract_id"] = instrument_id
        merged["ticker"] = ticker
        try:
            normalized.append(normalize_option_quote(merged, ticker=ticker))
        except ValueError:
            continue
    return normalized


def get_option_chain(ticker: str, option_type: str, min_dte: object = None, max_dte: object = None, strike_min: object = None, strike_max: object = None, limit: object = 20) -> dict[str, object]:
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
        if (min_dte is None or (quote.expiration - today).days >= int(str(min_dte)))
        and (max_dte is None or (quote.expiration - today).days <= int(str(max_dte)))
        and (strike_min is None or quote.strike >= Decimal(str(strike_min)))
        and (strike_max is None or quote.strike <= Decimal(str(strike_max)))
    ]
    if not filtered:
        return {
            "error": f"No Robinhood {option_type} contracts matched the requested filters for {ticker}",
            "source": "robinhood_mcp",
        }
    bounded = max(1, min(int(str(limit or 20)), 30))
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


def analyze_option_contract(ticker: str, expiration: str, strike: object, option_type: str, target_price: object = None) -> dict[str, object]:
    quotes = _load_option_quotes(ticker.strip().upper(), option_type.lower(), expiration_date=expiration)
    matches = [quote for quote in quotes if quote.expiration.isoformat() == expiration and quote.strike == Decimal(str(strike))]
    if not matches:
        return {"error": "No matching Robinhood option contract found", "source": "robinhood_mcp"}
    return {"result_type": "option_analysis", **analyze_option(matches[0], target_price=(str(target_price) if target_price is not None else None)), "source": "robinhood_mcp"}


def compare_robinhood_options(ticker: str, option_type: str, target_price: object, min_dte: object = None, max_dte: object = None, strike_min: object = None, strike_max: object = None, limit: object = 20) -> dict[str, object]:
    quotes = _load_option_quotes(
        ticker.strip().upper(), option_type.lower(), min_dte=min_dte, max_dte=max_dte, strike_min=strike_min, strike_max=strike_max
    )
    today = date.today()
    filtered = [
        quote for quote in quotes
        if (min_dte is None or (quote.expiration - today).days >= int(str(min_dte)))
        and (max_dte is None or (quote.expiration - today).days <= int(str(max_dte)))
        and (strike_min is None or quote.strike >= Decimal(str(strike_min)))
        and (strike_max is None or quote.strike <= Decimal(str(strike_max)))
    ]
    if not filtered:
        return {
            "error": f"No Robinhood {option_type} contracts matched the requested filters for {ticker}",
            "source": "robinhood_mcp",
        }
    return {"result_type": "option_comparison", "ticker": ticker.upper(), "source": "robinhood_mcp", **compare_options(filtered, target_price=(str(target_price) if target_price is not None else None), limit=int(str(limit or 20)))}


def _search_web(args: dict[str, object], model: str) -> dict[str, object]:
    """Exa-backed web search; harness-level soft failures must not stop the run."""
    raw_inc = args.get("include_domains")
    if isinstance(raw_inc, list):
        include_domains: list[str] | None = [str(x) for x in raw_inc]
    else:
        include_domains = None
    raw_exc = args.get("exclude_domains")
    if isinstance(raw_exc, list):
        exclude_domains: list[str] | None = [str(x) for x in raw_exc]
    else:
        exclude_domains = None
    result = exa_client.search(
        str(args["query"]),
        category=_str_or_none(args.get("category")),
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        start_published_date=_str_or_none(args.get("start_published_date")),
        end_published_date=_str_or_none(args.get("end_published_date")),
        search_type=str(args.get("search_type") or "auto"),
        limit=args.get("limit") or exa_client.EXA_DEFAULT_LIMIT,
    )
    if isinstance(result, dict) and "error" in result:
        result["soft"] = True  # harness-level: soft failures must not stop the run
    return result


def plan_public_search_queries(
    primary_name: str | None = None,
    primary_ticker: str | None = None,
    related_names: Sequence[object] = (),
) -> list[dict[str, object]]:
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
    relationships: Sequence[EntityRelationship] = (),
    names_by_entity: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Warehouse-aware wrapper: single-hop EntityRelationships."""
    related: list[str] = []
    if primary_entity_id:
        for rel in relationships or ():
            other: str | None = None
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


def _google_soft(result: dict[str, object]) -> dict[str, object]:
    """Collector passthrough: error dicts get soft:true like _search_web."""
    if isinstance(result, dict) and "error" in result and "soft" not in result:
        result = dict(result)
        result["soft"] = True
    return result


def _google_import_error(source: str, exc: Exception) -> dict[str, object]:
    return {"status": "unavailable", "source": source, "soft": True,
            "error": f"Google data unavailable: {exc}", "error_type": "source_unavailable"}


def _arg_str(args: dict[str, object], key: str) -> str | None:
    """JSON-boundary narrow: schema strings only, None otherwise."""
    raw = args.get(key)
    return raw if isinstance(raw, str) else None


def _arg_str_list(args: dict[str, object], key: str) -> list[str]:
    raw = args.get(key)
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str)]
    return []


def _arg_int(args: dict[str, object], key: str, default: int) -> int:
    raw = args.get(key, default)
    return int(raw) if isinstance(raw, (int, str)) else default


def _find_alternative_signals(args: dict[str, object], model: str) -> dict[str, object]:
    """Local collected candidates only; disabled without credentials, never raises."""
    try:
        from .google_data import signals as _signals
    except Exception as exc:
        return _google_import_error("google", exc)
    try:
        limit = _arg_int(args, "limit", 20)
        rows = _signals.query_signals(
            query=_arg_str(args, "query"), geo=_arg_str(args, "geo"), as_of=_arg_str(args, "as_of"),
            limit=limit, data_root=get_data_root(),
        )
        try:
            capped = len(rows) >= max(1, limit)
        except (TypeError, ValueError):
            capped = False
        return {"status": "ok", "source": "google", "signals": rows,
                "count": len(rows),
                "coverage": {"query": _arg_str(args, "query"), "geo": _arg_str(args, "geo"),
                             "as_of": _arg_str(args, "as_of")},
                "warnings": [], "continuation": capped}
    except Exception as exc:
        logger.exception("find_alternative_signals failed")
        return {"error": f"Tool 'find_alternative_signals' failed: {exc}", "soft": True, "source": "google"}


def _get_trend_evidence(args: dict[str, object], model: str) -> dict[str, object]:
    try:
        from .google_data import trends as _trends
    except Exception as exc:
        return _google_import_error("trends", exc)
    try:
        geo = _arg_str(args, "geo")
        raw_geos = args.get("geos")
        str_geos: list[str] = [g for g in raw_geos if isinstance(g, str)] if isinstance(raw_geos, list) else []
        geos = str_geos or ([geo] if geo else ["US"])
        start_date = _arg_str(args, "start_date")
        end_date = _arg_str(args, "end_date")
        if start_date is None and end_date is None:
            _today = datetime.now(timezone.utc).date()
            end_date = _today.isoformat()
            start_date = (_today - timedelta(days=6)).isoformat()
        result = _google_soft(_trends.collect_trends(
            start_date=start_date, end_date=end_date,
            geos=list(geos), limit=_arg_int(args, "limit", 100),
            data_root=get_data_root(),
            week_start=_arg_str(args, "week_start"), week_end=_arg_str(args, "week_end"),
            term=_arg_str(args, "term"),
        ))
        return result


    except Exception as exc:
        logger.exception("get_trend_evidence failed")
        return {"error": f"Tool 'get_trend_evidence' failed: {exc}", "soft": True, "source": "trends"}


def _investigate_social_arbitrage_candidate(args: dict[str, object], model: str) -> dict[str, object]:
    """Evidence + gaps for one term; corroboration capped, causality never claimed."""
    term_raw = args.get("term", "")
    term = term_raw if isinstance(term_raw, str) else str(term_raw or "")
    geo = _arg_str(args, "geo") or "US"
    per_source = min(max(_arg_int(args, "limit", 5), 1), 25)
    evidence: dict[str, object] = {}
    confirmed: list[object] = []
    unresolved: list[object] = []
    gaps: list[str] = []
    result: dict[str, object] = {"term": term, "geo": geo, "source": "google",
                     "status": "ok", "evidence": evidence,
                     "entities": {"confirmed": confirmed, "unresolved": unresolved}, "gaps": gaps}
    try:
        from .google_data import signals as _signals
        evidence["signals"] = _signals.query_signals(
            query=term, geo=geo, limit=per_source, data_root=get_data_root())
    except Exception as exc:
        gaps.append(f"signals unavailable: {exc}")
    try:
        from datetime import datetime, timezone
        from .domain.market.identity import resolve_ticker_aliases as _resolve_alias
        from .sec.discovery.service import find_sec_entities as _find_sec
        from .storage.duckdb import ticker_alias_candidates as _alias_cands
        sec = _find_sec(query=term, max_results=5, data_root=get_data_root())
        for ent in list(getattr(sec, "entities", None) or []):
            cik = getattr(ent, "cik", None)
            entry = {"name": getattr(ent, "name", None) or term,
                     "cik": cik,
                     "verification_status": getattr(ent, "verification_status", None)}
            if getattr(ent, "verification_status", None) == "verified" and cik:
                confirmed.append(entry)
            else:
                unresolved.append(entry)
        as_of = datetime.now(timezone.utc)
        resolution = _resolve_alias(term.upper(),
                                    _alias_cands(term.upper(), as_of, get_data_root()),
                                    as_of=as_of)
        if resolution.resolved:
            confirmed.append(
                {"ticker": term.upper(), "entity_id": resolution.entity_id,
                 "security_id": resolution.security_id, "via": "ticker_alias"})
        elif not confirmed and not unresolved:
            unresolved.append({"ticker": term.upper(), "reason": "unresolved"})
    except Exception as exc:
        gaps.append(f"entity resolution unavailable: {exc}")
    # ponytail: no YouTube imports/calls/data here — evidence table has no expiry, so API content must not enter tool results
    gaps.append("youtube metrics excluded from saved evidence; run /youtube-analytics <thesis-id-or-slug> for the retention-safe view")
    if len(gaps) >= 3 and not evidence:
        result.update({"status": "unavailable", "soft": True, "error": "; ".join(gaps)})
    return result


def _get_macro_context(args: dict[str, object], model: str) -> dict[str, object]:
    try:
        from .google_data import datacommons as _dc
    except Exception as exc:
        return _google_import_error("datacommons", exc)
    try:
        return _google_soft(_dc.get_macro_context(
            _arg_str_list(args, "geos"), _arg_str_list(args, "variables"),
            start_date=_arg_str(args, "start_date"), end_date=_arg_str(args, "end_date"),
            limit=_arg_int(args, "limit", 100),
        ))
    except Exception as exc:
        logger.exception("get_macro_context failed")
        return {"error": f"Tool 'get_macro_context' failed: {exc}", "soft": True, "source": "datacommons"}


def _search_company_patents(args: dict[str, object], model: str) -> dict[str, object]:
    try:
        from .google_data import patents as _patents
    except Exception as exc:
        return _google_import_error("patents", exc)
    try:
        company_id = args["company_id"]
        if not isinstance(company_id, str) or not company_id:
            raise TypeError(f"company_id must be a non-empty string, got {type(company_id).__name__}")
        assignees_raw = args.get("assignees")
        assignees = [a for a in assignees_raw if isinstance(a, str)] if isinstance(assignees_raw, list) else None
        return _google_soft(_patents.search_company_patents(
            company_id, start_date=_arg_str(args, "start_date"), end_date=_arg_str(args, "end_date"),
            limit=_arg_int(args, "limit", 20), assignees=assignees,
        ))
    except Exception as exc:
        logger.exception("search_company_patents failed")
        return {"error": f"Tool 'search_company_patents' failed: {exc}", "soft": True, "source": "patents"}


def _wrap_list(identifier: object, records: object, key: str) -> dict[str, object]:
    """SEC list results: identifier echo, count, to_dict records, source."""
    if not isinstance(records, (list, tuple)):
        rec_list: list[object] = []
    else:
        rec_list = list(records)
    items: list[object] = []
    for r in rec_list:
        if hasattr(r, "to_dict"):
            to_dict = getattr(r, "to_dict")
            if callable(to_dict):
                items.append(to_dict())
            else:
                items.append(r)
        elif isinstance(r, dict):
            items.append(dict(r))
        elif isinstance(r, (list, tuple)):
            items.append(list(r))
        else:
            items.append(r)
    return {"subject": identifier, "count": len(items), key: items, "source": "SEC EDGAR"}




# Typed discovery metadata for progressive tool discovery.


@dataclass(frozen=True)
class ToolDiscovery:
    """Catalog metadata for one RESEARCH tool (generic lexical search source)."""

    domain: str
    family: str
    intent: str
    output_kind: str
    source: str
    entity_scope: str
    time_mode: str
    summary: str
    choose_when: tuple[str, ...]
    reject_when: tuple[str, ...] = ()
    conflicts_with: tuple[str, ...] = ()
    related_tools: tuple[str, ...] = ()
    prerequisites: tuple[str, ...] = ()
    direct_activation: bool = True


# Single source for domain descriptions (list_tool_domains + catalog generator share this).
DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "alternative": "Alternative and non-filing signals outside standard SEC and market feeds.",
    "analyst": "Analyst estimates and expectations for earnings, revenue, and price targets.",
    "events": "Material company events derived from 8-K and filing activity.",
    "sec": "SEC filing discovery, retrieval, and document reading via EDGAR.",
    "finra": "FINRA short interest, short volume, and threshold-securities data.",
    "fundamentals": "Reported numeric fundamentals such as EPS, dividends, and balance-sheet items.",
    "governance": "Proxy, meeting, vote, and board-compensation records.",
    "insider": "Insider transactions and planned sales from Forms 3/4/5 and 144.",
    "macro": "Macroeconomic context such as employment, inflation, and rates.",
    "market": "Market data such as index weights, option contracts, and trend evidence.",
    "offerings": "Financing history, offering terms, and dilution math.",
    "ownership": "Beneficial ownership stakes, holder changes, and relationship links.",
    "patents": "Patent records and innovation activity.",
    "thesis": "Thesis tracking, refinement, obligations, and operator notes.",
    "transactions": "Transaction status and mandate evaluation for deals.",
    "valuation": "Valuation multiples and financial-statement analysis.",
    "web": "General web search for facts outside structured financial sources.",
}


TOOL_DISCOVERY_REGISTRY: dict[str, ToolDiscovery] = {
    "describe_finra_dataset": ToolDiscovery(
        domain="finra",
        family="catalog",
        intent="inspect_finra_dataset_schema",
        output_kind="schema",
        source="finra",
        entity_scope="single_dataset",
        time_mode="current",
        summary="One FINRA dataset's fields, types, filter values, and supported methods.",
        choose_when=("Learning a named FINRA dataset's fields, types, filters, and coverage before querying.", "what is in.", "fields and coverage.",),
        reject_when=(
            "Not for finding which dataset covers a question (list_finra_datasets).",
            "Not for analyzed briefings.",
        ),
        conflicts_with=("list_finra_datasets",),
        related_tools=("list_finra_datasets", "query_finra", "get_finra_datapoints",),
        prerequisites=(),
    ),
    "diff_risk_factors": ToolDiscovery(
        domain="sec",
        family="filing-diff",
        intent="compare_risk_factors",
        output_kind="diff",
        source="sec",
        entity_scope="single_security",
        time_mode="latest",
        summary="Self-contained Risk Factors year-over-year diff for one ticker: what is new or changed.",
        choose_when=("What is new or changed in a company's risk disclosures for one ticker.",),
        reject_when=(
            "Do NOT use for full-filing diffs between accessions (diff_sec_filings).",
            "Do NOT use for disclosure search without change framing (search_sec_filings).",
            "Self-contained for one ticker; do NOT call list_sec_filings before or after.",
        ),
        conflicts_with=("diff_sec_filings", "search_sec_filings",),
        related_tools=("diff_sec_filings", "search_sec_filings",),
        prerequisites=(),
    ),
    "diff_sec_filings": ToolDiscovery(
        domain="sec",
        family="filing-diff",
        intent="compare_full_filings",
        output_kind="diff",
        source="sec",
        entity_scope="filing_pair_or_security",
        time_mode="latest_or_as_of",
        summary="Self-contained full-filing diff for one ticker or two accessions: amendment versus prior version.",
        choose_when=(
            "Comparing a ticker's latest amendment filing versus its predecessor filing.",
            "Comparing two known filing accessions for amendment or restatement changes.",
        ),
        reject_when=(
            "Do NOT use for risk-factor-only year-over-year diffs (diff_risk_factors).",
            "Do NOT call list_sec_filings first; ticker resolution is internal.",
            "Do NOT use for disclosure search without change framing (search_sec_filings).",
        ),
        conflicts_with=("diff_risk_factors", "search_sec_filings",),
        related_tools=("diff_risk_factors", "get_sec_filing",),
        prerequisites=(),
    ),
    "find_alternative_signals": ToolDiscovery(
        domain="alternative",
        family="signal-discovery",
        intent="discover_emerging_signals",
        output_kind="ranked_candidates",
        source="google_trends",
        entity_scope="market_wide",
        time_mode="latest_or_as_of",
        summary="Discovery scan for rising search-term and diffusion signals worth investigating.",
        choose_when=("Screening for emerging trend or attention signals across terms.",),
        reject_when=(
            "Not for evidence on one known trend.",
            "Not for a dated, geography-specific trend question.",
        ),
        conflicts_with=(),
        related_tools=("get_trend_evidence", "investigate_social_arbitrage_candidate",),
        prerequisites=(),
    ),
    "find_sec_entities": ToolDiscovery(
        domain="sec",
        family="entity-discovery",
        intent="resolve_sec_entity",
        output_kind="candidate_records",
        source="sec",
        entity_scope="entity_query",
        time_mode="current",
        summary="Resolve a company name, ticker, or CIK to verified SEC entity candidates with CIKs and tickers.",
        choose_when=("Starting from a company name when the exact ticker or CIK is not known.",),
        reject_when=("Unneeded when the exact ticker or CIK is already known.",),
        conflicts_with=(),
        related_tools=("list_sec_filings", "search_sec_filings",),
        prerequisites=(),
    ),
    "get_analyst_estimates": ToolDiscovery(
        domain="analyst",
        family="estimates",
        intent="retrieve_forward_consensus",
        output_kind="forecast_snapshot",
        source="yahoo_finance",
        entity_scope="single_security",
        time_mode="latest",
        summary="Forward sell-side consensus expectations: targets, ratings, forward EPS/revenue, revisions.",
        choose_when=("What analysts expect for one ticker: targets, consensus EPS, or estimate revisions.",),
        reject_when=(
            "Do NOT use for reported historical EPS (get_fundamentals).",
            "Do NOT use for cheap-vs-expensive multiples (get_valuation_metrics).",
        ),
        conflicts_with=("get_fundamentals", "get_valuation_metrics",),
        related_tools=("get_valuation_metrics", "get_fundamentals",),
        prerequisites=(),
    ),
    "get_beneficial_ownership": ToolDiscovery(
        domain="ownership",
        family="stakes",
        intent="retrieve_current_beneficial_owners",
        output_kind="current_snapshot",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Current 5%+ beneficial-ownership stakes (SC 13D/G): holder, shares, percent, voting powers.",
        choose_when=("Finding who owns more than 5% of a company.",),
        reject_when=(
            "Not for stake changes over time (get_ownership_changes).",
            "Not for relationship links in either direction (search_sec_relationships).",
            "Answer from these records; do not open filings or pull changes unless asked.",
        ),
        conflicts_with=("get_ownership_changes", "search_sec_relationships",),
        related_tools=("get_ownership_changes", "search_sec_relationships",),
        prerequisites=(),
    ),
    "get_dilution_profile": ToolDiscovery(
        domain="offerings",
        family="capital-raising",
        intent="calculate_dilution",
        output_kind="derived_analysis",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Deterministic dilution math for diluted shareholders: inputs, formula, and source accessions always shown.",
        choose_when=("Quantifying share-count impact from offerings, converts, or warrants.",),
        reject_when=("Not for offering-terms history (get_offering_history).",),
        conflicts_with=("get_offering_history",),
        related_tools=("get_offering_history",),
        prerequisites=(),
    ),
    "get_financial_statements": ToolDiscovery(
        domain="fundamentals",
        family="statements",
        intent="retrieve_financial_statement",
        output_kind="statement",
        source="sec",
        entity_scope="single_security",
        time_mode="latest",
        summary="Full parsed statements for one ticker: income statement, balance sheet, and cash flow.",
        choose_when=("Full financial statements rather than one numeric metric.",),
        reject_when=(
            "Do NOT use for a single metric like EPS (get_fundamentals).",
            "Do NOT use for a single XBRL fact (get_xbrl_facts).",
        ),
        conflicts_with=("get_fundamentals", "get_xbrl_facts",),
        related_tools=("get_fundamentals", "get_xbrl_facts",),
        prerequisites=(),
    ),
    "get_finra_datapoints": ToolDiscovery(
        domain="finra",
        family="short-interest",
        intent="retrieve_finra_records",
        output_kind="raw_records",
        source="finra",
        entity_scope="single_dataset",
        time_mode="date_range_or_latest",
        summary="Exact raw rows from any named FINRA dataset: only the requested fields, up to 25 rows.",
        choose_when=("Exact fields and values from a named FINRA dataset for an explicit data request.",),
        reject_when=(
            "Do NOT use for analyzed briefings or trends (query_finra).",
            "Do NOT use for one ticker's current short position (get_short_interest).",
        ),
        conflicts_with=("get_short_interest", "query_finra",),
        related_tools=("describe_finra_dataset", "query_finra", "list_finra_datasets",),
        prerequisites=(),
    ),
    "get_fundamentals": ToolDiscovery(
        domain="fundamentals",
        family="metrics",
        intent="retrieve_reported_metric",
        output_kind="metric_snapshot",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Single reported fundamental for one ticker: EPS, dividends, balance-sheet item, or shares outstanding.",
        choose_when=("One specific reported historical numeric fundamental for one ticker: basic/diluted/TTM EPS, dividends, or shares outstanding.",),
        reject_when=(
            "Do NOT use for full statements (get_financial_statements).",
            "Do NOT use for XBRL facts by concept (get_xbrl_facts).",
            "Do NOT use for cheap-vs-expensive multiples (get_valuation_metrics).",
            "Do NOT use for forward consensus (get_analyst_estimates).",
        ),
        conflicts_with=("get_analyst_estimates", "get_financial_statements", "get_valuation_metrics", "get_xbrl_facts",),
        related_tools=("get_xbrl_facts", "get_financial_statements", "get_valuation_metrics", "get_analyst_estimates",),
        prerequisites=(),
    ),
    "get_governance_events": ToolDiscovery(
        domain="governance",
        family="events",
        intent="retrieve_governance_events",
        output_kind="event_series",
        source="sec",
        entity_scope="single_security",
        time_mode="since_or_as_of",
        summary="Proxy and governance filing context (DEF 14A, meetings, votes) with retrieval pointers.",
        choose_when=("Finding shareholder-meeting, proxy-vote, or board-compensation records.",),
        reject_when=("Not for merger-deal status.",),
        conflicts_with=(),
        related_tools=("get_transaction_status",),
        prerequisites=(),
    ),
    "get_insider_activity": ToolDiscovery(
        domain="insider",
        family="trades",
        intent="retrieve_executed_insider_trades",
        output_kind="transaction_series",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Executed insider buys/sells for one ticker: actual purchases and sales from Forms 3/4/5.",
        choose_when=("One ticker's executed insider sales (buys/sells) by executives and directors (Forms 3/4/5).",),
        reject_when=("Do NOT use for planned but unexecuted Form 144 sales (get_planned_insider_sales).",),
        conflicts_with=("get_planned_insider_sales",),
        related_tools=("get_planned_insider_sales", "get_beneficial_ownership",),
        prerequisites=(),
    ),
    "get_macro_context": ToolDiscovery(
        domain="macro",
        family="statistics",
        intent="retrieve_macro_statistics",
        output_kind="statistic_series",
        source="datacommons",
        entity_scope="geography",
        time_mode="latest",
        summary="Macro statistics for a geography such as California: population (how many people live there), unemployment, inflation, GDP, rates.",
        choose_when=("Retrieving population, labor, inflation, GDP, or rate statistics for a geography.",),
        reject_when=("Not for company-specific facts.",),
        conflicts_with=(),
        related_tools=("search_web",),
        prerequisites=(),
    ),
    "get_material_events": ToolDiscovery(
        domain="events",
        family="company-events",
        intent="retrieve_recent_material_events",
        output_kind="event_series",
        source="sec",
        entity_scope="single_security",
        time_mode="since_or_as_of",
        summary="Deterministic 8-K-derived recent event feed with accession citations for what changed since a date.",
        choose_when=("Finding what changed recently: recent 8-K-derived events for a company since a date.",),
        reject_when=(
            "Does not cover market reaction or news commentary.",
            "Answer from the event feed; do not open filing documents unless the question needs document text.",
        ),
        conflicts_with=(),
        related_tools=("get_sec_document", "search_web", "get_recent_ownership_filings",),
        prerequisites=(),
    ),
    "get_obligations": ToolDiscovery(
        domain="fundamentals",
        family="obligations",
        intent="retrieve_future_obligations",
        output_kind="obligation_schedule",
        source="sec",
        entity_scope="single_security",
        time_mode="latest",
        summary="Future cash obligations from 10-K/10-Q notes: amounts, horizons, certainty language.",
        choose_when=("What a company is obligated to pay in the future for one ticker.",),
        reject_when=("Do NOT use for valuation multiples (get_valuation_metrics).",),
        conflicts_with=(),
        related_tools=("get_valuation_metrics", "get_financial_statements",),
        prerequisites=(),
    ),
    "get_offering_history": ToolDiscovery(
        domain="offerings",
        family="capital-raising",
        intent="retrieve_offering_history",
        output_kind="offering_series",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Offering history from S-1/S-3/424B filings: offering terms with source-registration links.",
        choose_when=("Reviewing past offerings, shelf registrations, or IPO terms for a ticker, including share-count impact context for converts or warrants.",),
        reject_when=("Not for dilution math (get_dilution_profile).",),
        conflicts_with=("get_dilution_profile",),
        related_tools=("get_dilution_profile",),
        prerequisites=(),
    ),
    "get_ownership_changes": ToolDiscovery(
        domain="ownership",
        family="stakes",
        intent="compare_ownership_stakes",
        output_kind="change_series",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Deterministic diffs between a holder's consecutive 13D/G filings: share and percent changes, changed stakes and positions.",
        choose_when=("Comparing consecutive 13D/G filings for changes in a holder's stake.",),
        reject_when=(
            "Not for the current snapshot of holders (get_beneficial_ownership).",
            "Not for relationship links in either direction (search_sec_relationships).",
        ),
        conflicts_with=("get_beneficial_ownership", "search_sec_relationships",),
        related_tools=("get_beneficial_ownership", "search_sec_relationships",),
        prerequisites=(),
    ),
    "get_planned_insider_sales": ToolDiscovery(
        domain="insider",
        family="trades",
        intent="retrieve_planned_insider_sales",
        output_kind="notice_series",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Planned Form 144 sale notices not yet executed: proposed insider sales for one ticker.",
        choose_when=("Proposed insider sales reported on Form 144 for one ticker.",),
        reject_when=("Do NOT use for completed insider trades (get_insider_activity).",),
        conflicts_with=("get_insider_activity",),
        related_tools=("get_insider_activity",),
        prerequisites=(),
    ),
    "get_recent_ownership_filings": ToolDiscovery(
        domain="events",
        family="ownership-filings",
        intent="retrieve_latest_ownership_filings",
        output_kind="filing_series",
        source="sec",
        entity_scope="market_wide",
        time_mode="latest",
        summary="Market-wide feed of the most recent SC 13D/13G filings from roughly the last 24 hours.",
        choose_when=("Finding the latest market-wide SC 13D/G filings when no ticker is given.",),
        reject_when=("Not for one company's current holders.",),
        conflicts_with=(),
        related_tools=("get_beneficial_ownership", "get_material_events",),
        prerequisites=(),
    ),
    "get_reg_sho_volume": ToolDiscovery(
        domain="finra",
        family="short-sale-volume",
        intent="daily_short_sale_volume",
        output_kind="daily_series",
        source="finra",
        entity_scope="single_security",
        time_mode="date_range_or_latest",
        summary="Self-contained daily short-sale volume by venue for one ticker: FINRA Reg SHO volume, rolling 12 months.",
        choose_when=("Daily short-sale volume or venue breakdowns for one ticker.",),
        reject_when=(
            "Do NOT use for biweekly short interest positions (get_short_interest).",
            "Do NOT call describe_finra_dataset or get_finra_datapoints; dataset and fields resolve internally.",
        ),
        conflicts_with=("get_short_interest",),
        related_tools=("get_short_interest", "query_finra",),
        prerequisites=(),
    ),
    "get_sec_document": ToolDiscovery(
        domain="sec",
        family="filing-catalog",
        intent="read_filing_document",
        output_kind="text_window",
        source="sec",
        entity_scope="single_document",
        time_mode="as_of",
        summary="Bounded text window of one filing document by accession number for targeted excerpt reading.",
        choose_when=("Reading a specific section such as MD&A or risk factors from a known accession.", "What the main document in a filing says; main-document text for a known accession.",),
        reject_when=(
            "Do NOT use for filing metadata by accession (get_sec_filing).",
            "Do NOT use to list a filing's documents or exhibits (list_sec_documents).",
            "Not for what-changed questions.",
        ),
        conflicts_with=("get_sec_filing", "list_sec_documents",),
        related_tools=("get_sec_filing", "get_material_events",),
        prerequisites=(),
    ),
    "get_sec_filing": ToolDiscovery(
        domain="sec",
        family="filing-catalog",
        intent="retrieve_filing_metadata",
        output_kind="record",
        source="sec",
        entity_scope="single_filing",
        time_mode="as_of",
        summary="One filing's record by accession number: filer, form, dates, primary document, source URL.",
        choose_when=("Fetching filing metadata after its accession number is known.",),
        reject_when=(
            "Does not discover filings; list or search for the accession when unknown.",
            "Do NOT use for document text windows (get_sec_document).",
            "Do NOT use to list a filing's documents or exhibits (list_sec_documents).",
        ),
        conflicts_with=("get_sec_document", "list_sec_documents",),
        related_tools=("list_sec_filings", "list_sec_documents",),
        prerequisites=(),
    ),
    "get_sec_search_coverage": ToolDiscovery(
        domain="sec",
        family="filing-search",
        intent="inspect_ingestion_coverage",
        output_kind="coverage_status",
        source="sec",
        entity_scope="dataset_partition",
        time_mode="current",
        summary="Persisted SEC ingestion coverage and backfill-job status for a form, source, or date partition.",
        choose_when=("Checking whether a form or date partition is covered or still queued before searching.",),
        reject_when=("Does not retrieve filing content.",),
        conflicts_with=(),
        related_tools=("search_sec_filings",),
        prerequisites=(),
    ),
    "get_short_interest": ToolDiscovery(
        domain="finra",
        family="short-interest",
        intent="current_reported_short_position",
        output_kind="current_snapshot",
        source="finra",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Biweekly short position for one ticker: FINRA short interest, days to cover, percent change.",
        choose_when=("One ticker's current short interest, short float, or days to cover.",),
        reject_when=(
            "Do NOT use for daily short-sale volume by venue (get_reg_sho_volume).",
            "Do NOT use for market-wide most-shorted screens (get_short_interest_leaderboard).",
            "Do NOT use for short-vs-shares context (get_short_pressure_profile).",
            "Do NOT use for exact source values (get_finra_datapoints).",
            "Do NOT use for analyzed briefings or trends over a dataset (query_finra).",
        ),
        conflicts_with=("get_finra_datapoints", "get_reg_sho_volume", "get_short_pressure_profile", "query_finra", "get_short_interest_leaderboard",),
        related_tools=("query_finra", "get_finra_datapoints", "get_reg_sho_volume", "get_short_pressure_profile", "get_short_interest_leaderboard",),
        prerequisites=(),
    ),
    "get_short_interest_leaderboard": ToolDiscovery(
        domain="finra",
        family="screens",
        intent="rank_short_interest",
        output_kind="leaderboard",
        source="finra_sec",
        entity_scope="market_wide",
        time_mode="latest_or_as_of",
        summary="Market-wide most-shorted screen: ranked stocks by short interest as a percent of SEC shares.",
        choose_when=("Screening which stocks are the most shorted across the market.",),
        reject_when=("Do NOT use for one ticker's short interest (get_short_interest).",),
        conflicts_with=("get_short_interest",),
        related_tools=("get_short_interest",),
        prerequisites=(),
    ),
    "get_short_pressure_profile": ToolDiscovery(
        domain="finra",
        family="short-interest",
        intent="assess_short_pressure",
        output_kind="derived_composite",
        source="finra_sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="Short pressure vs shares outstanding for one ticker: FINRA positioning plus SEC shares and ratio.",
        choose_when=("Short positioning relative to shares outstanding for one ticker.",),
        reject_when=(
            "Do NOT use for biweekly short position alone (get_short_interest).",
            "Do NOT use for daily short-sale volume (get_reg_sho_volume).",
        ),
        conflicts_with=("get_short_interest",),
        related_tools=("get_short_interest", "query_finra", "get_reg_sho_volume",),
        prerequisites=(),
    ),
    "get_sp500_weight": ToolDiscovery(
        domain="market",
        family="index-membership",
        intent="retrieve_sp500_weight",
        output_kind="current_snapshot",
        source="slickcharts",
        entity_scope="single_security",
        time_mode="latest",
        summary="A company's current weight and rank in the S&P 500 index from the constituent list.",
        choose_when=("Answering what percent of the S&P 500 a ticker represents.",),
        reject_when=("Do not use for valuation or short-positioning questions.",),
        conflicts_with=(),
        related_tools=("get_analyst_estimates",),
        prerequisites=(),
    ),
    "get_threshold_securities": ToolDiscovery(
        domain="finra",
        family="threshold-securities",
        intent="retrieve_threshold_status",
        output_kind="status_series",
        source="finra",
        entity_scope="single_security_or_market",
        time_mode="date_or_latest",
        summary="FINRA OTC Regulation SHO threshold securities, optionally filtered by ticker and date.",
        choose_when=("Checking whether securities appear on the Reg SHO threshold list.",),
        reject_when=("Not for ordinary short interest levels.",),
        conflicts_with=(),
        related_tools=("get_short_interest",),
        prerequisites=(),
    ),
    "get_transaction_status": ToolDiscovery(
        domain="transactions",
        family="deals",
        intent="retrieve_transaction_status",
        output_kind="event_series",
        source="sec",
        entity_scope="single_security",
        time_mode="latest_or_as_of",
        summary="M&A filing context: tender offers, 14D-9 recommendations, S-4s, and merger proxies.",
        choose_when=("Checking merger, acquisition, or tender-offer filing context for a ticker.",),
        reject_when=("Not for governance or proxy votes.",),
        conflicts_with=(),
        related_tools=("get_governance_events", "get_sec_document",),
        prerequisites=(),
    ),
    "get_trend_evidence": ToolDiscovery(
        domain="alternative",
        family="trend-evidence",
        intent="retrieve_known_trend_evidence",
        output_kind="evidence_series",
        source="google_trends",
        entity_scope="single_topic",
        time_mode="date_range",
        summary="Evidence for one known trend: search interest, rising queries, and geography.",
        choose_when=("Backing a known trend claim with dated, geography-specific search-interest evidence.",),
        reject_when=("Not for discovering new signals.",),
        conflicts_with=(),
        related_tools=("find_alternative_signals",),
        prerequisites=(),
    ),
    "get_valuation_metrics": ToolDiscovery(
        domain="valuation",
        family="multiples",
        intent="calculate_valuation_multiples",
        output_kind="derived_snapshot",
        source="yahoo_finance_sec",
        entity_scope="single_security",
        time_mode="latest",
        summary="Cheap-vs-expensive earnings multiples at live price: trailing plus forward P/E.",
        choose_when=("Whether a company is cheap or expensive on earnings multiples for one ticker.",),
        reject_when=(
            "Do NOT use for reported EPS alone (get_fundamentals).",
            "Do NOT use for forward consensus alone (get_analyst_estimates).",
        ),
        conflicts_with=("get_analyst_estimates", "get_fundamentals",),
        related_tools=("get_analyst_estimates", "get_obligations", "get_fundamentals",),
        prerequisites=(),
    ),
    "get_xbrl_facts": ToolDiscovery(
        domain="fundamentals",
        family="metrics",
        intent="retrieve_xbrl_concept",
        output_kind="fact_records",
        source="sec",
        entity_scope="single_security",
        time_mode="latest",
        summary="Single XBRL-tagged fact by concept name: revenue, net income, cash, debt, or equity.",
        choose_when=("One tagged line-item value by exact XBRL concept name.",),
        reject_when=(
            "Do NOT use for EPS (get_fundamentals).",
            "Do NOT use for full statements (get_financial_statements).",
        ),
        conflicts_with=("get_financial_statements", "get_fundamentals",),
        related_tools=("get_fundamentals", "get_financial_statements",),
        prerequisites=(),
    ),
    "investigate_social_arbitrage_candidate": ToolDiscovery(
        domain="alternative",
        family="social-arbitrage",
        intent="assess_attention_demand_gap",
        output_kind="derived_analysis",
        source="google_trends",
        entity_scope="single_topic",
        time_mode="latest_or_as_of",
        summary="Enrichment of one social-arbitrage candidate with corroboration and exposure gap. Social signals vetting.",
        choose_when=("Testing whether online attention around one candidate corresponds to real demand.",),
        reject_when=("Not for broad signal discovery.",),
        conflicts_with=(),
        related_tools=("find_alternative_signals", "get_trend_evidence",),
        prerequisites=(),
    ),
    "list_finra_datasets": ToolDiscovery(
        domain="finra",
        family="catalog",
        intent="discover_finra_dataset",
        output_kind="catalog",
        source="finra",
        entity_scope="dataset_catalog",
        time_mode="current",
        summary="Catalog of public FINRA datasets with canonical ids, groups, and ticker/date support.",
        choose_when=("Finding which FINRA dataset covers a question before querying.",),
        reject_when=("Does not return dataset fields or schemas (describe_finra_dataset).",),
        conflicts_with=("describe_finra_dataset",),
        related_tools=("describe_finra_dataset",),
        prerequisites=(),
    ),
    "list_sec_documents": ToolDiscovery(
        domain="sec",
        family="filing-catalog",
        intent="list_filing_documents",
        output_kind="record_series",
        source="sec",
        entity_scope="single_filing",
        time_mode="as_of",
        summary="Index of documents and exhibits attached to one filing, looked up by accession number.",
        choose_when=("Listing documents and exhibits attached to a known filing accession.",),
        reject_when=(
            "Do NOT use for document text windows (get_sec_document).",
            "Do NOT use for filing metadata records (get_sec_filing).",
        ),
        conflicts_with=("get_sec_document", "get_sec_filing",),
        related_tools=("get_sec_filing", "get_sec_document", "list_sec_filings",),
        prerequisites=(),
    ),
    "list_sec_filings": ToolDiscovery(
        domain="sec",
        family="filing-catalog",
        intent="list_entity_filings",
        output_kind="filing_series",
        source="sec",
        entity_scope="single_entity",
        time_mode="date_range_or_as_of",
        summary="List EDGAR filings for an exact ticker or CIK, filterable by form and date range.",
        choose_when=("Listing what a company filed lately; recent filings for an exact ticker or CIK, optionally filtered by form or date.",),
        reject_when=(
            "Do not guess an identifier from a bare company name; use the exact ticker when known, otherwise resolve the company's exact identifier first.",
            "Do NOT use for disclosure search without known identifier (search_sec_filings).",
        ),
        conflicts_with=("search_sec_filings",),
        related_tools=("get_sec_filing", "search_sec_filings", "find_sec_entities",),
        prerequisites=(),
    ),
    "query_finra": ToolDiscovery(
        domain="finra",
        family="short-interest",
        intent="analyze_historical_finra_records",
        output_kind="distribution_or_trend",
        source="finra",
        entity_scope="single_dataset",
        time_mode="date_range",
        summary="Analyzed FINRA briefing with trends and metrics over any named dataset, no raw rows.",
        choose_when=("Analyzing a FINRA dataset's coverage, distribution, and changes over time.",),
        reject_when=(
            "Do NOT use for exact source values (get_finra_datapoints).",
            "Do NOT use for one ticker's current short position (get_short_interest).",
        ),
        conflicts_with=("get_finra_datapoints", "get_short_interest",),
        related_tools=("describe_finra_dataset", "get_finra_datapoints", "get_short_interest", "list_finra_datasets",),
        prerequisites=(),
    ),
    "search_company_patents": ToolDiscovery(
        domain="patents",
        family="search",
        intent="search_company_patents",
        output_kind="patent_records",
        source="google_patents",
        entity_scope="single_company",
        time_mode="date_range_or_latest",
        summary="Company patent search: publications, assignees, counts, and classifications.",
        choose_when=("Finding patents a company filed or patented lately, with publication counts and classifications.",),
        reject_when=(
            "Not for financial or filing questions.",
            "Answer from patent records.",
        ),
        conflicts_with=(),
        related_tools=(),
        prerequisites=(),
    ),
    "search_sec_filings": ToolDiscovery(
        domain="sec",
        family="filing-search",
        intent="search_filing_text",
        output_kind="search_results",
        source="sec",
        entity_scope="multi_entity",
        time_mode="date_range_or_as_of",
        summary="General EDGAR full-text disclosure search across entity, EFTS, and 10-K/10-Q routes, with mentions.",
        choose_when=("Searching disclosed filing text, risk-factor language, and mentions when the accession number is unknown.", "SEC filings or filing full-text search when accession is unknown.",),
        reject_when=(
            "Not a filing lister for a known ticker (list_sec_filings).",
            "Do NOT use for year-over-year risk-factor changes (diff_risk_factors).",
            "Do NOT use for full-filing diffs between accessions (diff_sec_filings).",
        ),
        conflicts_with=("diff_risk_factors", "diff_sec_filings", "list_sec_filings",),
        related_tools=("list_sec_filings", "get_sec_filing", "find_sec_entities", "diff_risk_factors",),
        prerequisites=(),
    ),
    "search_sec_relationships": ToolDiscovery(
        domain="ownership",
        family="stakes",
        intent="search_ownership_relationships",
        output_kind="relationship_records",
        source="sec",
        entity_scope="single_entity",
        time_mode="latest_or_as_of",
        summary="Ownership and transaction relationship links for an entity: 13D/G owners, 13F holdings, deal links.",
        choose_when=("Mapping who owns, holds, or transacts with an entity in either direction.",),
        reject_when=(
            "Not for current 5%+ stake sizes (get_beneficial_ownership).",
            "Not for consecutive-filing stake diffs (get_ownership_changes).",
        ),
        conflicts_with=("get_beneficial_ownership", "get_ownership_changes",),
        related_tools=("get_beneficial_ownership", "get_ownership_changes",),
    ),
    "search_web": ToolDiscovery(
        domain="web",
        family="search",
        intent="search_external_web",
        output_kind="search_results",
        source="exa",
        entity_scope="open_query",
        time_mode="latest",
        summary="External web news and commentary for price moves, headlines, and industry developments: what outside commentators and people are saying, business risks.",
        choose_when=("Finding recent news, announcements, catalysts, market reaction, why a stock moved/rose/fell, or recent commentary outside structured sources.",),
        reject_when=("Not for FINRA short data.",),
        conflicts_with=(),
        related_tools=("get_material_events", "query_finra",),
        prerequisites=(),
    ),
    "thesis_create": ToolDiscovery(
        domain="thesis",
        family="lifecycle",
        intent="create_thesis",
        output_kind="governed_action",
        source="local",
        entity_scope="single_thesis",
        time_mode="current",
        summary="Start a new investment thesis proposal with scope, claims, and open questions.",
        choose_when=("Creating a new investment thesis to track and test.",),
        reject_when=("Not for reading an existing thesis.",),
        conflicts_with=(),
        related_tools=("thesis_show", "thesis_refine",),
        prerequisites=(),
        direct_activation=False,
    ),
    "thesis_journal": ToolDiscovery(
        domain="thesis",
        family="lifecycle",
        intent="append_thesis_journal",
        output_kind="governed_action",
        source="local",
        entity_scope="single_thesis",
        time_mode="current",
        summary="Append an operator note or journal entry to a thesis log. Pass the thesis ID as thesis:<uuid>.",
        choose_when=("Appending an operator note about ongoing monitoring without creating or changing a watch rule.",),
        reject_when=("Not for revising claims.", "Not for setting alerts (thesis_watch).",),
        conflicts_with=("thesis_watch",),
        prerequisites=(),
        direct_activation=False,
    ),
    "thesis_refine": ToolDiscovery(
        domain="thesis",
        family="lifecycle",
        intent="refine_thesis",
        output_kind="governed_action",
        source="local",
        entity_scope="single_thesis",
        time_mode="current",
        summary="Update a thesis with clarifications and deltas. Pass the thesis ID as thesis:<uuid>.",
        choose_when=("Revising a thesis after new evidence or feedback.",),
        reject_when=("Not for routine notes.",),
        conflicts_with=(),
        related_tools=("thesis_show", "thesis_journal",),
        prerequisites=(),
        direct_activation=False,
    ),
    "thesis_show": ToolDiscovery(
        domain="thesis",
        family="lifecycle",
        intent="retrieve_thesis",
        output_kind="current_snapshot",
        source="local",
        entity_scope="single_thesis",
        time_mode="latest_or_as_of",
        summary="Read a thesis: its status, assessment, and current state. Pass the thesis ID as thesis:<uuid>.",
        choose_when=("Checking a thesis and its current assessment.",),
        reject_when=("Not for changing a thesis.",),
        conflicts_with=(),
        related_tools=("thesis_create", "thesis_refine", "thesis_journal",),
        prerequisites=(),
    ),
    "thesis_watch": ToolDiscovery(
        domain="thesis",
        family="lifecycle",
        intent="list_or_add_thesis_watch",
        output_kind="governed_action",
        source="local",
        entity_scope="single_thesis",
        time_mode="current",
        summary="List existing watch rules, or add a validated monitoring rule that alerts when a thesis condition triggers.",
        choose_when=("Listing what is watched for a thesis, or setting an alert on an invalidator or trigger; what am I watching for, watch rules.",),
        reject_when=("Not for logging notes (thesis_journal).",),
        conflicts_with=("thesis_journal",),
        related_tools=("thesis_show",),
        prerequisites=(),
        direct_activation=False,
    ),
}


_DISCOVERY_TEXT_LIMIT = 200


_SLUG_KEBAB_RE = __import__("re").compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_SLUG_SNAKE_RE = __import__("re").compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def validate_tool_discovery_registry() -> dict[str, ToolDiscovery]:
    """Fail loudly on registry drift; returns the registry for verify scripts."""
    known_domains = set(DOMAIN_DESCRIPTIONS)
    for name in sorted(TOOL_DISCOVERY_REGISTRY):
        meta = TOOL_DISCOVERY_REGISTRY[name]
        if meta.domain not in known_domains:
            raise AssertionError(f"tool discovery {name!r} has unknown domain {meta.domain!r}")
        for field_name in ("domain", "family", "source"):
            value = getattr(meta, field_name)
            if not value or not _SLUG_KEBAB_RE.match(value):
                raise AssertionError(f"tool discovery {name!r} has non-slug {field_name} {value!r}")
        for field_name in ("intent", "output_kind", "entity_scope", "time_mode"):
            value = getattr(meta, field_name)
            if not value or not _SLUG_SNAKE_RE.match(value):
                raise AssertionError(f"tool discovery {name!r} has non-snake {field_name} {value!r}")
        if not meta.summary or len(meta.summary) > _DISCOVERY_TEXT_LIMIT:
            raise AssertionError(f"tool discovery {name!r} has empty/overlong summary")
        if not meta.choose_when or not meta.reject_when:
            raise AssertionError(f"tool discovery {name!r} needs >=1 choose_when and >=1 reject_when")
        bullets = (*meta.choose_when, *meta.reject_when, *meta.related_tools, *meta.prerequisites, *meta.conflicts_with)
        for bullet in bullets:
            if not bullet or len(bullet) > _DISCOVERY_TEXT_LIMIT:
                raise AssertionError(f"tool discovery {name!r} has empty/overlong bullet {bullet!r}")
        for ref in (*meta.related_tools, *meta.prerequisites):
            if ref not in TOOL_DISCOVERY_REGISTRY:
                raise AssertionError(f"tool discovery {name!r} references unknown tool {ref!r}")
        if name in meta.conflicts_with:
            raise AssertionError(f"tool discovery {name!r} self-conflicts with {name!r}")
        if len(set(meta.conflicts_with)) != len(meta.conflicts_with):
            raise AssertionError(f"tool discovery {name!r} has duplicate conflicts_with entry")
        for peer in meta.conflicts_with:
            if peer not in TOOL_DISCOVERY_REGISTRY:
                raise AssertionError(f"tool discovery {name!r} conflicts with unknown tool {peer!r}")
            peer_meta = TOOL_DISCOVERY_REGISTRY[peer]
            if name not in peer_meta.conflicts_with:
                raise AssertionError(f"tool discovery {name!r} conflicts with {peer!r} but {peer!r} does not reciprocate {name!r}")
            blob = " ".join(meta.reject_when)
            if peer not in blob:
                raise AssertionError(f"tool discovery {name!r} conflicts with {peer!r} but never names {peer!r} in reject_when")
    raw_caps = globals().get("TOOL_CAPABILITIES")
    if isinstance(raw_caps, dict):
        uncovered = sorted(
            str(tool)
            for tool, cap in raw_caps.items()
            if cap is Capability.RESEARCH and str(tool) not in {"search_tools", "list_tool_domains", "describe_tool", "browse_tools", "call_tool"} and str(tool) not in TOOL_DISCOVERY_REGISTRY
        )
        if uncovered:
            raise AssertionError(f"tool discovery registry missing RESEARCH tools: {uncovered}")
        for name, meta in TOOL_DISCOVERY_REGISTRY.items():
            if meta.direct_activation and raw_caps.get(name) is not Capability.RESEARCH:
                raise AssertionError(f"tool discovery {name!r} is direct-activatable but not Capability.RESEARCH")
    return TOOL_DISCOVERY_REGISTRY


# Content-derived registry version for observability records (schemas + routing metadata).
_TOOL_DISCOVERY_FINGERPRINT = json.dumps(
    {
        name: [
            meta.domain, meta.family, meta.intent, meta.output_kind, meta.source,
            meta.entity_scope, meta.time_mode, meta.summary,
            list(meta.choose_when), list(meta.reject_when),
            sorted(meta.conflicts_with), sorted(meta.related_tools),
            sorted(meta.prerequisites), meta.direct_activation,
        ]
        for name, meta in sorted(TOOL_DISCOVERY_REGISTRY.items())
    },
    sort_keys=True,
)
TOOL_REGISTRY_VERSION = hashlib.sha256((json.dumps(TOOLS, sort_keys=True) + _TOOL_DISCOVERY_FINGERPRINT).encode()).hexdigest()[:12]


validate_tool_discovery_registry()


def dynamically_activatable_tool_names() -> list[str]:
    """Sorted canonical names eligible for direct small-model activation."""
    return sorted(name for name, meta in validate_tool_discovery_registry().items() if meta.direct_activation)


def _routing_card(name: str) -> dict[str, object]:
    """Compact routing card: registry metadata plus canonical arg lists, never full parameters."""
    meta = TOOL_DISCOVERY_REGISTRY[name]
    _, required, optional = _canonical_tool_schema(name)
    return {
        "name": name,
        "domain": meta.domain,
        "family": meta.family,
        "summary": meta.summary,
        "intent": meta.intent,
        "output_kind": meta.output_kind,
        "source": meta.source,
        "entity_scope": meta.entity_scope,
        "time_mode": meta.time_mode,
        "choose_when": list(meta.choose_when),
        "reject_when": list(meta.reject_when),
        "required": required,
        "optional": optional,
    }


def build_prerequisite_graph_from_tool_metadata() -> dict[str, frozenset[str]]:
    """Direct prerequisite edges from the registry (no transitive expansion)."""
    return {name: frozenset(meta.prerequisites) for name, meta in TOOL_DISCOVERY_REGISTRY.items() if meta.prerequisites}


def _normalize_discovery_text(value: str) -> list[str]:
    """Lowercase, de-punctuate, and de-pluralize discovery text into tokens."""
    lowered = value.lower().replace("p/e", "pe").replace("13-d", "13d")
    cleaned = "".join(c if c.isalnum() or c == " " else " " for c in lowered)
    collapsed = " ".join(cleaned.split()).replace("10 k", "10k")
    tokens: list[str] = []
    for token in collapsed.split():
        if len(token) > 3:
            if token.endswith("ies"):
                token = token[:-3] + "y"
            elif token.endswith("s"):
                token = token[:-1]
        tokens.append(token)
    return tokens


# Generic stopwords for catalog search (standard filler, never domain/intent terms).
_DISCOVERY_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "in", "is", "it", "its", "me",
    "my", "of", "on", "or", "that", "the", "this", "to", "was", "were", "what",
    "when", "which", "who", "with", "show", "tell", "give",
})


def _discovery_keywords(value: str) -> set[str]:
    """Normalized discovery tokens minus stopwords and single characters."""
    return {t for t in _normalize_discovery_text(value) if len(t) > 1 and t not in _DISCOVERY_STOPWORDS}


def _ambiguity_groups(ranked_names: list[str]) -> tuple[bool, list[dict[str, object]]]:
    """Connected components of conflicts_with among returned matches."""
    if len(ranked_names) < 2:
        return False, []
    index = {name: i for i, name in enumerate(ranked_names)}
    parent: dict[str, str] = {n: n for n in ranked_names}
    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra
    present = set(ranked_names)
    for name in ranked_names:
        for peer in TOOL_DISCOVERY_REGISTRY[name].conflicts_with:
            if peer in present:
                _union(name, peer)
    comps: dict[str, list[str]] = {}
    for name in ranked_names:
        comps.setdefault(_find(name), []).append(name)
    groups: list[dict[str, object]] = []
    for comp in comps.values():
        if len(comp) < 2:
            continue
        # Only a real ambiguity group when at least one conflict edge is internal.
        if not any(
            any(peer in comp for peer in TOOL_DISCOVERY_REGISTRY[n].conflicts_with)
            for n in comp
        ):
            continue
        def _order_key(n: str) -> int:
            return index[n]
        ordered = sorted(comp, key=_order_key)
        paths = sorted({f"{TOOL_DISCOVERY_REGISTRY[n].domain}/{TOOL_DISCOVERY_REGISTRY[n].family}" for n in ordered})
        bits = [f"{n} \u2014 {TOOL_DISCOVERY_REGISTRY[n].choose_when[0]}" if TOOL_DISCOVERY_REGISTRY[n].choose_when else n for n in ordered]
        groups.append({
            "candidates": ordered,
            "paths": paths,
            "distinguishing_question": "Which outcome do you need: " + "; ".join(bits) + "?",
        })
    def _group_key(g: dict[str, object]) -> str:
        cands = g.get("candidates")
        if isinstance(cands, list) and cands and isinstance(cands[0], str):
            return str(cands[0])
        return ""
    groups.sort(key=_group_key)
    return (len(groups) > 0), groups


def _search_tools(args: dict[str, object], model: str) -> dict[str, object]:
    """Generic lexical ranking over TOOL_DISCOVERY_REGISTRY fields only.

    Signals (small generic weights, no per-intent boosts): exact tool-name
    match (10) > exact phrase in summary/choose_when (5) > token overlap over
    name/domain/family/intent/output_kind/summary/choose_when/reject_when/related names
    (1 per token) > domain-name overlap (1). Ties break alphabetically for
    determinism. A relative-noise margin keeps only hits within 4 points of
    the best score. Returns compact routing cards only: the top 3 exact.
    Confusable siblings surface only via ambiguity groups over conflicts_with.
    """
    del model
    query = str(args.get("query") or "")
    domain = str(args.get("domain") or "").strip().lower() or None
    if not query.strip():
        return {"matches": [], "count": 0, "ambiguous": False, "ambiguity_groups": []}
    query_norm = " ".join(_normalize_discovery_text(query))
    query_tokens = _discovery_keywords(query)
    scored: list[tuple[int, str]] = []
    for name, meta in TOOL_DISCOVERY_REGISTRY.items():
        if domain and meta.domain != domain:
            continue
        score = 0
        if query_norm and query_norm == " ".join(_normalize_discovery_text(name.replace("_", " "))):
            score += 10
        for text in (meta.summary, *meta.choose_when):
            phrase = " ".join(_normalize_discovery_text(text))
            if query_norm and phrase and (query_norm in phrase or phrase in query_norm):
                score += 5
                break
        field_tokens = _discovery_keywords(
            " ".join((
                name.replace("_", " "), meta.domain, meta.family.replace("-", " "),
                meta.intent.replace("_", " "), meta.output_kind.replace("_", " "),
                meta.summary, " ".join(meta.choose_when), " ".join(meta.reject_when),
                " ".join(meta.related_tools).replace("_", " "),
            ))
        )
        score += len(query_tokens & field_tokens)
        if query_tokens and set(_normalize_discovery_text(meta.domain)) <= query_tokens:
            score += 1
        if score > 0:
            scored.append((score, name))
    # Relative noise gate: keep hits within 4 points of the best; wide-open
    # queries with no strong match keep everything.
    if scored:
        best = max(score for score, _ in scored)
        scored = [(score, name) for score, name in scored if score >= best - 4]
    def _rank_key(hit: tuple[int, str]) -> tuple[int, str]:
        return (-hit[0], hit[1])
    scored.sort(key=_rank_key)
    top = scored[:3]
    ranked_names = [name for _, name in top]
    ranked = [_routing_card(name) for name in ranked_names]
    ambiguous, groups = _ambiguity_groups(ranked_names)
    return {"matches": ranked, "count": len(ranked), "ambiguous": ambiguous, "ambiguity_groups": groups}


def _list_tool_domains(args: dict[str, object], model: str) -> dict[str, object]:
    """Sorted domain catalog from the shared DOMAIN_DESCRIPTIONS map."""
    del args
    del model
    return {
        "domains": [
            {"name": name, "description": DOMAIN_DESCRIPTIONS[name]}
            for name in sorted(DOMAIN_DESCRIPTIONS)
        ],
    }


def _describe_one(name: str) -> dict[str, object]:
    """Full metadata for one named tool from the registry plus its canonical schema."""
    meta = TOOL_DISCOVERY_REGISTRY.get(name)
    if meta is None:
        return {"error": "unknown_tool", "name": name}
    required: list[str] = []
    optional: list[str] = []
    for tool in TOOLS:
        fn = _tool_function(tool)
        if fn.get("name") != name:
            continue
        raw_params = fn.get("parameters")
        params: dict[str, object] = {str(k): v for k, v in raw_params.items()} if isinstance(raw_params, dict) else {}
        raw_required = params.get("required")
        required = [str(k) for k in raw_required] if isinstance(raw_required, list) else []
        raw_props = params.get("properties")
        props: dict[str, object] = {str(k): v for k, v in raw_props.items()} if isinstance(raw_props, dict) else {}
        optional = sorted(key for key in props if key not in required)
        break
    return {
        "name": name,
        "domain": meta.domain,
        "family": meta.family,
        "summary": meta.summary,
        "intent": meta.intent,
        "output_kind": meta.output_kind,
        "source": meta.source,
        "entity_scope": meta.entity_scope,
        "time_mode": meta.time_mode,
        "choose_when": list(meta.choose_when),
        "reject_when": list(meta.reject_when),
        "conflicts_with": list(meta.conflicts_with),
        "related_tools": list(meta.related_tools),
        "prerequisites": list(meta.prerequisites),
        "required_arguments": required,
        "optional_arguments": optional,
    }


def _describe_tool(args: dict[str, object], model: str) -> dict[str, object]:
    """Full metadata for one named tool, or several tools in order with `names`."""
    del model
    raw = args.get("names")
    if isinstance(raw, list):
        return {"tools": [_describe_one(str(name)) for name in raw]}
    name = args.get("name") or ""
    if isinstance(name, str) and name.strip().startswith("["):
        # ponytail: agents serialize the names array into the name string;
        # coerce instead of failing their describe-then-call flow.
        try:
            parsed = json.loads(name)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list) and parsed:
            return {"tools": [_describe_one(str(item)) for item in parsed]}
    return _describe_one(str(name))


def _browse_key(nm: str) -> tuple[str, str, str]:
    """Sort key for the full catalog: (domain, family, name)."""
    meta = TOOL_DISCOVERY_REGISTRY[nm]
    return (meta.domain, meta.family, nm)


def _browse_tools(args: dict[str, object], model: str) -> dict[str, object]:
    """Hierarchical catalog: root domains, domain families, family tools + contrast, or one tool."""
    del model
    raw_name = args.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
    raw_domain = args.get("domain")
    domain = raw_domain.strip().lower() if isinstance(raw_domain, str) and raw_domain.strip() else None
    raw_family = args.get("family")
    family = raw_family.strip().lower() if isinstance(raw_family, str) and raw_family.strip() else None
    if name and (domain or family):
        return {"error": "ambiguous_browse", "hint": "call browse_tools with either name or domain/family, not both"}
    if name:
        meta = TOOL_DISCOVERY_REGISTRY.get(name)
        if meta is None:
            return {"error": "unknown_tool", "name": name}
        info = _describe_one(name)
        params, _, _ = _canonical_tool_schema(name)
        return {**info, "parameters": params}
    if family and not domain:
        return {"error": "family_requires_domain", "hint": "call browse_tools with domain and family"}
    if domain and domain not in DOMAIN_DESCRIPTIONS:
        return {"error": "unknown_domain", "domains": sorted(DOMAIN_DESCRIPTIONS)}
    if domain and family:
        names = sorted(
            (n for n, m in TOOL_DISCOVERY_REGISTRY.items() if m.domain == domain and m.family == family),
            key=_browse_key,
        )
        if not names:
            return {"error": "unknown_family", "domain": domain, "families": sorted({m.family for n, m in TOOL_DISCOVERY_REGISTRY.items() if m.domain == domain})}
        tools = [
            {
                "name": n,
                "domain": TOOL_DISCOVERY_REGISTRY[n].domain,
                "family": TOOL_DISCOVERY_REGISTRY[n].family,
                "summary": TOOL_DISCOVERY_REGISTRY[n].summary,
                "intent": TOOL_DISCOVERY_REGISTRY[n].intent,
                "output_kind": TOOL_DISCOVERY_REGISTRY[n].output_kind,
            }
            for n in names
        ]
        contrast = [
            {
                "tool": n,
                "use_it_for": TOOL_DISCOVERY_REGISTRY[n].choose_when[0] if TOOL_DISCOVERY_REGISTRY[n].choose_when else "",
                "do_not_use_it_for": " ".join(TOOL_DISCOVERY_REGISTRY[n].reject_when),
            }
            for n in names
        ]
        return {
            "path": f"/{domain}/{family}",
            "domain": domain,
            "family": family,
            "tools": tools,
            "count": len(tools),
            "contrast_table": contrast,
        }
    if domain:
        fams: dict[str, list[str]] = {}
        for n, m in TOOL_DISCOVERY_REGISTRY.items():
            if m.domain == domain:
                fams.setdefault(m.family, []).append(n)
        families = [{"name": f, "path": f"/{domain}/{f}", "tool_count": len(v)} for f, v in sorted(fams.items())]
        return {"path": f"/{domain}", "domain": domain, "families": families}
    domains = [{"name": d, "path": f"/{d}", "description": DOMAIN_DESCRIPTIONS[d]} for d in sorted(DOMAIN_DESCRIPTIONS)]
    return {"path": "/", "domains": domains}



def _search_envelope(result: SECSearchResult) -> dict[str, object]:
    """SECSearchResult -> model packet: roles, ledger, PIT, jobs, evidence."""
    data = result.to_dict()
    raw_cov = data.get("coverage")
    cov: dict[str, object] = {str(k): v for k, v in raw_cov.items()} if isinstance(raw_cov, dict) else {}
    raw_attempts = data.get("attempts")
    attempts: list[dict[str, object]] = [{str(k): v for k, v in a.items()} for a in raw_attempts if isinstance(a, dict)] if isinstance(raw_attempts, (list, tuple)) else []
    raw_hits = data.get("text_hits")
    hits: list[dict[str, object]] = []
    if isinstance(raw_hits, (list, tuple)):
        for hit in raw_hits:
            if isinstance(hit, dict):
                base: dict[str, object] = {str(k): v for k, v in hit.items()}
                base["match_role"] = "mention"
                base["subject_cik"] = None
                base["subject_name"] = None
                hits.append(base)
    bases: list[object] = []
    for a in attempts:
        raw_basis = a.get("pit_basis")
        if raw_basis is not None:
            bases.append(raw_basis)
    raw_req = data.get("request")
    request: dict[str, object] = {str(k): v for k, v in raw_req.items()} if isinstance(raw_req, dict) else {}
    return {
        "subject": request.get("query") or request.get("company_name"),
        "query": request.get("query"),
        "search_id": data.get("search_id"),
        "request": request,
        "count": len(hits),
        "entities": data.get("entities"),
        "filings": data.get("filings"),
        "parties": data.get("relationships"),
        "hits": hits,
        "coverage": cov,
        "attempts": attempts,
        "counts": {
            "results_reported": cov.get("results_reported", 0),
            "results_retrieved": cov.get("results_retrieved", 0),
            "pages": cov.get("pages", 0),
            "entities": len(data["entities"]) if isinstance(data.get("entities"), (list, tuple)) else 0,
            "filings": len(data["filings"]) if isinstance(data.get("filings"), (list, tuple)) else 0,
            "documents": len(data["documents"]) if isinstance(data.get("documents"), (list, tuple)) else 0,
        },
        "pit_basis": max(set(str(b) for b in bases), key=_bases_count_key(bases)) if bases else None,
        "warnings": data.get("warnings"),
        "errors": data.get("errors"),
        "backfill_jobs": list(cov["pending_backfill_jobs"]) if isinstance(cov.get("pending_backfill_jobs"), (list, tuple)) else [],
        "evidence_packet_ids": data.get("evidence_packet_ids"),
        "source": "SEC EDGAR",
    }


def _find_sec_entities(args: dict[str, object]) -> dict[str, object]:
    """Entity discovery -> envelope with candidate verification statuses."""
    exhaustive = bool(args.get("exhaustive", False))
    if args.get("limit") is not None:
        max_results = _optional_int(args.get("limit"))
    else:
        max_results = None if exhaustive else 20
    return _search_envelope(sec.find_sec_entities(
        str(args["query"]), as_of=_str_or_none(args.get("as_of")),
        exhaustive=exhaustive, max_results=max_results,
        data_root=get_data_root(),
    ))


def _sec_search_result(args: dict[str, object]) -> dict[str, object]:
    """Bounded discovery search -> envelope with jobs + evidence IDs."""
    if not any(args.get(key) for key in (
            "query", "ticker", "cik", "company_name", "person_name",
            "domain", "accession_no", "security_identifier")):
        raise ValueError(
            "search_sec_filings needs one of: query, ticker, cik, "
            "company_name, person_name, domain, accession_no, "
            "security_identifier")
    exhaustive = bool(args.get("exhaustive", False))
    if args.get("limit") is not None:
        max_results = _optional_int(args.get("limit"))
    else:
        max_results = None if exhaustive else 20
    raw_forms = args.get("forms")
    if isinstance(raw_forms, str):
        forms: tuple[str, ...] | None = (raw_forms,)
    elif isinstance(raw_forms, (list, tuple)):
        forms = tuple(str(x) for x in raw_forms)
    else:
        forms = None
    request = sec.SECSearchRequest(
        query=_str_or_none(args.get("query")), ticker=_str_or_none(args.get("ticker")), cik=_str_or_none(args.get("cik")),
        company_name=_str_or_none(args.get("company_name")),
        person_name=_str_or_none(args.get("person_name")), domain=_str_or_none(args.get("domain")),
        accession_no=_str_or_none(args.get("accession_no")),
        security_identifier=_str_or_none(args.get("security_identifier")),
        forms=forms,
        start_date=_str_or_none(args.get("start_date")), end_date=_str_or_none(args.get("end_date")),
        as_of=_str_or_none(args.get("as_of")),
        exhaustive=exhaustive,
        max_results=max_results,
    )
    return _search_envelope(
        sec.SECDiscoveryService(data_root=get_data_root()).search(request))


def _get_sec_document(args: dict[str, object], model: str) -> dict[str, object]:
    """Archive-first document read; model callers always get a bounded window."""
    del model
    raw_offset = args.get("offset", 0)
    if isinstance(raw_offset, bool):
        offset = int(raw_offset)
    elif isinstance(raw_offset, int):
        offset = raw_offset
    elif isinstance(raw_offset, float):
        offset = int(raw_offset)
    elif isinstance(raw_offset, str):
        offset = int(raw_offset.strip()) if raw_offset.strip() else 0
    elif raw_offset is None:
        offset = 0
    else:
        offset = int(str(raw_offset))
    raw_max = args.get("max_chars", 12_000)
    if raw_max is None:
        max_chars: int | None = 12_000
    elif isinstance(raw_max, bool):
        max_chars = int(raw_max)
    elif isinstance(raw_max, int):
        max_chars = raw_max
    elif isinstance(raw_max, float):
        max_chars = int(raw_max)
    elif isinstance(raw_max, str):
        max_chars = int(raw_max.strip()) if raw_max.strip() else 12_000
    else:
        max_chars = int(str(raw_max))
    try:
        return sec.get_sec_document(
            str(args["accession_no"]), _str_or_none(args.get("document_name")),
            as_of=_str_or_none(args.get("as_of")),
            offset=offset,
            max_chars=max_chars,
            data_root=get_data_root(),
        )
    except (KeyError, ValueError) as exc:
        return {"error": str(exc), "error_type": "invalid_tool_arguments"}


def _sec_relationships_result(args: dict[str, object]) -> dict[str, object]:
    raw_rt = args.get("relationship_types")
    if raw_rt is None:
        rel_types: Sequence[str] | None = None
    elif isinstance(raw_rt, str):
        rel_types = (raw_rt,)
    elif isinstance(raw_rt, (list, tuple)):
        rel_types = tuple(str(x) for x in raw_rt)
    else:
        rel_types = None
    result = sec.search_sec_relationships(
        str(args["entity"]), relationship_types=rel_types,
        as_of=_str_or_none(args.get("as_of")), limit=int(str(args.get("limit", 50) or 50)),
        exhaustive=bool(args.get("exhaustive", True)),
    )
    raw_typed = result.get("typed")
    typed_list: list[object] = list(raw_typed) if isinstance(raw_typed, (list, tuple)) else list[object]()
    raw_rels = result.get("relationships")
    rels_list: list[object] = list(raw_rels) if isinstance(raw_rels, (list, tuple)) else list[object]()
    raw_ment = result.get("mentions")
    ment_list: list[object] = list(raw_ment) if isinstance(raw_ment, (list, tuple)) else list[object]()
    found = len(typed_list) + len(rels_list) + len(ment_list)
    raw_errors = result.get("errors")
    errors: list[object] = list(raw_errors) if isinstance(raw_errors, (list, tuple)) else []
    raw_attempts = result.get("attempts")
    attempts: list[dict[str, object]] = [{str(k): v for k, v in a.items()} for a in raw_attempts if isinstance(a, dict)] if isinstance(raw_attempts, list) else []
    has_partial = any(a.get("status") in ("partial", "source_limited", "complete_within_source_limits", "retrying") for a in attempts)
    has_failed = any(a.get("status") == "failed" for a in attempts)
    return {
        "subject": args.get("entity"),
        "entity": result.get("entity"),
        "ciks": list(result["ciks"]) if isinstance(result.get("ciks"), (list, tuple)) else [],
        "request": {"entity": args.get("entity"),
                    "relationship_types": args.get("relationship_types"),
                    "as_of": args.get("as_of")},
        "count": found,
        "groups": result.get("groups"),
        "parties": result.get("typed"),
        "relationships": result.get("relationships"),
        "mentions": result.get("mentions"),
        "coverage": {"status": "failed" if (errors and not found) or (has_failed and not found) else (
            "partial" if errors or result.get("warnings") or has_partial or has_failed else "complete")},
        "attempts": result.get("attempts"),
        "counts": {"typed": len(typed_list),
                   "workflow": len(rels_list),
                   "mentions": len(ment_list)},
        "pit_basis": "known_at" if args.get("as_of") else None,
        "warnings": result.get("warnings"),
        "errors": errors,
        "backfill_jobs": [],
        "source": "SEC EDGAR",
    }
def _list_sec_filings(args: dict[str, object], model: str) -> dict[str, object]:
    """List filings with lenient tool-JSON coercions (forms union narrowed here)."""
    del model
    raw_forms = args.get("forms")
    if raw_forms is None:
        forms: str | list[str] | tuple[str, ...] | None = None
    elif isinstance(raw_forms, str):
        forms = raw_forms
    elif isinstance(raw_forms, (list, tuple)):
        forms = tuple(str(x) for x in raw_forms)
    else:
        forms = None
    return _wrap_list(
        args.get("identifier"),
        sec.list_sec_filings(
            str(args["identifier"]),
            forms=forms,
            start_date=_str_or_none(args.get("start_date")),
            end_date=_str_or_none(args.get("end_date")),
            as_of=_str_or_none(args.get("as_of")),
            limit=_optional_int(args.get("limit", 50)),
        ),
        "filings",
    )
def _diff_sec_filings(args: dict[str, object], model: str) -> dict[str, object]:
    """Accession pair direct, or ticker self-resolution via sec.list_sec_filings."""
    del model
    cur = _str_or_none(args.get("current_accession"))
    prev = _str_or_none(args.get("previous_accession"))
    section = _str_or_none(args.get("section"))
    if cur and prev:
        return sec.diff_filings(cur, prev, section=section)
    ticker = _str_or_none(args.get("ticker"))
    if not ticker:
        return _invalid_args_error("diff_sec_filings", "Provide ticker or current_accession+previous_accession for tool 'diff_sec_filings'")
    raw_forms = args.get("forms")
    if raw_forms is None:
        forms: str | list[str] | tuple[str, ...] | None = None
    elif isinstance(raw_forms, str):
        forms = raw_forms
    elif isinstance(raw_forms, (list, tuple)):
        forms = tuple(str(x) for x in raw_forms)
    else:
        forms = None
    try:
        filings = sec.list_sec_filings(ticker, forms=forms, as_of=_str_or_none(args.get("as_of")), limit=10)
    except Exception as exc:
        return {"error": str(exc)}
    if len(filings) < 2:
        return {"error": f"No pair of filings found for {ticker}: {len(filings)} match"}
    latest = filings[0]
    same = [f for f in filings[1:] if f.form == latest.form]
    current, previous = (latest, same[0]) if same else (filings[0], filings[1])
    out = sec.diff_filings(current.accession_no, previous.accession_no, section=section)
    if isinstance(out, dict) and "error" not in out:
        out = {**out, "ticker": ticker.strip().upper(), "resolved_via": "list_sec_filings-internal"}
    return out



# Direct-dispatch tools (EDGAR/analyst/obligations/valuation) — same
# registry pattern as the FINRA/Robinhood handler maps below.
_MODEL_HANDLERS: dict[str, ModelHandler] = {
    "evaluate_mandate": lambda args, model: evaluate_mandate(),
    "get_fundamentals": lambda args, model: sec_facts.get_fundamentals(
        str(args["ticker"]), str(args["metric"]), as_of=_str_or_none(args.get("as_of"))
    ),
    "find_sec_entities": lambda args, model: _find_sec_entities(args),
    "search_sec_relationships": lambda args, model: _sec_relationships_result(args),
    "get_sec_search_coverage": lambda args, model: sec.get_sec_search_coverage(
        source=_str_or_none(args.get("source")), form=_str_or_none(args.get("form")),
        search_id=_str_or_none(args.get("search_id")), limit=int(str(args.get("limit", 200))),
    ),
    "search_sec_filings": lambda args, model: _sec_search_result(args),
    "list_sec_filings": _list_sec_filings,
    "get_sec_filing": lambda args, model: sec.get_sec_filing(
        str(args["accession_no"]), as_of=_str_or_none(args.get("as_of"))).to_dict(),
    "list_sec_documents": lambda args, model: _wrap_list(
        args.get("accession_no"), sec.list_sec_documents(
            str(args["accession_no"]), as_of=_str_or_none(args.get("as_of"))), "documents",
    ),
    "get_sec_document": _get_sec_document,
    "diff_sec_filings": _diff_sec_filings,
    "get_material_events": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_material_events(
            str(args["ticker"]), str(args["since"]), as_of=_str_or_none(args.get("as_of")),
            limit=_optional_int(args.get("limit", 50)),
        ), "events",
    ),
    "get_beneficial_ownership": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_beneficial_ownership(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 20)),
        ), "records",
    ),
    "get_ownership_changes": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_ownership_changes(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 20)),
        ), "changes",
    ),
    "get_insider_activity": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_insider_activity(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 50)),
        ), "transactions",
    ),
    "get_planned_insider_sales": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_planned_insider_sales(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 20)),
        ), "proposed_sales",
    ),
    "get_offering_history": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_offering_history(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 50)),
        ), "offerings",
    ),
    "get_dilution_profile": lambda args, model: sec.get_dilution_profile(
        str(args["ticker"]), as_of=_str_or_none(args.get("as_of")),
    ),
    "get_governance_events": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_governance_events(
            str(args["ticker"]), since=_str_or_none(args.get("since")), as_of=_str_or_none(args.get("as_of")),
            limit=_optional_int(args.get("limit", 10)),
        ), "events",
    ),
    "get_transaction_status": lambda args, model: _wrap_list(
        args.get("ticker"), sec.get_transaction_status(
            str(args["ticker"]), as_of=_str_or_none(args.get("as_of")), limit=_optional_int(args.get("limit", 10)),
        ), "transactions",
    ),
    "get_short_pressure_profile": lambda args, model: sec.get_short_pressure_context(
        str(args["ticker"]),
    ),
    "search_tools": _search_tools,
    "list_tool_domains": _list_tool_domains,
    "describe_tool": _describe_tool,
    "browse_tools": _browse_tools,
    "get_recent_ownership_filings": lambda args, model: edgar_client.get_recent_ownership_filings(str(args.get("form_type", "both")), int(str(args.get("limit", 10)))),
    "diff_risk_factors": lambda args, model: edgar_client.diff_risk_factors(str(args["ticker"])),
    "get_xbrl_facts": lambda args, model: sec_facts.get_xbrl_facts(str(args["ticker"]), str(args["concept"])),
    "get_financial_statements": lambda args, model: edgar_client.get_financial_statements(
        str(args["ticker"]), str(args["statement_type"])
    ),
    "get_analyst_estimates": lambda args, model: analyst_client.get_analyst_estimates(str(args["ticker"])),
    "get_sp500_weight": lambda args, model: analyst_client.get_sp500_weight(str(args["ticker"])),
    "get_obligations": lambda args, model: obligations.get_obligations(str(args["ticker"])),
    "get_valuation_metrics": lambda args, model: valuation.get_valuation_metrics(str(args["ticker"])),
    "search_web": _search_web,
    "find_alternative_signals": _find_alternative_signals,
    "get_trend_evidence": _get_trend_evidence,
    "investigate_social_arbitrage_candidate": _investigate_social_arbitrage_candidate,
    "get_macro_context": _get_macro_context,
    "search_company_patents": _search_company_patents,
}

# FINRA dispatch registry — kept next to the FINRA tool schemas above so the
# parity test can prove every FINRA schema has an executable dispatcher.
_FINRA_HANDLERS: dict[str, ModelHandler] = {
    "get_short_interest_leaderboard": lambda args, model: screens.get_short_interest_leaderboard(
        limit=_optional_int(args.get("limit")), settlement_date=_str_or_none(args.get("settlement_date")), as_of=_str_or_none(args.get("as_of"))
    ),
    "get_short_interest": lambda args, model: finra_client.get_short_interest(
        str(args["ticker"]), _str_or_none(args.get("settlementDate"))
    ),
    "get_reg_sho_volume": lambda args, model: finra_client.get_reg_sho_volume(
        str(args["ticker"]), _str_or_none(args.get("tradeDate"))
    ),
    "get_threshold_securities": lambda args, model: finra_client.get_threshold_securities(
        _str_or_none(args.get("ticker")), _str_or_none(args.get("tradeDate"))
    ),
    "list_finra_datasets": lambda args, model: finra_client.list_datasets(
        group=_str_or_none(args.get("group")), search=_str_or_none(args.get("search"))
    ),
    "describe_finra_dataset": lambda args, model: finra_client.describe_dataset(
        str(args.get("dataset_id") or args.get("dataset") or "")
    ),
    "get_finra_datapoints": lambda args, model: finra_client.get_finra_datapoints(
        str(args["dataset"]),
        fields=args.get("fields"),
        ticker=_str_or_none(args.get("ticker") or args.get("symbol")),
        start_date=_str_or_none(args.get("start_date")),
        end_date=_str_or_none(args.get("end_date")),
        limit=_optional_int(args.get("limit")),
        filters=args.get("filters"),
        sort_fields=args.get("sort_fields"),
        sort_order=_str_or_none(args.get("sort_order")),
    ),
    "query_finra": lambda args, model: finra_client.query_dataset(
        str(args["dataset"]),
        ticker=_str_or_none(args.get("ticker") or args.get("symbol")),
        start_date=_str_or_none(args.get("start_date")),
        end_date=_str_or_none(args.get("end_date")),
        limit=_optional_int(args.get("limit")),
        offset=_optional_int(args.get("offset")),
        filters=args.get("filters"),
        analysis_goal=_str_or_none(args.get("analysis_goal")),
    ),
}

_ROBINHOOD_HANDLERS: dict[str, ModelHandler] = {
    "get_market_snapshot": lambda args, model: get_market_snapshot(str(args["ticker"])),
    "get_option_chain": lambda args, model: get_option_chain(
        str(args["ticker"]), str(args["option_type"]), args.get("min_dte"), args.get("max_dte"),
        args.get("strike_min"), args.get("strike_max"), args.get("limit", 20)
    ),
    "analyze_option_contract": lambda args, model: analyze_option_contract(
        str(args["ticker"]), str(args["expiration"]), args["strike"], str(args["option_type"]), args.get("target_price")
    ),
    "compare_options": lambda args, model: compare_robinhood_options(
        str(args["ticker"]), str(args["option_type"]), args["target_price"], args.get("min_dte"), args.get("max_dte"),
        args.get("strike_min"), args.get("strike_max"), args.get("limit", 20)
    ),
    "get_portfolio_snapshot": _get_portfolio_snapshot,
    "get_scanner_filter_specs": _get_scanner_filter_specs,
    "get_scans": _get_scans,
    "run_scan": _run_scan,
}
# Every model-visible tool has one application-level capability. This is
# separate from the Robinhood MCP registry, which governs broker operations.
# Broker/account-connected market reads (Robinhood quotes/options/scans)
# require BROKER_MARKET_READ so generic research contexts never expose them;
# portfolio/account tools additionally require PORTFOLIO_READ.
TOOL_CAPABILITIES: dict[str, Capability] = {
    "evaluate_mandate": Capability.PORTFOLIO_READ,
    "get_fundamentals": Capability.RESEARCH,
    "find_sec_entities": Capability.RESEARCH,
    "search_sec_filings": Capability.RESEARCH,
    "search_sec_relationships": Capability.RESEARCH,
    "get_sec_search_coverage": Capability.RESEARCH,
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
    "get_governance_events": Capability.RESEARCH,
    "get_transaction_status": Capability.RESEARCH,
    "get_short_pressure_profile": Capability.RESEARCH,
    "search_tools": Capability.RESEARCH,
    "list_tool_domains": Capability.RESEARCH,
    "describe_tool": Capability.RESEARCH,
    "browse_tools": Capability.RESEARCH,
    "call_tool": Capability.RESEARCH,
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
    "find_alternative_signals": Capability.RESEARCH,
    "get_trend_evidence": Capability.RESEARCH,
    "investigate_social_arbitrage_candidate": Capability.RESEARCH,
    "get_macro_context": Capability.RESEARCH,
    "search_company_patents": Capability.RESEARCH,
    "list_finra_datasets": Capability.RESEARCH,
    "describe_finra_dataset": Capability.RESEARCH,
    "get_finra_datapoints": Capability.RESEARCH,
    "query_finra": Capability.RESEARCH,
    "thesis_create": Capability.RESEARCH,
    "thesis_show": Capability.RESEARCH,
    "thesis_refine": Capability.RESEARCH,
    "thesis_watch": Capability.RESEARCH,
    "thesis_journal": Capability.RESEARCH,
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


def tools_for_capabilities(capabilities: frozenset[Capability]) -> list[dict[str, object]]:
    """Return only schemas whose application capability is granted."""
    out: list[dict[str, object]] = []
    for tool in TOOLS:
        raw_name = _tool_function(tool).get("name")
        if isinstance(raw_name, str) and TOOL_CAPABILITIES.get(raw_name) in capabilities:
            out.append(tool)
    return out


def tool_is_permitted(name: str, context: RequestContext) -> bool:
    capability = TOOL_CAPABILITIES.get(name)
    return capability is not None and capability in context.capabilities


def _validate_tool_arguments(name: str, arguments: object) -> str | None:
    """Schema-level argument check: object-ness plus required keys. Returns
    an error message, or None when the arguments are acceptable. Type
    checking is intentionally out of scope; lenient handler coercions
    (int(...), ...) remain the source of truth for value shapes."""
    if not isinstance(arguments, dict):
        return f"Tool arguments must be a JSON object for tool '{name}'"
    tool = next((t for t in TOOLS if _tool_function(t).get("name") == name), None)
    fn_dict: dict[str, object] = _tool_function(tool) if tool is not None else {}
    raw_params = fn_dict.get("parameters")
    params_dict: dict[str, object] = {str(k): v for k, v in raw_params.items()} if isinstance(raw_params, dict) else {}
    raw_required = params_dict.get("required")
    required_keys: list[str] = [str(k) for k in raw_required] if isinstance(raw_required, list) else []
    missing = [key for key in required_keys if key not in arguments]
    if missing:
        return f"Missing required argument(s) for tool '{name}': {', '.join(missing)}"
    return None


def _canonical_tool_schema(name: str) -> tuple[dict[str, object], list[str], list[str]]:
    """Canonical parameters object plus required/optional lists via _tool_function."""
    tool = next((t for t in TOOLS if _tool_function(t).get("name") == name), None)
    fn = _tool_function(tool) if tool is not None else {}
    raw_params = fn.get("parameters")
    params: dict[str, object] = {str(k): v for k, v in raw_params.items()} if isinstance(raw_params, dict) else {}
    raw_required = params.get("required")
    required: list[str] = [str(k) for k in raw_required] if isinstance(raw_required, list) else []
    raw_props = params.get("properties")
    props: dict[str, object] = {str(k): v for k, v in raw_props.items()} if isinstance(raw_props, dict) else {}
    optional = sorted(key for key in props if key not in required)
    return params, required, optional


def _invalid_args_error(name: str, message: str) -> dict[str, object]:
    """Repairable validation shape reusing the canonical schema."""
    params, required, optional = _canonical_tool_schema(name)
    return {"error": message, "error_type": "invalid_tool_arguments", "tool": name, "parameters": params, "required": required, "optional": optional}


def _unknown_tool_error(name: str) -> dict[str, object]:
    """Unknown-tool shape for call_tool dispatch (executes nothing)."""
    return {"error": f"unknown_tool '{name}'", "error_type": "unknown_tool", "tool": name, "hint": "call browse_tools with no arguments, then call_tool with an exact catalog name"}


def _thesis_repo_for(context: RequestContext) -> ThesisRepository:
    """Thesis repository rooted at the invocation's data root (never CWD)."""
    from app.thesis.repository import ThesisRepository

    base = getattr(context, "data_root", None) or get_data_root()
    return ThesisRepository(Path(str(base)) / "thesis")

def _effective_at(context: RequestContext) -> str | None:
    as_of = getattr(context, "as_of", None)
    return as_of if isinstance(as_of, str) and as_of else None


def _normalize_thesis_id(value: str) -> str:
    """Accept a bare UUID for a thesis:uuid (agents strip the prefix)."""
    text = value.strip()
    if text and ":" not in text:
        return f"thesis:{text}"
    return text


def _thesis_for_context(repo: ThesisRepository, id_or_slug: str, context: RequestContext) -> Thesis:
    """Thesis as seen at the run cutoff; live when the run has none."""
    id_or_slug = _normalize_thesis_id(id_or_slug)
    cutoff = _effective_at(context)
    if not cutoff:
        return repo.load_thesis(id_or_slug)
    from app.thesis.models import Thesis  # local: avoids a module cycle

    current = repo.load_thesis(id_or_slug)
    snap = repo.load_state_as_of(current.thesis_id, cutoff)
    return Thesis.from_dict(dict(snap.thesis), "<as_of>")


_PIT_INSTANT_TOOLS = frozenset({"thesis_show"})
_PIT_GOVERNED_MUTATORS = frozenset({"thesis_create", "thesis_refine", "thesis_watch", "thesis_journal"})


def _pit_day(cutoff: str) -> str | None:
    from datetime import timezone  # local: keep module import surface minimal
    from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

    dt = _as_dt(cutoff)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).date().isoformat()


def _apply_pit_cutoff(name: str, arguments: object, context: RequestContext) -> tuple[dict[str, object], dict[str, object] | None]:
    """Default `as_of` to the run cutoff; reject a model value beyond it."""
    cutoff = _effective_at(context)
    if not isinstance(arguments, dict):
        return {}, {"error": f"Tool arguments must be a JSON object for tool '{name}'", "error_type": "invalid_tool_arguments"}
    args: dict[str, object] = arguments
    if not cutoff:
        return args, None
    tool = next((t for t in TOOLS if _tool_function(t).get("name") == name), None)
    fn = _tool_function(tool) if tool is not None else {}
    raw_parameters = fn.get("parameters")
    parameters: dict[str, object] = {str(k): v for k, v in raw_parameters.items()} if isinstance(raw_parameters, dict) else {}
    raw_props = parameters.get("properties")
    props: dict[str, object] = {str(k): v for k, v in raw_props.items()} if isinstance(raw_props, dict) else {}
    if "as_of" not in props:
        return args, None
    supplied = args.get("as_of")
    if supplied is None or (isinstance(supplied, str) and not supplied):
        if name in _PIT_INSTANT_TOOLS:
            return {**args, "as_of": cutoff}, None
        day = _pit_day(cutoff)
        if day is None:
            return args, {"error": f"tool '{name}': bad run cutoff {cutoff!r}", "error_type": "invalid_tool_arguments"}
        return {**args, "as_of": day}, None
    if not isinstance(supplied, str):
        return args, None  # handler validation owns the message
    from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

    supplied_dt, cutoff_dt = _as_dt(supplied), _as_dt(cutoff)
    if supplied_dt is not None and cutoff_dt is not None and supplied_dt > cutoff_dt:
        return args, {"error": f"tool '{name}': as_of {supplied!r} is beyond the run cutoff {cutoff!r}", "error_type": "invalid_tool_arguments"}
    return args, None


def _thesis_proposal(arguments: dict[str, object], user_thesis: str, path: str) -> IntakeProposal:
    """Structured tool args -> validated IntakeProposal (raises ValueError)."""
    from app.thesis import intake as thesis_intake

    payload: dict[str, object] = {"user_thesis": user_thesis}
    for key in _THESIS_PROPOSAL_KEYS:
        if arguments.get(key) is not None:
            payload[key] = arguments[key]
    return thesis_intake.IntakeProposal.from_dict(payload, path)


def _thesis_create(arguments: dict[str, object], context: RequestContext) -> dict[str, object]:
    from app.thesis import intake as thesis_intake

    user_thesis = arguments.get("user_thesis")
    if not isinstance(user_thesis, str) or not user_thesis.strip():
        raise ValueError("thesis_create: 'user_thesis' must be a non-empty string")
    proposal = _thesis_proposal(arguments, user_thesis, "<thesis_create>")
    return thesis_intake.create_thesis_from_proposal(
        _thesis_repo_for(context), proposal, effective_at=_effective_at(context))

def _thesis_show(arguments: dict[str, object], context: RequestContext) -> dict[str, object]:
    repo = _thesis_repo_for(context)
    thesis = _thesis_for_context(repo, str(arguments["id"]), context)
    tid = thesis.thesis_id
    model_as_of = arguments.get("as_of")
    cutoff = _effective_at(context)
    if isinstance(model_as_of, str) and model_as_of and cutoff:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers

        model_dt, cutoff_dt = _as_dt(model_as_of), _as_dt(cutoff)
        if model_dt is not None and cutoff_dt is not None and model_dt > cutoff_dt:
            raise ValueError(f"thesis_show: as_of {model_as_of!r} is beyond the run cutoff {cutoff!r}")
    as_of = model_as_of if isinstance(model_as_of, str) and model_as_of else cutoff
    if isinstance(as_of, str) and as_of:
        snap = repo.load_state_as_of(tid, as_of)
        t, state, watch, questions = snap.thesis, snap.state, snap.watch, snap.questions
        _rules = watch.get("rules", [])
        rules = [r for r in (_rules if isinstance(_rules, list) else ()) if isinstance(r, dict)]
        live = [r for r in rules if r.get("enabled") and r.get("support_status") == "supported"]
        _qq = questions.get("questions", [])
        return {
            "thesis_id": tid,
            "slug": t.get("slug"),
            "status": t.get("status"),
            "user_thesis": t.get("user_thesis"),
            "scope": t.get("scope"),
            "claims": list(_claims) if isinstance((_claims := t.get("claims", [])), list) else [],
            "expressions": list(_exprs) if isinstance((_exprs := t.get("expressions", [])), list) else [],
            "assessment": state.get("assessment"),
            "rules": rules,
            "setup_needed": not live,
            "open_questions": [q for q in (_qq if isinstance(_qq, list) else ()) if isinstance(q, dict) and q.get("status") == "open"],
        }
    rules = [r.to_dict() for r in repo.load_watch_rules(tid)]
    live = [r for r in rules if r.get("enabled") and r.get("support_status") == "supported"]
    return {
        "thesis_id": tid,
        "slug": thesis.slug,
        "status": thesis.status,
        "user_thesis": thesis.user_thesis,
        "scope": thesis.scope,
        "claims": [c.to_dict() for c in thesis.claims],
        "expressions": [e.to_dict() for e in thesis.expressions],
        "assessment": repo.load_state(tid).assessment,
        "rules": rules,
        "setup_needed": not live,
        "open_questions": [q.to_dict() for q in repo.load_questions(tid) if q.status == "open"],
    }


def _thesis_refine(arguments: dict[str, object], context: RequestContext) -> dict[str, object]:
    from app.thesis import intake as thesis_intake

    repo = _thesis_repo_for(context)
    thesis_id = arguments.get("id")
    if not isinstance(thesis_id, str) or not thesis_id.strip():
        raise ValueError("thesis_refine: 'id' must be a non-empty string")
    thesis = _thesis_for_context(repo, thesis_id, context)
    clarification = arguments.get("clarification")
    if not isinstance(clarification, str) or not clarification.strip():
        raise ValueError("thesis_refine: 'clarification' must be a non-empty string")
    proposal = _thesis_proposal(
        arguments, f"{thesis.user_thesis}\n{clarification.strip()}", "<thesis_refine>")
    plan = thesis_intake.plan_refinement(thesis, proposal)
    merged = plan["merged"]
    merged_thesis = merged.get("user_thesis") if isinstance(merged, dict) else None
    if (not plan["added_claims"] and not plan["added_expressions"]
            and merged_thesis == thesis.user_thesis):
        return {"thesis_id": thesis.thesis_id, "slug": thesis.slug, "applied": False}
    out = thesis_intake.apply_refinement(
        repo, thesis.thesis_id, plan, proposal, effective_at=_effective_at(context))
    return {"applied": True, **out}


def _thesis_watch(arguments: dict[str, object], context: RequestContext) -> dict[str, object]:
    from app.thesis.models import new_rule_id
    from app.thesis.monitor import SUPPORTED_HANDLERS

    repo = _thesis_repo_for(context)
    thesis = _thesis_for_context(repo, str(arguments["id"]), context)
    tid = thesis.thesis_id
    if arguments.get("rule_type") is None:
        cutoff = _effective_at(context)
        if cutoff:
            snap = repo.load_state_as_of(tid, cutoff)
            _wrules = snap.watch.get("rules", [])
            rules = [r for r in (_wrules if isinstance(_wrules, list) else ()) if isinstance(r, dict)]
            live = [r for r in rules if r.get("enabled") and r.get("support_status") == "supported"]
            return {"thesis_id": tid, "rules": rules, "setup_needed": not live}
        rules = [r.to_dict() for r in repo.load_watch_rules(tid)]
        live = [r for r in rules if r.get("enabled") and r.get("support_status") == "supported"]
        return {"thesis_id": tid, "rules": rules, "setup_needed": not live}
    rule_type = arguments["rule_type"]
    if not isinstance(rule_type, str) or not rule_type.strip():
        raise ValueError("thesis_watch: 'rule_type' must be a non-empty string")
    if rule_type not in SUPPORTED_HANDLERS:
        raise ValueError(f"thesis_watch: unsupported rule_type {rule_type!r}; supported: {sorted(SUPPORTED_HANDLERS)}")
    if thesis.status != "active":
        raise ValueError(f"thesis {tid!r} is {thesis.status}; refusing watch change")
    for key in ("claim_ids", "expression_ids"):
        vals = arguments.get(key, [])
        if not isinstance(vals, list) or not all(isinstance(v, str) for v in vals):
            raise ValueError(f"thesis_watch: '{key}' must be a list of IDs")
    raw_claims = arguments.get("claim_ids", [])
    claim_ids = list(raw_claims) if isinstance(raw_claims, (list, tuple)) else []
    raw_exprs = arguments.get("expression_ids", [])
    expression_ids = list(raw_exprs) if isinstance(raw_exprs, (list, tuple)) else []
    rule: dict[str, object] = {
        "rule_id": new_rule_id(),
        "rule_type": rule_type,
        "enabled": True,
        "support_status": "supported",
        "support_reason": "",
        "claim_ids": claim_ids,
        "expression_ids": expression_ids,
    }
    repo.apply_research_result(
        tid, {"watch_add": [rule]}, "", effective_at=_effective_at(context))
    return {"thesis_id": tid, "added": rule}


def _thesis_journal(arguments: dict[str, object], context: RequestContext) -> dict[str, object]:
    repo = _thesis_repo_for(context)
    thesis = _thesis_for_context(repo, str(arguments["id"]), context)
    if thesis.status != "active":
        raise ValueError(f"thesis {thesis.thesis_id!r} is {thesis.status}; refusing journal append")
    body = arguments["body"]
    if not isinstance(body, str) or not body.strip():
        raise ValueError("thesis_journal: 'body' must be a non-empty string")
    title = arguments.get("title", "Operator note")
    if title is not None and not isinstance(title, str):
        raise ValueError("thesis_journal: 'title' must be a string")
    entry: dict[str, object] = {
        "title": title or "Operator note",
        "body": body.strip(),
    }
    trigger_id = arguments.get("trigger_id")
    if trigger_id is not None:
        if not isinstance(trigger_id, str) or not trigger_id:
            raise ValueError("thesis_journal: 'trigger_id' must be a non-empty string")
        if not any(t.trigger_id == trigger_id for t in repo.load_triggers(thesis.thesis_id)):
            raise ValueError(f"thesis_journal: trigger {trigger_id!r} does not belong to thesis {thesis.thesis_id!r}")
        entry["trigger_id"] = trigger_id
    run_id = arguments.get("run_id")
    if run_id is not None:
        if trigger_id is None:
            raise ValueError("thesis_journal: 'run_id' requires 'trigger_id'")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("thesis_journal: 'run_id' must be a non-empty string")
        entry["run_id"] = run_id
    known_at = arguments.get("known_at")
    if trigger_id is not None:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers
        cutoff = _effective_at(context)
        if cutoff:
            if not isinstance(known_at, str) or not known_at or _as_dt(known_at) is None:
                raise ValueError("thesis_journal: 'known_at' is required for trigger-linked entries and must be a parseable ISO-8601 string")
            known_dt, cutoff_dt = _as_dt(known_at), _as_dt(cutoff)
            if (known_dt is not None or cutoff_dt is not None) and known_dt != cutoff_dt:
                raise ValueError(f"thesis_journal: 'known_at' {known_at!r} must equal the run cutoff {cutoff!r}")
            entry["known_at"] = known_at
        elif known_at is not None:
            if not isinstance(known_at, str) or not known_at or _as_dt(known_at) is None:
                raise ValueError("thesis_journal: 'known_at' must be a parseable ISO-8601 string")
            entry["known_at"] = known_at
    elif known_at is not None:
        from app.thesis.monitor import _as_dt  # local: monitor owns the clock helpers
        if not isinstance(known_at, str) or not known_at or _as_dt(known_at) is None:
            raise ValueError("thesis_journal: 'known_at' must be a parseable ISO-8601 string")
        entry["known_at"] = known_at
    dest = repo.append_journal_entry(thesis.thesis_id, entry)
    return {"thesis_id": thesis.thesis_id, "journal_path": str(dest)}


_THESIS_HANDLERS: dict[str, ContextHandler] = {
    "thesis_create": _thesis_create,
    "thesis_show": _thesis_show,
    "thesis_refine": _thesis_refine,
    "thesis_watch": _thesis_watch,
    "thesis_journal": _thesis_journal,
}

# Thesis tools are direct local dispatch (no broker) but take
# (arguments, context) instead of (arguments, model) for data-root scoping.
# Merged view for backward compat (tests/scripts import _DIRECT_HANDLERS).
_DIRECT_HANDLERS: dict[str, object] = {**_MODEL_HANDLERS, **_THESIS_HANDLERS}
_CONTEXT_CALL_HANDLERS = frozenset(_THESIS_HANDLERS)

def execute_tool(
    name: str,
    arguments: dict[str, object],
    model: str,
    *,
    context: RequestContext,
) -> dict[str, object]:
    """Dispatch a tool call by name. Always returns a JSON-serializable dict;
    never raises — errors are returned as {"error": ...} so the model can
    report them honestly (guardrail behavior)."""
    try:
        if not tool_is_permitted(name, context):
            return {"error": f"Tool is not permitted: {name}"}
        arguments, pit_error = _apply_pit_cutoff(name, arguments, context)
        if pit_error is not None:
            return pit_error
        if _effective_at(context) and name not in _PIT_GOVERNED_MUTATORS:
            _pit_tool = next((t for t in TOOLS if _tool_function(t).get("name") == name), None)
            _pit_fn = _tool_function(_pit_tool) if _pit_tool is not None else {}
            _pit_raw_params = _pit_fn.get("parameters")
            _pit_params: dict[str, object] = {str(k): v for k, v in _pit_raw_params.items()} if isinstance(_pit_raw_params, dict) else {}
            _pit_raw_props = _pit_params.get("properties")
            _pit_props: dict[str, object] = {str(k): v for k, v in _pit_raw_props.items()} if isinstance(_pit_raw_props, dict) else {}
            if "as_of" not in _pit_props:
                return {"error": f"Tool '{name}' is not point-in-time safe under this historical run.", "error_type": "pit_unsafe_tool", "soft": True}
        invalid = _validate_tool_arguments(name, arguments)
        if invalid is not None:
            return _invalid_args_error(name, invalid)
        if name in _CONTEXT_CALL_HANDLERS:
            ctx_handler = _THESIS_HANDLERS.get(name)
            if ctx_handler is None:
                return _unknown_tool_error(name)
            return ctx_handler(arguments, context)
        model_handler = (
            _MODEL_HANDLERS.get(name)
            or _FINRA_HANDLERS.get(name)
            or _ROBINHOOD_HANDLERS.get(name)
        )
        if model_handler is None:
            return _unknown_tool_error(name)
        result = model_handler(arguments, model)
        if _effective_at(context) and isinstance(result, dict):
            tool = next((t for t in TOOLS if _tool_function(t).get("name") == name), None)
            fn = _tool_function(tool) if tool is not None else {}
            raw_parameters = fn.get("parameters")
            parameters: dict[str, object] = {str(k): v for k, v in raw_parameters.items()} if isinstance(raw_parameters, dict) else {}
            raw_props = parameters.get("properties")
            props: dict[str, object] = {str(k): v for k, v in raw_props.items()} if isinstance(raw_props, dict) else {}
            if "as_of" not in props and name not in _PIT_GOVERNED_MUTATORS:
                result.setdefault("pit_safe", False)
        return result
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


validate_tool_discovery_registry()
