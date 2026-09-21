"""Fail-closed authority guards: registry abort + JEV-outage objective-only.

Pinned to HEAD ``_jev_admit(sid, objective, proposals)`` (no jev kwarg):
outage is simulated by monkeypatching ``JevClient`` in
``app.decision_client``. If a jev-injection param ever lands, extend — do not
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


def test_single_proposal_passthrough() -> None:
    props = _props()[:1]
    assert kw._jev_admit("s", "q", props) == props


def test_graph_prompt_registry_failure_is_terminal() -> None:
    with mock.patch("app.research.scheduler.build_registry", side_effect=RuntimeError("boom")):
        resp = kw._run({"id": "r1", "op": "run", "prompt": "Is XYZ solvent?"})
    assert resp["id"] == "r1"
    terminal = resp.get("terminal")
    assert isinstance(terminal, dict) and "registry guard forbids" in str(terminal.get("message"))
