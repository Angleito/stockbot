"""Final synthesis: rich FinalResearchResult + legacy answer, no recommendation.

The synthesizer never issues a forced buy/sell/hold call. ``to_dict`` carries
the deep sections (bottom line, direct evidence, first/second-order impact,
base/bull/bear, critical disagreements, unknowns, what would change the view,
source limitations, filing references) with every claim's declared
``claim_type`` preserved, so an inference is never rendered as direct fact.
Absence stays scoped ("No disclosure located within the searched SEC scope"),
never a real-world nonexistence claim, and depth follows the researched
material (no fixed word count, no caps).

Fake-model sketch (no live calls): canned trio of analyses -> canned
``CommitteeDisagreement`` -> ``synthesize_final``; assert claims stay
within the freeze and ``answer`` is non-empty with no invented evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.research.agents import (
    CLAIM_TYPES,
    GroundedClaim,
    claims_refs,
    conservative_claim_type,
)
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis

from .committee import CommitteeDisagreement, _coerce_wave_id

SEC_SCOPE_ABSENCE = "No disclosure located within the searched SEC scope"
"""Scoped-absence phrasing: a searched-scope observation, never nonexistence."""

SEC_ONLY_LIMITATION = (
    "SEC-only scope: no non-SEC source (news, transcripts, private documents, "
    "market data) was searched."
)

_CLAIM_SEVERITY = {
    "observed_fact": "direct",
    "inference": "indirect",
    "unknown": "uncertain",
    "contradicted": "contested",
}


@dataclass
class FinalSynthesis:
    session_id: str
    wave_id: int
    freeze_id: str
    as_of: str
    question: str
    answer: str
    base_case: str
    bull_case: str
    bear_case: str
    disagreement: CommitteeDisagreement
    unknowns: list[str] = field(default_factory=list)
    what_would_change: list[str] = field(default_factory=list)
    claims: list[GroundedClaim] = field(default_factory=list)
    executive_summary: str = ""
    consensus: str = ""
    impact_channels: list[dict[str, object]] = field(default_factory=list)
    first_order_effects: list[dict[str, object]] = field(default_factory=list)
    second_order_effects: list[dict[str, object]] = field(default_factory=list)
    bull_evidence_ids: list[str] = field(default_factory=list)
    bear_evidence_ids: list[str] = field(default_factory=list)
    critical_disagreements: list[str] = field(default_factory=list)
    evidence_limitations: list[str] = field(default_factory=list)
    research_scope: dict[str, object] = field(default_factory=dict)
    direct_evidence: list[dict[str, object]] = field(default_factory=list)
    absence_observations: list[str] = field(default_factory=list)
    filing_references: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)

    def to_dict(self) -> dict[str, object]:
        """Deep FinalResearchResult dict (legacy answer/claims keys kept)."""
        claim_rows = [
            {"text": c.text, "claim_type": c.claim_type, "evidence_ids": list(c.evidence_ids)}
            for c in self.claims
        ]
        return {
            "answer": self.answer,
            "executive_summary": self.executive_summary or self.answer,
            "consensus": self.consensus,
            "base_case": self.base_case,
            "direct_evidence": [dict(row) for row in self.direct_evidence],
            "bull_case": {"summary": self.bull_case, "evidence_ids": list(self.bull_evidence_ids)},
            "bear_case": {"summary": self.bear_case, "evidence_ids": list(self.bear_evidence_ids)},
            "impact_channels": [dict(ch) for ch in self.impact_channels],
            "first_order_effects": [dict(e) for e in self.first_order_effects],
            "second_order_effects": [dict(e) for e in self.second_order_effects],
            "critical_disagreements": list(self.critical_disagreements),
            "uncertainties": _union_texts((list(self.unknowns), list(self.absence_observations))),
            "absence_observations": list(self.absence_observations),
            "what_would_change": list(self.what_would_change),
            "evidence_limitations": list(self.evidence_limitations),
            "grounded_claims": [dict(r) for r in claim_rows],
            "claims": [dict(r) for r in claim_rows],
            "filing_references": list(self.filing_references),
            "research_scope": dict(self.research_scope),
            "freeze_id": self.freeze_id,
            "as_of": self.as_of,
        }


def scoped_absence(text: str) -> str:
    """Scoped absence phrasing: what a searched scope located, never real-world nonexistence."""
    clean = text.strip()[:2000]
    if not clean:
        return f"{SEC_SCOPE_ABSENCE}."
    if clean.lower().startswith("no disclosure"):
        return clean
    return f"{SEC_SCOPE_ABSENCE}: {clean}"


def _union_texts(groups: tuple[Sequence[str], ...]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in out:
                out.append(item)
    return out


def _strs(values: object) -> list[str]:
    """Non-empty strings from a list payload ([] for anything else)."""
    if not isinstance(values, (list, tuple)):
        return []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _consensus_text(disagreement: CommitteeDisagreement) -> str:
    """Committee consensus: dedicated field when present, else joined agreement."""
    for attr in ("consensus", "agreement"):
        joined = "; ".join(_strs(getattr(disagreement, attr, None)))
        if joined:
            return joined
    return "none stated"


def _critical_disagreements(disagreement: CommitteeDisagreement) -> list[str]:
    """Dedicated critical-disagreement field when present, else the legacy split."""
    raw = getattr(disagreement, "critical_disagreements", None)
    if isinstance(raw, (list, tuple)):
        out = [v.strip() for v in raw if isinstance(v, str) and v.strip()]
        if out:
            return out
    return _strs(getattr(disagreement, "disagreement", []))


def _analysis_uncertainties(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[str]:
    """Uncertainties across trio analyses (tolerates the newer uncertainties attr)."""
    out: list[str] = []
    for analysis in (stock, bull, bear):
        for attr in ("uncertainties", "unknowns"):
            for item in _strs(getattr(analysis, attr, [])):
                if item not in out:
                    out.append(item)
    return out


def _channel_field(entry: object, key: str, *aliases: str) -> object:
    """First present alias value (Mapping get, else getattr; skips None)."""
    for candidate in (key, *aliases):
        value: object = entry.get(candidate, None) if isinstance(entry, Mapping) else getattr(entry, candidate, None)
        if value is not None:
            return value
    return None


def _as_text(value: object) -> str:
    """Coerce one optional prose field (non-strings stay blank)."""
    return value.strip() if isinstance(value, str) else ""


def _cited_channel_ids(raw_ids: object) -> list[str]:
    """Non-blank string ids from a channel payload ([] for anything else)."""
    return [e for e in raw_ids if isinstance(e, str) and e.strip()] if isinstance(raw_ids, (list, tuple)) else []


def _normalize_channel(entry: object) -> dict[str, object] | None:
    """One impact channel in render shape (new {text, direction} or legacy {name, assessment}); None when ungrounded."""
    text = _as_text(_channel_field(entry, "text", "name", "title", "explanation"))
    detail = _as_text(_channel_field(entry, "explanation", "assessment")) or text
    direction = _as_text(_channel_field(entry, "direction", "severity", "materiality", "impact"))
    ids = _cited_channel_ids(_channel_field(entry, "evidence_ids", "refs"))
    if not text or not ids:
        return None
    return {"name": text[:80], "severity": direction or "unspecified", "explanation": detail, "evidence_ids": ids}


def _channels_from_analyses(stock: object, bull: object, bear: object) -> list[dict[str, object]]:
    """Committee-provided impact channels when present (deduped, grounded only; duck-typed)."""
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for analysis in (stock, bull, bear):
        raw = getattr(analysis, "impact_channels", None)
        if not isinstance(raw, (list, tuple)):
            continue
        for entry in raw:
            chan = _normalize_channel(entry)
            if chan is None:
                continue
            key = str(chan["name"]) + "|" + str(chan["explanation"])
            if key in seen:
                continue
            seen.add(key)
            out.append(chan)
    return out


def _channel_from_claim(claim: GroundedClaim) -> dict[str, object] | None:
    """Fallback channel derived from one cited claim (None when uncited)."""
    text = claim.text.strip()
    ids = [e for e in claim.evidence_ids if isinstance(e, str) and e.strip()]
    if not text or not ids:
        return None
    return {"name": text[:80], "severity": _CLAIM_SEVERITY.get(claim.claim_type, "indirect"),
            "explanation": text, "evidence_ids": ids}


def _channels_from_claims(claims: list[GroundedClaim]) -> list[dict[str, object]]:
    """Fallback channels derived from claims (deduped, grounded only)."""
    out: list[dict[str, object]] = []
    seen: set[str] = set()
    for claim in claims:
        chan = _channel_from_claim(claim)
        if chan is None:
            continue
        key = str(chan["name"]) + "|" + str(chan["explanation"])
        if key in seen:
            continue
        seen.add(key)
        out.append(chan)
    return out


def _effect_row(claim: GroundedClaim) -> dict[str, object]:
    """One effect row: claim text with its declared type and freeze refs."""
    return {"text": claim.text, "claim_type": claim.claim_type, "evidence_ids": list(claim.evidence_ids)}


def _split_effects(claims: Sequence[GroundedClaim]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Directly observed claims vs reasoned/contested claims; unknown reads stay in the unknowns section."""
    first = [_effect_row(c) for c in claims if c.claim_type == "observed_fact"]
    second = [_effect_row(c) for c in claims if c.claim_type in ("inference", "contradicted")]
    return first, second


def _coerce_extra_claim(item: object) -> GroundedClaim | None:
    """One caller claim in grounded shape; None when blank, ungrounded, or mistyped."""
    if isinstance(item, GroundedClaim):
        raw_text, raw_type, raw_ids = item.text, item.claim_type, item.evidence_ids
    elif isinstance(item, Mapping):
        raw_text = _channel_field(item, "text", "claim_text", "claim")
        raw_type = _channel_field(item, "claim_type")
        raw_ids = _channel_field(item, "evidence_ids", "refs")
    else:
        return None
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    declared = raw_type.strip().lower() if isinstance(raw_type, str) else ""
    claim_type = declared if declared in CLAIM_TYPES else "inference"
    ids = _cited_channel_ids(raw_ids)
    if not text or not (ids or claim_type == "unknown"):
        return None
    return GroundedClaim(text=text[:500], claim_type=claim_type, evidence_ids=ids)


def _normalize_extra(extra: Sequence[GroundedClaim | dict[str, object]] | None) -> list[GroundedClaim]:
    """Caller claims into grounded records (drops ungrounded entries)."""
    return [claim for claim in (_coerce_extra_claim(item) for item in extra or []) if claim is not None]


def _merge_claims(*groups: Sequence[GroundedClaim]) -> list[GroundedClaim]:
    """Dedupe the trio + caller claims by text: union refs, keep the least assertive declared type."""
    merged: list[GroundedClaim] = []
    index: dict[str, int] = {}
    for claim in (claim for group in groups for claim in group):
        text = claim.text.strip()
        if not text:
            continue
        prior_index = index.get(text)
        if prior_index is None:
            index[text] = len(merged)
            merged.append(GroundedClaim(text=text, claim_type=claim.claim_type, evidence_ids=list(dict.fromkeys(claim.evidence_ids))))
            continue
        prior = merged[prior_index]
        merged[prior_index] = GroundedClaim(
            text=text,
            claim_type=conservative_claim_type([prior.claim_type, claim.claim_type]),
            evidence_ids=list(dict.fromkeys([*prior.evidence_ids, *claim.evidence_ids])),
        )
    return merged


def _normalize_scope(scope: Mapping[str, object] | None) -> dict[str, object]:
    """Research scope in spec shape; SEC-only default carries its explicit limitation."""
    candidates = (scope.get("allowed_sources"), scope.get("allowed")) if isinstance(scope, Mapping) else ()
    sources = next((_strs(raw) for raw in candidates if isinstance(raw, (list, tuple))), None) or ["SEC"]
    sec_only = [s.upper() for s in sources] == ["SEC"]
    return {"allowed_sources": sources, "sec_only": sec_only,
            "limitation": SEC_ONLY_LIMITATION if sec_only else ""}


def _synth_unknowns(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis, disagreement: CommitteeDisagreement) -> list[str]:
    """Deduped uncertainties across the trio plus the committee critical list."""
    return list(dict.fromkeys((*_analysis_uncertainties(stock, bull, bear), *_strs(getattr(disagreement, "critical_uncertainties", [])))))


def _synth_changes(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[str]:
    """Union of the trio what-would-change lists in first-seen order."""
    return _union_texts((list(getattr(stock, "what_would_change", []) or []), list(getattr(bull, "what_would_change", []) or []), list(getattr(bear, "what_would_change", []) or [])))


def _synth_limitations(evidence_limitations: Sequence[str] | None, scope: Mapping[str, object]) -> list[str]:
    """Caller limitations plus the explicit SEC-only boundary when the policy is SEC-only."""
    lims = [v.strip() for v in (evidence_limitations or []) if isinstance(v, str) and v.strip()]
    if scope.get("sec_only") is True:
        lims = _union_texts((lims, [SEC_ONLY_LIMITATION]))
    return lims


def _effect_line(row: Mapping[str, object]) -> str:
    """One rendered evidence/effect line preserving its declared claim_type."""
    text = _as_text(row.get("text"))
    claim_type = _as_text(row.get("claim_type")) or "inference"
    ids = _cited_channel_ids(row.get("evidence_ids"))
    refs = f" [{', '.join(ids)}]" if ids else ""
    return f"{text} ({claim_type}){refs}"


def _section(title: str, lines: Sequence[str]) -> list[str]:
    """One rendered section ([] when it has no lines)."""
    return [f"{title}:", *(f"- {line}" for line in lines if line)] if any(lines) else []


def _deep_answer(synth: FinalSynthesis) -> str:
    """Deterministic deep answer: every required section, depth taken from the researched material."""
    out: list[str] = [f"Bottom line: {synth.executive_summary}"]
    if synth.consensus and synth.consensus.lower() != "none stated":
        out.append(f"Consensus: {synth.consensus}")
    out += _section("What the evidence directly shows", [_effect_line(row) for row in synth.direct_evidence])
    out += _section("First-order impact", [_effect_line(row) for row in synth.first_order_effects])
    out += _section("Second-order impact", [_effect_line(row) for row in synth.second_order_effects])
    out += _section("Base case (stockbot)", [synth.base_case])
    out += _section("Bull case (bullbot)", [synth.bull_case])
    out += _section("Bear case (bearbot)", [synth.bear_case])
    out += _section("Critical disagreements", synth.critical_disagreements)
    out += _section("Unknowns / unresolved", _union_texts((synth.unknowns, synth.absence_observations)))
    out += _section("What would change the view", synth.what_would_change)
    out += _section("Source limitations", synth.evidence_limitations)
    if synth.filing_references:
        out.append(f"Filing references: {', '.join(synth.filing_references)}")
    return "\n".join(out)


def synthesize_final(
    question: str,
    *,
    session_id: str,
    wave_id: int | str,
    freeze_id: str,
    as_of: str,
    stock: StockbotAnalysis,
    bull: BullAnalysis,
    bear: BearAnalysis,
    disagreement: CommitteeDisagreement,
    model: object = None,
    extra_claims: Sequence[GroundedClaim | dict[str, object]] | None = None,
    evidence_limitations: Sequence[str] | None = None,
    research_scope: Mapping[str, object] | None = None,
) -> FinalSynthesis:
    """Package the trio + disagreement + caller claims into the final record (no live calls).

    ``model`` overrides the deterministic deep answer when it is a string
    (e.g. the caller's drafted answer); otherwise the sections are joined
    deterministically so synthesis stays reproducible. Cited ids derive only
    from accepted per-claim mappings, never the whole freeze.
    """
    claims = _merge_claims(
        list(getattr(stock, "claims", []) or []),
        list(getattr(bull, "claims", []) or []),
        list(getattr(bear, "claims", []) or []),
        _normalize_extra(extra_claims),
    )
    scope = _normalize_scope(research_scope)
    direct_evidence, second_order = _split_effects(claims)
    synth = FinalSynthesis(
        session_id=session_id,
        wave_id=_coerce_wave_id(wave_id),
        freeze_id=freeze_id,
        as_of=as_of,
        question=question,
        answer="",
        base_case=stock.base_case,
        bull_case=bull.bull_case,
        bear_case=bear.bear_case,
        disagreement=disagreement,
        unknowns=_synth_unknowns(stock, bull, bear, disagreement),
        what_would_change=_synth_changes(stock, bull, bear),
        claims=claims,
        executive_summary=stock.executive_view or stock.base_case or "No grounded SEC findings.",
        consensus=_consensus_text(disagreement),
        impact_channels=_channels_from_analyses(stock, bull, bear) or _channels_from_claims(claims),
        first_order_effects=direct_evidence,
        second_order_effects=second_order,
        bull_evidence_ids=claims_refs(bull.claims),
        bear_evidence_ids=claims_refs(bear.claims),
        critical_disagreements=_critical_disagreements(disagreement),
        evidence_limitations=_synth_limitations(evidence_limitations, scope),
        research_scope=scope,
        direct_evidence=direct_evidence,
        absence_observations=[scoped_absence(c.text) for c in claims if c.claim_type == "unknown"],
        filing_references=claims_refs(claims),
    )
    synth.answer = _deep_answer(synth) if model is None else str(model)
    return synth


__all__ = ["SEC_ONLY_LIMITATION", "SEC_SCOPE_ABSENCE", "FinalSynthesis", "scoped_absence", "synthesize_final"]
