"""Observability: redaction units and recorder-independent budget reserves.

RUNS_DB_PATH is isolated per session by the root conftest fixture.
"""

import pytest

from app import finra_analysis
from app.redact import redact_json, redact_text, redact_value
from app.runtime import BudgetExhaustedError, ExecutionBudget
from app.security import quarantine_reader
from app.storage.runs import reset_current_budget, set_current_budget


def _usage(**overrides):
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cost": 0.00012,
    }
    usage.update(overrides)
    return usage


def test_redact_text_units():
    assert redact_text("Authorization: Bearer abc123") == "Authorization: Bearer [REDACTED]"
    assert redact_text("key=sk-or-v1-abcdefghijklmnop end") == "key=sk-or-v1-[REDACTED] end"
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert redact_text(jwt) == "eyJ[REDACTED JWT]"
    assert redact_text("plain text") == "plain text"
    # Already-redacted output is stable under re-redaction.
    assert redact_text("Bearer [REDACTED]") == "Bearer [REDACTED]"
    assert redact_text("sk-or-v1-[REDACTED]") == "sk-or-v1-[REDACTED]"
    # Account identifiers in free text (review round 2 P1).
    assert redact_text("My account_number is 12345678") == "My account_number is [REDACTED]"
    assert redact_text("My account_id=87654321") == "My account_id=[REDACTED]"
    # Bare digit runs stay untouched (CIKs/accession numbers are structural).
    assert redact_text("cik 0000320193 filing") == "cik 0000320193 filing"
    assert redact_text("account 2026 taxes") == "account 2026 taxes"


def test_redact_value_units():
    value = {
        "accountNumber": "12345678",
        "nested": {"client_secret": "s3cret", "position_id": "pos-42"},
        "tags": ["a", "Bearer tok"],
        "count": 3,
        "flag": None,
        "provider_instrument_id": "inst-7",
    }
    redacted = redact_value(value)
    assert redacted["accountNumber"] == "[REDACTED]"
    assert redacted["nested"]["client_secret"] == "[REDACTED]"
    # Structural research identifiers pass through.
    assert redacted["nested"]["position_id"] == "pos-42"
    assert redacted["provider_instrument_id"] == "inst-7"
    assert redacted["tags"] == ["a", "Bearer [REDACTED]"]
    assert redacted["count"] == 3
    assert redacted["flag"] is None


def test_redact_json_units():
    assert redact_json('{"token": "abc", "ticker": "AAPL"}') == (
        '{"token": "[REDACTED]", "ticker": "AAPL"}'
    )
    assert redact_json("not json") == "not json"


def test_nested_helpers_reserve_budget(monkeypatch):
    """Nested model helpers reserve against the active budget before any
    network call; with capacity they proceed to the call."""
    budget = ExecutionBudget(
        max_rounds=8, max_tool_calls=64, max_model_calls=1,
        max_runtime=600.0, max_evidence_tokens=48000,
    )
    assert budget.reserve_model_call() is True
    token = set_current_budget(budget)
    monkeypatch.setattr(
        quarantine_reader.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    monkeypatch.setattr(
        finra_analysis.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            quarantine_reader._llm_complete("test", "prompt")
        with pytest.raises(BudgetExhaustedError):
            finra_analysis._post_completion("test", [{"role": "user", "content": "x"}], 10)
    finally:
        reset_current_budget(token)

    # With capacity the helpers run through to the (stubbed) network call.
    class _FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "req_nested",
                "usage": _usage(),
                "choices": [{"message": {"content": "summary text", "finish_reason": "stop"}}],
            }

    budget2 = ExecutionBudget(
        max_rounds=8, max_tool_calls=64, max_model_calls=2,
        max_runtime=600.0, max_evidence_tokens=48000,
    )
    token2 = set_current_budget(budget2)
    monkeypatch.setattr(quarantine_reader.requests, "post", lambda *a, **k: _FakeResp())
    monkeypatch.setattr(finra_analysis.requests, "post", lambda *a, **k: _FakeResp())
    try:
        assert quarantine_reader._llm_complete("test", "prompt") == "summary text"
        assert (
            finra_analysis._post_completion("test", [{"role": "user", "content": "x"}], 10)
            == "summary text"
        )
    finally:
        reset_current_budget(token2)


def test_reserve_methods_enforce_runtime(monkeypatch):
    """Reserves refuse once elapsed runtime is gone, even with call slots left."""
    budget = ExecutionBudget(
        max_rounds=8, max_tool_calls=2, max_model_calls=2,
        max_runtime=1.0, max_evidence_tokens=48000,
    )
    assert budget.reserve_model_call() is True
    assert budget.reserve_tool_call() is True
    budget._started -= 60  # pretend the budget started 60s ago
    assert budget.runtime_remaining() <= 0
    assert budget.reserve_model_call() is False
    assert budget.reserve_tool_call() is False

    # The nested-helper path surfaces the same refusal as an exception.
    token = set_current_budget(budget)
    monkeypatch.setattr(
        quarantine_reader.requests, "post",
        lambda *a, **k: pytest.fail("nested model call must not run"),
    )
    try:
        with pytest.raises(BudgetExhaustedError):
            quarantine_reader._llm_complete("test", "prompt")
    finally:
        reset_current_budget(token)
