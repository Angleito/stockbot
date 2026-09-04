"""Corporate-event and evidence domain models (provider-free).

A ``CorporateEvent`` is a material, filing-disclosed fact about a company
(e.g. a supply commitment, an 8-K guarantee); ``Evidence`` anchors an event
to archived filing text or an XBRL fact.

Fields are kept as strings/None exactly as the storage datasets persist them;
no storage, HTTP, or LLM imports — deterministic id builders only, matching
``app/domain/market/ids.py`` conventions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CorporateEvent:
    event_id: str
    entity_id: str | None
    security_id: str | None
    ticker: str | None
    event_type: str
    amount_billions: float | None
    certainty: str | None
    status: str | None
    revenue_matched: bool | None
    default_triggered: bool | None
    fiscal_year: str | None
    filed_at: str | None
    known_at: str | None
    retrieved_at: str | None
    accession: str | None
    source: str | None
    source_url: str | None
    content_hash: str | None
    parser_version: str | None
    schedule_json: str | None = None
    payment_timing_json: str | None = None
    agreement_key: str | None = None
    lifecycle_event: str | None = None
    schedule_component: bool | None = None
    headline_type: str | None = None


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    event_id: str
    source_type: str  # 'filing_text' | 'xbrl_fact'
    archive_key: str | None
    content_hash: str | None
    excerpt: str | None
    span_start: int | None
    span_end: int | None
    retrieved_at: str | None
    parser_version: str | None


def sec_event_id(ticker: str, content_hash: str) -> str:
    """Deterministic event id from the source row's content hash."""
    return f"sec:event:{ticker}:{content_hash[:16]}"


def sec_evidence_id(event_id: str, content_hash: str) -> str:
    """Deterministic evidence id scoped to its owning event."""
    return f"sec:evidence:{event_id}:{content_hash[:16]}"
