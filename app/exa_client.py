"""Exa web-search client: optional external research evidence provider.

Bounded, highlight-based web search for current qualitative evidence (news,
announcements, competitive/industry developments, publications, commentary).
Exa is optional: every call returns an error dict when disabled or on any
failure — it never raises, and the agent treats failures as soft. Results are
research evidence, never canonical financial records.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import config

logger = logging.getLogger(__name__)

EXA_TIMEOUT_SECONDS = 20
EXA_HIGHLIGHT_MAX_CHARS = 600
EXA_DEFAULT_LIMIT = 5
EXA_MAX_LIMIT = 25
_APPROVED_SEARCH_TYPES = frozenset({"auto", "fast", "deep-lite"})
_APPROVED_CATEGORIES = frozenset({"news", "company", "publication", "financial report"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_session: requests.Session | None = None


def _ensure_session() -> requests.Session:
    """Lazily created, shared session (mirrors analyst_client)."""
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _error(message: str) -> dict:
    return {"error": message, "source": "exa"}


def search(
    query: str,
    *,
    category: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    start_published_date: str | None = None,
    end_published_date: str | None = None,
    search_type: str = "auto",
    limit: int = 5,
) -> dict:
    """Search the web via Exa, returning bounded highlight-based evidence.

    Never raises; every failure returns {"error": ..., "source": "exa"}.
    """
    if not config.exa_enabled() or not config.get_exa_api_key():
        return _error("Exa search unavailable")
    if not isinstance(query, str) or not query.strip():
        return _error("Exa search query must be a non-empty string")
    if search_type not in _APPROVED_SEARCH_TYPES:
        return _error(
            f"Unsupported search_type '{search_type}'. Allowed: auto, fast, deep-lite"
        )
    if category is not None and category not in _APPROVED_CATEGORIES:
        return _error(
            f"Unsupported category '{category}'. Allowed: news, company, publication, financial report"
        )
    for label, value in (
        ("start_published_date", start_published_date),
        ("end_published_date", end_published_date),
    ):
        if value is not None and not _DATE_RE.match(value):
            return _error(f"Invalid date '{value}'. Expected YYYY-MM-DD")
    if limit is None:
        limit = EXA_DEFAULT_LIMIT
    if not isinstance(limit, int):
        return _error("limit must be an integer")
    limit = max(1, min(limit, EXA_MAX_LIMIT))

    # Exa returns HTTP 400 for date/domain-exclusion params with
    # category=company; reject instead of silently dropping filters so the
    # caller learns the request cannot be honored (include_domains is fine).
    if (
        category == "company"
        and (start_published_date or end_published_date or exclude_domains)
    ):
        return _error(
            "category 'company' does not support start_published_date, "
            "end_published_date, or exclude_domains"
        )

    payload: dict = {
        "query": query,
        "numResults": limit,
        "contents": {"highlights": True},
    }
    if category is not None:
        payload["category"] = category
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    if start_published_date is not None:
        payload["startPublishedDate"] = f"{start_published_date}T00:00:00.000Z"
    if end_published_date is not None:
        payload["endPublishedDate"] = f"{end_published_date}T23:59:59.999Z"
    payload["type"] = search_type

    retrieved_at = datetime.now(timezone.utc).isoformat()
    try:
        response = _ensure_session().post(
            f"{config.EXA_API_BASE}/search",
            headers={"x-api-key": config.get_exa_api_key()},
            json=payload,
            timeout=EXA_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return _error("Exa search timed out")
    except requests.RequestException:
        return _error("Exa search unavailable")
    if response.status_code != 200:
        return _error(f"Exa search failed: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return _error("Exa search returned an invalid response")
    raw_results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(raw_results, list):
        return _error("Exa search returned an invalid response")

    evidence: list[dict] = []
    for item in raw_results:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        highlights = item.get("highlights")
        first_highlight = (
            highlights[0] if isinstance(highlights, list) and highlights else None
        )
        highlight = (
            str(first_highlight)[:EXA_HIGHLIGHT_MAX_CHARS]
            if first_highlight is not None
            else ""
        )
        evidence.append({
            "title": str(item.get("title") or ""),
            "url": item["url"],
            "source_domain": urlparse(item["url"]).netloc,
            "published_at": item.get("publishedDate"),
            "retrieved_at": retrieved_at,
            "highlight": highlight,
            "category": category,
        })

    return {
        "result_type": "web_search",
        "query": query,
        "search_type": search_type,
        "evidence": evidence,
        "omitted_count": max(0, len(raw_results) - len(evidence)),
        "row_count": len(evidence),
        "source": "exa",
        "retrieved_at": retrieved_at,
    }
