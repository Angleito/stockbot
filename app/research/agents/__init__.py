"""Agent package: shared request type + stable re-exports.

Fake-model sketch (no live calls): build a ``ScoutAssignment``, pass a
``dispatch`` callable wrapping ``app.tools.execute_tool`` (or a dict-returning
fake), pass a ``model`` callable returning canned JSON, assert on
``ScoutResult`` / ``StockbotAnalysis`` fields.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.research.models import FailureCategory


@dataclass
class ResearchRequest:
    """One actionable follow-up question from a committee agent."""

    question: str
    why_material: str
    requested_source_domain: str
    expected_gain: str
    requesting_agents: list[str] = field(default_factory=list)


class ModelOutputFailure(ValueError):
    """Model cited an unknown id or left a factual claim uncited (fail-closed)."""

    _failure_category: FailureCategory

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self._failure_category = FailureCategory.MODEL_OUTPUT_FAILURE


CLAIM_CLASSES = ("DIRECTLY_SUPPORTED", "INFERENCE", "UNKNOWN", "CONTRADICTED")
MATERIALITY_LEVELS = ("critical", "high", "medium", "low")

_MANAGEABLE_RE = re.compile(
    r"\b(manageab\w*|immaterial\w*|absorb\w*|contained|digestible|modest|limited\s+impact)\b",
    re.IGNORECASE,
)
_CLAIM_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("CONTRADICT",), "CONTRADICTED"),
    (("UNKNOWN", "UNCERTAIN", "UNCLEAR"), "UNKNOWN"),
    (("INFERENCE", "INFER", "MAY ", "LIKELY", "SUGGEST"), "INFERENCE"),
)


def classify_claim(text: str, *, cited: bool) -> str:
    """Deterministic claim label: uncited manageable/immaterial reads are never DIRECTLY_SUPPORTED."""
    hay = f" {(text or '').upper()} "
    for markers, label in _CLAIM_RULES:
        if any(m in hay for m in markers):
            return label
    if not cited:
        return "INFERENCE" if _MANAGEABLE_RE.search(text or "") else "UNKNOWN"
    return "DIRECTLY_SUPPORTED"


@dataclass
class ImpactChannel:
    """One impact channel with frozen evidence links."""

    name: str
    assessment: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class CommitteeMateriality:
    """Overall materiality judgment (routing only, never a score)."""

    overall: str = "medium"
    reasoning: str = ""


@dataclass
class GroundedClaim:
    """One finding/claim with explicit freeze-contained evidence links."""

    text: str
    evidence_ids: list[str] = field(default_factory=list)

    @property
    def claim_class(self) -> str:
        """Deterministic classification; manageable/immaterial without support is INFERENCE/UNKNOWN."""
        return classify_claim(self.text, cited=bool(self.evidence_ids))

    @property
    def label(self) -> str:
        """Alias for claim_class (eval hook)."""
        return self.claim_class


def _frozen_set(frozen: Sequence[str]) -> set[str]:
    """Known freeze ids (non-empty strings only)."""
    return {e for e in frozen if isinstance(e, str) and e}


def _decode_claims_list(text: str) -> list[object]:
    """Parse one JSON list document from model text."""
    stripped = text.strip()
    if not stripped:
        raise ModelOutputFailure("claims must be one JSON document (got blank)")
    try:
        decoded: object = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelOutputFailure(f"claims must be one JSON document: {exc}") from exc
    if not isinstance(decoded, list):
        raise ModelOutputFailure("claims must be a JSON list")
    return decoded


def _claim_text(item: dict[object, object]) -> str:
    """Validated non-empty claim text (truncation happens at build time; 'statement' aliases 'text')."""
    raw_text = item.get("text", item.get("statement"))
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ModelOutputFailure("each claim needs non-empty text")
    return raw_text


def _claim_ids(
    item: dict[object, object], raw_text: str, frozen_set: set[str]
) -> list[str]:
    """Validated deduped evidence ids, all contained in the freeze."""
    raw_ids = item.get("evidence_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(e, str) or not e for e in raw_ids)
    ):
        raise ModelOutputFailure(f"uncited factual claim: {str(raw_text)[:120]!r}")
    seen: list[str] = []
    for eid in raw_ids:
        if eid not in frozen_set:
            raise ModelOutputFailure(f"unknown evidence id {eid!r}")
        if eid not in seen:
            seen.append(eid)
    return seen


def _build_claim(item: object, frozen_set: set[str]) -> GroundedClaim:
    """Validate one {text, evidence_ids} record against the freeze (statement aliases text)."""
    if not isinstance(item, dict):
        raise ModelOutputFailure("each claim must be {text, evidence_ids}")
    keys = set(item)
    if (
        keys - {"text", "statement", "evidence_ids"}
        or "evidence_ids" not in keys
        or not ({"text", "statement"} & keys)
    ):
        raise ModelOutputFailure("each claim must contain exactly {text, evidence_ids}")
    raw_text = _claim_text(item)
    return GroundedClaim(
        text=raw_text.strip()[:500], evidence_ids=_claim_ids(item, raw_text, frozen_set)
    )


def parse_grounded_claims(text: str, *, frozen: Sequence[str]) -> list[GroundedClaim]:
    """Parse model JSON records; each claim must cite only freeze ids.

    Expected shape: [{"text": "...", "evidence_ids": ["EV-1", ...]}, ...].
    Unknown IDs (e.g. EV-999), empty citations, or blank/non-JSON output
    raise ``ModelOutputFailure``. Represent nothing found explicitly as [].
    """
    frozen_set = _frozen_set(frozen)
    return [_build_claim(item, frozen_set) for item in _decode_claims_list(text)]


def claims_refs(claims: Sequence[GroundedClaim]) -> list[str]:
    """Union of claim evidence ids in first-seen order."""
    refs: list[str] = []
    for claim in claims:
        for eid in claim.evidence_ids:
            if eid not in refs:
                refs.append(eid)
    return refs


def _decode_envelope(text: str) -> dict[object, object]:
    """Parse one JSON object document from committee model text."""
    stripped = text.strip()
    if not stripped:
        raise ModelOutputFailure(
            "committee output must be one JSON document (got blank)"
        )
    try:
        decoded: object = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelOutputFailure(
            f"committee output must be one JSON document: {exc}"
        ) from exc
    if not isinstance(decoded, dict):
        raise ModelOutputFailure("committee output must be a JSON object")
    return decoded


def _envelope_lists(decoded: dict[object, object]) -> tuple[list[object], list[object]]:
    """Validated claims + follow_ups lists from the envelope."""
    raw_claims = decoded.get("claims", [])
    raw_follow = decoded.get("follow_ups", decoded.get("research_requests", []))
    if not isinstance(raw_claims, list):
        raise ModelOutputFailure("committee claims must be a list")
    if not isinstance(raw_follow, list):
        raise ModelOutputFailure("committee follow_ups must be a list")
    return raw_claims, raw_follow


def _follow_up_question(item: object) -> str:
    """Validated follow-up question text from a string or question/text mapping."""
    raw_q: object = item.get("question", item.get("text", "")) if isinstance(item, dict) else item
    if not isinstance(raw_q, str):
        raise ModelOutputFailure(f"committee follow_up must be a question string, got {type(item).__name__}")
    q = raw_q.strip()
    if len(q) < 12 or len(q) > 500 or not q.endswith("?"):
        raise ModelOutputFailure(f"malformed committee follow_up: {q[:120]!r}")
    return q


def _build_follow_up(item: object, agent: str) -> ResearchRequest:
    """Validate one follow-up question string or {question, why_it_matters, suggested_source} mapping."""
    q = _follow_up_question(item)
    why = item.get("why_it_matters", item.get("why_material", "committee follow-up")) if isinstance(item, dict) else "committee follow-up"
    src = item.get("suggested_source", item.get("requested_source_domain", "SEC")) if isinstance(item, dict) else "SEC"
    return ResearchRequest(question=q, why_material=why.strip() if isinstance(why, str) and why.strip() else "committee follow-up", requested_source_domain=src.strip() if isinstance(src, str) and src.strip() else "SEC", expected_gain="medium", requesting_agents=[agent])


def _str_or_blank(value: object, cap: int = 2000) -> str:
    """Strip one optional prose field; non-strings coerce to blank."""
    return value.strip()[:cap] if isinstance(value, str) else ""


def _channel_ids_ok(raw_ids: object, frozen_set: set[str]) -> list[str] | None:
    """Validated deduped channel ids; None when malformed, raises on unknown freeze id."""
    if not isinstance(raw_ids, list) or any(not isinstance(eid, str) or not eid for eid in raw_ids):
        return None
    unknown = next((eid for eid in raw_ids if eid not in frozen_set), None)
    if unknown is not None:
        raise ModelOutputFailure(f"unknown evidence id {unknown!r}")
    return list(dict.fromkeys(raw_ids))


def _parse_channel(item: object, frozen_set: set[str]) -> ImpactChannel | None:
    """One {name, assessment, evidence_ids} channel; None when malformed or ungrounded (skipped)."""
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    seen = _channel_ids_ok(item.get("evidence_ids", []), frozen_set)
    if not isinstance(name, str) or not name.strip() or not seen:
        return None
    return ImpactChannel(name=name.strip()[:200], assessment=_str_or_blank(item.get("assessment")), evidence_ids=seen)


def _parse_materiality(raw: object) -> CommitteeMateriality:
    """Coerce {overall, reasoning}; unknown overall falls back to medium."""
    overall = raw.get("overall") if isinstance(raw, dict) else None
    level = overall.strip().lower() if isinstance(overall, str) else "medium"
    reasoning = raw.get("reasoning") if isinstance(raw, dict) else None
    return CommitteeMateriality(overall=level if level in MATERIALITY_LEVELS else "medium", reasoning=_str_or_blank(reasoning))


def _parse_str_items(raw: object, cap: int = 2000) -> list[str]:
    """Filter one uncertainties list down to stripped strings."""
    if not isinstance(raw, list):
        return []
    return [s.strip()[:cap] for s in raw if isinstance(s, str) and s.strip()]


@dataclass
class CommitteeEnvelope:
    """Expanded role output: claims + impact channels + materiality + uncertainties + requests."""

    claims: list[GroundedClaim] = field(default_factory=list)
    follow_ups: list[ResearchRequest] = field(default_factory=list)
    executive_view: str = ""
    impact_channels: list[ImpactChannel] = field(default_factory=list)
    materiality: CommitteeMateriality = field(default_factory=CommitteeMateriality)
    uncertainties: list[str] = field(default_factory=list)


def parse_committee_envelope(
    text: str, *, frozen: Sequence[str], agent: str
) -> CommitteeEnvelope:
    """Expanded envelope: legacy claims/follow_ups plus executive_view, impact_channels, materiality, uncertainties."""
    decoded = _decode_envelope(text)
    frozen_set = _frozen_set(frozen)
    raw_claims, raw_follow = _envelope_lists(decoded)
    claims = parse_grounded_claims(json.dumps(raw_claims), frozen=frozen)
    follow_ups = [_build_follow_up(item, agent) for item in raw_follow[:3]]
    raw_channels = decoded.get("impact_channels", [])
    parsed: list[ImpactChannel] = []
    if isinstance(raw_channels, list):
        for item in raw_channels:
            channel = _parse_channel(item, frozen_set)
            if channel is not None:
                parsed.append(channel)
    return CommitteeEnvelope(
        claims=claims,
        follow_ups=follow_ups,
        executive_view=_str_or_blank(decoded.get("executive_view")),
        impact_channels=parsed,
        materiality=_parse_materiality(decoded.get("materiality")),
        uncertainties=_parse_str_items(
            decoded.get("uncertainties", decoded.get("unknowns", []))
        )[:10],
    )


def parse_committee_output(
    text: str, *, frozen: Sequence[str], agent: str
) -> tuple[list[GroundedClaim], list[ResearchRequest]]:
    """Strict JSON envelope: {"claims": [{text, evidence_ids}], "follow_ups": ["Q?", ...]} (extra keys ignored for back-compat)."""
    env = parse_committee_envelope(text, frozen=frozen, agent=agent)
    return env.claims, env.follow_ups


__all__ = [
    "CLAIM_CLASSES",
    "MATERIALITY_LEVELS",
    "CommitteeEnvelope",
    "CommitteeMateriality",
    "GroundedClaim",
    "ImpactChannel",
    "ModelOutputFailure",
    "ResearchRequest",
    "claims_refs",
    "classify_claim",
    "parse_committee_envelope",
    "parse_committee_output",
    "parse_grounded_claims",
]
