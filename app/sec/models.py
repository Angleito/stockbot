"""SEC filing domain objects: a Filing is filedBy a Company.

Point-in-time fields stay distinct: filed_at (filer's date), accepted_at
(SEC acceptance, when exposed), known_at (when the market could know it),
report_period (the period the filing covers). Every record keeps provenance
via accession_no + source.
"""

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional


def pit_of(record) -> tuple[Optional[str], Optional[str]]:
    """Point-in-time timestamp precedence: known_at > accepted_at > filed_at.

    Returns (value, basis). (None, None) when the record carries no timestamp;
    callers with an as_of bound must exclude such records and record a gap.
    """
    get = (lambda name: record.get(name)) if isinstance(record, dict) else (
        lambda name: getattr(record, name, None)
    )
    for basis in ("known_at", "accepted_at", "filed_at"):
        try:
            value = get(basis)
        except Exception:
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
    accepted_at: Optional[str]
    known_at: str
    report_period: Optional[str]
    primary_document: Optional[str]
    is_amendment: bool
    amendment_of: Optional[str]
    source: str  # filing homepage URL
    subject_cik: Optional[int] = None
    subject_name: Optional[str] = None
    accepted_at_missing: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FilingDocument:
    accession_no: str
    document_name: Optional[str]  # filename
    description: Optional[str]
    size: Optional[int]
    url: str
    document_type: Optional[str]
    file_type: Optional[str] = None
    file_description: Optional[str] = None
    items: tuple = field(default_factory=tuple)
    sic: Optional[str] = None
    location: Optional[str] = None
    state: Optional[str] = None
    inc_state: Optional[str] = None
    is_primary: Optional[bool] = None
    filed_at: Optional[str] = None
    accepted_at: Optional[str] = None
    known_at: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SECSearchRequest:
    query: Optional[str] = None
    ticker: Optional[str] = None
    cik: Optional[str] = None
    company_name: Optional[str] = None
    person_name: Optional[str] = None
    domain: Optional[str] = None
    accession_no: Optional[str] = None
    security_identifier: Optional[str] = None
    forms: Optional[tuple] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    as_of: Optional[str] = None
    search_documents: bool = True
    search_entities: bool = True
    search_relationships: bool = True
    exhaustive: bool = True
    max_results: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EntityCandidate:
    cik: Optional[int]
    name: str
    tickers: tuple = field(default_factory=tuple)
    exchange: Optional[str] = None
    match_source: str = ""
    match_score: float = 0.0
    match_type: str = ""
    verification_status: Literal["unverified", "verified", "ambiguous", "conflict", "not_found"] = "unverified"
    entity_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FilingParty:
    accession_no: str
    entity_id: Optional[str]
    cik: Optional[int]
    name: str
    role: str
    source: str
    known_at: str
    parser_version: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SECTextHit:
    search_id: str
    attempt_id: str
    query: str
    accession_no: str
    form: str
    filed_at: str
    filer_cik: Optional[int] = None
    filer_name: Optional[str] = None
    matched_document: Optional[str] = None
    file_type: Optional[str] = None
    file_description: Optional[str] = None
    items: tuple = field(default_factory=tuple)
    sic: Optional[str] = None
    location: Optional[str] = None
    state: Optional[str] = None
    inc_state: Optional[str] = None
    score: float = 0.0
    source_url: Optional[str] = None
    page: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchAttempt:
    attempt_id: str
    search_id: str
    backend: str
    query: str
    filters: dict = field(default_factory=dict)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    status: Literal["complete", "source_limited", "partial", "failed", "not_applicable"] = "complete"
    results_reported: int = 0
    results_retrieved: int = 0
    pages_retrieved: int = 0
    truncated: bool = False
    source_limit: Optional[str] = None
    pit_basis: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SearchCoverage:
    status: Literal["complete", "complete_within_source_limits", "partial", "failed"] = "complete"
    sources_attempted: tuple = field(default_factory=tuple)
    sources_completed: tuple = field(default_factory=tuple)
    sources_failed: tuple = field(default_factory=tuple)
    source_limits: tuple = field(default_factory=tuple)
    results_reported: int = 0
    results_retrieved: int = 0
    pages: int = 0
    date_coverage: Optional[str] = None
    forms_covered: tuple = field(default_factory=tuple)
    pending_backfill_jobs: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SECSearchResult:
    search_id: str
    request: SECSearchRequest
    entities: tuple = field(default_factory=tuple)
    filings: tuple = field(default_factory=tuple)
    documents: tuple = field(default_factory=tuple)
    relationships: tuple = field(default_factory=tuple)
    text_hits: tuple = field(default_factory=tuple)
    coverage: SearchCoverage = field(default_factory=SearchCoverage)
    attempts: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)
    errors: tuple = field(default_factory=tuple)
    retrieval_order: tuple = field(default_factory=tuple)
    evidence_packet_ids: tuple = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CurrentReportEvent:
    accession_no: str
    item_number: str
    item_name: str
    event_date: Optional[str]
    text: str
    exhibit_refs: tuple = ()

    def to_dict(self) -> dict:
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
    effective_date: Optional[str]
    known_at: str
    source_accessions: tuple
    severity: str = "routine"
    structured_data: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type: {self.event_type!r}")
        accessions = tuple(self.source_accessions)
        if not accessions:
            raise ValueError("source_accessions must be non-empty")
        object.__setattr__(self, "source_accessions", accessions)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source_accessions"] = list(d["source_accessions"])
        return d


@dataclass(frozen=True)
class BeneficialOwnership:
    filer_name: str
    filer_cik: Optional[str]
    issuer: str
    form: str
    filed_at: Optional[str]
    accession_no: str
    shares: Optional[int] = None
    percent: Optional[float] = None
    sole_voting: Optional[int] = None
    shared_voting: Optional[int] = None
    sole_dispositive: Optional[int] = None
    shared_dispositive: Optional[int] = None
    is_amendment: bool = False
    purpose_text: Optional[str] = None
    subject_cik: Optional[str] = None
    subject_name: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class OwnershipChangeEvent:
    filer_name: str
    filer_cik: Optional[str]
    issuer: str
    previous_accession: str
    current_accession: str
    filed_at: Optional[str]
    prev_shares: Optional[int] = None
    curr_shares: Optional[int] = None
    share_change: Optional[int] = None
    prev_percent: Optional[float] = None
    curr_percent: Optional[float] = None
    percent_change: Optional[float] = None
    voting_changed: bool = False
    text_changed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Insider:
    insider_name: str
    insider_cik: Optional[str]
    issuer: str
    position: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InsiderTransaction:
    insider_name: Optional[str]
    insider_cik: Optional[str]
    issuer: str
    form: str
    filed_at: Optional[str]
    accession_no: str
    transaction_date: Optional[str] = None
    security: Optional[str] = None
    transaction_code: Optional[str] = None
    transaction_kind: str = "other"
    shares: Optional[int] = None
    price: Optional[float] = None
    acquired_disposed: Optional[str] = None
    holdings_after: Optional[int] = None
    issuer_cik: Optional[str] = None
    is_director: Optional[bool] = None
    is_officer: Optional[bool] = None
    is_ten_percent: Optional[bool] = None
    is_other: Optional[bool] = None
    role_title: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProposedInsiderSale:
    seller_name: Optional[str]
    seller_cik: Optional[str]
    issuer: str
    filed_at: Optional[str]
    accession_no: str
    shares_proposed: Optional[int] = None
    issuer_cik: Optional[str] = None
    form: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class InstitutionalHolding:
    manager_name: Optional[str]
    manager_cik: Optional[str]
    accession_no: str
    report_period: Optional[str] = None
    issuer_name: Optional[str] = None
    entity_id: Optional[str] = None
    security_id: Optional[str] = None
    class_title: Optional[str] = None
    cusip: Optional[str] = None
    isin: Optional[str] = None
    shares: Optional[int] = None
    value: Optional[int] = None
    put_call: Optional[str] = None
    discretion: Optional[str] = None
    voting: Optional[str] = None
    filed_at: Optional[str] = None
    known_at: Optional[str] = None
    document_name: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Registration:
    issuer: str
    form: str
    filed_at: Optional[str]
    accession_no: str
    offering_type: Optional[str] = None
    securities_registered: Optional[int] = None
    max_aggregate_price: Optional[float] = None
    is_shelf: bool = False
    status: str = "filed"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Offering:
    issuer: str
    form: str
    filed_at: Optional[str]
    accession_no: str
    offering_type: Optional[str] = None
    shares: Optional[int] = None
    price_per_share: Optional[float] = None
    gross_proceeds: Optional[float] = None
    underwriters: tuple = ()
    has_warrants: Optional[bool] = None
    has_convertibles: Optional[bool] = None
    is_atm: bool = False
    source_registration: Optional[str] = None
    status: str = "filed"
    # Phase 7: filer/registrant split (registrant defaults to issuer, never
    # the reverse); amounts stay proposed/registered via amount_basis, never
    # issuance. Provenance mirrors Phase 6 ownership rows.
    filer_cik: Optional[str] = None
    filer_name: Optional[str] = None
    registrant_cik: Optional[str] = None
    registrant_name: Optional[str] = None
    security_title: Optional[str] = None
    amount_basis: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None
    source_url: Optional[str] = None
    extraction_method: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "underwriters", tuple(self.underwriters or ()))

    def to_dict(self) -> dict:
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
    meeting_date: Optional[str] = None
    filed_at: Optional[str] = None
    contested: bool = False
    source: Optional[str] = None
    # Phase 7: filer/subject split (subject only from structured/explicit
    # evidence, never a filer copy) plus document/PIT provenance.
    filer_cik: Optional[str] = None
    filer_name: Optional[str] = None
    subject_cik: Optional[str] = None
    subject_name: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None
    source_url: Optional[str] = None
    extraction_method: Optional[str] = None

    def __post_init__(self):
        if self.event_type not in GOVERNANCE_EVENT_TYPES:
            raise ValueError(f"unknown governance event_type: {self.event_type!r}")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProxyProposal:
    proposal_id: str
    issuer: str
    accession_no: str
    description: Optional[str] = None
    proposal_type: Optional[str] = None
    board_recommendation: Optional[str] = None
    status: str = "unknown"
    # Phase 7: exact "start:end" offsets of the heading span backing
    # description, plus the matched document when known.
    source_span: Optional[str] = None
    document_name: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ShareholderVote:
    issuer: str
    accession_no: str
    meeting_date: Optional[str] = None
    description: Optional[str] = None
    votes_for: Optional[int] = None
    votes_against: Optional[int] = None
    abstentions: Optional[int] = None
    outcome: Optional[str] = None
    # Phase 7: exact "start:end" offsets of the vote-count span.
    source_span: Optional[str] = None
    document_name: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Transaction:
    event_id: str
    target: str
    accession_no: str
    buyer: Optional[str] = None
    deal_type: str = "unknown"
    announced_at: Optional[str] = None
    consideration: Optional[str] = None
    exchange_ratio: Optional[str] = None
    implied_value: Optional[str] = None
    financing: Optional[str] = None
    termination_fee: Optional[str] = None
    reverse_termination_fee: Optional[str] = None
    vote_conditions: Optional[str] = None
    regulatory_conditions: Optional[str] = None
    tender_expiry: Optional[str] = None
    expected_close: Optional[str] = None
    competing_offer: bool = False
    status: str = "unknown"
    source_accessions: tuple = ()
    # Phase 7: filer/subject/target/acquirer/offeror/security split.
    # Subject/target come only from structured/explicit evidence, never a
    # filer copy. Status stays unknown without closing evidence.
    filer_cik: Optional[str] = None
    filer_name: Optional[str] = None
    subject_cik: Optional[str] = None
    subject_name: Optional[str] = None
    acquirer_cik: Optional[str] = None
    acquirer_name: Optional[str] = None
    offeror: Optional[str] = None
    security_title: Optional[str] = None
    document_name: Optional[str] = None
    known_at: Optional[str] = None
    source_url: Optional[str] = None
    extraction_method: Optional[str] = None

    def __post_init__(self):
        object.__setattr__(self, "source_accessions",
                           tuple(self.source_accessions or ()))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source_accessions"] = list(d["source_accessions"])
        return d
