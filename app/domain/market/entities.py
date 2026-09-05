"""Stockbot-owned market entity domain models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Entity:
    entity_id: str
    name: str | None
    entity_type: str | None
    sic: str | None
    source: str
    known_at: str | None
    retrieved_at: str | None


@dataclass(frozen=True)
class EntityRelationship:
    """Verified-current projection of one relationship instance.

    ``relationship_type`` is open vocabulary normalized to lowercase
    snake case (raw label lives on the evidence workflow, not here).
    This row answers "what is currently believed", never how it was
    extracted: provenance stays in ``relationship_evidence`` and every
    transition in ``relationship_revisions``.
    """

    relationship_id: str
    relationship_type: str
    from_entity_id: str
    to_entity_id: str
    status: str = "verified"
    valid_from: str | None = None
    valid_to: str | None = None
    known_at: str | None = None
    current_revision_id: str | None = None
