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

    @classmethod
    def from_row(cls, row: dict) -> "Entity":
        return cls(
            entity_id=str(row["entity_id"]),
            name=row.get("name"),
            entity_type=row.get("entity_type"),
            sic=row.get("sic"),
            source=row.get("source"),
            known_at=row.get("known_at"),
            retrieved_at=row.get("retrieved_at"),
        )


@dataclass(frozen=True)
class EntityRelationship:
    relationship_type: str
    from_entity_id: str
    to_entity_id: str
    source: str
    known_at: str | None = None
