"""Deterministic SEC/FINRA normalizers for the research data store.

Network-free and agent-free: raw payloads in, normalized rows out.
"""

from __future__ import annotations

from typing import Any, Optional

from .domain.market import ids

COMPANY_TICKERS_PARSER_VERSION = "sec-company-tickers-v1"
COMPANY_FACTS_PARSER_VERSION = "sec-companyfacts-v4"

SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
_ORIGINAL_CONCEPT = "dei:EntityCommonStockSharesOutstanding"
EPS_UNIT = "USD/shares"
EPS_CONCEPT_NAMES: tuple[str, ...] = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
DIVIDEND_PER_SHARE_CONCEPT = "CommonStockDividendsPerShareDeclared"

CANONICAL_CONCEPTS: dict[str, tuple[str, ...]] = {
    "Revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
    "NetIncomeLoss": ("NetIncomeLoss",),
    "CashAndCashEquivalents": ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"),
    "LongTermDebt": ("LongTermDebtCurrentAndNoncurrent", "LongTermDebtNoncurrent", "LongTermDebt"),
}


def normalize_sec_tickers(raw: Any, *, retrieved_at: str, content_hash: str) -> dict[str, list[dict]]:
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

def _extract_eps_facts(raw: Any) -> list[tuple[str, str, str, dict]]:
    """Per-share earnings facts, accepted only under the ``USD/shares`` unit."""
    entries: list[tuple[str, str, str, dict]] = []
    namespaces = raw.get("facts") or {}
    if not isinstance(namespaces, dict):
        return entries
    for namespace, concepts in namespaces.items():
        if not isinstance(concepts, dict):
            continue
        for tag, payload in concepts.items():
            if tag not in EPS_CONCEPT_NAMES:
                continue
            units = (payload or {}).get("units") or {}
            for fact in units.get(EPS_UNIT) or []:
                if isinstance(fact, dict):
                    entries.append((tag, f"{namespace}:{tag}", EPS_UNIT, fact))
    return entries


def _extract_dividend_facts(raw: Any) -> list[tuple[str, str, str, dict]]:
    """Declared dividend-per-share facts, accepted only under ``USD/shares``."""
    entries: list[tuple[str, str, str, dict]] = []
    namespaces = raw.get("facts") or {}
    if not isinstance(namespaces, dict):
        return entries
    for namespace, concepts in namespaces.items():
        if not isinstance(concepts, dict):
            continue
        for tag, payload in concepts.items():
            if tag != DIVIDEND_PER_SHARE_CONCEPT:
                continue
            units = (payload or {}).get("units") or {}
            for fact in units.get(EPS_UNIT) or []:
                if isinstance(fact, dict):
                    entries.append((tag, f"{namespace}:{tag}", EPS_UNIT, fact))
    return entries


def _extract_facts(raw: Any) -> list[tuple[str, str, str, dict]]:
    entries = [(SHARES_OUTSTANDING_CONCEPT, _ORIGINAL_CONCEPT, "shares", fact) for fact in _extract_shares_facts(raw)]
    entries.extend(_extract_canonical_facts(raw))
    entries.extend(_extract_eps_facts(raw))
    entries.extend(_extract_dividend_facts(raw))
    return entries


def normalize_sec_company_facts(
    raw: Any,
    *,
    retrieved_at: str,
    content_hash: str,
    source_url: str,
    source_record_id: str,
) -> dict[str, list[dict]]:
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
        start_raw = fact.get("start")
        period_start = str(start_raw) if start_raw else None
        try:
            fiscal_year = int(fact.get("fy"))
        except (TypeError, ValueError):
            fiscal_year = None
        fiscal_period = str(fact.get("fp") or "") or None
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
            "period_start": period_start,
            "fiscal_year": fiscal_year,
            "fiscal_period": fiscal_period,
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


SHORT_INTEREST_PARSER_VERSION = "finra-short-interest-v1"


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_finra_short_interest(
    rows: list[dict],
    *,
    settlement_date: str,
    known_at: str,
    retrieved_at: str,
    content_hash: str,
    source_url: str,
    source_record_id: str,
) -> dict[str, list[dict]]:
    short_interest: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbolCode") or "").strip().upper()
        if not symbol:
            continue
        short_position = _to_float(row.get("currentShortPositionQuantity"))
        if short_position is not None and short_position < 0:
            short_position = None
        entity_id = ids.finra_entity_id(symbol)
        # The row ID includes the snapshot content hash so a corrected source
        # payload becomes a NEW source version (new known_at) instead of
        # colliding with the original row in the dedupe.
        short_interest.append({
            "row_id": f"finra:row:{settlement_date}:{symbol}:{content_hash[:12]}",
            "entity_id": entity_id,
            "security_id": None,
            "symbol_code": symbol,
            "issue_name": str(row.get("issueName") or "").strip() or None,
            "settlement_date": settlement_date,
            "short_position": short_position,
            "prev_position": _to_float(row.get("previousShortPositionQuantity")),
            "avg_daily_volume": _to_float(row.get("averageDailyVolumeQuantity")),
            "days_to_cover": _to_float(row.get("daysToCoverQuantity")),
            "source_url": source_url,
            "source_record_id": source_record_id,
            "known_at": known_at,
            "retrieved_at": retrieved_at,
            "content_hash": content_hash,
            "parser_version": SHORT_INTEREST_PARSER_VERSION,
        })
    return {"short_interest": short_interest}
