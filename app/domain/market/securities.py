"""Stockbot-owned security identity domain models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Security:
    security_id: str
    entity_id: str
    security_type: str
    ticker: str | None
    exchange: str | None
    source: str
    known_at: str | None
    retrieved_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> "Security":
        return cls(
            security_id=str(row["security_id"]),
            entity_id=str(row["entity_id"]),
            security_type=str(row["security_type"]),
            ticker=row.get("ticker"),
            exchange=row.get("exchange"),
            source=row.get("source"),
            known_at=row.get("known_at"),
            retrieved_at=row.get("retrieved_at"),
        )


@dataclass(frozen=True)
class TickerAlias:
    alias_type: str
    alias_value: str
    entity_id: str
    security_id: str | None
    source: str
    valid_from: str | None
    valid_to: str | None
    known_at: str | None
    retrieved_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> "TickerAlias":
        return cls(
            alias_type=str(row["alias_type"]),
            alias_value=str(row["alias_value"]),
            entity_id=str(row["entity_id"]),
            security_id=row.get("security_id"),
            source=row.get("source"),
            valid_from=row.get("valid_from"),
            valid_to=row.get("valid_to"),
            known_at=row.get("known_at"),
            retrieved_at=row.get("retrieved_at"),
        )


@dataclass(frozen=True)
class SecurityResolution:
    security_id: str | None
    entity_id: str | None
    ticker: str
    resolved: bool
    resolution_method: str
