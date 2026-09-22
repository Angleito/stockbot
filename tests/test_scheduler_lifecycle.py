"""Scheduler lifecycle: domain wiring, admit-then-complete, stall, expansion."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.research import scheduler as sched


def _node() -> SimpleNamespace:
    return SimpleNamespace(node_id="n1", session_id="s1", question="q?", why_it_matters="w")


class _Kernel:
    """Fake kernel recording lifecycle order; evidence store is in-memory."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.evidence: list[str] = []

    def start_job(self, sid: str, **kw: Any) -> dict[str, str]:
        jid = f"job-{len(self.calls)}"
        self.calls.append(("start", kw.get("source")))
        return {"job_id": jid}

    def heartbeat_job(self, jid: str) -> None:
        pass

    def admit_evidence(self, sid: str, jid: str, cand: dict[str, Any]) -> dict[str, str]:
        assert jid.startswith("job-"), jid
        self.calls.append(("admit", jid))
        eid = f"ev-{len(self.evidence)}"
        self.evidence.append(eid)
        return {"evidence_id": eid}

    def complete_job(self, jid: str, out: Any = None) -> None:
        self.calls.append(("complete", jid))

    def fail_job(self, jid: str, cat: str, msg: str) -> None:
        self.calls.append(("fail", jid))

    def record_decision(self, sid: str, dtype: str, **kw: Any) -> None:
        pass

    def resolve_node(self, sid: str, nid: str) -> None:
        pass

    def block_node(self, sid: str, nid: str, reason: str = "") -> None:
        pass


def _outcome(content: str = "record values here") -> SimpleNamespace:
    return SimpleNamespace(
        tool_name="query_finra",
        content=content,
        source_handle=None,
        source_refs=None,
        error=None,
        error_type=None,
        retryable=False,
        meta=None,
    )


class _JevAdmit:
    async def select_tool(self, *a: Any, **k: Any) -> SimpleNamespace:
        return SimpleNamespace(tool_name="query_finra", probabilities={}, confidence=1.0)

    async def assess_result(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {
            "probabilities": {},
            "confidence": 1.0,
            "continuation": "resolve_node",
            "continue": "resolve_node",
            "action": "resolve_node",
            "evidence": None,
            "candidate": None,
            "admit": None,
            "evidence_state": "sufficient_support",
            "decision": "sufficient_support",
        }

    async def adjudicate(self, *a: Any, **k: Any) -> SimpleNamespace:
        return SimpleNamespace(tool_name="query_finra", probabilities={}, confidence=1.0)


def _run_node(kernel: _Kernel, jev: Any) -> dict[str, Any]:
    async def fake_gen(**kw: Any) -> dict[str, Any]:
        return {"tool": "query_finra", "arguments": {}, "reasoning": "r"}

    async def fake_invoke(name: str, args: dict[str, Any], sess: Any, **kw: Any) -> dict[str, Any]:
        return {
            "tool_result_id": "s1:tr:abc",
            "records": [{"settlementDate": "2024-01-01", "symbolCode": "XYZ", "currentShortPositionQuantity": 123}],
            "briefing": "B",
        }

    return asyncio.run(
        sched._run_node(
            _node(),
            session_id="s1",
            kernel=kernel,
            jev=jev,
            repo=None,
            needle_generate=fake_gen,
            invoke=fake_invoke,
            to_outcome=lambda name, result: _outcome(),
            registry=[{"name": "query_finra", "parameters": {}}],
            tool_session=SimpleNamespace(),
        )
    )


def test_admit_before_complete_with_finra_domain() -> None:
    import json

    from app.research.service import _finra_record_texts

    kernel = _Kernel()
    res = _run_node(kernel, _JevAdmit())
    assert kernel.calls[0] == ("start", "FINRA")
    seq = [c[0] for c in kernel.calls]
    assert seq.index("admit") < seq.index("complete"), seq
    assert res["status"] == "resolved" and res["admitted"] == 1
    candidate = sched._evidence_candidate(
        "query_finra",
        "FINRA",
        {
            "tool_result_id": "s1:tr:abc",
            "records": [{"settlementDate": "2024-01-01", "symbolCode": "XYZ", "currentShortPositionQuantity": 123}],
        },
        _outcome(),
    )
    assert candidate is not None
    row_text = " ".join(
        json.dumps(
            {"settlementDate": "2024-01-01", "symbolCode": "XYZ", "currentShortPositionQuantity": 123},
            sort_keys=True,
            default=str,
        ).split()
    )
    assert candidate["record_identity"] == row_text
    replay = _finra_record_texts(
        {
            "result": {
                "records": [{"settlementDate": "2024-01-01", "symbolCode": "XYZ", "currentShortPositionQuantity": 123}]
            },
            "tool_name": "query_finra",
        }
    )
    assert any(str(candidate["record_identity"]) in t for t in replay)


def test_sec_candidate_needs_handle_and_text() -> None:
    """SEC boundary pins: missing handle or empty window/content stays uncitable."""
    good_handle = {"tool_result_id": "s1:tr:sec"}
    good_result = {"source_handle": good_handle, "text": "window words"}
    assert sched._sec_locator(sched._persisted_shapes(good_result)) == "window words"
    assert sched._outcome_summary(_outcome("words")) == "words"
    assert sched._sec_evidence_candidate(good_result, _outcome("words")) is not None
    assert sched._sec_evidence_candidate({"text": "window words"}, _outcome("words")) is None
    assert sched._sec_evidence_candidate({"source_handle": good_handle}, _outcome("")) is None


def test_insufficient_evidence_state_skips_admission() -> None:
    class _JevWeak(_JevAdmit):
        async def assess_result(self, *a: Any, **k: Any) -> dict[str, Any]:
            base = await super().assess_result(*a, **k)
            return {
                **base,
                "continuation": "continue_research",
                "continue": "continue_research",
                "action": "continue_research",
                "evidence_state": "insufficient",
                "decision": "insufficient",
            }

    kernel = _Kernel()
    res = _run_node(kernel, _JevWeak())
    assert res["admitted"] == 0
    assert not any(c[0] == "admit" for c in kernel.calls)
    assert any(c[0] == "complete" for c in kernel.calls)


def test_pure_stall_is_guard_false_with_stall_reason() -> None:
    class _KS(_Kernel):
        def ready_nodes(self, sid: str) -> list[SimpleNamespace]:
            return [_node()]

    async def stall(n: Any, sid: str, **kw: Any) -> dict[str, Any]:
        return {"node_id": "n1", "status": "gathering", "admitted": 0, "incomplete_guard": False}

    with mock.patch.object(sched, "run_node", stall):
        out = asyncio.run(sched.run("s1", kernel=_KS(), repo=None))
    assert out["status"] == "stalled"
    assert out["incomplete_guard"] is False
    assert str(out["reason"]).startswith("stalled:")


def test_node_guard_trip_yields_incomplete_guard_not_stalled() -> None:
    class _KS(_Kernel):
        def ready_nodes(self, sid: str) -> list[SimpleNamespace]:
            return [_node()]

    async def trip(n: Any, sid: str, **kw: Any) -> dict[str, Any]:
        return {"node_id": "n1", "status": "blocked", "admitted": 0, "incomplete_guard": True}

    with mock.patch.object(sched, "run_node", trip):
        out = asyncio.run(sched.run("s1", kernel=_KS(), repo=None))
    assert out["status"] == "incomplete_guard"
    assert out["incomplete_guard"] is True
    assert "guard trip stalled" in str(out["reason"])


def test_session_ceiling_yields_incomplete_guard() -> None:
    class _KS(_Kernel):
        def ready_nodes(self, sid: str) -> list[SimpleNamespace]:
            return [_node()]

    async def trip(n: Any, sid: str, **kw: Any) -> dict[str, Any]:
        return {"node_id": "n1", "status": "blocked", "admitted": 0, "incomplete_guard": True}

    with mock.patch.object(sched, "run_node", trip), mock.patch.object(sched, "_MAX_TOOL_ROUNDS", 0):
        out = asyncio.run(sched.run("s1", kernel=_KS(), repo=None))
    assert "runtime guard" in str(out["reason"])


def test_expansion_creates_jev_admitted_nodes_with_dep() -> None:
    created: list[tuple[str, str, tuple[str, ...]]] = []
    seen: list[tuple[str, dict[str, Any]]] = []
    orig = sched._record
    try:
        sched._record = lambda k, sid, dtype, **kw: seen.append((dtype, kw.get("selected")))  # type: ignore[assignment]

        class _K:
            def record_decision(self, sid: str, t: str, **kw: Any) -> None:
                pass

            def create_node(self, sid: str, q: str, w: str, depends_on: Any = None) -> SimpleNamespace:
                created.append((q, w, tuple(depends_on or ())))
                return SimpleNamespace(node_id=f"n-{len(created)}")

        class _J:
            async def decide(self, state: Any, questions: Any, decision_type: Any = None, **kw: Any) -> dict[str, Any]:
                assert decision_type == "graph_expansion"
                return {qid: {"choice": "admit"} for qid in questions}

        proposal = {
            "proposals": [
                {"question": "Follow-up A?", "whyItMatters": "Why A"},
                {"question": "Follow-up B?", "whyItMatters": ""},
            ]
        }
        n = asyncio.run(sched._expand_graph(_J(), _K(), "s1", "Objective?", "n0", proposal))
    finally:
        sched._record = orig  # type: ignore[assignment]
    assert n == 2
    assert created[0] == ("Follow-up A?", "Why A", ("n0",))
    assert created[1] == ("Follow-up B?", "Route question.", ("n0",))
    assert ("graph_expansion", {"created": 2, "proposed": 2}) in seen


def test_expansion_is_fail_closed_on_jev_outage() -> None:
    created: list[str] = []

    class _K:
        def record_decision(self, sid: str, t: str, **kw: Any) -> None:
            pass

        def create_node(self, sid: str, q: str, w: str, depends_on: Any = None) -> SimpleNamespace:
            created.append(q)
            return SimpleNamespace(node_id="n-x")

    class _JDown:
        async def decide(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("jev down")

    proposal = {"proposals": [{"question": "Follow-up?", "whyItMatters": "Why"}]}
    n = asyncio.run(sched._expand_graph(_JDown(), _K(), "s1", "Objective?", "n0", proposal))
    assert n == 0 and created == []


def test_reason_path_calls_analyze_then_expand() -> None:
    """Production reason path: analyze shape (no proposals) -> expand supplies proposals."""
    created: list[str] = []
    calls: list[str] = []

    class _K:
        def create_node(self, sid: str, q: str, w: str, depends_on: Any = None) -> SimpleNamespace:
            created.append(q)
            return SimpleNamespace(node_id=f"n-{len(created)}")

        def get_session(self, sid: str) -> dict[str, Any]:
            return {"session_id": sid, "objective": "Objective?", "query": "", "as_of": None}

        def record_decision(self, sid: str, t: str, **kw: Any) -> None:
            pass

        def start_job(self, sid: str, **kw: Any) -> dict[str, str]:
            return {"job_id": "job-0"}

        def heartbeat_job(self, jid: str) -> None:
            pass

        def complete_job(self, jid: str, out: Any = None) -> None:
            pass

        def block_node(self, sid: str, nid: str, reason: str = "") -> None:
            pass

    class _J:
        async def select_tool(self, *a: Any, **k: Any) -> SimpleNamespace:
            return SimpleNamespace(tool_name="reasoning_required", probabilities={}, confidence=1.0)

        async def adjudicate(self, proposal: Any, *a: Any, **k: Any) -> SimpleNamespace:
            assert isinstance(proposal, dict) and "analyses" in proposal and "proposals" not in proposal
            calls.append("adjudicate")
            return SimpleNamespace(tool_name="query_finra", probabilities={}, confidence=1.0)

        async def decide(self, state: Any, questions: Any, decision_type: Any = None, **kw: Any) -> dict[str, Any]:
            return {qid: {"choice": "admit"} for qid in questions}

        async def assess_result(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {
                "probabilities": {},
                "confidence": 1.0,
                "continuation": "continue_research",
                "continue": "continue_research",
                "action": "continue_research",
                "evidence_state": "insufficient",
                "decision": "insufficient",
            }

    class _R:
        async def analyze(self, prompt: Any) -> dict[str, Any]:
            calls.append("analyze")
            assert isinstance(prompt, str) and "CONTEXT" in prompt
            return {"analyses": [{"nodeId": "n1"}], "evidenceRequests": []}

        async def expand(self, prompt: Any, objective_id: str, prior_ids: Any) -> dict[str, Any]:
            calls.append("expand")
            assert isinstance(prompt, str) and "n1" in prompt
            assert objective_id == "s1" and "n1" in prior_ids
            return {"proposals": [{"question": "Follow-up?", "whyItMatters": "Why"}]}

    orig = sched._record
    orig_rounds = sched._MAX_TOOL_ROUNDS
    try:
        sched._record = lambda k, sid, dtype, **kw: None  # type: ignore[assignment]
        sched._MAX_TOOL_ROUNDS = 1
        out = asyncio.run(
            sched._run_node(
                _node(),
                session_id="s1",
                kernel=_K(),
                jev=_J(),
                reasoner=_R(),
                repo=None,
                needle_generate=lambda **kw: {"tool": "query_finra", "arguments": {}, "reasoning": "r"},
                invoke=lambda *a, **k: {"tool_result_id": None},
                to_outcome=lambda name, result: _outcome(),
                registry=[{"name": "query_finra", "parameters": {}}],
                tool_session=SimpleNamespace(),
            )
        )
    finally:
        sched._record = orig  # type: ignore[assignment]
        sched._MAX_TOOL_ROUNDS = orig_rounds
    assert calls[0] == "analyze" and "expand" in calls
    assert calls.index("analyze") < calls.index("adjudicate") < calls.index("expand")
    assert created == ["Follow-up?"]
    assert out["admitted"] == 0


def test_other_no_resolve_without_admitted_evidence() -> None:
    """OTHER tool success + sufficient_support but no admitted evidence MUST NOT resolve."""
    resolved: list[str] = []
    assess_calls: list[str] = []

    class _K(_Kernel):
        def resolve_node(self, sid: str, nid: str) -> None:
            resolved.append(nid)

        def get_session(self, sid: str) -> dict[str, Any]:
            return {"session_id": sid, "objective": "q?", "query": "", "as_of": None}

    class _J(_JevAdmit):
        async def select_tool(self, *a: Any, **k: Any) -> SimpleNamespace:
            return SimpleNamespace(tool_name="mystery_tool", probabilities={}, confidence=1.0)

        async def assess_result(self, *a: Any, **k: Any) -> dict[str, Any]:
            assess_calls.append("assess")
            return await super().assess_result(*a, **k)

    with mock.patch.object(sched, "_MAX_TOOL_ROUNDS", 1):
        out = asyncio.run(
            sched._run_node(
                _node(),
                session_id="s1",
                kernel=_K(),
                jev=_J(),
                repo=None,
                needle_generate=lambda **kw: {"tool": "mystery_tool", "arguments": {}, "reasoning": "r"},
                invoke=lambda *a, **k: {"note": "uncitable bytes"},
                to_outcome=lambda name, result: _outcome(),
                registry=[{"name": "mystery_tool", "parameters": {}}],
                tool_session=SimpleNamespace(),
            )
        )
    assert resolved == []
    assert out["status"] == "blocked" and out["admitted"] == 0


def test_source_fallback_returns_other() -> None:
    with mock.patch.dict("sys.modules", {"app.research.agents.source_agent": None}):
        import builtins

        real_import = builtins.__import__

        def _boom(name: str, *a: Any, **k: Any) -> Any:
            if name == "app.research.agents.source_agent":
                raise ImportError("no source_agent")
            return real_import(name, *a, **k)

        with mock.patch.object(builtins, "__import__", _boom):
            assert sched._source_for_tool("mystery_tool") == "OTHER"


def test_failed_outcome_never_reaches_assess() -> None:
    assess_calls: list[str] = []
    failed: list[str] = []

    class _K(_Kernel):
        def fail_job(self, jid: str, cat: str, msg: str) -> None:
            failed.append(jid)
            super().fail_job(jid, cat, msg)

        def get_session(self, sid: str) -> dict[str, Any]:
            return {"session_id": sid, "objective": "q?", "query": "", "as_of": None}

    class _J(_JevAdmit):
        async def assess_result(self, *a: Any, **k: Any) -> dict[str, Any]:
            assess_calls.append("assess")
            return await super().assess_result(*a, **k)

    def _bad_outcome(name: str, result: Any) -> SimpleNamespace:
        out = _outcome()
        out.error = "provider blew up"
        return out

    with mock.patch.object(sched, "_MAX_TOOL_ROUNDS", 1):
        out = asyncio.run(
            sched._run_node(
                _node(),
                session_id="s1",
                kernel=_K(),
                jev=_J(),
                repo=None,
                needle_generate=lambda **kw: {"tool": "query_finra", "arguments": {}, "reasoning": "r"},
                invoke=lambda *a, **k: {"tool_result_id": "s1:tr:x"},
                to_outcome=_bad_outcome,
                registry=[{"name": "query_finra", "parameters": {}}],
                tool_session=SimpleNamespace(),
            )
        )
    assert assess_calls == []
    assert failed != []
    assert out["admitted"] == 0
