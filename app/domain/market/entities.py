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
    relationship_type: str
    from_entity_id: str
    to_entity_id: str
    source: str
    known_at: str | None = None
