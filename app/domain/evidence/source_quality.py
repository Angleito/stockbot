"""Deterministic source classification. stdlib only; sole trust authority."""

from __future__ import annotations

from urllib.parse import urlparse

from app.security.context import Integrity

from .models import SourceClassification, SourceTier

_HIGH_TRUST = {
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "apnews.com": "Associated Press",
}


def _host(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0].rstrip(".")
    except Exception:
        return None
    return netloc or None


def _suffix_match(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def classify_source(url: str) -> SourceClassification:
    """Classify a source URL to (publisher, tier, integrity).

    Only reuters.com, bloomberg.com, and apnews.com elevate to
    HIGH_TRUST_NEWS/HIGH_TRUST_REPORTED. Everything else is
    UNKNOWN/EXTERNAL. Future company domains join via explicit-map
    edit, never label inference. Suffix-match on label boundaries
    so spoofs (evil-reuters.com, reuters.com.evil.com) never match.
    Malformed URL → UNKNOWN/EXTERNAL.
    """
    host = _host(url) if isinstance(url, str) else None
    if not host:
        return SourceClassification(None, SourceTier.UNKNOWN, Integrity.EXTERNAL)
    for domain, publisher in _HIGH_TRUST.items():
        if _suffix_match(host, domain):
            return SourceClassification(publisher, SourceTier.HIGH_TRUST_NEWS, Integrity.HIGH_TRUST_REPORTED)
    return SourceClassification(host, SourceTier.UNKNOWN, Integrity.EXTERNAL)
