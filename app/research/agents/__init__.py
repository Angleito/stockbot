"""Agent package: shared request type + stable re-exports.

Fake-model sketch (no live calls): build a ``ScoutAssignment``, pass a
``dispatch`` callable wrapping ``app.tools.execute_tool`` (or a dict-returning
fake), pass a ``model`` callable returning canned JSON, assert on
``ScoutResult`` / ``StockbotAnalysis`` fields.
"""

from __future__ import annotations

import json
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


@dataclass
class GroundedClaim:
    """One finding/claim with explicit freeze-contained evidence links."""

    text: str
    evidence_ids: list[str] = field(default_factory=list)


def parse_grounded_claims(text: str, *, frozen: Sequence[str]) -> list[GroundedClaim]:
    """Parse model JSON records; each claim must cite only freeze ids.

    Expected shape: [{"text": "...", "evidence_ids": ["EV-1", ...]}, ...].
    Unknown IDs (e.g. EV-999), empty citations, or blank/non-JSON output
    raise ``ModelOutputFailure``. Represent nothing found explicitly as [].
    """
    frozen_list = [e for e in frozen if isinstance(e, str) and e]
    frozen_set = set(frozen_list)
    stripped = text.strip()
    if not stripped:
        raise ModelOutputFailure("claims must be one JSON document (got blank)")
    try:
        decoded: object = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelOutputFailure(f"claims must be one JSON document: {exc}") from exc
    if not isinstance(decoded, list):
        raise ModelOutputFailure("claims must be a JSON list")
    claims: list[GroundedClaim] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise ModelOutputFailure("each claim must be {text, evidence_ids}")
        if set(item) != {"text", "evidence_ids"}:
            raise ModelOutputFailure("each claim must contain exactly {text, evidence_ids}")
        raw_text = item.get("text")
        raw_ids = item.get("evidence_ids")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ModelOutputFailure("each claim needs non-empty text")
        if not isinstance(raw_ids, list) or not raw_ids or any(not isinstance(e, str) or not e for e in raw_ids):
            raise ModelOutputFailure(f"uncited factual claim: {str(raw_text)[:120]!r}")
        seen: list[str] = []
        for eid in raw_ids:
            if eid not in frozen_set:
                raise ModelOutputFailure(f"unknown evidence id {eid!r}")
            if eid not in seen:
                seen.append(eid)
        claims.append(GroundedClaim(text=raw_text.strip()[:500], evidence_ids=seen))
    return claims


def claims_refs(claims: Sequence[GroundedClaim]) -> list[str]:
    """Union of claim evidence ids in first-seen order."""
    refs: list[str] = []
    for claim in claims:
        for eid in claim.evidence_ids:
            if eid not in refs:
                refs.append(eid)
    return refs


def parse_committee_output(text: str, *, frozen: Sequence[str], agent: str) -> tuple[list[GroundedClaim], list[ResearchRequest]]:
    """Strict JSON envelope: {"claims": [{text, evidence_ids}], "follow_ups": ["Q?", ...]}."""
    stripped = text.strip()
    if not stripped:
        raise ModelOutputFailure("committee output must be one JSON document (got blank)")
    try:
        decoded: object = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ModelOutputFailure(f"committee output must be one JSON document: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ModelOutputFailure("committee output must be a JSON object")
    raw_claims = decoded.get("claims", [])
    raw_follow = decoded.get("follow_ups", [])
    if not isinstance(raw_claims, list):
        raise ModelOutputFailure("committee claims must be a list")
    if not isinstance(raw_follow, list):
        raise ModelOutputFailure("committee follow_ups must be a list")
    claims = parse_grounded_claims(json.dumps(raw_claims), frozen=frozen)
    follow_ups: list[ResearchRequest] = []
    for item in raw_follow[:3]:
        if not isinstance(item, str):
            raise ModelOutputFailure(f"committee follow_up must be a question string, got {type(item).__name__}")
        q = item.strip()
        if len(q) < 12 or len(q) > 500 or not q.endswith("?"):
            raise ModelOutputFailure(f"malformed committee follow_up: {q[:120]!r}")
        follow_ups.append(ResearchRequest(question=q, why_material="committee follow-up", requested_source_domain="SEC", expected_gain="medium", requesting_agents=[agent]))
    return claims, follow_ups


__all__ = ["GroundedClaim", "ModelOutputFailure", "ResearchRequest", "claims_refs", "parse_committee_output", "parse_grounded_claims"]
