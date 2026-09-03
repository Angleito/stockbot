"""Corporate-event, evidence, and claim domain models (provider-free).

A ``CorporateEvent`` is a material, filing-disclosed fact about a company
(e.g. a supply commitment, an 8-K guarantee); ``Evidence`` anchors an event
to archived filing text or an XBRL fact; ``Claim`` is a higher-order
assertion built from evidence (schema defined here; no writer yet).

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


@dataclass(frozen=True)
class Claim:
    claim_id: str
    entity_id: str | None
    ticker: str | None
    claim_type: str | None
    statement: str | None
    known_at: str | None
    retrieved_at: str | None
    source: str | None
    content_hash: str | None
    parser_version: str | None


def sec_event_id(ticker: str, content_hash: str) -> str:
    """Deterministic event id from the source row's content hash."""
    return f"sec:event:{ticker}:{content_hash[:16]}"


def sec_evidence_id(event_id: str, content_hash: str) -> str:
    """Deterministic evidence id scoped to its owning event."""
    return f"sec:evidence:{event_id}:{content_hash[:16]}"


def sec_claim_id(content_hash: str) -> str:
    """Deterministic claim id from the claim content hash."""
    return f"sec:claim:{content_hash[:16]}"
