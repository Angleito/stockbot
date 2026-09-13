"""Source-agent infrastructure: SEC-only guard + dossier assembly.

Fake-model sketch (no live calls): fake ``dispatch`` + fake ``model`` into
``decompose_question`` / ``assemble_dossier``; assert non-SEC tools are
refused and refs validate against known evidence ids.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from . import GroundedClaim, ModelOutputFailure, ResearchRequest, claims_refs
from .scout import ScoutAssignment, ScoutResult, ScoutRole

# SEC-only allowlist: discovery wrappers + SEC/financial-statement tools.
# Domain guard also consults TOOL_DOMAINS (portfolio_read always denied).
SEC_TOOLS: frozenset[str] = frozenset({
    "browse_tools",
    "search_tools",
    "describe_tool",
    "list_tool_domains",
    "call_tool",
    "find_sec_entities",
    "list_sec_filings",
    "get_sec_filing",
    "list_sec_documents",
    "get_sec_document",
    "search_sec_filings",
    "search_sec_relationships",
    "get_sec_search_coverage",
    "diff_sec_filings",
    "get_material_events",
    "get_beneficial_ownership",
    "get_ownership_changes",
    "get_insider_activity",
    "get_planned_insider_sales",
    "get_offering_history",
    "get_dilution_profile",
    "get_governance_events",
    "get_transaction_status",
    "get_financial_statements",
    "get_xbrl_facts",
    "get_obligations",
    "get_valuation_metrics",
    "get_fundamentals",
    "diff_risk_factors",
    "get_recent_ownership_filings",
})


def is_sec_tool(name: str) -> bool:
    """Infra guard: allowlisted name and never a portfolio/private tool."""
    if name not in SEC_TOOLS:
        return False
    try:
        from app.security.action_policy import TOOL_DOMAINS
    except ImportError:
        return True
    return TOOL_DOMAINS.get(name) != "portfolio_read"


def _coerce_wave(wave_id: int | str) -> int:
    """Accept int>=1 or numeric str; reject bool/non-numeric/<1."""
    if isinstance(wave_id, bool):
        raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")
    if wave < 1:
        raise ValueError(f"source dossier: 'wave_id' must be >= 1, got {wave_id!r}")
    return wave


@dataclass
class SourceDossier:
    """Local dossier shape; mirrors dossiers/sec SECDossier fields (wave_id stored as int)."""

    dossier_id: str
    session_id: str
    wave_id: int
    as_of: str
    findings: list[GroundedClaim] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave(self.wave_id)


def decompose_question(
    question: str,
    *,
    session_id: str,
    as_of: str,
    tickers: Sequence[str],
    dispatch: Callable[[str, dict[str, object]], dict[str, object]],
) -> list[ScoutAssignment]:
    """Decompose via catalog discovery; models decide relevance, infra bounds."""
    discovered: list[str] = []
    for query in ("SEC filings material events", "XBRL financial statements trend"):
        result = dispatch("search_tools", {"query": query})
        matches = result.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                name_raw = match.get("name")
                if isinstance(name_raw, str) and is_sec_tool(name_raw) and name_raw not in discovered:
                    discovered.append(name_raw)
    _ = discovered  # hint only; roles below are fixed and bounded.
    roles: tuple[ScoutRole, ScoutRole, ScoutRole] = ("filings", "financials", "risk")
    return [
        ScoutAssignment(
            assignment_id=f"scout-{role}",
            session_id=session_id,
            as_of=as_of,
            role=role,
            question=question,
            tickers=list(tickers),
        )
        for role in roles
    ]

def assemble_dossier(
    *,
    dossier_id: str,
    session_id: str,
    wave_id: int | str,
    as_of: str,
    results: Sequence[ScoutResult],
    known_evidence_ids: Sequence[str],
    journal: Callable[[str, dict[str, object]], None] | None = None,
) -> SourceDossier:
    """Merge bounded scout outputs into one validated dossier (wave_id stored as int)."""
    wave = _coerce_wave(wave_id)
    known_set = set(e for e in known_evidence_ids if isinstance(e, str) and e)
    findings: list[GroundedClaim] = []
    unknowns: list[str] = []
    limitations: list[str] = []
    coverage_notes: list[str] = []
    for result in results:
        coverage_notes.append(result.coverage)
        unknowns.extend(result.unknowns)
        limitations.extend(result.limitations)
        result_findings = list(result.findings or [])
        if not result_findings:
            continue
        for claim in result_findings:
            if not claim.text.strip():
                raise ModelOutputFailure("each claim needs non-empty text")
            if not claim.evidence_ids:
                raise ModelOutputFailure(f"uncited finding: {claim.text[:120]!r}")
            for eid in claim.evidence_ids:
                if eid not in known_set:
                    if journal is not None:
                        journal("evidence.rejected", {"session_id": session_id, "evidence_id": eid})
                    raise ModelOutputFailure(f"unknown evidence id {eid!r}")
            prior = next((c for c in findings if c.text == claim.text), None)
            if prior is None:
                findings.append(GroundedClaim(text=claim.text, evidence_ids=list(dict.fromkeys(claim.evidence_ids))))
            else:
                merged_ids = list(dict.fromkeys([*prior.evidence_ids, *claim.evidence_ids]))
                findings[findings.index(prior)] = GroundedClaim(text=prior.text, evidence_ids=merged_ids)
    return SourceDossier(
        dossier_id=dossier_id,
        session_id=session_id,
        wave_id=wave,
        as_of=as_of,
        findings=findings,
        unknowns=list(dict.fromkeys(unknowns)),
        limitations=list(dict.fromkeys(limitations)),
        coverage_notes=coverage_notes,
    )
__all__ = [
    "SEC_TOOLS",
    "SourceDossier",
    "assemble_dossier",
    "decompose_question",
    "is_sec_tool",
]
