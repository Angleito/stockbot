"""Deterministic entity linking for evidence claims.

Sole linking path is ``resolve_ticker_aliases`` — no second implementation.
Ticker candidates arrive from providers through the ``SourceGateway`` seam;
name matching maps an exact name to a ticker, then resolves through the
same ticker path. Never guesses: unresolved/ambiguous keep IDs None.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from app.domain.market.identity import resolve_ticker_aliases
from app.domain.market.securities import SecurityResolution, TickerAlias


def resolve_subject(
    *,
    ticker: str | None,
    name: str | None,
    aliases_by_ticker: Callable[[str], Sequence[TickerAlias]],
    name_to_ticker: Callable[[str], str | None],
    as_of: datetime,
) -> SecurityResolution:
    """Resolve a claim subject (or object) to Stockbot identity."""
    if ticker is not None and ticker.strip():
        t = ticker.strip().upper()
        return resolve_ticker_aliases(t, aliases_by_ticker(t), as_of=as_of)
    if name is not None and name.strip():
        mapped = name_to_ticker(name.strip())
        if not mapped or not mapped.strip():
            return SecurityResolution(None, None, name.strip(), False, "unresolved")
        t = mapped.strip().upper()
        return resolve_ticker_aliases(t, aliases_by_ticker(t), as_of=as_of)
    return SecurityResolution(None, None, (ticker or name or "").strip(), False, "unresolved")
