"""SEC filing domain objects: a Filing is filedBy a Company.

Point-in-time fields stay distinct: filed_at (filer's date), accepted_at
(SEC acceptance, when exposed), known_at (when the market could know it),
report_period (the period the filing covers). Every record keeps provenance
via accession_no + source.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal

from .cusip import normalize_cusip as normalize_cusip


# Duck-typed PIT boundary: storage mapping rows and attribute records
# (dataclasses, namespaces) share no nominal type, so object is honest here.
def pit_of(record: object) -> tuple[str | None, str | None]:
    """Point-in-time timestamp precedence: known_at > accepted_at > filed_at.

    Returns (value, basis). (None, None) when the record carries no timestamp;
    callers with an as_of bound must exclude such records and record a gap.
    """
    mapping: Mapping[str, object] | None = (
        record if isinstance(record, dict) else None
    )

    def get(name: str) -> object:
        if mapping is not None:
            return mapping.get(name)
        return getattr(record, name, None)
    for basis in ("known_at", "accepted_at", "filed_at"):
        try:
            value = get(basis)
        except Exception:  # noqa: BLE001 - duck-typed PIT read coerces faulty attrs to None, never raises
            value = None
        if value:
            return str(value), basis
    return None, None


@dataclass(frozen=True)
class Filing:
    accession_no: str
    form: str
    filer_cik: int
    filer_name: str
    filed_at: str  # YYYY-MM-DD
    accepted_at: str | None
    known_at: str
    report_period: str | None
    primary_document: str | None
    is_amendment: bool
    amendment_of: str | None
    source: str  # filing homepage URL
    subject_cik: int | None = None
    subject_name: str | None = None
    accepted_at_missing: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FilingDocument:
    accession_no: str
    document_name: str | None  # filename
    description: str | None
    size: int | None
    url: str
    document_type: str | None
    file_type: str | None = None
    file_description: str | None = None
    items: tuple[str, ...] = field(default_factory=tuple)
    sic: str | None = None
    location: str | None = None
    state: str | None = None
    inc_state: str | None = None
    is_primary: bool | None = None
    filed_at: str | None = None
    accepted_at: str | None = None
    known_at: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SECSearchRequest:
    query: str | None = None
    ticker: str | None = None
    cik: str | None = None
    company_name: str | None = None
    person_name: str | None = None
    domain: str | None = None
    accession_no: str | None = None
    security_identifier: str | None = None
    forms: tuple[str, ...] | None = None
    start_date: str | None = None
    end_date: str | None = None
    as_of: str | None = None
    search_documents: bool = True
    search_entities: bool = True
    search_relationships: bool = True
    exhaustive: bool = False
    max_results: int | None = 20

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EntityCandidate:
    cik: int | None
    name: str
    tickers: tuple[str, ...] = field(default_factory=tuple)
    exchange: str | None = None
    match_source: str = ""
    match_score: float = 0.0
    match_type: str = ""
    verification_status: Literal["unverified", "verified", "ambiguous", "conflict", "not_found"] = "unverified"
    entity_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FilingParty:
    accession_no: str
    entity_id: str | None
    cik: int | None
    name: str
    role: str
    source: str
    known_at: str
    parser_version: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SECTextHit:
    search_id: str
    attempt_id: str
    query: str
    accession_no: str
    form: str
    filed_at: str
    filer_cik: int | None = None
    filer_name: str | None = None
    matched_document: str | None = None
    issuer_cik: int | None = None
    relevance_reason: tuple[str, ...] = field(default_factory=tuple)
    snippet: str | None = None
    resource_uri: str | None = None
    file_type: str | None = None
    file_description: str | None = None
    items: tuple[str, ...] = field(default_factory=tuple)
    sic: str | None = None
    location: str | None = None
    state: str | None = None
    inc_state: str | None = None
    score: float = 0.0
    source_url: str | None = None
    page: int = 1

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchAttempt:
    attempt_id: str
    search_id: str
    backend: str
    query: str
    filters: dict[str, object] = field(default_factory=dict)
    started_at: str | None = None
    completed_at: str | None = None
    status: Literal["complete", "source_limited", "partial", "failed", "not_applicable"] = "complete"
    results_reported: int = 0
    results_retrieved: int = 0
    pages_retrieved: int = 0
    truncated: bool = False
    source_limit: str | None = None
    pit_basis: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MatchingPassage:
    """One query hit within a document: which document, which query, score."""
    document: str | None = None
    query: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DocumentMatch:
    """One filing grouped with its matching passages."""
    accession: str = ""
    matching_passages: tuple[MatchingPassage, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchRun:
    """One executed query: provenance for positives, citation for negatives."""
    id: str = ""
    source: str = ""
    query: str = ""
    filters: dict[str, object] = field(default_factory=dict)
    executed_at: str | None = None
    as_of: str | None = None
    matched_entities: int = 0
    matched_documents: int = 0
    matched_passages: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchCoverage:
    status: Literal["complete", "complete_within_source_limits", "partial", "failed"] = "complete"
    sources_attempted: tuple[str, ...] = field(default_factory=tuple)
    sources_completed: tuple[str, ...] = field(default_factory=tuple)
    sources_failed: tuple[str, ...] = field(default_factory=tuple)
    source_limits: tuple[str, ...] = field(default_factory=tuple)
    results_reported: int = 0
    results_retrieved: int = 0
    pages: int = 0
    date_coverage: str | None = None
    forms_covered: tuple[str, ...] = field(default_factory=tuple)
    pending_backfill_jobs: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

@dataclass(frozen=True)
class SECSearchResult:
    search_id: str
    request: SECSearchRequest
    entities: tuple[EntityCandidate, ...] = field(default_factory=tuple)
    filings: tuple[Filing, ...] = field(default_factory=tuple)
    documents: tuple[FilingDocument, ...] = field(default_factory=tuple)
    relationships: tuple[FilingParty, ...] = field(default_factory=tuple)
    text_hits: tuple[SECTextHit, ...] = field(default_factory=tuple)
    coverage: SearchCoverage = field(default_factory=SearchCoverage)
    attempts: tuple[SearchAttempt, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    retrieval_order: tuple[str, ...] = field(default_factory=tuple)
    evidence_packet_ids: tuple[str, ...] = field(default_factory=tuple)
    search_runs: tuple[SearchRun, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CurrentReportEvent:
    accession_no: str
    item_number: str
    item_name: str
    event_date: str | None
    text: str
    exhibit_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["exhibit_refs"] = list(d["exhibit_refs"])
        return d


EVENT_TYPES = frozenset({
    "earnings",
    "guidance_change",
    "material_agreement",
    "debt_issuance",
    "default",
    "bankruptcy",
    "restructuring",
    "impairment",
    "acquisition",
    "asset_sale",
    "cybersecurity_incident",
    "delisting_notice",
    "equity_issuance",
    "auditor_change",
    "restatement",
    "management_change",
    "change_of_control",
    "large_holder_entry",
    "large_holder_exit",
    "activist_change",
    "insider_purchase",
    "insider_sale",
    "planned_insider_sale",
    "shelf_registration",
    "offering",
    "atm_program",
    "convertible_warrant_issuance",
    "institutional_entry",
    "institutional_exit",
    "proxy_fight",
    "shareholder_vote",
    "tender_offer",
    "merger",
    "going_private",
})


@dataclass(frozen=True)
class RegulatoryEvent:
    event_id: str
    issuer: str
    event_type: str
    effective_date: str | None
    known_at: str
    source_accessions: tuple[str, ...]
    severity: str = "routine"
    structured_data: dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {self.event_type!r}")
        accessions = tuple(self.source_accessions)
        if not accessions:
            raise ValueError("source_accessions must be non-empty")
        object.__setattr__(self, "source_accessions", accessions)

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["source_accessions"] = list(d["source_accessions"])
        return d


@dataclass(frozen=True)
class BeneficialOwnership:
    filer_name: str
    filer_cik: str | None
    issuer: str
    form: str
    filed_at: str | None
    accession_no: str
    shares: int | None = None
    percent: float | None = None
    sole_voting: int | None = None
    shared_voting: int | None = None
    sole_dispositive: int | None = None
    shared_dispositive: int | None = None
    is_amendment: bool = False
    purpose_text: str | None = None
    subject_cik: str | None = None
    subject_name: str | None = None
    document_name: str | None = None
    known_at: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class OwnershipChangeEvent:
    filer_name: str
    filer_cik: str | None
    issuer: str
    previous_accession: str
    current_accession: str
    filed_at: str | None
    prev_shares: int | None = None
    curr_shares: int | None = None
    share_change: int | None = None
    prev_percent: float | None = None
    curr_percent: float | None = None
    percent_change: float | None = None
    voting_changed: bool = False
    text_changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Insider:
    insider_name: str
    insider_cik: str | None
    issuer: str
    position: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InsiderTransaction:
    insider_name: str | None
    insider_cik: str | None
    issuer: str
    form: str
    filed_at: str | None
    accession_no: str
    transaction_date: str | None = None
    security: str | None = None
    transaction_code: str | None = None
    transaction_kind: str = "other"
    shares: int | None = None
    price: float | None = None
    acquired_disposed: str | None = None
    holdings_after: int | None = None
    issuer_cik: str | None = None
    is_director: bool | None = None
    is_officer: bool | None = None
    is_ten_percent: bool | None = None
    is_other: bool | None = None
    role_title: str | None = None
    document_name: str | None = None
    known_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProposedInsiderSale:
    seller_name: str | None
    seller_cik: str | None
    issuer: str
    filed_at: str | None
    accession_no: str
    shares_proposed: int | None = None
    issuer_cik: str | None = None
    form: str | None = None
    document_name: str | None = None
    known_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class InstitutionalHolding:
    manager_name: str | None
    manager_cik: str | None
    accession_no: str
    source_row: int
    holding_id: str
    report_period: str | None = None
    issuer_name: str | None = None
    entity_id: str | None = None
    security_id: str | None = None
    class_title: str | None = None
    cusip: str | None = None
    isin: str | None = None
    shares: int | None = None
    value: int | None = None
    put_call: str | None = None
    discretion: str | None = None
    other_manager: str | None = None
    shares_prn_type: str | None = None
    voting: str | None = None
    filed_at: str | None = None
    known_at: str | None = None
    document_name: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def institutional_holding_id(accession_no: str, source_row: int, security_id: str | None) -> str:
    payload = f"{accession_no}\0{source_row}\0{security_id or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Registration:
    issuer: str
    form: str
    filed_at: str | None
    accession_no: str
    offering_type: str | None = None
    securities_registered: int | None = None
    max_aggregate_price: float | None = None
    is_shelf: bool = False
    status: str = "filed"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Offering:
    issuer: str
    form: str
    filed_at: str | None
    accession_no: str
    offering_type: str | None = None
    shares: int | None = None
    price_per_share: float | None = None
    gross_proceeds: float | None = None
    underwriters: tuple[str, ...] = ()
    has_warrants: bool | None = None
    has_convertibles: bool | None = None
    is_atm: bool = False
    source_registration: str | None = None
    status: str = "filed"
    # Phase 7: filer/registrant split (registrant defaults to issuer, never
    # the reverse); amounts stay proposed/registered via amount_basis, never
    # issuance. Provenance mirrors Phase 6 ownership rows.
    filer_cik: str | None = None
    filer_name: str | None = None
    registrant_cik: str | None = None
    registrant_name: str | None = None
    security_title: str | None = None
    amount_basis: str | None = None
    document_name: str | None = None
    known_at: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "underwriters", tuple(self.underwriters or ()))

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["underwriters"] = list(d["underwriters"])
        return d


GOVERNANCE_EVENT_TYPES = frozenset({
    "annual_meeting",
    "special_meeting",
    "information_statement",
    "proxy_contest",
    "director_election",
    "say_on_pay",
    "equity_plan",
    "auditor_ratification",
    "shareholder_proposal",
    "merger_vote",
})


@dataclass(frozen=True)
class GovernanceEvent:
    event_id: str
    issuer: str
    event_type: str
    accession_no: str
    meeting_date: str | None = None
    filed_at: str | None = None
    contested: bool = False
    source: str | None = None
    # Phase 7: filer/subject split (subject only from structured/explicit
    # evidence, never a filer copy) plus document/PIT provenance.
    filer_cik: str | None = None
    filer_name: str | None = None
    subject_cik: str | None = None
    subject_name: str | None = None
    document_name: str | None = None
    known_at: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None

    def __post_init__(self):
        if self.event_type not in GOVERNANCE_EVENT_TYPES:
            raise ValueError(f"unknown governance event_type: {self.event_type!r}")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProxyProposal:
    proposal_id: str
    issuer: str
    accession_no: str
    description: str | None = None
    proposal_type: str | None = None
    board_recommendation: str | None = None
    status: str = "unknown"
    # Phase 7: exact "start:end" offsets of the heading span backing
    # description, plus the matched document when known.
    source_span: str | None = None
    document_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ShareholderVote:
    issuer: str
    accession_no: str
    meeting_date: str | None = None
    description: str | None = None
    votes_for: int | None = None
    votes_against: int | None = None
    abstentions: int | None = None
    outcome: str | None = None
    # Phase 7: exact "start:end" offsets of the vote-count span.
    source_span: str | None = None
    document_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class Transaction:
    event_id: str
    target: str
    accession_no: str
    buyer: str | None = None
    deal_type: str = "unknown"
    announced_at: str | None = None
    consideration: str | None = None
    exchange_ratio: str | None = None
    implied_value: str | None = None
    financing: str | None = None
    termination_fee: str | None = None
    reverse_termination_fee: str | None = None
    vote_conditions: str | None = None
    regulatory_conditions: str | None = None
    tender_expiry: str | None = None
    expected_close: str | None = None
    competing_offer: bool = False
    status: str = "unknown"
    source_accessions: tuple[str, ...] = ()
    # Phase 7: filer/subject/target/acquirer/offeror/security split.
    # Subject comes only from structured/explicit evidence; target
    # additionally falls back to exact document spans, never a filer copy.
    # Status stays unknown without closing evidence.
    filer_cik: str | None = None
    filer_name: str | None = None
    subject_cik: str | None = None
    subject_name: str | None = None
    acquirer_cik: str | None = None
    acquirer_name: str | None = None
    offeror: str | None = None
    security_title: str | None = None
    document_name: str | None = None
    known_at: str | None = None
    source_url: str | None = None
    extraction_method: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "source_accessions",
                           tuple(self.source_accessions or ()))

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["source_accessions"] = list(d["source_accessions"])
        return d
