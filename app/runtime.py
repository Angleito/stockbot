"""Pi tool-dispatch budget, tool-result telemetry, and Pi lifecycle events.

Pure-stdlib module (no app imports): ExecutionBudget enforces Pi tool-call
limits, ToolResultMeta carries best-effort result telemetry, and EventType
names the lifecycle values emitted by the Pi bridge and run store.
"""

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True)
class ToolResultMeta:
    row_count: int
    returned_count: int | None
    truncated: bool
    as_of: str | None
    source_names: list[str]
    source_freshness: dict[str, str]


@dataclass
class ExecutionBudget:
    """Pi tool-dispatch budget: consumption counters + hard limits.

    RunRecorder (telemetry) only observes consumption; this object is the
    source of truth for limit enforcement, so observability failures never
    change research behavior. reserve_* methods consume BEFORE an external
    call.
    """

    max_tool_calls: int
    max_runtime: float
    max_evidence_tokens: int
    tool_calls: int = 0
    evidence_tokens: int = 0
    max_search_calls: int = 25
    search_calls: int = 0
    _started: float = field(default_factory=time.perf_counter, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def runtime_remaining(self) -> float:
        with self._lock:
            return max(0.0, self.max_runtime - (time.perf_counter() - self._started))

    def reserve_tool_call(self) -> bool:
        """Consume one tool-call slot; False when runtime or the call limit
        is exhausted."""
        with self._lock:
            if self.max_runtime - (time.perf_counter() - self._started) <= 0:
                return False
            if self.tool_calls >= self.max_tool_calls:
                return False
            self.tool_calls += 1
            return True

    def reserve_search_call(self) -> bool:
        """Consume one search-call slot; False when runtime or the search limit
        is exhausted."""
        with self._lock:
            if self.max_runtime - (time.perf_counter() - self._started) <= 0:
                return False
            if self.search_calls >= self.max_search_calls:
                return False
            self.search_calls += 1
            return True


    def add_evidence_tokens(self, count: int) -> bool:
        """Register evidence tokens only while within budget; False refuses the addition."""
        with self._lock:
            if self.evidence_tokens + count > self.max_evidence_tokens:
                return False
            self.evidence_tokens += count
            return True


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    EVIDENCE_ADDED = "evidence_added"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
