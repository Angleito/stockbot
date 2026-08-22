"""SEC normalizers: company tickers and XBRL company facts.

Scope limit (documented in FUTURE_ARCHITECTURE.md): the companyfacts
normalizer currently extracts the common-share-class signal used by the
short-interest screen — ``dei:EntityCommonStockSharesOutstanding`` — and
classifies the security as equity-common when such a fact exists.  The full
payload is archived so a complete XBRL parse can be replayed from raw data
without re-downloading.
"""

from __future__ import annotations

from typing import Any, Optional

from ..storage import ids

COMPANY_TICKERS_PARSER_VERSION = "sec-company-tickers-v1"
COMPANY_FACTS_PARSER_VERSION = "sec-companyfacts-v1"

SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
_ORIGINAL_CONCEPT = "dei:EntityCommonStockSharesOutstanding"


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
    facts = _extract_shares_facts(raw)
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
    for fact in facts:
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
            "fact_id": ids.sec_fact_id(cik, accession, SHARES_OUTSTANDING_CONCEPT, period_end, value),
            "entity_id": entity_id,
            "security_id": security_id,
            "concept": SHARES_OUTSTANDING_CONCEPT,
            "original_concept": _ORIGINAL_CONCEPT,
            "value": value,
            "unit": "shares",
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