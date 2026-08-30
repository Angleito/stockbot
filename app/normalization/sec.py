"""SEC normalizers: company tickers and XBRL company facts.

The companyfacts normalizer extracts the common-share-class signal used by
the short-interest screen — ``dei:EntityCommonStockSharesOutstanding`` — plus
canonical financial metrics (revenue, net income, cash, long-term debt) that
later research layers query.  Each canonical metric maps several provider
XBRL tags onto one stable concept name.  The full payload is archived so a
complete XBRL parse can be replayed from raw data without re-downloading.
"""

from __future__ import annotations

from typing import Any, Optional

from ..storage import ids

COMPANY_TICKERS_PARSER_VERSION = "sec-company-tickers-v1"
COMPANY_FACTS_PARSER_VERSION = "sec-companyfacts-v2"

SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
_ORIGINAL_CONCEPT = "dei:EntityCommonStockSharesOutstanding"

CANONICAL_CONCEPTS: dict[str, tuple[str, ...]] = {
    "Revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
    "NetIncomeLoss": ("NetIncomeLoss",),
    "CashAndCashEquivalents": ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"),
    "LongTermDebt": ("LongTermDebtCurrentAndNoncurrent", "LongTermDebtNoncurrent", "LongTermDebt"),
}


def normalize_company_tickers(raw: Any, *, retrieved_at: str, content_hash: str) -> dict[str, list[dict]]:
    """company_tickers.json -> entities and entity_aliases rows."""
    entities: list[dict] = []
    aliases: list[dict] = []
    items = raw.values() if isinstance(raw, dict) else (raw or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        cik_raw = item.get("cik_str")
        if not ticker or cik_raw is None:
            continue
        try:
            cik = int(cik_raw)
        except (TypeError, ValueError):
            continue
        entity_id = ids.sec_entity_id(cik)
        entities.append({
            "entity_id": entity_id,
            "name": str(item.get("title") or "").strip() or None,
            "entity_type": "unknown",
            "sic": None,
            "source": "sec:company_tickers",
            "known_at": retrieved_at,
            "retrieved_at": retrieved_at,
            "content_hash": content_hash,
            "parser_version": COMPANY_TICKERS_PARSER_VERSION,
        })
        aliases.append({
            "alias_type": "ticker",
            "alias_value": ticker,
            "entity_id": entity_id,
            "security_id": ids.sec_security_id(cik),
            "source": "sec:company_tickers",
            "valid_from": None,
            "valid_to": None,
            "known_at": retrieved_at,
            "retrieved_at": retrieved_at,
            "content_hash": content_hash,
            "parser_version": COMPANY_TICKERS_PARSER_VERSION,
        })
    return {"entities": entities, "entity_aliases": aliases}


def _extract_shares_facts(raw: Any) -> list[dict]:
    units = (((raw.get("facts") or {}).get("dei") or {}).get(SHARES_OUTSTANDING_CONCEPT) or {}).get("units") or {}
    facts = units.get("shares") or []
    return [fact for fact in facts if isinstance(fact, dict)]


def _extract_canonical_facts(raw: Any) -> list[tuple[str, str, str, dict]]:
    """(canonical concept, original_concept, unit, fact) for canonical metric facts."""
    entries: list[tuple[str, str, str, dict]] = []
    namespaces = raw.get("facts") or {}
    if not isinstance(namespaces, dict):
        return entries
    for namespace, concepts in namespaces.items():
        if not isinstance(concepts, dict):
            continue
        for tag, payload in concepts.items():
            canonical = next((name for name, aliases in CANONICAL_CONCEPTS.items() if tag in aliases), None)
            if canonical is None:
                continue
            units = (payload or {}).get("units") or {}
            for fact in units.get("USD") or []:
                if isinstance(fact, dict):
                    entries.append((canonical, f"{namespace}:{tag}", "USD", fact))
    return entries


def _extract_facts(raw: Any) -> list[tuple[str, str, str, dict]]:
    """(concept, original_concept, unit, fact) for every extracted fact."""
    entries = [(SHARES_OUTSTANDING_CONCEPT, _ORIGINAL_CONCEPT, "shares", fact) for fact in _extract_shares_facts(raw)]
    entries.extend(_extract_canonical_facts(raw))
    return entries


def normalize_company_facts(
    raw: Any,
    *,
    retrieved_at: str,
    content_hash: str,
    source_url: str,
    source_record_id: str,
) -> dict[str, list[dict]]:
    """companyfacts JSON -> documents, financial_facts, and securities rows."""
    try:
        cik = int(raw.get("cik") or 0)
    except (TypeError, ValueError):
        cik = 0
    entity_id = ids.sec_entity_id(cik)
    security_id = ids.sec_security_id(cik)
    extracted_facts = _extract_facts(raw)
    documents = [{
        "doc_id": ids.sec_doc_id("companyfacts", source_record_id, content_hash),
        "source": "sec",
        "kind": "companyfacts",
        "key": source_record_id,
        "source_url": source_url,
        "accession": None,
        "sha256": content_hash,
        "retrieved_at": retrieved_at,
        "published_at": None,
        "known_at": retrieved_at,
        "content_hash": content_hash,
        "parser_version": COMPANY_FACTS_PARSER_VERSION,
    }]
    financial_facts: list[dict] = []
    for concept, original_concept, unit, fact in extracted_facts:
        period_end = str(fact.get("end") or "")
        filed_at = str(fact.get("filed") or "")
        accession = str(fact.get("accn") or "")
        try:
            value = float(fact.get("val"))
        except (TypeError, ValueError):
            continue
        if not period_end or not filed_at or not accession:
            continue
        duration_type = "duration" if fact.get("start") else "instant"
        financial_facts.append({
            "fact_id": ids.sec_fact_id(cik, accession, concept, period_end, value),
            "entity_id": entity_id,
            "security_id": security_id,
            "concept": concept,
            "original_concept": original_concept,
            "value": value,
            "unit": unit,
            "duration_type": duration_type,
            "period_end": period_end,
            "filed_at": filed_at,
            "accession": accession,
            "frame": fact.get("frame"),
            "known_at": filed_at,
            "retrieved_at": retrieved_at,
            "source_url": source_url,
            "source_record_id": source_record_id,
            "content_hash": content_hash,
            "parser_version": COMPANY_FACTS_PARSER_VERSION,
        })
    securities = [{
        "security_id": security_id,
        "entity_id": entity_id,
        "security_type": "equity-common" if financial_facts else "unknown",
        "ticker": None,
        "exchange": None,
        "source": "sec:companyfacts",
        "known_at": retrieved_at,
        "retrieved_at": retrieved_at,
        "content_hash": content_hash,
        "parser_version": COMPANY_FACTS_PARSER_VERSION,
    }]
    return {"documents": documents, "financial_facts": financial_facts, "securities": securities}


def fact_as_of(fact: dict) -> Optional[str]:
    """The date a market participant could first have seen this fact: its
    filed date.  None when the fact has no filed date."""
    return str(fact.get("filed_at") or "") or None