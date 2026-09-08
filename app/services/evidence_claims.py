"""Assemble reader items into typed EvidenceClaims. Deterministic; no LLM."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.domain.evidence.claims import build_claim, claim_content_hash
from app.domain.evidence.models import ClaimType, EvidenceClaim, ResolutionStatus, SourceClassification
from app.domain.evidence.source_quality import classify_source
from app.domain.market.securities import SecurityResolution, TickerAlias

from .evidence_resolution import resolve_subject


def _resolution(res: SecurityResolution) -> ResolutionStatus:
    if res.resolved:
        return ResolutionStatus.RESOLVED
    if res.resolution_method == "ambiguous":
        return ResolutionStatus.AMBIGUOUS
    return ResolutionStatus.UNRESOLVED


def _domain(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    try:
        host = urlparse(url).netloc.lower().split(":")[0].rstrip(".")
    except Exception:
        return None
    return host or None


def build_evidence_claims(
    *,
    reader_items: list[dict[str, object]],
    classify: Callable[[str], SourceClassification] = classify_source,
    resolve: Callable[..., SecurityResolution] | None = None,
    aliases_by_ticker: Callable[[str], Sequence[TickerAlias]] | None = None,
    name_to_ticker: Callable[[str], str | None] | None = None,
    as_of: datetime | None = None,
    data_root: Path | None = None,
    retrieved_fallback: str,
) -> list[EvidenceClaim]:
    """Classify + link each reader item; never guesses identity."""
    # ponytail: one loop over ≤20 items, no batching infra for this size
    if not isinstance(reader_items, list) or not reader_items:
        return []
    instant = as_of or datetime.now(timezone.utc)
    if aliases_by_ticker is None or name_to_ticker is None:
        from .evidence_resolution import warehouse_aliases_fn, warehouse_name_to_ticker

        aliases_by_ticker = aliases_by_ticker or warehouse_aliases_fn(instant, data_root)
        name_to_ticker = name_to_ticker or (lambda n: warehouse_name_to_ticker(n, data_root))

    def _resolve(ticker: str | None, name: str | None):
        if resolve is not None:
            return resolve(ticker=ticker, name=name, as_of=instant)
        return resolve_subject(
            ticker=ticker,
            name=name,
            aliases_by_ticker=aliases_by_ticker,
            name_to_ticker=name_to_ticker,
            as_of=instant,
        )

    claims: list[EvidenceClaim] = []
    for item in reader_items:
        if not isinstance(item, dict):
            continue
        source_url_raw = item.get("source_url")
        source_url: str | None = source_url_raw if isinstance(source_url_raw, str) else None
        classification = classify(source_url or "")
        subject_ticker = item.get("subject_ticker")
        subject_name_raw = item.get("subject_name")
        subject_name: str | None = subject_name_raw if isinstance(subject_name_raw, str) else None
        subj = _resolve(
            subject_ticker if isinstance(subject_ticker, str) else None,
            subject_name,
        )
        object_name_raw = item.get("object_name")
        object_name: str | None = object_name_raw if isinstance(object_name_raw, str) else None
        obj = _resolve(None, object_name)
        text_raw = item.get("claim") or ""
        text: str = text_raw if isinstance(text_raw, str) else ""
        retrieved_raw = item.get("retrieved_at") or retrieved_fallback
        retrieved_at: str = retrieved_raw if isinstance(retrieved_raw, str) else retrieved_fallback
        raw_claim_type = item.get("claim_type")
        if isinstance(raw_claim_type, ClaimType):
            claim_type_val: ClaimType | str = raw_claim_type
        elif isinstance(raw_claim_type, str) and raw_claim_type:
            claim_type_val = raw_claim_type
        else:
            claim_type_val = "other"
        event_raw = item.get("event_at")
        event_at: str | None = event_raw if isinstance(event_raw, str) else None
        published_raw = item.get("published_at")
        published_at: str | None = published_raw if isinstance(published_raw, str) else None
        domain_raw = item.get("source_domain")
        source_domain: str | None = (
            domain_raw if isinstance(domain_raw, str)
            else _domain(source_url)
        )
        summary_raw = item.get("evidence_summary")
        evidence_summary: str | None = summary_raw if isinstance(summary_raw, str) else None
        claims.append(
            build_claim(
                entity_id=subj.entity_id,
                security_id=subj.security_id,
                ticker=subj.ticker if subj.resolved else None,
                reported_ticker=(subject_ticker.strip().upper() if isinstance(subject_ticker, str) and subject_ticker.strip() else None),
                subject_resolution=_resolution(subj),
                object_resolution=_resolution(obj),
                subject_name=subject_name,
                claim_type=claim_type_val,
                text=text,
                object_entity_id=obj.entity_id,
                object_name=object_name,
                event_at=event_at,
                published_at=published_at,
                retrieved_at=retrieved_at,
                source_url=source_url,
                source_domain=source_domain,
                publisher=classification.publisher,
                source_tier=classification.source_tier,
                integrity=classification.integrity,
                evidence_summary=evidence_summary,
                confidence=None,
            )
        )
    return claims

def claim_to_enriched_dict(claim: EvidenceClaim) -> dict[str, object]:
    """EvidenceClaim → gateway/render/persist dict (enums as values)."""
    return {
        "claim_id": claim.claim_id,
        "content_hash": claim_content_hash(claim.text),
        "entity_id": claim.entity_id,
        "security_id": claim.security_id,
        "ticker": claim.ticker,
        "reported_ticker": claim.reported_ticker,
        "subject_resolution": claim.subject_resolution.value,
        "object_resolution": claim.object_resolution.value,
        "subject_name": claim.subject_name,
        "claim_type": claim.claim_type.value,
        "object_entity_id": claim.object_entity_id,
        "object_name": claim.object_name,
        "event_at": claim.event_at,
        "published_at": claim.published_at,
        "retrieved_at": claim.retrieved_at,
        "source_url": claim.source_url,
        "source_domain": claim.source_domain,
        "publisher": claim.publisher,
        "source_tier": claim.source_tier.value,
        "integrity": claim.integrity.value,
        "evidence_summary": claim.evidence_summary,
        "confidence": claim.confidence,
        # Back-compat for existing claim renderers/tests.
        "claim": claim.text,
        "text": claim.text,
    }
