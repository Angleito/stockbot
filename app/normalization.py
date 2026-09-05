"""Deterministic SEC/FINRA normalizers for the research data store.

Network-free and agent-free: raw payloads in, normalized rows out.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .domain.market import ids

COMPANY_TICKERS_PARSER_VERSION = "sec-company-tickers-v1"
COMPANY_FACTS_PARSER_VERSION = "sec-companyfacts-v5"
FILING_TEXT_PARSER_VERSION = "sec-filing-text-v1"

SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
_ORIGINAL_CONCEPT = "dei:EntityCommonStockSharesOutstanding"
EPS_UNIT = "USD/shares"
EPS_CONCEPT_NAMES: tuple[str, ...] = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
DIVIDEND_PER_SHARE_CONCEPT = "CommonStockDividendsPerShareDeclared"
DIVIDEND_EVENT_AMOUNT_CONCEPT = "DividendsPayableAmountPerShare"
DIVIDEND_EVENT_DECLARED_CONCEPT = "DividendsPayableDateDeclaredDayMonthAndYear"
DIVIDEND_EVENT_RECORD_CONCEPT = "DividendsPayableDateOfRecordDayMonthAndYear"
DIVIDEND_EVENT_PAYMENT_CONCEPT = "DividendPayableDateToBePaidDayMonthAndYear"
_DIVIDEND_EVENT_DATE_ROLES = {
    DIVIDEND_EVENT_DECLARED_CONCEPT: "declaration_date",
    DIVIDEND_EVENT_RECORD_CONCEPT: "record_date",
    DIVIDEND_EVENT_PAYMENT_CONCEPT: "payment_date",
}

CANONICAL_CONCEPTS: dict[str, tuple[str, ...]] = {
    "Revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
    "NetIncomeLoss": ("NetIncomeLoss",),
    "CashAndCashEquivalents": ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalentsAtCarryingValueIncludingDiscontinuedOperations"),
    "LongTermDebt": ("LongTermDebtCurrentAndNoncurrent", "LongTermDebtNoncurrent", "LongTermDebt"),
    "OperatingCashFlow": ("NetCashProvidedByUsedInOperatingActivities",),
    "CapEx": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "DividendsPaid": ("PaymentsOfDividendsCommonStock", "PaymentsOfDividends",),
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


def _extract_dividend_event_facts(
    raw: Any,
    *,
    cik: int,
    entity_id: str,
    security_id: str,
    source_url: str,
    retrieved_at: str,
    content_hash: str,
) -> list[dict]:
    """Declared-dividend events from structured XBRL facts; never infers dates."""
    del retrieved_at  # known_at is filed_at, never extraction time.
    namespaces = (raw.get("facts") if isinstance(raw, dict) else None) or {}
    if not isinstance(namespaces, dict):
        return []
    amounts: list[tuple[str, str, float, str, str]] = []
    dates: dict[tuple[str, str], dict[str, set[str]]] = {}
    for namespace, concepts in namespaces.items():
        if not isinstance(concepts, dict):
            continue
        for tag, payload in concepts.items():
            is_amount = tag == DIVIDEND_EVENT_AMOUNT_CONCEPT
            role = _DIVIDEND_EVENT_DATE_ROLES.get(tag)
            if not is_amount and role is None:
                continue
            units = (payload or {}).get("units") or {}
            if not isinstance(units, dict):
                continue
            if is_amount:
                unit_facts: list[tuple[str, list]] = [
                    (unit, facts) for unit, facts in units.items()
                    if isinstance(facts, list) and facts
                ]
                preferred = [u for u, _ in unit_facts if u == EPS_UNIT]
                # Contingency A: fall back to the sole observed unit for this concept.
                chosen = preferred[:1] if preferred else ([unit_facts[0][0]] if len(unit_facts) == 1 else [])
                for unit in chosen:
                    for fact in units.get(unit) or []:
                        if not isinstance(fact, dict):
                            continue
                        try:
                            amount = float(fact.get("val"))
                        except (TypeError, ValueError):
                            continue
                        accession, filed = str(fact.get("accn") or ""), str(fact.get("filed") or "")
                        if not accession or not filed:
                            continue
                        amounts.append((accession, filed, amount, unit, f"{namespace}:{tag}"))
            else:
                assert role is not None
                for facts in units.values():
                    if not isinstance(facts, list):
                        continue
                    for fact in facts:
                        if not isinstance(fact, dict):
                            continue
                        value = str(fact.get("val") or "").strip()
                        accession, filed = str(fact.get("accn") or ""), str(fact.get("filed") or "")
                        if not value or not accession or not filed:
                            continue
                        dates.setdefault((accession, filed), {}).setdefault(role, set()).add(value)
    groups: dict[tuple[str, str], list[tuple[float, str, str]]] = {}
    for accession, filed, amount, unit, source_concept in amounts:
        groups.setdefault((accession, filed), []).append((amount, unit, source_concept))
    events: list[dict] = []
    for (accession, filed) in sorted(groups):
        seen: dict[float, tuple[str, str]] = {}
        for amount, unit, source_concept in groups[(accession, filed)]:
            seen.setdefault(amount, (unit, source_concept))
        group_dates = dates.get((accession, filed), {})
        declaration = min(group_dates.get("declaration_date") or (), default=None)
        record = min(group_dates.get("record_date") or (), default=None)
        payments = sorted(group_dates.get("payment_date") or ()) or [None]
        for amount in sorted(seen):
            unit, source_concept = seen[amount]
            for payment in payments:
                events.append({
                    "dividend_event_id": ids.sec_dividend_event_id(
                        cik, amount, record, payment, "unknown", accession, declaration),
                    "entity_id": entity_id,
                    "security_id": security_id,
                    "ticker": None,
                    "amount_per_share": amount,
                    "currency": unit.split("/")[0] if "/" in unit else unit,
                    "dividend_type": "unknown",
                    "declaration_date": declaration,
                    "record_date": record,
                    "payment_date": payment,
                    "ex_dividend_date": None,
                    "ex_dividend_date_source": "unknown",
                    "status": "unknown",
                    "source_form": None,
                    "accession": accession,
                    "filed_at": filed,
                    "known_at": filed,
                    "source_url": source_url,
                    "source_concept": source_concept,
                    "source_type": "structured_xbrl",
                    "evidence_excerpt": None,
                    "content_hash": content_hash,
                    "parser_version": COMPANY_FACTS_PARSER_VERSION,
                })
    return events


_DIVIDEND_TEXT_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}
_DIVIDEND_DECLARE_RE = re.compile(
    r"declared\s+an?\s+"
    r"(?:(quarterly|monthly|semiannual|annual|special|supplemental|extraordinary)\s+)?"
    r"(?:cash\s+)?dividend\s+of\s+\$(?P<amount>\d[\d,]*(?:\.\d+)?)\s+per\s+share",
    re.IGNORECASE,
)
_DIVIDEND_DATE_RES = {
    "payment_date": re.compile(r"payable\s+(?:on\s+)?(?P<date>[A-Za-z]+\.?\s+\d{1,2},\s*\d{4})", re.IGNORECASE),
    "record_date": re.compile(
        r"(?:shareholders|stockholders)\s+of\s+record\s+(?:on\s+|as\s+of\s+)?"
        r"(?P<date>[A-Za-z]+\.?\s+\d{1,2},\s*\d{4})", re.IGNORECASE),
    "ex_dividend_date": re.compile(
        r"ex[-\s]?dividend\s+(?:date\s+)?(?:of\s+|on\s+|is\s+)?"
        r"(?P<date>[A-Za-z]+\.?\s+\d{1,2},\s*\d{4})", re.IGNORECASE),
}


def _parse_dividend_text_date(value: str) -> Optional[str]:
    match = re.match(r"([A-Za-z]+)\.?\s+(\d{1,2}),\s*(\d{4})", value.strip())
    if not match:
        return None
    month = _DIVIDEND_TEXT_MONTHS.get(match.group(1)[:3].lower())
    day = int(match.group(2))
    if month is None or not 1 <= day <= 31:
        return None
    return f"{match.group(3)}-{month}-{day:02d}"


def _extract_dividend_events_from_text(
    text: str,
    *,
    cik: int,
    entity_id: str,
    security_id: str,
    accession: str,
    filed_at: str,
    source_url: str,
    content_hash: str,
) -> list[dict]:
    """Proposed dividend events from filing prose; amount mandatory, dates optional."""
    events: list[dict] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        sentence = sentence.strip()
        match = _DIVIDEND_DECLARE_RE.search(sentence)
        if not match:
            continue
        try:
            amount = float(match.group("amount").replace(",", ""))
        except (TypeError, ValueError):
            continue
        lowered = sentence.lower()
        if "supplemental" in lowered:
            dividend_type = "supplemental"
        elif "special" in lowered or "extraordinary" in lowered:
            dividend_type = "special"
        else:
            dividend_type = "regular"
        found = {key: _parse_dividend_text_date(m.group("date")) for key, m in
                 ((key, rx.search(sentence)) for key, rx in _DIVIDEND_DATE_RES.items()) if m}
        record = found.get("record_date")
        payment = found.get("payment_date")
        ex_date = found.get("ex_dividend_date")
        events.append({
            "dividend_event_id": ids.sec_dividend_event_id(
                cik, amount, record, payment, dividend_type, accession, None),
            "entity_id": entity_id,
            "security_id": security_id,
            "ticker": None,
            "amount_per_share": amount,
            "currency": "USD",
            "dividend_type": dividend_type,
            "declaration_date": None,
            "record_date": record,
            "payment_date": payment,
            "ex_dividend_date": ex_date,
            "ex_dividend_date_source": "explicit" if ex_date else "unknown",
            "status": "unknown",
            "source_form": None,
            "accession": accession,
            "filed_at": filed_at,
            "known_at": filed_at,
            "source_url": source_url,
            "source_concept": None,
            "source_type": "filing_text",
            "evidence_excerpt": sentence,
            "content_hash": content_hash,
            "parser_version": FILING_TEXT_PARSER_VERSION,
        })
    return events


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
    dividend_events = _extract_dividend_event_facts(
        raw, cik=cik, entity_id=entity_id, security_id=security_id,
        source_url=source_url, retrieved_at=retrieved_at, content_hash=content_hash,
    )
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
    return {"documents": documents, "financial_facts": financial_facts,
            "securities": securities, "dividend_events": dividend_events}


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
