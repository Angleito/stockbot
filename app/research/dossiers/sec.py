"""Canonical SEC dossier: validated findings, commercial relationships, and aliases over frozen evidence. stdlib only."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.research.models import NO_CUTOFF_AS_OF

__all__ = [
    "ALIAS_SOURCES",
    "COMMERCIAL_RELATIONSHIP_TYPES",
    "CONCEPT_FAMILIES",
    "COVERAGE_KEYS",
    "MATERIALITY_FACTORS",
    "MATERIALITY_LEVELS",
    "DossierIntegrityError",
    "SECDossier",
    "create_dossier",
    "default_coverage",
    "dossier_to_dict",
    "should_expand_entity",
    "validate_dossier",
]

COVERAGE_KEYS = ("entities", "forms", "time_range", "sources_examined", "complete", "exclusions")
COMMERCIAL_RELATIONSHIP_TYPES = (
    "investor_in",
    "customer_of",
    "supplier_to",
    "cloud_provider_to",
    "has_receivable_from",
    "purchase_commitment_to",
    "capacity_commitment_to",
    "warrant_issued_to",
    "material_contract_with",
)
"""Known commercial/economic types; any other non-empty type uses the open-vocabulary fallback."""

MATERIALITY_LEVELS = ("critical", "high", "medium", "low")
"""Follow-up routing only, never a score: critical/high/medium may expand, low is record-only."""

MATERIALITY_FACTORS = (
    "dollar_amount",
    "revenue_pct",
    "concentration",
    "balance_sheet_footprint",
    "contract_capacity_size",
    "dependency",
    "impairment_default_exposure",
)
"""What a grader weighs when setting materiality; guidance, not model inputs."""

ALIAS_SOURCES = ("sec_document", "entity_metadata", "grounded_evidence")
"""Only allowed alias origins; model guesses and ungrounded text are rejected."""

CONCEPT_FAMILIES = {
    "ownership_investment": ("investor_in", "beneficial_ownership", "stake", "equity_interest"),
    "revenue_customer": ("customer_of", "revenue", "customer", "sales"),
    "receivable_credit": ("has_receivable_from", "receivable", "credit_exposure"),
    "supplier": ("supplier_to", "supplier", "vendor", "supply_chain"),
    "purchase_capacity_commitment": (
        "purchase_commitment_to", "capacity_commitment_to", "commitment", "take_or_pay",
    ),
    "guarantee": ("guarantee", "guarantor", "indemnity"),
    "debt_financing": ("debt", "financing", "loan", "credit_facility", "notes_payable"),
    "warrant_equity_linked": ("warrant_issued_to", "warrant", "convertible", "equity_linked"),
    "license_ip": ("license", "intellectual_property", "royalty", "patent"),
    "revenue_share": ("revenue_share", "profit_share", "royalties"),
    "concentration": ("concentration", "major_customer", "customer_concentration"),
    "dependency": ("dependency", "sole_source", "key_supplier", "cloud_provider_to"),
    "termination_default": ("termination", "default", "breach", "covenant", "impairment"),
}
"""Shared extraction/search vocabulary as data: any scout maps filing language to
these families instead of embedding its own concept list."""


def dossier_to_dict(dossier: SECDossier) -> dict[str, object]:
    """Immutable payload for the dossier table; datetimes as ISO, created_at stamped."""
    return {
        "dossier_id": dossier.dossier_id,
        "session_id": dossier.session_id,
        "wave_id": dossier.wave_id,
        "subject": dossier.subject,
        "coverage": deepcopy(dossier.coverage),
        "findings": [dict(finding) for finding in dossier.findings],
        "relationships": [dict(relationship) for relationship in dossier.relationships],
        "aliases": [dict(alias) for alias in dossier.aliases],
        "supporting_evidence_ids": list(dossier.supporting_evidence_ids),
        "contradicting_evidence_ids": list(dossier.contradicting_evidence_ids),
        "unknowns": list(dossier.unknowns),
        "limitations": list(dossier.limitations),
        "open_questions": list(dossier.open_questions),
        "as_of": dossier.as_of.isoformat() if dossier.as_of else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


class DossierIntegrityError(ValueError):
    """Dangling evidence ref, broken coverage contract, or bad wave id."""


def default_coverage() -> dict[str, object]:
    """Empty coverage: explicitly incomplete until a scout fills it.

    ``resolved``/``partially_resolved``/``unresolved`` track SEC-answerable
    question state (SEC vs overall sufficiency stays separate: a dossier can
    resolve its SEC slice while the overall question needs non-SEC sources);
    ``source_limitations`` records source-scoped gaps; unknowns survive to the
    freeze via ``unknowns``/``open_questions``. Negative-evidence keys
    (forms/dates/partitions/docs/gaps + complete) scope every no-hit claim:
    a non-exhaustive negative stays scoped, never universal.
    """
    return {
        "entities": [],
        "forms": [],
        "time_range": {"start": None, "end": None},
        "sources_examined": [],
        "complete": False,
        "exclusions": [],
        "resolved": [],
        "partially_resolved": [],
        "unresolved": [],
        "source_limitations": [],
        "dates": [],
        "partitions": [],
        "docs": [],
        "gaps": [],
    }


@dataclass(frozen=True)
class SECDossier:
    """One validated SEC slice; refs must resolve against the ledger at validate time. Commercial links live in ``relationships`` (SEC-sourced, evidence-cited); session aliases live in ``aliases``."""

    dossier_id: str
    session_id: str
    wave_id: int
    subject: str = ""
    coverage: dict[str, object] = field(default_factory=default_coverage)
    findings: list[dict[str, object]] = field(default_factory=list)
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    as_of: datetime | None = None
    relationships: list[dict[str, object]] = field(default_factory=list)
    aliases: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.wave_id, bool) or not isinstance(self.wave_id, int):
            raise DossierIntegrityError(f"dossier {self.dossier_id}: 'wave_id' must be an int")


def _coerce_wave(wave_id: int | str) -> int:
    if isinstance(wave_id, bool):
        raise DossierIntegrityError(f"dossier: 'wave_id' must be an int, got {wave_id!r}")
    if isinstance(wave_id, int):
        return wave_id
    text = wave_id.strip()
    if text.isdigit():
        return int(text)
    raise DossierIntegrityError(f"dossier: 'wave_id' must be an int, got {wave_id!r}")


def _coerce_as_of(value: datetime | str | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip().lower() in NO_CUTOFF_AS_OF:
        return None  # the unbounded sentinel means "no PIT cutoff", not an ISO instant
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DossierIntegrityError(f"dossier: 'as_of' must be ISO-8601, got {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _ground_findings(
    findings: Sequence[Mapping[str, object]], dossier_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validated findings + derived supporting set (order-stable, deduped)."""
    grounded: list[dict[str, object]] = []
    supporting: list[str] = []
    for finding in findings:
        text, uniq = _validate_finding_shape(finding, dossier_id)
        grounded.append({"text": text, "evidence_ids": uniq})
        for eid in uniq:
            if eid not in supporting:
                supporting.append(eid)
    return grounded, supporting
def _nonempty_str(value: object) -> str | None:
    """Stripped text or None (non-strings and blanks collapse to one arm)."""
    text = value.strip() if isinstance(value, str) else None
    return text or None


def _relationship_evidence_ids(ids: object) -> list[str] | None:
    """Deduped evidence ids or None (shape failure collapses `or`/`any` into one `all`)."""
    if not isinstance(ids, list) or not ids or not all(isinstance(e, str) and e for e in ids):
        return None
    return list(dict.fromkeys(ids))


def _validate_finding_shape(finding: Mapping[str, object], dossier_id: str) -> tuple[str, list[str]]:
    """Single finding text + deduped evidence ids (raises on free-text)."""
    text = _nonempty_str(finding.get("text"))
    if text is None:
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'text' must be a non-empty string")
    uniq = _relationship_evidence_ids(finding.get("evidence_ids"))
    if uniq is None:
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'evidence_ids' must be a non-empty list of strings")
    return text, uniq


def _relationship_verification(raw: object) -> dict[str, object] | None:
    """Default observed verification or None (bad status collapses to one arm)."""
    if raw is None:
        return {"status": "observed_from_filing"}
    return dict(raw) if isinstance(raw, Mapping) and raw.get("status") == "observed_from_filing" else None

def _relationship_fields(rel: Mapping[str, object], rid: str, dossier_id: str) -> tuple[object, object]:
    """Flat table over the three entity fields (one loop, one raise site); returns (materiality, attributes)."""
    for key in ("from_entity", "to_entity", "relationship_type"):
        if _nonempty_str(rel.get(key)) is None:
            raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} {key!r} must be a non-empty string")
    materiality = rel.get("materiality")
    if materiality is not None and materiality not in MATERIALITY_LEVELS:
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} 'materiality' must be one of {list(MATERIALITY_LEVELS)}")
    attributes = rel.get("attributes")
    if attributes is not None and not isinstance(attributes, Mapping):
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} 'attributes' must be a mapping")
    return materiality, attributes


def _validate_relationship_shape(
    rel: object, dossier_id: str,
) -> tuple[dict[str, object], list[str]]:
    """One commercial record + deduped evidence ids (raises on ungrounded links).

    Beside beneficial-ownership rows (app/sec ownership paths): this record covers
    commercial/economic links (e.g. MSFT ``investor_in``/``cloud_provider_to`` OpenAI).
    Known types live in ``COMMERCIAL_RELATIONSHIP_TYPES``; any other non-empty
    type is kept verbatim as the open-vocabulary fallback.
    """
    if not isinstance(rel, Mapping):
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship must be a mapping")
    raw_rid = rel.get("relationship_id")
    if not isinstance(raw_rid, str) or not raw_rid.strip():
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship 'relationship_id' must be a non-empty string")
    rid: str = raw_rid
    materiality, attributes = _relationship_fields(rel, rid, dossier_id)
    if rel.get("source") != "SEC":
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} 'source' must be 'SEC'")
    uniq = _relationship_evidence_ids(rel.get("evidence_ids"))
    if uniq is None:
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} 'evidence_ids' must be a non-empty list of strings")
    verification = _relationship_verification(rel.get("verification"))
    if verification is None:
        raise DossierIntegrityError(f"dossier {dossier_id}: relationship {rid!r} 'verification.status' must be 'observed_from_filing'")
    record: dict[str, object] = {
        "relationship_id": rel.get("relationship_id"),
        "source": "SEC",
        "from_entity": rel.get("from_entity"),
        "to_entity": rel.get("to_entity"),
        "relationship_type": rel.get("relationship_type"),
        "evidence_ids": uniq,
        "attributes": deepcopy(dict(attributes)) if isinstance(attributes, Mapping) else {},
        "verification": verification,
    }
    if materiality is not None:
        record["materiality"] = materiality
    return record, uniq


def _ground_relationships(
    relationships: Sequence[Mapping[str, object]], dossier_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validated commercial records + derived evidence ids (order-stable, deduped)."""
    grounded: list[dict[str, object]] = []
    supporting: list[str] = []
    seen: list[object] = []
    for rel in relationships:
        record, uniq = _validate_relationship_shape(rel, dossier_id)
        if record["relationship_id"] in seen:
            raise DossierIntegrityError(f"dossier {dossier_id}: duplicate relationship_id {record['relationship_id']!r}")
        seen.append(record["relationship_id"])
        grounded.append(record)
        for eid in uniq:
            if eid not in supporting:
                supporting.append(eid)
    return grounded, supporting


def _alias_evidence_ids(ids: object) -> list[str] | None:
    """Deduped alias provenance or None (non-list/blank entries collapse to one arm)."""
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(e, str) and e for e in ids):
        return None
    return list(dict.fromkeys(ids))


def _alias_fields(alias: Mapping[str, object], dossier_id: str) -> tuple[str, str, object]:
    """Flat table over alias/entity (one loop, one raise site)."""
    out: dict[str, object] = {}
    for key in ("alias", "entity"):
        value = _nonempty_str(alias.get(key))
        if value is None:
            name = alias.get("alias")
            prefix = f"dossier {dossier_id}: alias" if key == "alias" else f"dossier {dossier_id}: alias {name!r}"
            raise DossierIntegrityError(f"{prefix} {key!r} must be a non-empty string")
        out[key] = value
    name = out["alias"]
    entity = out["entity"]
    assert isinstance(name, str) and isinstance(entity, str)
    return name, entity, alias.get("source")


def _validate_alias_shape(
    alias: Mapping[str, object], dossier_id: str,
) -> tuple[dict[str, object], list[str]]:
    """One alias->result record + deduped evidence ids (raises on ungrounded aliases).

    ``evidence_ids`` are the alias->result provenance: each id resolves to a
    ledger evidence record, so alias-sourced results carry their grounding.
    ``sec_document``/``grounded_evidence`` aliases must cite evidence;
    ``entity_metadata`` aliases (validated outside any filing) may omit it.
    """
    if not isinstance(alias, Mapping):
        raise DossierIntegrityError(f"dossier {dossier_id}: alias must be a mapping")
    name, entity, source = _alias_fields(alias, dossier_id)
    if source not in ALIAS_SOURCES:
        raise DossierIntegrityError(f"dossier {dossier_id}: alias {name!r} 'source' must be one of {list(ALIAS_SOURCES)}")
    uniq = _alias_evidence_ids(alias.get("evidence_ids"))
    if uniq is None:
        raise DossierIntegrityError(f"dossier {dossier_id}: alias {name!r} 'evidence_ids' must be a list of strings")
    if source in ("sec_document", "grounded_evidence") and not uniq:
        raise DossierIntegrityError(f"dossier {dossier_id}: alias {name!r} from {source!r} must cite 'evidence_ids' provenance")
    return {"alias": name, "entity": entity, "source": source, "evidence_ids": uniq}, uniq

def _ground_aliases(
    aliases: Sequence[Mapping[str, object]], dossier_id: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Validated alias set + derived evidence ids (order-stable, deduped)."""
    grounded: list[dict[str, object]] = []
    supporting: list[str] = []
    seen: list[object] = []
    for alias in aliases:
        record, uniq = _validate_alias_shape(alias, dossier_id)
        if record["alias"] in seen:
            raise DossierIntegrityError(f"dossier {dossier_id}: duplicate alias {record['alias']!r}")
        seen.append(record["alias"])
        grounded.append(record)
        for eid in uniq:
            if eid not in supporting:
                supporting.append(eid)
    return grounded, supporting


def _check_relationship_records(dossier: SECDossier) -> list[str]:
    """Relationship shape + duplicate gates; returns cited evidence ids."""
    if not isinstance(dossier.relationships, list):
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: 'relationships' must be a list")
    ids: list[str] = []
    seen: list[object] = []
    for rel in dossier.relationships:
        record, uniq = _validate_relationship_shape(rel, dossier.dossier_id)
        if record["relationship_id"] in seen:
            raise DossierIntegrityError(f"dossier {dossier.dossier_id}: duplicate relationship_id {record['relationship_id']!r}")
        seen.append(record["relationship_id"])
        ids.extend(uniq)
    return ids


def _check_alias_records(dossier: SECDossier) -> list[str]:
    """Alias shape + provenance gates; returns cited evidence ids."""
    if not isinstance(dossier.aliases, list):
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: 'aliases' must be a list")
    ids: list[str] = []
    seen: list[object] = []
    for alias in dossier.aliases:
        record, uniq = _validate_alias_shape(alias, dossier.dossier_id)
        if record["alias"] in seen:
            raise DossierIntegrityError(f"dossier {dossier.dossier_id}: duplicate alias {record['alias']!r}")
        seen.append(record["alias"])
        ids.extend(uniq)
    return ids


def create_dossier(
    *,
    dossier_id: str,
    session_id: str,
    wave_id: int | str,
    subject: str = "",
    as_of: datetime | str | None = None,
    coverage: Mapping[str, object] | None = None,
    findings: Sequence[Mapping[str, object]] = (),
    relationships: Sequence[Mapping[str, object]] = (),
    aliases: Sequence[Mapping[str, object]] = (),
    contradicting_evidence_ids: Sequence[str] = (),
    unknowns: Sequence[str] = (),
    limitations: Sequence[str] = (),
    open_questions: Sequence[str] = (),
) -> SECDossier:
    """Pure constructor, no I/O. Supporting set derives from findings, relationships, and aliases.

    Each finding must be ``{"text": str, "evidence_ids": [str, ...]}`` with a
    non-empty citation set; free-text or whole-freeze citations are rejected.
    Each relationship must be ``{"relationship_id": str, "source": "SEC",
    "from_entity": str, "to_entity": str, "relationship_type": str,
    "evidence_ids": [str, ...]}`` with optional ``attributes`` (amount/currency/
    period), ``materiality`` (critical/high/medium/low, follow-up routing only),
    and ``verification`` (defaults to ``{"status": "observed_from_filing"}``);
    e.g. MSFT ``investor_in``/``cloud_provider_to`` OpenAI with the filing
    evidence ids and a high materiality. Known types live in
    ``COMMERCIAL_RELATIONSHIP_TYPES``; any other non-empty type is kept verbatim
    as the open-vocabulary fallback. Each alias must be ``{"alias": str,
    "entity": str, "source": sec_document|entity_metadata|grounded_evidence}``
    with optional ``evidence_ids`` as the alias->result provenance (required for
    sec_document/grounded_evidence sources; entity_metadata may omit it); aliases
    from any other origin are rejected. Inputs are copied so later caller mutation
    cannot leak in. ``coverage`` may carry ``resolved``/``partially_resolved``/
    ``unresolved`` plus ``source_limitations``; ``unknowns`` and
    ``open_questions`` are preserved verbatim so unknowns survive to the freeze.
    """
    grounded, supporting = _ground_findings(findings, dossier_id)
    grounded_relationships, relationship_evidence = _ground_relationships(relationships, dossier_id)
    grounded_aliases, alias_evidence = _ground_aliases(aliases, dossier_id)
    for eid in (*relationship_evidence, *alias_evidence):
        if eid not in supporting:
            supporting.append(eid)
    return SECDossier(
        dossier_id=dossier_id,
        session_id=session_id,
        wave_id=_coerce_wave(wave_id),
        subject=subject,
        coverage=deepcopy(dict(coverage)) if coverage is not None else default_coverage(),
        findings=grounded,
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=list(dict.fromkeys(contradicting_evidence_ids)),
        unknowns=list(unknowns),
        limitations=list(limitations),
        open_questions=list(open_questions),
        as_of=_coerce_as_of(as_of),
        relationships=grounded_relationships,
        aliases=grounded_aliases,
    )


def _require_str_list(coverage: Mapping[str, object], key: str, dossier_id: str) -> None:
    value = coverage.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage[{key!r}] must be a list of strings")


def _check_dossier_identity(dossier: SECDossier) -> None:
    """Non-empty dossier/session ids (first gate, no coverage touch)."""
    if not dossier.dossier_id:
        raise DossierIntegrityError("dossier: 'dossier_id' must be non-empty")
    if not dossier.session_id:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: 'session_id' must be non-empty")


def _check_coverage_time_range(coverage: Mapping[str, object], dossier_id: str) -> None:
    """time_range must be {start: str|None, end: str|None}."""
    time_range = coverage.get("time_range")
    bad = (
        not isinstance(time_range, dict)
        or "start" not in time_range
        or "end" not in time_range
        or not (time_range["start"] is None or isinstance(time_range["start"], str))
        or not (time_range["end"] is None or isinstance(time_range["end"], str))
    )
    if bad:
        raise DossierIntegrityError(
            f"dossier {dossier_id}: coverage['time_range'] must be {{start: str|None, end: str|None}}"
        )


def _check_coverage_contract(coverage: Mapping[str, object], dossier_id: str) -> None:
    """Keys present + list types + time_range shape + complete flag.

    Resolution keys (resolved/partially_resolved/unresolved/source_limitations)
    are optional so older callers still validate; when present they must be
    string lists. Negative-evidence keys (dates/partitions/docs/gaps) are
    optional but, when present, must be string lists so no-hit claims carry
    their {forms,dates,partitions,docs,gaps,complete} scope.
    """
    missing = [key for key in COVERAGE_KEYS if key not in coverage]
    if missing:
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage missing keys {missing}")
    for key in ("entities", "forms", "sources_examined", "exclusions"):
        _require_str_list(coverage, key, dossier_id)
    for key in ("resolved", "partially_resolved", "unresolved", "source_limitations",
                "dates", "partitions", "docs", "gaps"):
        if key in coverage:
            _require_str_list(coverage, key, dossier_id)
    _check_coverage_time_range(coverage, dossier_id)
    if not isinstance(coverage.get("complete"), bool):
        raise DossierIntegrityError(f"dossier {dossier_id}: coverage['complete'] must be a bool")



def _check_finding_ids_shape(ids: object, dossier_id: str) -> list[str]:
    """Finding citation list shape (non-empty strings); returns the ids."""
    if not isinstance(ids, list) or not ids or any(not isinstance(e, str) for e in ids):
        raise DossierIntegrityError(f"dossier {dossier_id}: finding 'evidence_ids' must be a non-empty list of strings")
    return list(ids)


def _check_finding_membership(ids: list[str], known: set[str], supporting_set: set[str], dossier_id: str) -> None:
    """Every cited id resolves to the ledger and the supporting set."""
    for eid in ids:
        if eid not in known:
            raise DossierIntegrityError(f"dossier {dossier_id}: unknown evidence ids {[eid][:5]}")
        if eid not in supporting_set:
            raise DossierIntegrityError(f"dossier {dossier_id}: finding cites id outside supporting set: {eid!r}")


def _check_single_finding_ref(
    finding: object, known: set[str], supporting_set: set[str], dossier_id: str,
) -> None:
    """One finding mapping + membership (shape then ledger gates)."""
    if not isinstance(finding, dict):
        raise DossierIntegrityError(f"dossier {dossier_id}: finding must be a mapping")
    ids = _check_finding_ids_shape(finding.get("evidence_ids"), dossier_id)
    _check_finding_membership(ids, known, supporting_set, dossier_id)


def _check_dossier_refs(dossier: SECDossier, known: set[str]) -> None:
    """Supporting/contradicting/relationship/alias ids resolve; findings cite supporting."""
    relationship_ids = _check_relationship_records(dossier)
    alias_ids = _check_alias_records(dossier)
    cited = (
        set(dossier.supporting_evidence_ids)
        | set(dossier.contradicting_evidence_ids)
        | set(relationship_ids)
        | set(alias_ids)
    )
    dangling = sorted(cited - known)
    if dangling:
        raise DossierIntegrityError(f"dossier {dossier.dossier_id}: unknown evidence ids {dangling[:5]}")
    supporting_set = set(dossier.supporting_evidence_ids)
    for eid in (*relationship_ids, *alias_ids):
        if eid not in supporting_set:
            raise DossierIntegrityError(f"dossier {dossier.dossier_id}: relationship/alias cites id outside supporting set: {eid!r}")
    for finding in dossier.findings:
        _check_single_finding_ref(finding, known, supporting_set, dossier.dossier_id)


def validate_dossier(dossier: SECDossier, ledger_ids: Collection[str]) -> None:
    """Coverage contract + every supporting/contradicting/finding/relationship/alias id must exist in the ledger."""
    _check_dossier_identity(dossier)
    _check_coverage_contract(dossier.coverage, dossier.dossier_id)
    _check_dossier_refs(dossier, set(ledger_ids))


def should_expand_entity(
    relationship: Mapping[str, object] | str, could_change_answer: bool = False,
) -> bool:
    """One-hop expansion rule: follow a discovered entity iff its link is materially
    significant (critical/high/medium) AND the linked entity could change the answer.
    Low-materiality and tangential mentions are record-only: no follow-up, no extra
    searches. Missing or unknown materiality never expands (fail closed). One hop
    only: expansion results are recorded, and any further hop needs its own
    materially-significant link.
    """
    materiality: object = relationship if isinstance(relationship, str) else (
        relationship.get("materiality") if isinstance(relationship, Mapping) else None)
    return could_change_answer and materiality in ("critical", "high", "medium")
