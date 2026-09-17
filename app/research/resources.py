"""Canonical resource URI reader: evidence:// research:// job:// dossier:// freeze:// coverage://."""

from __future__ import annotations

from collections.abc import Mapping

__all__ = [
    "KNOWN_RESOURCE_NAMESPACES",
    "ResourceError",
    "ResourceNotFoundError",
    "parse_resource_uri",
    "read_resource",
]

KNOWN_RESOURCE_NAMESPACES = frozenset({"evidence", "research", "job", "dossier", "freeze", "coverage"})


class ResourceError(ValueError):
    """Malformed URI, unknown namespace, or no store wired."""


class ResourceNotFoundError(ResourceError):
    """Well-formed URI with no backing record."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        super().__init__(f"resource not found: {uri}")


def parse_resource_uri(uri: str) -> tuple[str, str]:
    """Split ``namespace://key``; research keys may carry a ``/section`` suffix."""
    scheme, sep, rest = uri.partition("://")
    namespace = scheme.strip().lower()
    key = rest.strip()
    if not sep or not key:
        raise ResourceError(f"malformed resource URI (want namespace://key): {uri!r}")
    if namespace not in KNOWN_RESOURCE_NAMESPACES:
        raise ResourceError(f"unknown resource namespace {namespace!r} in {uri!r}")
    return namespace, key


def read_resource(
    uri: str,
    *,
    evidence: Mapping[str, object] | None = None,
    freezes: Mapping[str, object] | None = None,
    dossiers: Mapping[str, object] | None = None,
    jobs: Mapping[str, object] | None = None,
    sessions: Mapping[str, object] | None = None,
    coverage: Mapping[str, object] | None = None,
) -> object:
    """Resolve one URI against injected id->record stores (no I/O, no sibling imports).

    ``research://<session_id>`` ignores any ``/section`` suffix and returns the session.
    ``coverage://<artifact_id>`` reads a session's search-coverage artifact (absence
    observation): inspection only, never a raw-document citation.
    """
    namespace, key = parse_resource_uri(uri)
    stores: dict[str, Mapping[str, object] | None] = {
        "evidence": evidence,
        "freeze": freezes,
        "dossier": dossiers,
        "job": jobs,
        "research": sessions,
        "coverage": coverage,
    }
    store = stores[namespace]
    if store is None:
        raise ResourceError(f"no {namespace} store wired for {uri!r}")
    lookup = key.split("/", 1)[0].strip() if namespace == "research" else key
    if not lookup or lookup not in store:
        raise ResourceNotFoundError(uri)
    return store[lookup]
