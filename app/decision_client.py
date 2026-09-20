"""JEV bridge Python client: typed decide + select_tool/assess_result/adjudicate.

Transport only + persistence. The TypeSafe SDK call and answer-shape
validation live in decision/runtime.ts (JSONL sidecar); Python owns
persistence: DecisionRecord domain fields -> research.sqlite, full
request/response + latency -> runs.sqlite via the ambient RunRecorder.
Fallbacks: injectable transport stub (tests), direct HTTPS systemOne when
the sidecar is absent. stdlib only.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_data_root
from app.research.models import (
    DecisionRecord,
    ToolDecision,
    new_decision_id,
    utcnow,
    validate_json_mapping,
    validate_json_value,
)
from app.research.repository import ResearchRepository, get_research_db_path
from app.storage.runs import get_current_recorder

logger = logging.getLogger(__name__)

__all__ = ["JevClient"]

_PROVIDER = "typesafe"
_DEFAULT_MODEL = "jev-latest"

# Mirror of decision/jev.ts TOOL_SELECTION_SENTINELS (names are the contract).
REASON_SENTINEL = "reasoning_required"
RESOLVED_SENTINEL = "node_resolved"
_SENTINEL_DESCRIPTIONS = {
    REASON_SENTINEL: "Escalate to the reasoner (decompose/analyze proposals); no tool call fits this node.",
    RESOLVED_SENTINEL: "Existing evidence resolves the node; no further tool call needed.",
}

# Mirror of decision/jev.ts EVIDENCE_STATE_OPTIONS (choice labels are the contract).
EVIDENCE_STATE_OPTIONS = {
    "sufficient_support": "The cited evidence sufficiently supports the claim.",
    "sufficient_contradiction": "The cited evidence sufficiently contradicts the claim.",
    "conflicted": "The evidence both supports and contradicts the claim; do not resolve by guessing.",
    "insufficient": "The evidence is missing or too weak to support or contradict the claim.",
}

CONTINUE_OPTIONS = {
    "resolve_node": "Existing evidence resolves the node; stop gathering.",
    "continue_research": "Evidence is useful but the node needs more tool calls.",
    "reason_over_evidence": "Escalate to the reasoner over the gathered evidence.",
}


class _SidecarUnavailable(RuntimeError):
    """Sidecar missing or dead; caller falls back to direct HTTPS."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_prob(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 1.0


def _parse_noul(name: str, qid: str, ans: object) -> dict[str, Any]:
    bad = ValueError(f"{name}: malformed_typesafe_answer for {qid}")
    if not isinstance(ans, dict) or ans.get("type") != "noul":
        raise bad
    if not _is_prob(ans.get("noul")):
        raise bad
    return {"kind": "noul", "probability": float(ans["noul"])}


def _parse_choice(name: str, qid: str, ans: object, options: Mapping[str, str]) -> dict[str, Any]:
    bad = ValueError(f"{name}: malformed_typesafe_answer for {qid}")
    if not isinstance(ans, dict) or ans.get("type") != "choice":
        raise bad
    choice = ans.get("choice")
    probs = ans.get("probabilities")
    conf = ans.get("confidence")
    if not isinstance(choice, str) or choice not in options:
        raise bad
    if not isinstance(probs, dict) or not isinstance(conf, (int, float)) or isinstance(conf, bool):
        raise bad
    want = sorted(options)
    got = sorted(probs)
    if got != want or not _is_prob(conf):
        raise bad
    out: dict[str, Any] = {}
    for k in want:
        if not _is_prob(probs[k]):
            raise bad
        out[k] = float(probs[k])
    return {"kind": "choice", "choice": choice, "probabilities": out, "confidence": float(conf)}


def _parse_score(name: str, qid: str, ans: object, max_score: float | None) -> dict[str, Any]:
    bad = ValueError(f"{name}: malformed_typesafe_answer for {qid}")
    if not isinstance(ans, dict) or ans.get("type") != "score":
        raise bad
    score = ans.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not __import__("math").isfinite(score):
        raise bad
    if max_score is not None and not 0 <= float(score) <= max_score:
        raise bad
    out: dict[str, Any] = {"kind": "score", "score": score}
    if ans.get("confidence") is not None:
        if not _is_prob(ans.get("confidence")):
            raise bad
        out["confidence"] = float(ans["confidence"])
    if ans.get("probabilities") is not None:
        probs = ans.get("probabilities")
        if not isinstance(probs, dict):
            raise bad
        keep: dict[str, float] = {}
        for k, v in probs.items():
            if not _is_prob(v):
                raise bad
            keep[str(k)] = float(v)
        out["probabilities"] = keep
    if "legend" in ans:
        out["raw"] = ans["legend"]
    return out


def _score_max_from_question(q: object) -> float | None:
    if not isinstance(q, dict):
        return None
    for key in ("criteria", "levels"):
        seq = q.get(key)
        if isinstance(seq, list) and len(seq) >= 2:
            return float(len(seq) - 1)
    return None


def _options_from_question(q: object) -> dict[str, str]:
    if isinstance(q, dict) and isinstance(q.get("criteria"), dict):
        return {str(k): str(k) for k in q["criteria"]}
    return {}


def parse_decisions(
    questions: Mapping[str, Any],
    raw: object,
    choice_options: Mapping[str, Mapping[str, str]] | None = None,
    name: str = "decide",
) -> dict[str, dict[str, Any]]:
    """Validate a raw SystemOne payload into per-question decisions (mirrors askDecisions)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), dict):
        raise ValueError(f"{name}: malformed_typesafe_response")
    answers: dict[str, Any] = raw["answers"]
    if sorted(answers) != sorted(questions):
        raise ValueError(f"{name}: typesafe answers do not match questions")
    out: dict[str, dict[str, Any]] = {}
    for qid in sorted(questions):
        q = questions[qid]
        ans = answers[qid]
        qtype = q.get("type") if isinstance(q, dict) else None
        atype = ans.get("type") if isinstance(ans, dict) else None
        kind = qtype if qtype in ("choice", "score") else atype
        if kind == "choice":
            opts = (choice_options or {}).get(qid) or _options_from_question(q)
            out[qid] = _parse_choice(name, qid, ans, opts)
        elif kind == "score":
            out[qid] = _parse_score(name, qid, ans, _score_max_from_question(q))
        else:
            out[qid] = _parse_noul(name, qid, ans)
    return out


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] is not None:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _node_dict(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        raw = dict(node)
    elif hasattr(node, "to_dict"):
        try:
            raw = dict(node.to_dict())
        except Exception:
            raw = {}
    else:
        raw = {}
        try:
            from dataclasses import asdict, is_dataclass

            if is_dataclass(node):
                raw = dict(asdict(node))
        except Exception:
            raw = {}
        if not raw:
            for key in (
                "node_id",
                "id",
                "session_id",
                "question",
                "why_it_matters",
                "depends_on",
                "status",
                "evidence_ids",
                "missing_evidence",
            ):
                value = getattr(node, key, None)
                if value is not None:
                    raw[key] = value
    out: dict[str, Any] = {}
    for key in (
        "node_id",
        "id",
        "session_id",
        "question",
        "why_it_matters",
        "depends_on",
        "status",
        "evidence_ids",
        "missing_evidence",
    ):
        value = raw.get(key)
        if value is None:
            continue
        out[key] = list(value) if isinstance(value, tuple) else value
    return out


def _outcome_dict(outcome: Any) -> dict[str, Any]:
    def text(value: Any) -> str | None:
        return value if isinstance(value, str) else (None if value is None else str(value))

    return {
        "tool": _field(outcome, "tool_name", "tool"),
        "content": text(_field(outcome, "content")),
        "error": text(_field(outcome, "error")),
        "error_type": text(_field(outcome, "error_type")),
    }


def _evidence_ids(evidence: Any) -> list[str]:
    ids: list[str] = []
    if not isinstance(evidence, list):
        return ids
    for item in evidence:
        if isinstance(item, dict):
            for key in ("evidence_id", "id"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    ids.append(value)
                    break
        if len(ids) >= 10:
            break
    return ids


def _manifest_line(entry: dict[str, Any]) -> str:
    """One compact option line per registry entry (mirrors decision/jev.ts manifestLine)."""

    def opt(value: Any) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return " ".join(value.split())

    desc = entry.get("description")
    line = " ".join(desc.split()) if isinstance(desc, str) and desc else entry.get("name", "")
    purpose = opt(entry.get("purpose"))
    if purpose and purpose != line:
        line += f" Purpose: {purpose}."
    inputs = opt(entry.get("keyInputs"))
    if inputs:
        line += f" Inputs: {inputs}."
    output = opt(entry.get("outputKind"))
    if output:
        line += f" Output: {output}."
    evidence = opt(entry.get("evidence"))
    if evidence:
        line += f" Evidence: {evidence}."
    prereq = opt(entry.get("prerequisites"))
    if prereq:
        line += f" Needs: {prereq}."
    pit = opt(entry.get("pitSupport"))
    if pit:
        line += f" PIT: {pit}."
    domain = opt(entry.get("domain"))
    if domain:
        line = f"[{domain}] {line}"
    return line


def _tool_options_prompt(
    registry: list[dict[str, Any]],
    node: dict[str, Any],
    evidence: Any,
    attempts: Any,
) -> tuple[dict[str, str], str]:
    if not registry:
        raise ValueError("tool_selection: empty registry")
    node_id = node.get("node_id") or node.get("id")
    question = node.get("question")
    if not node_id or not question:
        raise ValueError("tool_selection: node needs nodeId and question")
    options: dict[str, str] = {}
    for entry in registry:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("tool_selection: registry entry needs a name")
        if name in options:
            raise ValueError(f"tool_selection: duplicate tool {name}")
        options[name] = _manifest_line(entry) if isinstance(entry, dict) else name
    for key, desc in _SENTINEL_DESCRIPTIONS.items():
        if key in options:
            raise ValueError(f"tool_selection: registry collides with sentinel {key}")
        options[key] = desc
    prompt = f"Which single tool runs next for research node {node_id}? Question: {question} JEV owns this selection and every transition over the whole canonical registry; Needle runs args only and never selects, chains, or judges. Choose exactly one winner."
    why = node.get("why_it_matters")
    if isinstance(why, str) and why:
        prompt += f" Why it matters: {why}"
    ids = _evidence_ids(evidence)
    count = len(evidence) if isinstance(evidence, list) else 0
    prompt += f" Evidence on hand: {count} item(s){f' [{chr(44).join(ids)}]' if ids else ''}."
    if isinstance(attempts, list):
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            tool = attempt.get("tool")
            error = attempt.get("error")
            if isinstance(tool, str) and isinstance(error, str) and tool and error:
                prompt += f" Prior attempt {tool} failed: {error[:200]}."
    return options, prompt


def _auto_registry() -> list[dict[str, Any]]:
    try:
        from app.research.scheduler import build_registry

        reg = build_registry()
        if reg:
            return reg
    except Exception:
        pass
    from app.policy import Capability
    from app.tools import TOOL_DISCOVERY_REGISTRY, tools_for_capabilities

    out: list[dict[str, Any]] = []
    for tool in tools_for_capabilities(frozenset({Capability.RESEARCH})):
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        desc = fn.get("description")
        meta = TOOL_DISCOVERY_REGISTRY.get(name)
        out.append(
            {
                "name": name,
                "description": desc if isinstance(desc, str) else "",
                "purpose": meta.summary if meta is not None else "",
                "outputKind": meta.output_kind if meta is not None else "",
                "evidence": meta.output_kind if meta is not None else "",
            }
        )
    return out


_LIVE_PROCS: set[subprocess.Popen[str]] = set()
_ATEXIT_ARMED = False


def _arm_atexit() -> None:
    global _ATEXIT_ARMED
    if _ATEXIT_ARMED:
        return
    _ATEXIT_ARMED = True

    def _teardown() -> None:
        for proc in list(_LIVE_PROCS):
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001, S110 - best-effort teardown at exit
                pass

    atexit.register(_teardown)


class JevClient:
    """JEV bridge: typed decide over the sidecar, multi-tool-aware select, persistence."""

    def __init__(
        self,
        *,
        transport: Any | None = None,
        runtime_path: Path | str | None = None,
        timeout_s: float = 60.0,
        data_root: Path | None = None,
        provider: str = _PROVIDER,
        model: str = _DEFAULT_MODEL,
    ) -> None:
        self._transport = transport
        self._timeout_s = timeout_s
        self._data_root = Path(data_root) if data_root is not None else get_data_root()
        self._provider = provider
        self._model = model
        # ponytail: single-flight lock; pipeline if sidecar throughput matters.
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        root = Path(__file__).resolve().parent.parent
        self._runtime_ts = Path(runtime_path) if runtime_path is not None else root / "decision" / "runtime.ts"
        self._repo_root = root

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None:
            _LIVE_PROCS.discard(proc)
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001, S110 - best-effort close
                pass

    def _ensure_proc(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if self._proc is not None:
            _LIVE_PROCS.discard(self._proc)
            self._proc = None
        if not self._runtime_ts.exists():
            raise _SidecarUnavailable(f"sidecar missing: {self._runtime_ts}")
        try:
            proc = subprocess.Popen(
                ["bun", str(self._runtime_ts)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(self._repo_root),
            )
        except FileNotFoundError as exc:
            raise _SidecarUnavailable("bun unavailable for sidecar") from exc
        _arm_atexit()
        _LIVE_PROCS.add(proc)
        self._proc = proc
        return proc

    def _sidecar_roundtrip(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            try:
                proc = self._ensure_proc()
            except _SidecarUnavailable:
                raise
            line = json.dumps(payload) + "\n"
            try:
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(line)
                proc.stdin.flush()
                annia = proc.stdout.readline()
            except BrokenPipeError, OSError:
                _LIVE_PROCS.discard(proc)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001, S110 - best-effort restart
                    pass
                self._proc = None
                proc = self._ensure_proc()
                assert proc.stdin is not None and proc.stdout is not None
                proc.stdin.write(line)
                proc.stdin.flush()
                annia = proc.stdout.readline()
            if not annia:
                _LIVE_PROCS.discard(proc)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001, S110 - best-effort restart
                    pass
                self._proc = None
                raise _SidecarUnavailable("sidecar closed (EOF)")
            try:
                response = json.loads(annia)
            except ValueError as exc:
                raise RuntimeError(f"decide: malformed sidecar response: {exc}") from exc
            if not isinstance(response, dict):
                raise RuntimeError("decide: malformed sidecar response")
            return response

    def _http_system_one(self, state: Any, questions: Mapping[str, Any]) -> dict[str, Any]:
        key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("decide: jev unavailable (no sidecar, no TYPESAFE_API_KEY for direct call)")
        base = (os.environ.get("TYPESAFE_BASE_URL") or "https://api.typesafe.ai").rstrip("/")
        body: dict[str, Any] = {"state": state, "questions": dict(questions)}
        model = (os.environ.get("TYPESAFE_DEFAULT_MODEL") or "").strip()
        if model:
            body["model"] = model
        req = urllib.request.Request(
            f"{base}/v1/systemone",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                raw = json.loads(resp.read().decode())
        except Exception as exc:
            raise RuntimeError(f"decide: typesafe_request_failed: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("decide: malformed_typesafe_response")
        return raw

    async def _invoke(
        self,
        state: Any,
        questions: Mapping[str, Any],
        choice_options: Mapping[str, Mapping[str, str]] | None,
    ) -> tuple[dict[str, dict[str, Any]], Any, str]:
        if self._transport is not None:
            raw = self._transport(state, dict(questions))
            if inspect.isawaitable(raw):
                raw = await raw
            return parse_decisions(questions, raw, choice_options), raw, "stub"
        payload: dict[str, Any] = {
            "id": f"jev:{uuid.uuid4().hex[:12]}",
            "op": "decide",
            "state": state,
            "questions": dict(questions),
        }
        if choice_options:
            payload["choiceOptions"] = {k: dict(v) for k, v in choice_options.items()}
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._sidecar_roundtrip, payload), timeout=self._timeout_s
            )
        except _SidecarUnavailable:
            raw = await asyncio.to_thread(self._http_system_one, state, questions)
            return parse_decisions(questions, raw, choice_options), raw, "http"
        except TimeoutError as exc:
            self.close()
            raise RuntimeError(f"decide: typesafe timeout after {self._timeout_s}s") from exc
        if not isinstance(response, dict) or response.get("id") != payload["id"]:
            raise RuntimeError("decide: malformed sidecar response")
        if "error" in response:
            raise RuntimeError(f"decide: {response['error']}")
        decisions = response.get("decisions")
        if not isinstance(decisions, dict) or not decisions:
            raise RuntimeError("decide: malformed sidecar response")
        return decisions, response.get("raw"), "sidecar"

    async def decide(
        self,
        state: Any,
        questions: Mapping[str, Any],
        *,
        decision_type: str,
        session_id: str,
        node_id: str | None = None,
        job_id: str | None = None,
        choice_options: Mapping[str, Mapping[str, str]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Typed JEV decide; persists the round best-effort, raises on any defect."""
        if not decision_type or not isinstance(decision_type, str):
            raise ValueError("decide: decision_type required")
        if not session_id or not isinstance(session_id, str):
            raise ValueError("decide: session_id required")
        if not isinstance(questions, Mapping) or not questions:
            raise ValueError("decide: missing_questions")
        try:
            json.dumps({"state": state, "questions": dict(questions), "options": choice_options})
        except TypeError as exc:
            raise ValueError(f"decide: unserializable request: {exc}") from exc
        started_at = _now()
        start = time.perf_counter()
        decisions, raw, via = await self._invoke(state, questions, choice_options)
        completed_at = _now()
        latency_ms = (time.perf_counter() - start) * 1000.0
        self._persist(
            decision_type=decision_type,
            session_id=session_id,
            node_id=node_id,
            job_id=job_id,
            questions=dict(questions),
            decisions=decisions,
            raw=raw,
            via=via,
            latency_ms=latency_ms,
            started_at=started_at,
            completed_at=completed_at,
        )
        return decisions

    async def select_tool(
        self,
        objective: str,
        node: Any,
        registry: list[dict[str, Any]] | None = None,
        evidence: Any = None,
        attempts: Any = None,
        *,
        session_id: str,
        job_id: str | None = None,
    ) -> ToolDecision:
        """JEV owns ALL tool selection/transitions over the whole canonical registry every round; Needle is args-only (never selects/chains/judges). Caller assembles the whole registry; single-winner choice + 2 sentinels."""
        reg = list(registry) if registry else _auto_registry()
        node_d = _node_dict(node)
        nid = node_d.get("node_id") or node_d.get("id")
        options, prompt = _tool_options_prompt(reg, node_d, evidence, attempts)
        questions = {"tool_selection": {"type": "choice", "instructions": prompt, "criteria": options}}
        state = {"objective": objective, "node": node_d, "evidence": evidence or [], "attempts": attempts or []}
        decisions = await self.decide(
            state,
            questions,
            decision_type="tool_selection",
            session_id=session_id,
            node_id=nid if isinstance(nid, str) else None,
            job_id=job_id,
            choice_options={"tool_selection": options},
        )
        return self._to_tool_decision(
            decisions.get("tool_selection"), options, set_of_registry={o for o in options} - set(_SENTINEL_DESCRIPTIONS)
        )

    async def adjudicate(
        self,
        proposal: Any,
        node: Any,
        *,
        session_id: str,
        job_id: str | None = None,
        registry: list[dict[str, Any]] | None = None,
    ) -> ToolDecision:
        """JEV adjudicates a reasoner proposal over the same whole-registry options as select_tool; proposal lives in state only, never filters the registry. Needle never selects/chains/judges."""
        reg = list(registry) if registry else _auto_registry()
        node_d = _node_dict(node)
        nid = node_d.get("node_id") or node_d.get("id")
        options, prompt = _tool_options_prompt(reg, node_d, None, None)
        questions = {"tool_selection": {"type": "choice", "instructions": prompt, "criteria": options}}
        state = {"proposal": proposal, "node": node_d, "objective": node_d.get("question")}
        decisions = await self.decide(
            state,
            questions,
            decision_type="reason_adjudication",
            session_id=session_id,
            node_id=nid if isinstance(nid, str) else None,
            job_id=job_id,
            choice_options={"tool_selection": options},
        )
        return self._to_tool_decision(
            decisions.get("tool_selection"), options, set_of_registry={o for o in options} - set(_SENTINEL_DESCRIPTIONS)
        )

    async def assess_result(
        self,
        node: Any,
        outcome: Any,
        evidence: Any = None,
        *,
        session_id: str,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        """Post-tool assessment: relevance noul + evidence-state choice + continue choice."""
        node_d = _node_dict(node)
        nid = node_d.get("node_id") or node_d.get("id")
        question = node_d.get("question") or ""
        questions = {
            "relevance": {"type": "noul", "instructions": f"Do the available facts support resolving: {question}"},
            "evidence_state": {
                "type": "choice",
                "instructions": "What best characterizes the evidence state for this node?",
                "criteria": dict(EVIDENCE_STATE_OPTIONS),
            },
            "continue": {
                "type": "choice",
                "instructions": "What should happen next for this node?",
                "criteria": dict(CONTINUE_OPTIONS),
            },
        }
        state = {"node": node_d, "outcome": _outcome_dict(outcome), "evidence": evidence or []}
        decisions = await self.decide(
            state,
            questions,
            decision_type="result_assessment",
            session_id=session_id,
            node_id=nid if isinstance(nid, str) else None,
            job_id=job_id,
            choice_options={"evidence_state": dict(EVIDENCE_STATE_OPTIONS), "continue": dict(CONTINUE_OPTIONS)},
        )
        relevance = decisions.get("relevance") or {}
        ev_state = decisions.get("evidence_state") or {}
        cont = decisions.get("continue") or {}
        probs: dict[str, Any] = {}
        if isinstance(relevance.get("probability"), (int, float)):
            probs["relevance"] = relevance["probability"]
        if isinstance(ev_state.get("probabilities"), dict):
            probs.update(ev_state["probabilities"])
        if isinstance(cont.get("probabilities"), dict):
            probs.update(cont["probabilities"])
        continuation = cont.get("choice") if isinstance(cont.get("choice"), str) else "continue_research"
        ev_choice = ev_state.get("choice") if isinstance(ev_state.get("choice"), str) else None
        return {
            "probabilities": probs,
            "confidence": cont.get("confidence"),
            "continuation": continuation,
            "continue": continuation,
            "action": continuation,
            "evidence": None,
            "candidate": None,
            "admit": None,
            "evidence_state": ev_choice,
            "decision": ev_choice,
            "relevance": relevance.get("probability"),
        }

    @staticmethod
    def _to_tool_decision(decision: Any, options: Mapping[str, str], set_of_registry: set[str]) -> ToolDecision:
        if not isinstance(decision, dict) or decision.get("kind") != "choice":
            raise ValueError("decide: malformed tool_selection decision")
        winner = decision.get("choice")
        probs = decision.get("probabilities")
        conf = decision.get("confidence")
        if not isinstance(winner, str) or not isinstance(probs, dict):
            raise ValueError("decide: malformed tool_selection decision")
        probabilities = {str(k): float(v) for k, v in probs.items()}
        confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else None
        if winner == REASON_SENTINEL:
            out = ToolDecision(action="reason", probabilities=probabilities, confidence=confidence)
        elif winner == RESOLVED_SENTINEL:
            out = ToolDecision(action="resolved", probabilities=probabilities, confidence=confidence)
        else:
            if winner not in set_of_registry:
                raise ValueError(f"decide: tool_selection winner {winner!r} not in registry")
            out = ToolDecision(
                action="invoke",
                tool_name=winner,
                tool_names=(winner,),
                probabilities=probabilities,
                confidence=confidence,
            )
        out.validate("<decision_client>")
        return out

    def _persist(
        self,
        *,
        decision_type: str,
        session_id: str,
        node_id: str | None,
        job_id: str | None,
        questions: dict[str, Any],
        decisions: dict[str, dict[str, Any]],
        raw: Any,
        via: str,
        latency_ms: float,
        started_at: str,
        completed_at: str,
    ) -> None:
        try:
            probabilities: dict[str, Any] = {}
            for qid, decision in decisions.items():
                if not isinstance(decision, dict):
                    continue
                kind = decision.get("kind")
                if kind == "noul" and isinstance(decision.get("probability"), (int, float)):
                    probabilities[qid] = {"probability": decision["probability"]}
                elif kind == "choice" and isinstance(decision.get("probabilities"), dict):
                    probabilities[qid] = dict(decision["probabilities"])
                elif kind == "score":
                    entry: dict[str, Any] = {"score": decision.get("score")}
                    if isinstance(decision.get("probabilities"), dict):
                        entry.update(decision["probabilities"])
                    if decision.get("confidence") is not None:
                        entry["confidence"] = decision["confidence"]
                    probabilities[qid] = entry
                else:
                    probabilities[qid] = dict(decision) if isinstance(decision, dict) else {"result": decision}
            confidence: float | None = None
            if len(decisions) == 1:
                sole = next(iter(decisions.values()))
                raw_conf = sole.get("confidence") if isinstance(sole, dict) else None
                if (
                    isinstance(raw_conf, (int, float))
                    and not isinstance(raw_conf, bool)
                    and 0.0 <= float(raw_conf) <= 1.0
                ):
                    confidence = float(raw_conf)
            record = DecisionRecord(
                decision_id=new_decision_id(),
                session_id=session_id,
                node_id=node_id,
                job_id=job_id,
                decision_type=decision_type,
                candidates=validate_json_mapping(dict(questions), "<decision_client>: 'candidates'"),
                probabilities=validate_json_mapping(probabilities, "<decision_client>: 'probabilities'"),
                selected=validate_json_value(decisions, "<decision_client>: 'selected'"),
                confidence=confidence,
                created_at=utcnow(),
            )
            record.validate("<decision_client>")
        except Exception as exc:  # noqa: BLE001 - persistence never breaks a decision
            logger.debug("jev persist: skipping record build (%s: %s)", type(exc).__name__, exc)
            return
        request = {"state": None, "questions": questions}
        try:
            self._persist_domain(record, request=request, response=raw, latency_ms=latency_ms)
        except Exception as exc:  # noqa: BLE001 - persistence never breaks a decision
            logger.debug("jev persist: domain skipped (%s: %s)", type(exc).__name__, exc)
        try:
            self._persist_runs(
                record,
                request=request,
                response=raw,
                via=via,
                latency_ms=latency_ms,
                started_at=started_at,
                completed_at=completed_at,
                decisions=decisions,
            )
        except Exception as exc:  # noqa: BLE001 - persistence never breaks a decision
            logger.debug("jev persist: runs skipped (%s: %s)", type(exc).__name__, exc)

    def _persist_domain(
        self, record: DecisionRecord, *, request: dict[str, Any], response: Any, latency_ms: float
    ) -> None:
        try:
            repo = ResearchRepository(data_root=self._data_root)
            save = getattr(repo, "save_decision", None)
            if callable(save):
                try:
                    save(
                        record,
                        request=request,
                        response=response,
                        provider=self._provider,
                        latency_ms=latency_ms,
                    )
                except TypeError:
                    save(record)
                return
        except Exception:
            pass
        # ponytail: local jev_decisions table until KernelPersistence's
        # save_decision lands; then the duck-typed path above wins.
        path = get_research_db_path(self._data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = record.to_dict()
        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jev_decisions ("
                " decision_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, node_id TEXT, job_id TEXT,"
                " decision_type TEXT NOT NULL, candidates TEXT NOT NULL, probabilities TEXT NOT NULL,"
                " selected TEXT NOT NULL, confidence REAL, created_at TEXT NOT NULL,"
                " request TEXT NOT NULL, response TEXT NOT NULL, provider TEXT NOT NULL, latency_ms REAL NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO jev_decisions (decision_id, session_id, node_id, job_id,"
                " decision_type, candidates, probabilities, selected, confidence, created_at,"
                " request, response, provider, latency_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc["decision_id"],
                    doc["session_id"],
                    doc["node_id"],
                    doc["job_id"],
                    doc["decision_type"],
                    json.dumps(doc["candidates"], sort_keys=True),
                    json.dumps(doc["probabilities"], sort_keys=True),
                    json.dumps(doc["selected"], sort_keys=True),
                    doc["confidence"],
                    doc["created_at"],
                    json.dumps(request, sort_keys=True, default=str),
                    json.dumps(response, sort_keys=True, default=str),
                    self._provider,
                    latency_ms,
                ),
            )
            conn.commit()

    def _persist_runs(
        self,
        record: DecisionRecord,
        *,
        request: dict[str, Any],
        response: Any,
        via: str,
        latency_ms: float,
        started_at: str,
        completed_at: str,
        decisions: dict[str, dict[str, Any]],
    ) -> None:
        recorder = get_current_recorder()
        if recorder is None or not getattr(recorder, "enabled", True):
            return
        doc = record.to_dict()
        recorder.record_event(
            "jev_decision",
            model=self._model,
            result_summary=json.dumps(decisions, sort_keys=True),
            success=True,
            metadata={
                "decision_id": doc["decision_id"],
                "decision_type": doc["decision_type"],
                "session_id": doc["session_id"],
                "node_id": doc["node_id"],
                "job_id": doc["job_id"],
                "provider": self._provider,
                "via": via,
                "latency_ms": latency_ms,
                "request": request,
                "response": response,
            },
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=latency_ms,
        )
