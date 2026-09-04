"""Deterministic claim id + assembly. stdlib only."""

from __future__ import annotations

import hashlib
import re

from app.security.context import Integrity

from .models import ClaimType, EvidenceClaim, ResolutionStatus, SourceTier, coerce_claim_type


def _claim_type_value(claim_type: ClaimType | str) -> str:
    if isinstance(claim_type, ClaimType):
        return claim_type.value
    return coerce_claim_type(claim_type).value


def make_claim_id(
    source_url: str | None,
    claim_type: ClaimType | str,
    entity_id: str | None,
    text: str,
) -> str:
    """Deterministic id over (url|type|entity|normalized text)."""
    url = (source_url or "").strip().lower()
    ctype = _claim_type_value(claim_type)
    norm_text = re.sub(r"\s+", " ", text or "").strip().casefold()
    digest = hashlib.sha256(
        f"{url}|{ctype}|{entity_id or ''}|{norm_text}".encode()
    ).hexdigest()[:16]
    return f"exa:claim:{digest}"


def claim_content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def build_claim(
    *,
    entity_id: str | None,
    security_id: str | None,
    ticker: str | None,
    subject_name: str | None,
    claim_type: ClaimType | str,
    text: str,
    object_entity_id: str | None = None,
    object_name: str | None = None,
    event_at: str | None = None,
    published_at: str | None = None,
    retrieved_at: str | None = None,
    source_url: str | None = None,
    source_domain: str | None = None,
    publisher: str | None = None,
    source_tier: SourceTier = SourceTier.UNKNOWN,
    integrity: Integrity = Integrity.EXTERNAL,
    evidence_summary: str | None = None,
    confidence: str | None = None,
    reported_ticker: str | None = None,
    subject_resolution: ResolutionStatus = ResolutionStatus.UNRESOLVED,
    object_resolution: ResolutionStatus = ResolutionStatus.UNRESOLVED,
) -> EvidenceClaim:
    ctype = claim_type if isinstance(claim_type, ClaimType) else coerce_claim_type(claim_type)
    return EvidenceClaim(
        claim_id=make_claim_id(source_url, ctype, entity_id, text),
        entity_id=entity_id,
        security_id=security_id,
        ticker=ticker,
        subject_name=subject_name,
        claim_type=ctype,
        text=text,
        object_entity_id=object_entity_id,
        object_name=object_name,
        event_at=event_at,
        published_at=published_at,
        retrieved_at=retrieved_at,
        source_url=source_url,
        source_domain=source_domain,
        publisher=publisher,
        source_tier=source_tier,
        integrity=integrity,
        evidence_summary=evidence_summary,
        confidence=confidence,
        reported_ticker=reported_ticker,
        subject_resolution=subject_resolution,
        object_resolution=object_resolution,
    )
