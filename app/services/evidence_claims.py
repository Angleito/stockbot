"""Assemble reader items into typed EvidenceClaims. Deterministic; no LLM."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.domain.evidence.claims import build_claim, claim_content_hash
from app.domain.evidence.models import EvidenceClaim
from app.domain.evidence.source_quality import classify_source
from app.domain.market.securities import TickerAlias

from .evidence_resolution import resolve_subject


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
    reader_items: list[dict],
    classify: Callable[[str], object] = classify_source,  # type: ignore[assignment]
    resolve=None,
    aliases_by_ticker: Callable[[str], Sequence[TickerAlias]] | None = None,
    name_to_ticker: Callable[[str], str | None] | None = None,
    as_of: datetime | None = None,
    retrieved_fallback: str,
) -> list[EvidenceClaim]:
    """Classify + link each reader item; never guesses identity."""
    # ponytail: one loop over ≤20 items, no batching infra for this size
    if not isinstance(reader_items, list) or not reader_items:
        return []
    instant = as_of or datetime.now(timezone.utc)
    if aliases_by_ticker is None or name_to_ticker is None:
        from .evidence_resolution import warehouse_aliases_fn, warehouse_name_to_ticker

        aliases_by_ticker = aliases_by_ticker or warehouse_aliases_fn(instant)
        name_to_ticker = name_to_ticker or (lambda n: warehouse_name_to_ticker(n))

    def _resolve(ticker: str | None, name: str | None):
        if resolve is not None:
            return resolve(ticker=ticker, name=name, as_of=instant)
        return resolve_subject(
            ticker=ticker,
            name=name,
            aliases_by_ticker=aliases_by_ticker,  # type: ignore[arg-type]
            name_to_ticker=name_to_ticker,  # type: ignore[arg-type]
            as_of=instant,
        )

    claims: list[EvidenceClaim] = []
    for item in reader_items:
        if not isinstance(item, dict):
            continue
        source_url = item.get("source_url")
        classification = classify(source_url or "")  # type: ignore[operator]
        subject_ticker = item.get("subject_ticker")
        subject_name = item.get("subject_name")
        subj = _resolve(
            subject_ticker if isinstance(subject_ticker, str) else None,
            subject_name if isinstance(subject_name, str) else None,
        )
        object_name = item.get("object_name")
        obj = _resolve(None, object_name if isinstance(object_name, str) else None)
        text = item.get("claim") or ""
        retrieved_at = item.get("retrieved_at") or retrieved_fallback
        claims.append(
            build_claim(
                entity_id=subj.entity_id,
                security_id=subj.security_id,
                ticker=(subj.ticker if subj.resolved else None)
                or (
                    subject_ticker.strip().upper()
                    if isinstance(subject_ticker, str) and subject_ticker.strip()
                    else None
                ),
                subject_name=subject_name if isinstance(subject_name, str) else None,
                claim_type=item.get("claim_type") or "other",
                text=text if isinstance(text, str) else "",
                object_entity_id=obj.entity_id,
                object_name=object_name if isinstance(object_name, str) else None,
                event_at=item.get("event_at") if isinstance(item.get("event_at"), str) else None,
                published_at=item.get("published_at") if isinstance(item.get("published_at"), str) else None,
                retrieved_at=retrieved_at if isinstance(retrieved_at, str) else retrieved_fallback,
                source_url=source_url if isinstance(source_url, str) else None,
                source_domain=item.get("source_domain")
                if isinstance(item.get("source_domain"), str)
                else _domain(source_url if isinstance(source_url, str) else None),
                publisher=classification.publisher,  # type: ignore[attr-defined]
                source_tier=classification.source_tier,  # type: ignore[attr-defined]
                integrity=classification.integrity,  # type: ignore[attr-defined]
                evidence_summary=item.get("evidence_summary")
                if isinstance(item.get("evidence_summary"), str)
                else None,
                confidence=None,
            )
        )
    return claims


def claim_to_enriched_dict(claim: EvidenceClaim) -> dict:
    """EvidenceClaim → gateway/render/persist dict (enums as values)."""
    return {
        "claim_id": claim.claim_id,
        "content_hash": claim_content_hash(claim.text),
        "entity_id": claim.entity_id,
        "security_id": claim.security_id,
        "ticker": claim.ticker,
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
