"""Intake table: local interpretation + structured validation (no network)."""

import pytest

from app.policy import Capability, RequestContext
from app.thesis.intake import (
    IntakeProposal,
    apply_refinement,
    create_thesis_from_proposal,
    interpret_idea,
    plan_refinement,
)
from app.thesis.repository import ThesisRepository


def _ctx(*caps) -> RequestContext:
    return RequestContext(principal_id="test",
                          capabilities=frozenset(caps or (Capability.RESEARCH,)))


def test_belief_preserved_as_unvalidated_claim():
    p = interpret_idea("I think NVDA demand stays strong", {}, _ctx())
    assert p.claims[0]["statement"] == "I think NVDA demand stays strong"
    assert all(c["status"] == "unvalidated" for c in p.claims)


def test_objective_claims_expression_separated():
    p = interpret_idea("I want to own NVDA for ten years", {}, _ctx())
    assert p.user_thesis.startswith("I want to own NVDA")
    assert len(p.claims) == 1 and len(p.expressions) == 1
    assert p.claims[0]["statement"] != p.expressions[0]["structure"]


def test_ten_years_maps_to_long_equity_without_options_questions():
    p = interpret_idea("I want to own NVDA for ten years", {}, _ctx())
    (e,) = p.expressions
    assert (e["instrument"], e["direction"], e["horizon"]) == ("equity", "long", "long-term")
    assert all("option" not in q.question.lower() and "put" not in q.question.lower()
               for q in p.questions)


def test_profit_if_drops_without_strategy_asks_expression_choice():
    p = interpret_idea("I want to profit if NVDA drops", {}, _ctx())
    assert p.expressions == ()
    assert len(p.questions) == 1 and p.questions[0].question_type == "expression_choice"


def test_still_deciding_persists_zero_expressions_nothing_chosen():
    p = interpret_idea("I am still deciding how to play NVDA", {}, _ctx())
    assert p.expressions == ()
    assert all(e.get("status", "undecided") == "undecided" for e in p.expressions)


def test_unknowns_preserved_as_literal_unknown():
    p = interpret_idea("NVDA might do something", {}, _ctx())
    assert p.scope == "unknown" and "unknown" in p.unknowns


def test_never_a_recommendation_or_trade_selection():
    for text in ("I want to own NVDA for ten years",
                 "I want to profit if NVDA drops",
                 "I am still deciding how to play NVDA"):
        p = interpret_idea(text, {}, _ctx())
        assert all(e["status"] == "undecided" for e in p.expressions)
        assert len(p.questions) <= 3


def test_showcase_two_expressions_all_claims_unvalidated():
    p = interpret_idea(
        "I think AI infrastructure expectations are too high and valuations eventually reprice.",
        {"a": "buy puts and accumulate after the selloff"}, _ctx())
    assert len(p.expressions) == 2
    assert all(c["status"] == "unvalidated" for c in p.claims)


def test_intake_requires_research_only_context():
    broker = _ctx(Capability.RESEARCH, Capability.BROKER_MARKET_READ)
    with pytest.raises(ValueError):
        interpret_idea("NVDA thesis", {}, broker)


def _structured(user_thesis="NVDA demand stays strong", **over) -> dict:
    payload = {"user_thesis": user_thesis, "scope": "NVDA",
               "claims": [{"statement": "demand holds"}],
               "expressions": [{"key": "e1", "intent": "bullish", "instrument": "equity",
                                "direction": "long", "structure": "equity"}],
               "questions": [{"question": "What horizon?"}]}
    payload.update(over)
    return payload


def test_structured_create_persists_thesis_slug_rules(tmp_path):
    repo = ThesisRepository(tmp_path / "theses")
    proposal = IntakeProposal.from_dict(_structured(), "<thesis_create>")
    out = create_thesis_from_proposal(repo, proposal)
    assert out["thesis_id"] and out["slug"] and not out["setup_needed"]
    assert [r["rule_type"] for r in out["rules"]] == ["new_filing"]
    thesis = repo.load_thesis(out["thesis_id"])
    assert thesis.user_thesis == "NVDA demand stays strong"
    assert all(c.status == "unvalidated" for c in thesis.claims)
    assert {(q.status, q.text) for q in repo.load_questions(out["thesis_id"])} == {
        ("open", "What horizon?")}
    assert (tmp_path / "theses" / out["slug"] / "watch.yaml").is_file()


def test_structured_create_missing_user_thesis_errors_without_write(tmp_path):
    repo = ThesisRepository(tmp_path / "theses")
    for bad in ({}, {"user_thesis": "  "}):
        with pytest.raises(ValueError):
            IntakeProposal.from_dict({**bad, "scope": "NVDA"}, "<thesis_create>")
    assert not (tmp_path / "theses").exists() or repo.list_theses() == []


def test_structured_dangling_requirement_rejected_pre_write(tmp_path):
    repo = ThesisRepository(tmp_path / "theses")
    with pytest.raises(ValueError, match="absent expression"):
        IntakeProposal.from_dict(_structured(
            expressions=[{"key": "e1", "structure": "equity"}],
            requirements=[{"expression_id": "zzz", "requirement_type": "x",
                           "statement": "y"}]), "<thesis_create>")
    assert not (tmp_path / "theses").exists() or repo.list_theses() == []


def test_structured_refine_persists_and_refuses_paused_closed(tmp_path):
    repo = ThesisRepository(tmp_path / "theses")
    first = IntakeProposal.from_dict(_structured(), "<thesis_create>")
    created = create_thesis_from_proposal(repo, first)
    thesis = repo.load_thesis(created["thesis_id"])
    proposal = IntakeProposal.from_dict(_structured(
        user_thesis=f"{thesis.user_thesis}\nAlso adding puts",
        claims=[{"statement": "demand holds"}, {"statement": "puts hedge drawdown"}]),
        "<thesis_refine>")
    out = apply_refinement(repo, thesis.thesis_id, plan_refinement(thesis, proposal), proposal)
    assert out["added_claims"] == 1 and out["slug"] == created["slug"]
    assert "puts hedge drawdown" in {c.statement for c in repo.load_thesis(thesis.thesis_id).claims}
    before = (tmp_path / "theses" / created["slug"] / "thesis.yaml").read_bytes()
    with pytest.raises(ValueError):
        IntakeProposal.from_dict({"user_thesis": "  "}, "<thesis_refine>")
    assert (tmp_path / "theses" / created["slug"] / "thesis.yaml").read_bytes() == before
    repo.pause_thesis(thesis.thesis_id)
    with pytest.raises(ValueError):
        apply_refinement(repo, thesis.thesis_id, plan_refinement(repo.load_thesis(thesis.thesis_id),
                                                                 proposal), proposal)
    repo.resume_thesis(thesis.thesis_id)
    repo.close_thesis(thesis.thesis_id)
    with pytest.raises(ValueError):
        apply_refinement(repo, thesis.thesis_id, plan_refinement(repo.load_thesis(thesis.thesis_id),
                                                                 proposal), proposal)


def test_thesis_tool_create_preserves_belief_and_watch_rejects_unsupported(tmp_path):
    from app import tools as tools_mod

    ctx = RequestContext(principal_id="test", capabilities=frozenset({Capability.RESEARCH}), data_root=tmp_path)
    created = tools_mod.execute_tool(
        "thesis_create",
        {"user_thesis": "I think NVDA AI demand will stay strong.",
         "claims": [{"statement": "AI demand holds"}]},
        "test-model", context=ctx)
    assert "error" not in created
    thesis = ThesisRepository(tmp_path / "thesis").load_thesis(created["thesis_id"])
    assert thesis.user_thesis == "I think NVDA AI demand will stay strong."
    assert thesis.claims and all(c.status == "unvalidated" for c in thesis.claims)
    denied = tools_mod.execute_tool(
        "thesis_watch",
        {"id": created["thesis_id"], "rule_type": "new_external_evidence"},
        "test-model", context=ctx)
    assert "unsupported" in denied.get("error", "")
