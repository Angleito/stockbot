"""Stable Stockbot identity derivation rules (entity/security/doc ids)."""

from __future__ import annotations


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


def sec_dividend_event_id(
    cik: int | str,
    amount: float,
    record_date: str | None,
    payment_date: str | None,
    dividend_type: str,
    accession: str | None = None,
    declaration_date: str | None = None,
) -> str:
    """Deterministic dividend-event ID: reruns dedup, amended amounts differ."""
    event_id = (
        f"sec:divevt:{int(cik):010d}:{float(amount):.4f}"
        f":{record_date or 'norec'}:{payment_date or 'nopay'}:{dividend_type}"
    )
    if not record_date and not payment_date:
        event_id += f":{accession}:{declaration_date or 'nodecl'}"
    return event_id


def finra_entity_id(symbol: str) -> str:
    """Provisional FINRA-only entity ID, used before the symbol resolves to
    an SEC CIK.  The authoritative entity ID is the SEC one once mapped."""
    return f"finra:symbol:{symbol.strip().upper()}"


def sec_doc_id(kind: str, key: str, content_hash: str) -> str:
    """Stable document ID for an archived raw payload."""
    return f"sec:doc:{kind}:{key}:{content_hash[:16]}"
