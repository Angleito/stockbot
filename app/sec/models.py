"""SEC filing domain objects: a Filing is filedBy a Company.

Point-in-time fields stay distinct: filed_at (filer's date), accepted_at
(SEC acceptance, when exposed), known_at (when the market could know it),
report_period (the period the filing covers). Every record keeps provenance
via accession_no + source.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Filing:
    accession_no: str
    form: str
    cik: int
    company: str
    filed_at: str  # YYYY-MM-DD
    accepted_at: Optional[str]
    known_at: str
    report_period: Optional[str]
    primary_document: Optional[str]
    is_amendment: bool
    amendment_of: Optional[str]
    issuer_cik: int
    source: str  # filing homepage URL
    accepted_at_missing: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FilingDocument:
    accession_no: str
    document: Optional[str]  # filename
    description: Optional[str]
    size: Optional[int]
    url: str
    document_type: Optional[str]

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

    def __post_init__(self):
        object.__setattr__(self, "source_accessions",
                           tuple(self.source_accessions or ()))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["source_accessions"] = list(d["source_accessions"])
        return d
