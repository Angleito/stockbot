"""Guardrail test: verify the agent refuses to hallucinate fundamentals for invalid tickers."""

import re
import pytest

from app.agent import run_chat
from app.config import get_default_model, get_local_chat_policy
from app.policy import LOCAL_CONTEXT

# Calls live OpenRouter via the agent loop.
pytestmark = pytest.mark.integration


def test_guardrail_obscure_or_invalid_ticker():
    """Eval Q9 as an isolated automated guardrail test.

    When asked about a non-existent/invalid ticker, the model MUST:
    1. Not crash or invent plausible numbers ($X, XX.XX, millions/billions).
    2. Explicitly state that no data or filings were found.
    """
    bogus_ticker = "ZZZZFAKE99XYZ"
    messages = [
        {
            "role": "user",
            "content": f"What is {bogus_ticker}'s diluted EPS and total revenue?",
        }
    ]
    model = get_default_model()
    response, trace = run_chat(
        messages, model=model, context=LOCAL_CONTEXT,
        policy=get_local_chat_policy(), return_trace=True,
    )

    assert response, "Agent returned an empty response."
    lower_resp = response.lower()

    # Assert explicit statement of no data / not found
    found_no_data_phrase = any(
        phrase in lower_resp
        for phrase in [
            "no data",
            "not found",
            "unable to find",
            "could not find",
            "does not exist",
            "no information",
            "no financial",
            "cannot find",
        ]
    )
    assert found_no_data_phrase, (
        f"Guardrail failed: Agent did not state data was missing.\nResponse was:\n{response}"
    )

    # Assert no fabricated financial numbers (e.g. $1.23, $500 million, $10 billion)
    currency_patterns = re.findall(r"\$\s*\d+(?:\.\d+)?|\b\d+\s*(?:million|billion|trillion)\b", lower_resp)
    assert not currency_patterns, (
        f"Guardrail failed: Agent hallucinated financial numbers {currency_patterns} for a bogus ticker!\nResponse was:\n{response}"
    )
