"""Intake table: local interpretation + fake-gateway validation (no network)."""

import pytest

from app.policy import Capability, RequestContext
from app.thesis.intake import interpret_idea


def _ctx(*caps) -> RequestContext:
    return RequestContext(principal_id="test",
                          capabilities=frozenset(caps or (Capability.RESEARCH,)))


class _GW:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete_json(self, prompt, *, request_context):
        self.calls += 1
        return self.payload


def test_belief_preserved_as_unvalidated_claim():
    p = interpret_idea("I think NVDA demand stays strong", {}, None, _ctx())
    assert p.claims[0]["statement"] == "I think NVDA demand stays strong"
    assert all(c["status"] == "unvalidated" for c in p.claims)


def test_objective_claims_expression_separated():
    p = interpret_idea("I want to own NVDA for ten years", {}, None, _ctx())
    assert p.user_thesis.startswith("I want to own NVDA")
    assert len(p.claims) == 1 and len(p.expressions) == 1
    assert p.claims[0]["statement"] != p.expressions[0]["structure"]


def test_ten_years_maps_to_long_equity_without_options_questions():
    p = interpret_idea("I want to own NVDA for ten years", {}, None, _ctx())
    (e,) = p.expressions
    assert (e["instrument"], e["direction"], e["horizon"]) == ("equity", "long", "long-term")
    assert all("option" not in q.question.lower() and "put" not in q.question.lower()
               for q in p.questions)


def test_profit_if_drops_without_strategy_asks_expression_choice():
    p = interpret_idea("I want to profit if NVDA drops", {}, None, _ctx())
    assert p.expressions == ()
    assert len(p.questions) == 1 and p.questions[0].question_type == "expression_choice"


def test_still_deciding_persists_zero_expressions_nothing_chosen():
    p = interpret_idea("I am still deciding how to play NVDA", {}, None, _ctx())
    assert p.expressions == ()
    assert all(e.get("status", "undecided") == "undecided" for e in p.expressions)


def test_unknowns_preserved_as_literal_unknown():
    p = interpret_idea("NVDA might do something", {}, None, _ctx())
    assert p.scope == "unknown" and "unknown" in p.unknowns


def test_never_a_recommendation_or_trade_selection():
    for text in ("I want to own NVDA for ten years",
                 "I want to profit if NVDA drops",
                 "I am still deciding how to play NVDA"):
        p = interpret_idea(text, {}, None, _ctx())
        assert all(e["status"] == "undecided" for e in p.expressions)
        assert len(p.questions) <= 3


def test_showcase_two_expressions_all_claims_unvalidated():
    p = interpret_idea(
        "I think AI infrastructure expectations are too high and valuations eventually reprice.",
        {"a": "buy puts and accumulate after the selloff"}, None, _ctx())
    assert len(p.expressions) == 2
    assert all(c["status"] == "unvalidated" for c in p.claims)


def test_fake_gateway_valid_proposal_accepted():
    gw = _GW({"user_thesis": "NVDA thesis", "scope": "NVDA",
              "claims": [{"statement": "demand holds"}],
              "expressions": [{"structure": "custom collar-ish thing"}],
              "questions": [{"question": "What horizon?"}]})
    p = interpret_idea("NVDA thesis", {}, gw, _ctx())
    assert gw.calls == 1 and p.expressions[0]["structure"] == "custom collar-ish thing"
    assert p.claims[0]["status"] == "unvalidated"


def test_invalid_gateway_json_rejected():
    with pytest.raises(ValueError):
        interpret_idea("NVDA thesis", {}, _GW("not json {{{"), _ctx())
    with pytest.raises(ValueError):
        interpret_idea("NVDA thesis", {}, _GW({"user_thesis": ""}), _ctx())


def test_intake_requires_research_only_context():
    broker = _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ)
    with pytest.raises(ValueError):
        interpret_idea("NVDA thesis", {}, None, broker)
