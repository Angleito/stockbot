"""Typed runtime objects for the research loop.

Pure-stdlib module (no app imports): request/plan/budget/state/result
dataclasses plus the event/tool/model records and the EventType enum.
"""

import time
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
    as_of: str

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "as_of": self.as_of,
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
    failures: list[dict] = field(default_factory=list)
    budget_remaining: BudgetRemaining | None = None


@dataclass(frozen=True)
class ResearchResult:
    run_id: str
    answer: str
    evidence_refs: list[str]
    groundedness: str
    data_freshness: dict[str, str]
    completed_at: str


@dataclass(frozen=True)
class ToolResultMeta:
    row_count: int
    returned_count: int | None
    truncated: bool
    as_of: str | None
    source_names: list[str]
    source_freshness: dict[str, str]


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
    returned_count: int | None
    truncated: bool
    result_bytes: int
    result_hash: str
    source_names: str
    source_freshness: str
    as_of: str | None
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


class BudgetExhaustedError(RuntimeError):
    """Raised by nested model helpers when no model-call budget remains."""


@dataclass
class ExecutionBudget:
    """Enforcement-side run budget: consumption counters + hard limits.

    RunRecorder (telemetry) only observes consumption; this object is the
    source of truth for limit enforcement, so observability failures never
    change research behavior. reserve_* methods consume BEFORE an external
    call; remaining() reports the typed BudgetRemaining view.
    """

    max_rounds: int
    max_tool_calls: int
    max_model_calls: int
    max_runtime: float
    max_evidence_tokens: int
    tool_calls: int = 0
    model_calls: int = 0
    evidence_tokens: int = 0
    _started: float = field(default_factory=time.perf_counter, init=False, repr=False)

    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self._started

    def model_calls_remaining(self) -> int:
        return max(0, self.max_model_calls - self.model_calls)

    def tool_calls_remaining(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls)

    def evidence_remaining(self) -> int:
        return max(0, self.max_evidence_tokens - self.evidence_tokens)

    def runtime_remaining(self) -> float:
        return max(0.0, self.max_runtime - self.elapsed_seconds())

    def reserve_model_call(self) -> bool:
        """Consume one model-call slot; False when runtime or the call limit
        is exhausted."""
        if self.runtime_remaining() <= 0:
            return False
        if self.model_calls >= self.max_model_calls:
            return False
        self.model_calls += 1
        return True

    def reserve_tool_call(self) -> bool:
        """Consume one tool-call slot; False when runtime or the call limit
        is exhausted."""
        if self.runtime_remaining() <= 0:
            return False
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        return True


    def add_evidence_tokens(self, count: int) -> bool:
        """Register evidence tokens only while within budget; False refuses the addition."""
        if self.evidence_tokens + count > self.max_evidence_tokens:
            return False
        self.evidence_tokens += count
        return True

    def remaining(self, rounds_used: int = 0) -> BudgetRemaining:
        """Typed view for AgentState.budget_remaining; rounds come from the loop's state.round."""
        return BudgetRemaining(
            rounds=max(0, self.max_rounds - rounds_used),
            tool_calls=self.tool_calls_remaining(),
            model_calls=self.model_calls_remaining(),
            runtime_seconds=self.runtime_remaining(),
            evidence_tokens=self.evidence_remaining(),
        )


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    RESEARCH_CONTEXT_CREATED = "research_context_created"
    MODEL_REQUESTED = "model_requested"
    MODEL_RESPONDED = "model_responded"
    MODEL_FAILED = "model_failed"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    EVIDENCE_ADDED = "evidence_added"
    FINALIZATION_STARTED = "finalization_started"
    FINAL_ANSWER_CREATED = "final_answer_created"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
