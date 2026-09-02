"""Pure point-in-time ticker-to-identity resolution.

Reproduces the entity_aliases resolution semantics — visibility, entity
ambiguity, newest-instant selection, security-id ambiguity — with no
storage dependency.  Storage hands over candidate rows via
``ticker_alias_candidates`` and the row mapper materializes fields; the
resolver derives sec:cik security ids itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from .ids import sec_security_id
from .securities import SecurityResolution, TickerAlias

_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a persisted ISO-8601 value to an aware UTC datetime.

    ``Z`` becomes ``+00:00``; naive values (date-only strings parse to
    midnight) are treated as UTC.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _instant(alias: TickerAlias) -> tuple[datetime, datetime]:
    """Newest-instant key ``(known_at, retrieved_at)``; NULLs sort last."""
    return (
        _parse_iso(alias.known_at) or _NEVER,
        _parse_iso(alias.retrieved_at) or _NEVER,
    )


def _resolved_security_id(alias: TickerAlias) -> str | None:
    """The alias's effective security id, deriving sec:cik entities."""
    if alias.security_id is not None:
        return alias.security_id
    if alias.entity_id.startswith("sec:cik:"):
        return sec_security_id(int(alias.entity_id[len("sec:cik:"):]))
    return None


def resolve_ticker_aliases(
    ticker: str,
    aliases: Sequence[TickerAlias],
    *,
    as_of: datetime,
) -> SecurityResolution:
    """Resolve a ticker to a Stockbot security/entity identity.

    Point-in-time over the given aliases: an alias is visible only when
    ``known_at <= as_of`` (timestamp precision, both normalized to UTC) AND
    its half-open validity interval ``[valid_from, valid_to)`` contains
    ``as_of`` (``NULL`` bounds are unbounded; date-only values are midnight,
    so ``valid_to="2026-08-25"`` is expired on the 25th).  Multiple active
    entities for one ticker resolve as ``"ambiguous"`` — never
    source/order-wins.  Rows tied at the newest known_at/retrieved_at instant
    that carry conflicting security ids for one entity also resolve as
    ``"ambiguous"`` — never an arbitrary row pick.  A row without an
    explicit security id inherits the entity's derived common-equity id
    (``sec:equity:<cik>`` for ``sec:cik:`` entities) before comparison, so
    explicit and derived ids for the same security never conflict.  Older
    revisions never create ambiguity; the newest record wins.  No mappings
    are ever invented: unknown tickers resolve to ``resolved=False``.
    """
    if not isinstance(as_of, datetime):
        raise TypeError(f"as_of must be a timezone-aware datetime, got {type(as_of).__name__}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    visible = [
        alias for alias in aliases
        if (known := _parse_iso(alias.known_at)) is not None
        and known <= as_of
        and (alias.valid_from is None or _parse_iso(alias.valid_from) <= as_of)
        and (alias.valid_to is None or _parse_iso(alias.valid_to) > as_of)
    ]
    if not visible:
        return SecurityResolution(None, None, ticker, False, "unresolved")
    entities = list(dict.fromkeys(alias.entity_id for alias in visible))
    if len(entities) > 1:
        return SecurityResolution(None, None, ticker, False, "ambiguous")
    newest_key = max(_instant(alias) for alias in visible)
    newest = [alias for alias in visible if _instant(alias) == newest_key]
    distinct_security_ids = {_resolved_security_id(alias) for alias in newest}
    if len(distinct_security_ids) > 1:
        return SecurityResolution(None, entities[0], ticker, False, "ambiguous")
    first = newest[0]
    return SecurityResolution(_resolved_security_id(first), first.entity_id, ticker, True, "entity_alias")
