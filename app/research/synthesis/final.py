"""Final synthesis: rich FinalResearchResult + legacy answer, no recommendation.

The synthesizer never issues a forced buy/sell/hold call; ``answer`` (and
``executive_summary``) summarize what the frozen evidence supports and name
what would change it. The rich result keeps every factual claim tied to raw
SEC evidence ids: impact channels, first/second-order effects, bull/bear
cases, disagreements, uncertainties, limitations, and scope.

Fake-model sketch (no live calls): canned trio of analyses -> canned
``CommitteeDisagreement`` -> ``synthesize_final``; assert claims stay
within the freeze and ``answer`` is non-empty with no invented evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.research.agents import GroundedClaim
from app.research.agents.bearbot import BearAnalysis
from app.research.agents.bullbot import BullAnalysis
from app.research.agents.stockbot import StockbotAnalysis

from .committee import CommitteeDisagreement, _coerce_wave_id


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

    def __post_init__(self) -> None:
        self.wave_id = _coerce_wave_id(self.wave_id)

    def to_dict(self) -> dict[str, object]:
        """Rich FinalResearchResult dict (legacy answer/claims keys kept)."""
        claims_json = [{"text": c.text, "evidence_ids": list(c.evidence_ids)} for c in self.claims]
        scope = {"allowed_sources": ["SEC"], **(self.research_scope or {})}
        return {
            "answer": self.answer,
            "executive_summary": self.executive_summary or self.answer,
            "consensus": self.consensus,
            "base_case": self.base_case,
            "bull_case": {"summary": self.bull_case, "evidence_ids": list(self.bull_evidence_ids)},
            "bear_case": {"summary": self.bear_case, "evidence_ids": list(self.bear_evidence_ids)},
            "impact_channels": [dict(ch) for ch in self.impact_channels],
            "first_order_effects": [dict(e) for e in self.first_order_effects],
            "second_order_effects": [dict(e) for e in self.second_order_effects],
            "critical_disagreements": list(self.critical_disagreements),
            "uncertainties": list(self.unknowns),
            "what_would_change": list(self.what_would_change),
            "evidence_limitations": list(self.evidence_limitations),
            "grounded_claims": [dict(r) for r in claims_json],
            "claims": [dict(r) for r in claims_json],
            "research_scope": scope,
            "freeze_id": self.freeze_id,
            "as_of": self.as_of,
        }


def _bare_text(text: str) -> str:
    """Claim text without a leading [CLASS] tag (merge key stays stable across passes)."""
    if text.startswith("["):
        head, _, rest = text[1:].partition("] ")
        if rest and head in ("DIRECTLY_SUPPORTED", "INFERENCE", "UNKNOWN", "CONTRADICTED"):
            return rest
    return text


def _merge_final_claims(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[GroundedClaim]:
    from app.research.agents import classify_claim
    claims: list[GroundedClaim] = []
    for claim in (*getattr(stock, "claims", []), *getattr(bull, "claims", []), *getattr(bear, "claims", [])):
        bare = _bare_text(claim.text)
        prior = next((c for c in claims if _bare_text(c.text) == bare), None)
        if prior is None:
            label = classify_claim(bare, cited=bool(list(claim.evidence_ids)))
            claims.append(GroundedClaim(text=f"[{label}] {bare}", evidence_ids=list(claim.evidence_ids)))
        else:
            merged = list(dict.fromkeys([*prior.evidence_ids, *claim.evidence_ids]))
            label = classify_claim(_bare_text(prior.text), cited=bool(merged))
            claims[claims.index(prior)] = GroundedClaim(text=f"[{label}] {_bare_text(prior.text)}", evidence_ids=merged)
    return claims


def _union_texts(groups: tuple[list[str], ...]) -> list[str]:
    out: list[str] = []
    for group in groups:
        for item in group:
            if item not in out:
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
    """One impact channel in spec shape; None when nameless or ungrounded."""
    explanation = _as_text(_channel_field(entry, "explanation", "text", "why", "assessment"))
    name = _as_text(_channel_field(entry, "name", "title", "text")) or explanation[:80]
    severity = _as_text(_channel_field(entry, "severity", "materiality", "impact")) or "direct"
    ids = _cited_channel_ids(_channel_field(entry, "evidence_ids", "refs"))
    return {"name": name, "severity": severity, "explanation": explanation or name, "evidence_ids": ids} if name and ids else None


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


def _severity_for(bare: str) -> str:
    """Channel severity from the deterministic claim label."""
    from app.research.agents import classify_claim
    label = classify_claim(bare, cited=True)
    return {"DIRECTLY_SUPPORTED": "direct", "INFERENCE": "indirect", "UNKNOWN": "uncertain", "CONTRADICTED": "contested"}.get(label, "direct")


def _channel_from_claim(claim: GroundedClaim) -> dict[str, object] | None:
    """Fallback channel derived from one grounded claim (always cited)."""
    bare = _bare_text(claim.text).strip()
    ids = [e for e in claim.evidence_ids if isinstance(e, str) and e.strip()]
    if not bare or not ids:
        return None
    return {"name": bare[:80], "severity": _severity_for(bare), "explanation": bare, "evidence_ids": ids}


def _refs_of(analysis: StockbotAnalysis | BullAnalysis | BearAnalysis) -> list[str]:
    """Union of an analysis' claim evidence ids in first-seen order."""
    out: list[str] = []
    for claim in getattr(analysis, "claims", []) or []:
        for eid in getattr(claim, "evidence_ids", []) or []:
            if isinstance(eid, str) and eid.strip() and eid not in out:
                out.append(eid)
    return out


def _channels_from_claims(claims: list[GroundedClaim]) -> list[dict[str, object]]:
    """Fallback channels derived from grounded claims (deduped, grounded only)."""
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


def _split_effects(stock: StockbotAnalysis, merged: list[GroundedClaim]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """First-order (balanced/direct) vs second-order (stance/inferred) effects, all cited."""
    from app.research.agents import classify_claim
    stock_texts: set[str] = {_bare_text(c.text).strip() for c in getattr(stock, "claims", []) or [] if getattr(c, "text", None)}
    first: list[dict[str, object]] = []
    second: list[dict[str, object]] = []
    for claim in merged:
        bare = _bare_text(claim.text).strip()
        ids = list(claim.evidence_ids)
        if not bare or not ids:
            continue
        row: dict[str, object] = {"text": bare, "evidence_ids": ids}
        if bare in stock_texts or classify_claim(bare, cited=True) == "DIRECTLY_SUPPORTED":
            first.append(row)
        else:
            second.append(row)
    return first, second


def _coerce_extra_claim(item: object) -> GroundedClaim | None:
    """One caller claim in grounded shape; None when blank, ungrounded, or mistyped."""
    if isinstance(item, GroundedClaim):
        raw_text, raw_ids = item.text, item.evidence_ids
    elif isinstance(item, Mapping):
        raw_text, raw_ids = _channel_field(item, "text", "claim_text", "claim"), _channel_field(item, "evidence_ids", "refs")
    else:
        return None
    text = raw_text.strip() if isinstance(raw_text, str) else ""
    ids = _cited_channel_ids(raw_ids)
    return GroundedClaim(text=text[:500], evidence_ids=ids) if text and ids else None


def _normalize_extra(extra: Sequence[GroundedClaim | dict[str, object]] | None) -> list[GroundedClaim]:
    """Caller claims into grounded records (drops ungrounded entries)."""
    return [claim for claim in (_coerce_extra_claim(item) for item in extra or []) if claim is not None]


def _merge_extra(claims: list[GroundedClaim], extra: list[GroundedClaim]) -> list[GroundedClaim]:
    """Fold caller claims into the trio merge (bare-text dedupe, id union)."""
    from app.research.agents import classify_claim
    merged = list(claims)
    for claim in extra:
        bare = _bare_text(claim.text).strip()
        prior = next((c for c in merged if _bare_text(c.text).strip() == bare), None)
        if prior is None:
            label = classify_claim(bare, cited=True)
            merged.append(GroundedClaim(text=f"[{label}] {bare}", evidence_ids=list(claim.evidence_ids)))
        else:
            ids = list(dict.fromkeys([*prior.evidence_ids, *claim.evidence_ids]))
            label = classify_claim(_bare_text(prior.text).strip(), cited=bool(ids))
            merged[merged.index(prior)] = GroundedClaim(text=f"[{label}] {_bare_text(prior.text).strip()}", evidence_ids=ids)
    return merged


def _normalize_scope(scope: Mapping[str, object] | None) -> dict[str, object]:
    """Research scope in spec shape; SEC-only default."""
    raws: list[object] = [scope.get(key) for key in ("allowed_sources", "allowed")] if isinstance(scope, Mapping) else []
    allowed: list[str] = next(([s.strip() for s in raw if isinstance(s, str) and s.strip()] for raw in raws if isinstance(raw, (list, tuple))), [])
    return {"allowed_sources": allowed or ["SEC"]}


def _synth_unknowns(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis, disagreement: CommitteeDisagreement) -> list[str]:
    """Deduped uncertainties across the trio plus the committee critical list."""
    return list(dict.fromkeys((*_analysis_uncertainties(stock, bull, bear), *_strs(getattr(disagreement, "critical_uncertainties", [])))))


def _synth_changes(stock: StockbotAnalysis, bull: BullAnalysis, bear: BearAnalysis) -> list[str]:
    """Union of the trio what-would-change lists in first-seen order."""
    return _union_texts((list(getattr(stock, "what_would_change", []) or []), list(getattr(bull, "what_would_change", []) or []), list(getattr(bear, "what_would_change", []) or [])))


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

    ``model`` is accepted for API symmetry but unused: final text is the
    deterministic join below so synthesis stays reproducible. Pass a string
    to override the joined answer (e.g. a canned model draft in tests).
    Cited ids derive only from accepted per-claim mappings, never the whole freeze.
    """
    claims = _merge_extra(_merge_final_claims(stock, bull, bear), _normalize_extra(extra_claims))
    unknowns = _synth_unknowns(stock, bull, bear, disagreement)
    changes = _synth_changes(stock, bull, bear)
    joined = (
        f"Balanced: {stock.base_case} Bull: {bull.bull_case} Bear: {bear.bear_case} "
        f"Agreed: {'; '.join(disagreement.agreement) or 'none stated'}."
    )
    answer = joined if model is None else str(model)
    channels = _channels_from_analyses(stock, bull, bear) or _channels_from_claims(claims)
    first, second = _split_effects(stock, claims)
    lims = [v.strip() for v in (evidence_limitations or []) if isinstance(v, str) and v.strip()]
    return FinalSynthesis(
        session_id=session_id,
        wave_id=_coerce_wave_id(wave_id),
        freeze_id=freeze_id,
        as_of=as_of,
        question=question,
        answer=answer,
        base_case=stock.base_case,
        bull_case=bull.bull_case,
        bear_case=bear.bear_case,
        disagreement=disagreement,
        unknowns=unknowns,
        what_would_change=changes,
        claims=claims,
        executive_summary=answer,
        consensus=_consensus_text(disagreement),
        impact_channels=channels,
        first_order_effects=first,
        second_order_effects=second,
        bull_evidence_ids=_refs_of(bull),
        bear_evidence_ids=_refs_of(bear),
        critical_disagreements=_critical_disagreements(disagreement),
        evidence_limitations=lims,
        research_scope=_normalize_scope(research_scope),
    )


__all__ = ["FinalSynthesis", "synthesize_final"]
