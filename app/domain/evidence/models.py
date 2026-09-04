"""Typed evidence claims (provider-free).

Stockbot owns identity/ontology; Exa stays an external evidence provider.
Fields are str|None like domain/events.py; no datetimes, no embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.security.context import Integrity


class SourceTier(StrEnum):
    CANONICAL = "canonical"
    HIGH_TRUST_NEWS = "high_trust_news"
    PRIMARY_SOURCE = "primary_source"
    ESTABLISHED_NEWS = "established_news"
    SPECIALIST = "specialist"
    COMMUNITY = "community"
    UNKNOWN = "unknown"

class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"

class ClaimType(StrEnum):
    CORPORATE_EVENT = "corporate_event"
    EARNINGS_COMMENTARY = "earnings_commentary"
    GUIDANCE = "guidance"
    PRODUCT_ANNOUNCEMENT = "product_announcement"
    PRODUCT_DELAY = "product_delay"
    PARTNERSHIP = "partnership"
    SUPPLIER_RELATIONSHIP = "supplier_relationship"
    CUSTOMER_RELATIONSHIP = "customer_relationship"
    ACQUISITION = "acquisition"
    ACQUISITION_TALK = "acquisition_talk"
    FINANCING = "financing"
    CAPITAL_EXPENDITURE = "capital_expenditure"
    REGULATORY_ACTION = "regulatory_action"
    LITIGATION = "litigation"
    MANAGEMENT_CHANGE = "management_change"
    DEMAND_SIGNAL = "demand_signal"
    SUPPLY_SIGNAL = "supply_signal"
    COMPETITIVE_DEVELOPMENT = "competitive_development"
    INDUSTRY_DEVELOPMENT = "industry_development"
    ANALYST_COMMENTARY = "analyst_commentary"
    OTHER = "other"


@dataclass(frozen=True)
class SourceClassification:
    publisher: str | None
    source_tier: SourceTier
    integrity: Integrity


@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: str
    entity_id: str | None
    security_id: str | None
    ticker: str | None
    subject_name: str | None
    claim_type: ClaimType
    text: str
    object_entity_id: str | None = None
    object_name: str | None = None
    event_at: str | None = None
    published_at: str | None = None
    retrieved_at: str | None = None
    source_url: str | None = None
    source_domain: str | None = None
    publisher: str | None = None
    source_tier: SourceTier = SourceTier.UNKNOWN
    integrity: Integrity = Integrity.EXTERNAL
    evidence_summary: str | None = None
    confidence: str | None = None
    reported_ticker: str | None = None
    subject_resolution: ResolutionStatus = ResolutionStatus.UNRESOLVED
    object_resolution: ResolutionStatus = ResolutionStatus.UNRESOLVED


def coerce_claim_type(v: object) -> ClaimType:
    """Coerce free-form reader output to a ClaimType; unknown → OTHER."""
    if isinstance(v, ClaimType):
        return v
    if not isinstance(v, str):
        return ClaimType.OTHER
    norm = v.strip().lower().replace(" ", "_").replace("-", "_")
    try:
        return ClaimType(norm)
    except ValueError:
        return ClaimType.OTHER
