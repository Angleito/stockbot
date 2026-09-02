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


@dataclass(frozen=True)
class SecurityResolution:
    security_id: str | None
    entity_id: str | None
    ticker: str
    resolved: bool
    resolution_method: str
