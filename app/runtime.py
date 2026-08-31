"""Typed runtime objects for the research loop.

Pure-stdlib module (no app imports): request/plan/budget/state/result
dataclasses plus the event/tool/model records and the EventType enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True)
class ResearchRequest:
    request_id: str
    question: str
    created_at: str
    as_of: str
    principal: str
    capabilities: tuple[str, ...]
    preferred_model: str
    mode: str


@dataclass
class ResearchPlan:
    question: str
    entities: list[str]
    as_of: str
    hypotheses: list[str]
    required_data: list[dict]

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "entities": self.entities,
            "as_of": self.as_of,
            "hypotheses": self.hypotheses,
            "required_data": self.required_data,
        }


@dataclass(frozen=True)
class BudgetRemaining:
    rounds: int
    tool_calls: int
    model_calls: int
    runtime_seconds: float
    evidence_tokens: int


@dataclass
class AgentState:
    run_id: str
    plan: ResearchPlan
    round: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    budget_remaining: BudgetRemaining | None = None


@dataclass(frozen=True)
class ResearchResult:
    run_id: str
    answer: str
    claims: list[dict]
    evidence_refs: list[str]
    counterevidence_refs: list[str]
    confidence: float
    data_freshness: dict[str, str]
    unresolved_questions: list[str]
    completed_at: str


@dataclass(frozen=True)
class AgentEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    started_at: str
    completed_at: str | None
    duration_ms: float | None
    round: int | None
    model: str | None
    tool_name: str | None
    arguments: str | None
    result_summary: str | None
    success: bool | None
    error_type: str | None
    evidence_ids: str | None
    metadata: str | None


@dataclass(frozen=True)
class ToolCall:
    tool_call_id: str
    run_id: str
    round: int
    tool_name: str
    tool_version: str
    arguments_json: str
    started_at: str
    completed_at: str
    duration_ms: float
    status: str
    result_row_count: int
    result_bytes: int
    result_hash: str
    source_names: str
    source_freshness: str
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True)
class ModelCall:
    model_call_id: str
    run_id: str
    round: int
    provider: str
    model: str
    started_at: str
    completed_at: str
    duration_ms: float
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cached_tokens: int
    estimated_cost: float
    finish_reason: str | None
    tool_call_count: int
    provider_request_id: str | None


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_CREATED = "plan_created"
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    EVIDENCE_ADDED = "evidence_added"
    CLAIM_CREATED = "claim_created"
    CLAIM_UPDATED = "claim_updated"
    CHALLENGE_STARTED = "challenge_started"
    CHALLENGE_COMPLETED = "challenge_completed"
    FINALIZATION_STARTED = "finalization_started"
    FINAL_ANSWER_CREATED = "final_answer_created"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
