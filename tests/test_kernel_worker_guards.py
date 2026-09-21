"""Fail-closed authority guards: registry abort + JEV-outage objective-only.

Pinned to HEAD ``_jev_admit(sid, objective, proposals, jev=None)``: outage is simulated by
monkeypatching ``JevClient`` in ``app.decision_client`` or by passing a
failing ``jev`` directly. If further injection params land, extend — do not
replace — these tests.
"""

from unittest import mock

from app.research import kernel_worker as kw


def test_registry_failure_raises_not_empty() -> None:
    with mock.patch("app.research.scheduler.build_registry", side_effect=RuntimeError("boom")):
        try:
            kw._registry_portfolio_hit()
        except RuntimeError as exc:
            assert "registry guard forbids" in str(exc)
        else:
            raise AssertionError("registry failure must raise")


def _props() -> list[dict[str, object]]:
    return [
        {"id": "s-q1", "objectiveId": "s", "question": "Other angle?", "dependsOn": [], "whyItMatters": "w"},
        {"id": "s-q2", "objectiveId": "s", "question": "Exact user objective?", "dependsOn": [], "whyItMatters": "w"},
    ]


class _JevDown:
    def decide(self, *a: object, **k: object) -> object:
        raise RuntimeError("jev down")


def _run_outage(fn: object, *args: object) -> object:
    with mock.patch("app.decision_client.JevClient", lambda: _JevDown()):
        assert callable(fn)
        return fn(*args)  # type: ignore[operator]


def test_jev_outage_returns_objective_only() -> None:
    props = _props()
    out = _run_outage(kw._jev_admit, "s", "Exact user objective?", props)
    assert out == [props[1]]


def test_jev_outage_synthesizes_single_objective_node() -> None:
    out = _run_outage(kw._jev_admit, "s", "Missing objective?", _props())
    assert isinstance(out, list) and len(out) == 1
    first = out[0]
    assert isinstance(first, dict) and first["question"] == "Missing objective?"


class _JevAdmitTwoOfThree:
    async def decide(self, *a: object, **k: object) -> dict[str, dict[str, str]]:
        return {"s-q1": {"choice": "analyze"}, "s-q2": {"choice": "gather_evidence"}, "s-q3": {"choice": "reject"}}


def test_jev_success_preserves_all_admitted_proposals() -> None:
    props: list[dict[str, object]] = [
        {"id": "s-q1", "objectiveId": "s", "question": "Other angle?", "dependsOn": [], "whyItMatters": "w"},
        {"id": "s-q2", "objectiveId": "s", "question": "Exact user objective?", "dependsOn": [], "whyItMatters": "w"},
        {"id": "s-q3", "objectiveId": "s", "question": "Tangent?", "dependsOn": [], "whyItMatters": "w"},
    ]
    out = kw._jev_admit("s", "Exact user objective?", props, jev=_JevAdmitTwoOfThree())  # type: ignore[arg-type]
    assert out == props[:2]


def test_single_proposal_passthrough() -> None:
    props = _props()[:1]
    assert kw._jev_admit("s", "q", props) == props


def test_graph_prompt_registry_failure_is_terminal() -> None:
    with mock.patch("app.research.scheduler.build_registry", side_effect=RuntimeError("boom")):
        resp = kw._run({"id": "r1", "op": "run", "prompt": "Is XYZ solvent?"})
    assert resp["id"] == "r1"
    terminal = resp.get("terminal")
    assert isinstance(terminal, dict) and "registry guard forbids" in str(terminal.get("message"))


class _JevRoute:
    def __init__(self, choice: str | None = None, fail: bool = False) -> None:
        self.choice = choice
        self.fail = fail
        self.seen: list[object] = []

    async def route_entry(self, prompt: str, registry: object | None = None) -> str:
        if self.fail:
            raise RuntimeError("jev down")
        self.seen.append((prompt, registry))
        return self.choice or "research_required"


def test_route_reasoning_choice_returns_fast_path() -> None:
    out = kw._route({"id": "r1", "op": "route", "prompt": "hello"}, jev=_JevRoute("reasoning_required"))  # type: ignore[arg-type]
    assert out == {"id": "r1", "route": "reasoning_required"}


def test_route_outage_fails_open_to_research() -> None:
    out = kw._route({"id": "r1", "op": "route", "prompt": "hello"}, jev=_JevRoute(fail=True))  # type: ignore[arg-type]
    assert out == {"id": "r1", "route": "research_required"}


def test_route_blank_prompt_needs_no_jev() -> None:
    out = kw._route({"id": "r1", "op": "route", "prompt": "  "}, jev=_JevRoute("reasoning_required"))  # type: ignore[arg-type]
    assert out == {"id": "r1", "route": "research_required"}


def test_route_tool_winner_returns_exact_tool() -> None:
    out = kw._route({"id": "r1", "op": "route", "prompt": "what time is it?"}, jev=_JevRoute("get_current_time"))  # type: ignore[arg-type]
    assert out == {"id": "r1", "route": "get_current_time"}
