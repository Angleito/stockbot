"""Context-aware SEC source agent: generic context + query families + dossier assembly.

Fake-model sketch (no live calls): fake ``dispatch`` + fake ``model`` into
``decompose_question`` / ``assemble_dossier``; assert non-SEC tools are
refused and refs validate against known evidence ids.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from . import GroundedClaim, ModelOutputFailure
from .scout import ScoutAssignment, ScoutResult, ScoutRole, normalize_query

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
        raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
    if isinstance(wave_id, int):
        wave = wave_id
    elif isinstance(wave_id, str):
        text = wave_id.strip()
        if not text.isdigit():
            raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")
        wave = int(text)
    else:
        raise ValueError(f"source dossier: 'wave_id' must be an int >= 1, got {wave_id!r}")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
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


_CONTEXT_KEYS = ("primary_entities", "related_entities", "industries", "products",
                 "technologies", "relationships", "concepts", "risks", "catalysts")

_STOPWORDS = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "how", "why",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "has", "have", "had", "having", "the", "a", "an", "of", "to", "for",
    "in", "on", "at", "by", "with", "from", "as", "and", "or", "nor",
    "but", "if", "then", "than", "so", "such", "any", "all", "each",
    "its", "it", "their", "his", "her", "this", "that", "these", "those",
    "there", "here", "about", "into", "over", "under", "between", "among",
    "through", "during", "latest", "current", "recent", "new", "tell", "me",
    "please", "show", "give", "find", "list", "describe", "explain",
    "s", "t", "d", "ll", "m", "re", "ve",
})

_CAP_PHRASE_RE = re.compile(r"[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*)*")
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")
_RELATION_KEYS = ("subject", "relation", "object")


def _dedupe_keep(items: Sequence[object]) -> list[str]:
    """Strip + normalized-key dedupe (lower/whitespace), first form wins."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        key = normalize_query(text)
        if key and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _strip_possessive(text: str) -> str:
    """Drop a trailing 's so entity mentions match ticker scope."""
    low = text.lower()
    if low.endswith("'s") or low.endswith("\u2019s"):
        return text[:-2].strip()
    return text


def _capitalized_phrases(question: str) -> list[str]:
    """Generic entity mentions: multi-word phrases or 2+ letter names."""
    phrases: list[str] = []
    for match in _CAP_PHRASE_RE.findall(question or ""):
        cleaned = " ".join(_strip_possessive(match).split())
        if len(cleaned) < 2 or not any(ch.isalpha() for ch in cleaned):
            continue
        words = [w.strip(".,") for w in cleaned.split()]
        if words and all(w.lower() in _STOPWORDS for w in words):
            continue
        if len(words) == 1 and len(words[0]) < 2:
            continue
        phrases.append(cleaned)
    return _dedupe_keep(phrases)


def _keywords(question: str, exclude: Sequence[str]) -> list[str]:
    """Distinctive lowercase terms outside ticker scope (concepts/catalysts)."""
    excluded = {normalize_query(e) for e in exclude if isinstance(e, str)}
    words: list[str] = []
    for match in _WORD_RE.findall((question or "").lower()):
        word = match.strip()
        if len(word) < 3 or word in _STOPWORDS or word.isdigit():
            continue
        if normalize_query(word) in excluded:
            continue
        words.append(word)
    return _dedupe_keep(words)


def _deterministic_context(question: str, tickers: Sequence[str]) -> dict[str, object]:
    """Base context without a model: tickers + phrases + keyword concepts."""
    scoped = _dedupe_keep([t.upper() for t in (tickers or []) if isinstance(t, str) and t.strip()])
    scoped_set = {s.upper() for s in scoped}
    phrases = [p for p in _capitalized_phrases(question or "") if p.upper() not in scoped_set]
    return {
        "primary_entities": list(scoped),
        "related_entities": phrases[:8],
        "industries": [],
        "products": [],
        "technologies": [],
        "relationships": [],
        "concepts": _keywords(question, scoped)[:8],
        "risks": [],
        "catalysts": [],
    }


def _context_prompt(question: str, tickers: Sequence[str]) -> str:
    """Provider-agnostic JSON prompt for the generic research context."""
    scoped = ", ".join(t for t in tickers if isinstance(t, str) and t.strip()) or "none provided"
    return (
        "Extract a generic SEC research context as JSON only with keys "
        "primary_entities, related_entities, industries, products, technologies, "
        "relationships [{subject, relation, object}], concepts, risks, catalysts. "
        f"Question: {question}\nScope tickers: {scoped}\n"
        "List scope tickers as primary entities; other named issuers (customers, "
        "competitors, suppliers, peers, funds) as related entities; "
        "subject matter as concepts/risks/catalysts. "
        "Expand dynamically to the causal channels the question implies "
        "(counterparty, credit, concentration, lending, commitments, derivatives, "
        "off-balance-sheet, supply-chain, funding/liquidity) — never a fixed issuer list. JSON only."
    )


def _coerce_str_list(value: object) -> list[str]:
    """String list from model output, normalized and deduped."""
    return _dedupe_keep(value) if isinstance(value, list) else []


def _coerce_relationships(value: object) -> list[dict[str, str]]:
    """{subject, relation, object} triples from model output, deduped."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        triple = {k: item.get(k) for k in _RELATION_KEYS}
        if not all(isinstance(v, str) and v.strip() for v in triple.values()):
            continue
        subj = normalize_query(str(triple["subject"]))
        rel = normalize_query(str(triple["relation"]))
        obj = normalize_query(str(triple["object"]))
        key: tuple[str, str, str] = (subj, rel, obj)
        if key in seen:
            continue
        seen.add(key)
        out.append({k: str(triple[k]).strip() for k in _RELATION_KEYS})
    return out[:12]

def _merge_context(base: dict[str, object], decoded: object) -> dict[str, object]:
    """Union model output into the deterministic base, normalized."""
    if not isinstance(decoded, dict):
        return base
    merged: dict[str, object] = {k: (list(v) if isinstance(v, list) else v) for k, v in base.items()}
    for key in _CONTEXT_KEYS:
        if key == "relationships":
            extra = _coerce_relationships(decoded.get(key))
            existing_raw = merged.get("relationships", [])
            existing: list[dict[str, str]] = [r for r in existing_raw if isinstance(r, dict)] if isinstance(existing_raw, list) else []
            seen_rels: set[tuple[str, str, str]] = {
                (normalize_query(str(r.get("subject", ""))), normalize_query(str(r.get("relation", ""))), normalize_query(str(r.get("object", "")))) for r in existing}
            for rel in extra:
                rel_key: tuple[str, str, str] = (normalize_query(rel["subject"]), normalize_query(rel["relation"]), normalize_query(rel["object"]))
                if rel_key not in seen_rels:
                    seen_rels.add(rel_key)
                    existing.append(rel)
            merged["relationships"] = existing
        else:
            current = merged.get(key, [])
            current_list = current if isinstance(current, list) else []
            merged[key] = _dedupe_keep([*current_list, *_coerce_str_list(decoded.get(key))])[:12]
    return merged


def build_research_context(
    question: str,
    tickers: Sequence[str] = (),
    model: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """Generic research context: model-proposed, deterministically normalized.

    The deterministic base (tickers + capitalized phrases + keyword concepts)
    is always present; model output unions in. Never raises on model output --
    unparseable replies fall back to the base.
    """
    base = _deterministic_context(question, tickers)
    if model is None:
        return base
    try:
        decoded: object = json.loads(model(_context_prompt(question, tickers)))
    except Exception:
        return base
    try:
        return _merge_context(base, decoded)
    except Exception:
        return base


def _grouped_queries(context: Mapping[str, object]) -> dict[str, list[str]]:
    """Family-grouped SEC queries with global normalized dedup."""
    source: Mapping[str, object] = context if isinstance(context, Mapping) else {}
    primaries = _coerce_str_list(source.get("primary_entities"))
    related = _coerce_str_list(source.get("related_entities"))
    industries = _coerce_str_list(source.get("industries"))
    products = _coerce_str_list(source.get("products"))
    technologies = _coerce_str_list(source.get("technologies"))
    concepts = _coerce_str_list(source.get("concepts"))
    risks = _coerce_str_list(source.get("risks"))
    catalysts = _coerce_str_list(source.get("catalysts"))
    relationships = _coerce_relationships(source.get("relationships"))
    groups: dict[str, list[str]] = {"a": [], "b": [], "c": [], "d": [], "e": [],
                                    "f": [], "related": [], "supp_bare": [],
                                    "supp_filing": [], "supp_risk": []}
    for entity in [*primaries, *related]:
        groups["a"].append(entity)
    for a in primaries:
        for b in related:
            if normalize_query(a) != normalize_query(b):
                groups["b"].append(f"{a} {b}")
    for industry in industries[:6]:
        groups["c"].append(industry)
        groups["c"].append(f"{industry} risk factors")
    for term in [*products[:6], *technologies[:6]]:
        groups["d"].append(term if "demand" in term.lower() else f"{term} demand")
    for rel in relationships:
        groups["e"].append(f"{rel['subject']} {rel['relation']} {rel['object']}")
    for a in primaries:
        for b in related:
            if normalize_query(a) != normalize_query(b):
                groups["e"].append(f"{a} exposure {b}")
    for risk in risks[:6]:
        groups["f"].append(risk)
    for a in primaries:
        groups["f"].append(f"{a} risk factors")
        for risk in risks[:4]:
            groups["f"].append(f"{a} {risk}")
    for entity in related[:6]:
        groups["related"].append(f"{entity} customer")
        groups["related"].append(f"{entity} supplier")
        groups["related"].append(f"{entity} competitor")
        groups["related"].append(f"{entity} peer")
        groups["related"].append(f"{entity} fund")
    supp_terms = _dedupe_keep([*concepts, *products, *technologies, *catalysts])[:5]
    for term in supp_terms:
        groups["supp_bare"].append(term)
        groups["supp_filing"].append(f"{term} 8-K")
        groups["supp_filing"].append(f"{term} proxy")
        groups["supp_filing"].append(f"{term} N-PX")
        groups["supp_filing"].append(f"{term} agreement")
        groups["supp_risk"].append(f"{term} risk factor")
    seen: set[str] = set()
    deduped: dict[str, list[str]] = {}
    for key in groups:
        unique: list[str] = []
        for query in groups[key]:
            norm = normalize_query(query)
            if norm and norm not in seen:
                seen.add(norm)
                unique.append(query.strip())
        deduped[key] = unique
    return deduped


_ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "filings": ("a", "b", "related", "supp_filing"),
    "financials": ("c", "d", "supp_bare"),
    "risk": ("e", "f", "supp_risk"),
}


def build_query_families(context: Mapping[str, object], as_of: str = "") -> list[str]:
    """Families A-F + related-issuer + conceptual supplements, deduped.

    A named entity, B entity-pair, C industry, D demand, E exposure, F risk,
    then related-issuer role anchors (customer/supplier/competitor/peer/fund)
    and conceptual supplements (risk-factor/8-K/proxy/N-PX/agreement mentions).
    Non-issuer terms search as concepts, never dead-end. ``as_of`` is accepted
    for call symmetry; point-in-time filtering applies at execution.
    """
    _ = as_of
    groups = _grouped_queries(context)
    out: list[str] = []
    for key in ("a", "b", "c", "d", "e", "f", "related", "supp_bare", "supp_filing", "supp_risk"):
        out.extend(groups.get(key, []))
    return out


def _baseline_queries(tickers: Sequence[str], context: Mapping[str, object]) -> list[str]:
    """Latest-filing baseline seeds: terminology only, no extra dispatch by default."""
    return []


def expand_queries(
    queries: Sequence[str],
    finding_texts: Sequence[str],
    context: Mapping[str, object] | None = None,
) -> list[str]:
    """New material queries from filing findings, deduped against prior queries."""
    seen = {normalize_query(q) for q in (queries or []) if isinstance(q, str)}
    seen.discard("")
    candidates: list[str] = []
    for text in (finding_texts or []):
        if not isinstance(text, str):
            continue
        for match in _CAP_PHRASE_RE.findall(text):
            cleaned = " ".join(match.split())
            if len(cleaned) >= 3 and normalize_query(cleaned) not in seen:
                candidates.append(cleaned)
        for word in _WORD_RE.findall(text.lower()):
            if len(word) > 5 and word.isalpha() and word not in _STOPWORDS and normalize_query(word) not in seen:
                candidates.append(word)
        if len(candidates) >= 8:
            break
    if isinstance(context, Mapping):
        for key in ("related_entities", "concepts", "products", "technologies"):
            for value in _coerce_str_list(context.get(key))[:4]:
                if normalize_query(value) not in seen:
                    candidates.append(value)
    out: list[str] = []
    for candidate in candidates:
        key = normalize_query(candidate)
        if key and key not in seen:
            seen.add(key)
            out.append(candidate.strip())
        if len(out) >= 5:
            break
    return out


def expansion_stop(
    *,
    sec_answerable_remaining: bool = True,
    new_queries: bool = True,
    repeats_yield_nothing: bool = False,
    baseline_reviewed: bool = True,
) -> tuple[bool, str]:
    """Info-based stop: (should_stop, reason); never count-based."""
    if not sec_answerable_remaining:
        return True, "all SEC-answerable resolved; remainder non-SEC-answerable"
    if repeats_yield_nothing and not new_queries:
        return True, "repeats yield nothing new"
    if not new_queries:
        return True, "no new material queries; baseline reviewed" if baseline_reviewed else "remainder non-SEC-answerable"
    return False, "continue"


def decompose_question(
    question: str,
    *,
    session_id: str,
    as_of: str,
    tickers: Sequence[str],
    dispatch: Callable[[str, dict[str, object]], dict[str, object]],
    model: Callable[[str], str] | None = None,
) -> list[ScoutAssignment]:
    """Catalog discovery + generic context into three role assignments with queries."""
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
    _ = discovered  # hint only; assignments below carry context queries.
    context = build_research_context(question, tickers, model)
    groups = _grouped_queries(context)
    baseline = _baseline_queries(tickers, context)
    scoped = [t for t in (tickers or []) if isinstance(t, str)]
    roles: tuple[ScoutRole, ScoutRole, ScoutRole] = ("filings", "financials", "risk")
    assignments: list[ScoutAssignment] = []
    for role in roles:
        queries: list[str] = []
        for family in _ROLE_FAMILIES[role]:
            queries.extend(groups.get(family, [])[:2])
        context_copy = {k: (list(v) if isinstance(v, list) else v) for k, v in context.items()}
        assignments.append(
            ScoutAssignment(
                assignment_id=f"scout-{role}",
                session_id=session_id,
                as_of=as_of,
                role=role,
                question=question,
                tickers=list(scoped),
                context=context_copy,
                queries=list(queries),
                baseline=list(baseline),
            )
        )
    return assignments


def _check_claim_refs(claim: GroundedClaim, known_set: set[str], session_id: str, journal: Callable[[str, dict[str, object]], None] | None) -> None:
    """Fail-closed ref check: non-empty text, cited, freeze-contained ids."""
    if not claim.text.strip():
        raise ModelOutputFailure("each claim needs non-empty text")
    if not claim.evidence_ids:
        raise ModelOutputFailure(f"uncited finding: {claim.text[:120]!r}")
    for eid in claim.evidence_ids:
        if eid not in known_set:
            if journal is not None:
                journal("evidence.rejected", {"session_id": session_id, "evidence_id": eid})
            raise ModelOutputFailure(f"unknown evidence id {eid!r}")


def _merge_claim(findings: list[GroundedClaim], claim: GroundedClaim) -> None:
    """Append new claim text or union evidence ids into the prior same-text claim."""
    prior = next((c for c in findings if c.text == claim.text), None)
    if prior is None:
        findings.append(GroundedClaim(text=claim.text, evidence_ids=list(dict.fromkeys(claim.evidence_ids))))
    else:
        merged_ids = list(dict.fromkeys([*prior.evidence_ids, *claim.evidence_ids]))
        findings[findings.index(prior)] = GroundedClaim(text=prior.text, evidence_ids=merged_ids)

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
    """Merge scout outputs into one validated dossier (wave_id stored as int)."""
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
        for claim in list(result.findings or []):
            _check_claim_refs(claim, known_set, session_id, journal)
            _merge_claim(findings, claim)
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
    "build_query_families",
    "build_research_context",
    "decompose_question",
    "expand_queries",
    "expansion_stop",
    "is_sec_tool",
    "normalize_query",
]
