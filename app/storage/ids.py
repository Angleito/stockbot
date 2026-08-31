"""Stable internal identity helpers.

Tickers and names change; they are aliases, never primary keys.  Every
entity, security, and document gets a stable ID derived from the source's
own stable identifier (SEC CIK, accession number, content hash).
"""

from __future__ import annotations

from datetime import datetime, timezone


def sec_entity_id(cik: int | str) -> str:
    """Stable SEC entity ID: sec:cik:0000320193."""
    return f"sec:cik:{int(cik):010d}"


def sec_security_id(cik: int | str) -> str:
    """Stable SEC security ID for the common-share class of an entity.

    SEC company facts do not carry exchange-traded identifiers, so the
    common equity security of a company is identified by its CIK until a
    dedicated securities provider is integrated.
    """
    return f"sec:equity:{int(cik):010d}"


def sec_fact_id(cik: int | str, accession: str, concept: str, period_end: str, value: object) -> str:
    """Deterministic fact ID so reruns never duplicate a normalized fact."""
    return f"sec:fact:{int(cik):010d}:{accession}:{concept}:{period_end}:{value}"


def finra_entity_id(symbol: str) -> str:
    """Provisional FINRA-only entity ID, used before the symbol resolves to
    an SEC CIK.  The authoritative entity ID is the SEC one once mapped."""
    return f"finra:symbol:{symbol.strip().upper()}"


def sec_doc_id(kind: str, key: str, content_hash: str) -> str:
    """Stable document ID for an archived raw payload."""
    return f"sec:doc:{kind}:{key}:{content_hash[:16]}"


def finra_doc_id(dataset: str, partition: tuple[str, ...], content_hash: str) -> str:
    """Stable document ID for an archived FINRA partition payload."""
    safe = dataset.replace("/", "__")
    return f"finra:doc:{safe}:{':'.join(partition)}:{content_hash[:16]}"


def run_id() -> str:
    """New run identity for a research run."""
    return f"run:{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"


def request_id() -> str:
    """New request identity for a research request."""
    return f"req:{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}"
