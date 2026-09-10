"""Tool implementations + OpenAI-format JSON schemas for Pi."""

import hashlib
import json
import logging
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
            "description": "Returns a specific numeric fundamental (EPS, "
                "dividends, balance sheet line item, shares outstanding) for a ticker. "
                "Note: shares_outstanding is SEC-reported shares outstanding, "
                "not public float. Call this for any request for a specific "
                "numeric metric. Dividends responses include last paid and next "
                "SEC-declared (upcoming) dividends with filing provenance; "
                "undeclared estimates are never included.",
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
            "description": "EDGAR discovery over entity, full-text (EFTS), filer-submissions, global filing, and local routes (default non-exhaustive, capped at limit). Hits are text mentions: each names the filer (filer_name/filer_cik) and the exact matched document, never inferred subject identity. Returns coverage, attempts, counts, PIT basis, warnings/errors, auto-queued backfill jobs, and bounded evidence IDs. Retrieve via get_sec_filing, list_sec_documents, or get_sec_document.",
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
            "description": "Lists SEC EDGAR filings for an exact ticker or CIK. Does NOT search company names. If only a company, person, or domain is known, call find_sec_entities or search_sec_filings first, verify identity, then call with identifier.",
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
            "description": "Returns one filing's record (filer, subject when known, form, filed/accepted/known dates, period, primary document, amendment link, source URL) by accession number.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string"}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}},
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
                "properties": {"accession_no": {"type": "string"}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}},
                "required": ["accession_no"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_sec_document",
            "description": "Returns a bounded window of one filing document's text (default: primary document) by accession number. Defaults to the first 12000 characters; page with offset/max_chars. Load only the document relevant to the question, never full history.",
            "parameters": {
                "type": "object",
                "properties": {"accession_no": {"type": "string"}, "document_name": {"type": "string"}, "as_of": {"type": "string", "description": "Point-in-time date YYYY-MM-DD; filings known after it are excluded."}, "offset": {"type": "integer", "description": "Character offset into the document text (default 0)."}, "max_chars": {"type": "integer", "description": "Characters to return, 1..32000 (default 12000)."}},
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
                "required": list[str]()
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
                "required": list[str]()
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
                "required": list[str]()
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
                "required": list[str]()
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
            "description": "Creates a thesis from a structured proposal. Returns thesis_id, scope, initial supported watch rules, or setup-needed state with missing questions when no target resolves. Never invents thresholds.",
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
            "description": "Bounded Google Trends discovery collection over public top/rising lists with stable source identity and retrieval timestamps. List membership only, never search-volume claims.",
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
            "description": "Bounded Data Commons statistical observations for explicit geography/variable IDs with unit/facet/provider provenance. Distinct facets stay distinct; never splices incompatible series.",
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
            "description": "Bounded patent-publication search for documented company assignees via checked-in BigQuery templates. Counts publications explicitly; never labels counts as inventions or bullish signals.",
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
        str(item.get("finra_known_at") or ""),
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


# search_tools catalog: name -> (domain, keyword tags). New tools add one
# line here; prompts never change.
_SEC_TOOL_TAGS = {
    "find_sec_entities": ("filings", "entity CIK ticker company issuer verify identity private no-ticker former name ambiguous exhaustive"),
    "search_sec_filings": ("filings", "full text EFTS filing content founder person domain security mention filer coverage search forms accession exhaustive relationship backfill"),
    "search_sec_relationships": ("ownership", "relationships beneficial owner 13D 13G holding manager 13F insider issuer verified mention inverse transaction offering party"),
    "get_sec_search_coverage": ("filings", "coverage backfill jobs ledger partitions complete partial queued failed search persistence"),
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
    "get_governance_events": ("governance", "governance proxy DEF 14A vote shareholder board compensation"),
    "get_transaction_status": ("transactions", "transaction merger tender offer acquisition S-4 status"),
    "get_short_pressure_profile": ("market", "short interest pressure squeeze positioning outstanding"),
    "find_alternative_signals": ("alternative", "trends discovery candidate signal persistence diffusion social arbitrage term geography"),
    "get_trend_evidence": ("alternative", "trends evidence term geography rank list retrieval batch"),
    "investigate_social_arbitrage_candidate": ("alternative", "social arbitrage candidate evidence entity exposure gap youtube corroboration"),
    "get_macro_context": ("macro", "datacommons macro census geography statistical variable observation facet provider unit"),
    "search_company_patents": ("patents", "patents publication assignee classification publication count assignee alias"),
}

_SEC_DOMAIN_PACKS = {
    "filings": ["find_sec_entities", "search_sec_filings", "get_sec_search_coverage", "list_sec_filings", "get_sec_filing", "list_sec_documents", "get_sec_document", "diff_sec_filings"],
    "events": ["get_material_events"],
    "ownership": ["search_sec_relationships", "get_beneficial_ownership", "get_ownership_changes"],
    "insider": ["get_insider_activity", "get_planned_insider_sales"],
    "offerings": ["get_offering_history", "get_dilution_profile"],
    "governance": ["get_governance_events"],
    "transactions": ["get_transaction_status"],
    "market": ["get_short_pressure_profile"],
}


def _search_tools(args: dict[str, object], model: str) -> dict[str, object]:
    """Keyword search over tool names, descriptions, and domain tags."""
    del model
    query = str(args.get("query") or "").strip().lower().replace("_", " ")
    domain = str(args.get("domain") or "").strip().lower()
    by_name: dict[str, dict[str, object]] = {}
    for tool in TOOLS:
        fn = _tool_function(tool)
        raw_name = fn.get("name")
        if isinstance(raw_name, str):
            by_name[raw_name] = tool
    if domain and not query:
        names = _SEC_DOMAIN_PACKS.get(domain, [])
        return {"domain": domain, "schemas": [by_name[n] for n in names if n in by_name]}
    tokens = query.split()
    matches: list[dict[str, object]] = []
    for name, tool in by_name.items():
        if name == "search_tools":
            continue
        tags = _SEC_TOOL_TAGS.get(name, ("", ""))[1]
        fn_desc = _tool_function(tool).get("description", "")
        haystack = f"{name} {str(fn_desc)} {tags}".lower().replace("_", " ")
        if tokens and all(token in haystack for token in tokens):
            matches.append(tool)
    return {"query": args.get("query"), "count": len(matches), "schemas": matches}


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
    "diff_sec_filings": lambda args, model: sec.diff_filings(
        str(args["current_accession"]), str(args["previous_accession"]), section=_str_or_none(args.get("section")),
    ),
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


def _thesis_repo_for(context: RequestContext) -> ThesisRepository:
    """Thesis repository rooted at the invocation's data root (never CWD)."""
    from app.thesis.repository import ThesisRepository

    base = getattr(context, "data_root", None) or get_data_root()
    return ThesisRepository(Path(str(base)) / "thesis")

def _effective_at(context: RequestContext) -> str | None:
    as_of = getattr(context, "as_of", None)
    return as_of if isinstance(as_of, str) and as_of else None


def _thesis_for_context(repo: ThesisRepository, id_or_slug: str, context: RequestContext) -> Thesis:
    """Thesis as seen at the run cutoff; live when the run has none."""
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
            return {"error": invalid, "error_type": "invalid_tool_arguments"}
        if name in _CONTEXT_CALL_HANDLERS:
            ctx_handler = _THESIS_HANDLERS.get(name)
            if ctx_handler is None:
                return {"error": f"Unknown tool '{name}'"}
            return ctx_handler(arguments, context)
        model_handler = (
            _MODEL_HANDLERS.get(name)
            or _FINRA_HANDLERS.get(name)
            or _ROBINHOOD_HANDLERS.get(name)
        )
        if model_handler is None:
            return {"error": f"Unknown tool '{name}'"}
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
