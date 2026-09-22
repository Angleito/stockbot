"""Stockbot Runtime Kernel scheduler: ResearchNode -> JEV -> Needle -> ToolRuntime -> JEV.

Vertical slice loop (this module is the integration owner)::

    USER QUESTION -> ResearchSession -> Reasoner decompose -> JEV node disposition
      -> ResearchNodes (topo deps) -> JEV whole-registry tool select -> Needle arguments
      -> generic ToolRuntime -> JEV result eval -> kernel persist

Amendment (binding): JEV sees the whole registry (compact manifests) on EVERY
selection including post-tool transitions; JEV selects one tool per round
(successive rounds) or a parallel set (``asyncio.gather`` over the set) — both
shapes run here; Needle is execution-only
(single selected tool schema in, validated args out — mismatch with the JEV
tool rejects); Needle never chains tools, declares resolved, or escalates —
those come only from JEV decisions. Reasoner path: JEV escalates -> reasoner
proposes -> JEV adjudicates -> JEV selects the actual tool action.

Only stdlib + existing modules here. Peer-owned symbols resolve lazily so this
module imports cleanly before and after the foundation slices land; exact
frozen import paths live in the ``_default_*`` resolvers.
"""

import asyncio
import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.tool_runtime import RuntimeToolSession, execute_agent_tool, outcome_from_result

_contract_outcome_from_result = outcome_from_result

try:
    from app.research.models import DecisionRecord, ResearchNode, ResearchNodeStatus, ToolDecision  # noqa: F401
    from app.research.models import new_decision_id, new_node_id  # noqa: F401
except ImportError:  # ponytail: contract stub until KernelPersistence lands

    class ResearchNodeStatus(str):  # type: ignore[no-redef]
        pass

    @dataclass(frozen=True)
    class ResearchNode:  # type: ignore[no-redef]
        node_id: str
        session_id: str
        question: str = ""
        why_it_matters: str = ""
        depends_on: tuple[str, ...] = ()
        status: str = "proposed"
        evidence_ids: tuple[str, ...] = ()
        missing_evidence: tuple[str, ...] = ()

    @dataclass(frozen=True)
    class DecisionRecord:  # type: ignore[no-redef]
        decision_id: str
        session_id: str
        node_id: str | None = None
        job_id: str | None = None
        decision_type: str = ""
        candidates: Any = field(default_factory=dict)
        probabilities: Any = field(default_factory=dict)
        selected: Any = None
        confidence: float | None = None

    @dataclass
    class ToolDecision:  # type: ignore[no-redef]
        action: str = "invoke"
        tool_name: str | None = None
        tool_names: tuple[str, ...] = ()
        probabilities: Any = field(default_factory=dict)
        confidence: float | None = None

        @property
        def selected_tools(self) -> tuple[str, ...]:
            if self.tool_names:
                return self.tool_names
            return (self.tool_name,) if self.tool_name else ()

    def new_node_id() -> str:  # type: ignore[no-redef]
        return f"node:{uuid.uuid4()}"

    def new_decision_id() -> str:  # type: ignore[no-redef]
        return f"dec:{uuid.uuid4()}"


try:
    from app.decision_client import JevClient  # noqa: F401
except ImportError:  # ponytail: contract stub until JevBridge lands

    class JevClient(Protocol):  # type: ignore[no-redef]
        async def decide(self, *args: Any, **kwargs: Any) -> Any: ...
        async def select_tool(self, *args: Any, **kwargs: Any) -> Any: ...
        async def assess_result(self, *args: Any, **kwargs: Any) -> Any: ...
        async def adjudicate(self, *args: Any, **kwargs: Any) -> Any: ...


try:
    from app.reasoner_client import ReasonerClient  # noqa: F401
except ImportError:  # ponytail: contract stub until ToolSelect lands reasoner

    class ReasonerClient(Protocol):  # type: ignore[no-redef]
        async def decompose(self, *args: Any, **kwargs: Any) -> Any: ...
        async def analyze(self, *args: Any, **kwargs: Any) -> Any: ...
        async def expand(self, *args: Any, **kwargs: Any) -> Any: ...


# ponytail: absolute emergency ceiling only; normal stop is the JEV
# resolved/continue/reason verdicts. A trip blocks as visibly incomplete, never convergence.
_MAX_TOOL_ROUNDS = 10

# Amendment: JEV sees the whole canonical RESEARCH registry every selection.
# Meta/ranking-layer tools: never in front of JEV (no search_tools, no ranking
# layer). JEV sees every canonical tool directly; these five are the discovery
# mechanism itself, not research actions.
_JEV_REGISTRY_EXCLUDED = frozenset({"call_tool", "browse_tools", "search_tools", "list_tool_domains", "describe_tool"})

# Required params that name an upstream handle (a prior tool's output) rather
# than fresh node input — surfaced as manifest prerequisites.
_HANDLE_PARAMS = frozenset(
    {
        "accession_no",
        "record_id",
        "source_handle",
        "source_handle_id",
        "tool_result_id",
        "result_id",
        "document_name",
        "search_id",
        "dossier_id",
        "freeze_id",
        "session_id",
        "job_id",
        "evidence_id",
    }
)

# Tools known PIT-blind: no as_of/temporal param and no time_mode support, so a
# historical cutoff cannot scope them. Default true (documented); listed false
# only when confirmed blind. Thesis/research session-local reads are PIT-blind
# by construction (they read current kernel state, not point-in-time sources).
_PIT_BLIND_TOOLS = frozenset(
    {
        "thesis_create",
        "thesis_show",
        "thesis_refine",
        "thesis_watch",
        "thesis_journal",
        "thesis_status",
        "research_start",
        "research_resume",
        "research_status",
        "research_cancel",
        "research_read",
        "research_read_search",
        "research_add_evidence",
        "research_submit_source_result",
        "research_add_analysis",
        "research_finalize",
    }
)


# ponytail: allowlist-derived via source_agent (covers all 7 FINRA evidence
# tools + search_web + SEC allowlist); unknown tools map OTHER and run with
# source=None (no provenance lane until per-domain adapters land) so the default SEC-only policy never denies them.
def _source_for_tool(tool_name: str) -> str:
    """Job source_domain owning one canonical tool: WEB, FINRA, SEC, else OTHER."""
    try:
        from app.research.agents.source_agent import source_domain_for_tool
    except ImportError:
        return "SEC"
    return source_domain_for_tool(tool_name)


# ponytail: kernel builds the candidate from persisted-shaped tool bytes; JEV
# only gates relevance/state (assess_result returns candidate/admit=None).
# FINRA cites canonical row JSON (matches replay), WEB cites the persisted
# highlight verbatim, SEC cites the handle window text the kernel materializes.
def _persisted_shapes(result: Any) -> dict[str, Any]:
    """Persisted payload (tool_result.result or result) for replay-shaped locators."""
    if isinstance(result, dict):
        inner = result.get("result")
        if isinstance(inner, dict):
            return inner
        return result
    return {}


def _finra_locator(payload: dict[str, Any]) -> str | None:
    """First citable FINRA text: canonical row JSON (matches replay), else briefing/metrics."""
    import json as _json

    records = payload.get("records")
    if isinstance(records, list):
        for row in records:
            if isinstance(row, dict):
                text = " ".join(_json.dumps(row, sort_keys=True, default=str).split())
                if text:
                    return text[:2000]
    for key in ("briefing", "briefing_source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:2000]
    metrics = payload.get("metrics")
    if isinstance(metrics, dict):
        text = " ".join(_json.dumps(metrics, sort_keys=True, default=str).split())
        if text:
            return text[:2000]
    return None


def _web_locator(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """(url, highlight) of the first persisted web row with both set."""
    rows = payload.get("evidence")
    if not isinstance(rows, list):
        return None, None
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        highlight = row.get("highlight")
        if isinstance(url, str) and url.strip() and isinstance(highlight, str) and highlight.strip():
            return url.strip(), " ".join(highlight.split())[:2000]
    return None, None


def _sec_locator(payload: dict[str, Any]) -> str | None:
    """First window text of the SEC document result (the handle's window)."""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return " ".join(text.split())[:2000]
    return None


def _evidence_candidate(tool_name: str, domain: str, result: Any, outcome: Any) -> dict[str, Any] | None:
    """Kernel-side evidence candidate from persisted-shaped tool bytes; None when uncitable."""
    if domain not in ("FINRA", "WEB", "SEC"):
        return None
    if domain in ("FINRA", "WEB"):
        ref = _tool_result_ref(result, outcome)
        if ref is None:
            return None
        payload = _persisted_shapes(result)
        content = _outcome_summary(outcome)
        if not content:
            return None
        if domain == "FINRA":
            locator = _finra_locator(payload)
            if locator is None:
                return None
            return {
                "tool_result_id": ref,
                "content": content,
                "record_identity": locator,
                "matching_passage": locator,
            }
        url, highlight = _web_locator(payload)
        if url is None or highlight is None:
            return None
        return {
            "tool_result_id": ref,
            "content": content,
            "url": url,
            "excerpt": highlight,
            "matching_passage": highlight,
        }
    handle = _f(result, "source_handle", default=None)
    if handle is None:
        handle = _f(outcome, "source_handle", "source_refs", default=None)
    if not isinstance(handle, dict) or not handle:
        return None
    locator = _sec_locator(_persisted_shapes(result))
    if locator is None:
        locator = _outcome_summary(outcome)[:2000]
    if not locator:
        return None
    content = _outcome_summary(outcome)
    if not content:
        return None
    return {"source_handle": handle, "content": content, "matching_passage": locator}


_ADMITTABLE_EVIDENCE_STATES = frozenset({"sufficient_support", "sufficient_contradiction", "conflicted"})

# ponytail: one expansion per reason round, JEV-admitted only. JEV outage
# expands nothing (fail-closed); the tool path below still runs.
_EXPAND_CAP = 3
_REASON_SENTINELS = frozenset({"reasoning_required", "reason"})
_RESOLVED_SENTINELS = frozenset({"node_resolved", "resolved"})

_FATAL_ERROR_TYPES = frozenset(
    {
        "auth_required",
        "source_unavailable",
        "storage_error",
        "provider_error",
        "timeout",
        "deadline_exceeded",
    }
)


# ---------------------------------------------------------------------------
# Small duck-typing helpers (peers land dataclasses; fakes/tests use namespaces)
# ---------------------------------------------------------------------------


def _f(obj: Any, *names: str, default: Any = None) -> Any:
    """First present attribute/key under ``names``, else ``default``."""
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


async def _awaited(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _node_dict(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        return dict(node)
    if hasattr(node, "to_dict"):
        try:
            out = node.to_dict()
            if isinstance(out, dict):
                return dict(out)
        except Exception:
            pass
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(node):
            return dict(asdict(node))
    except Exception:
        pass
    return {k: getattr(node, k) for k in dir(node) if not k.startswith("_") and not callable(getattr(node, k, None))}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# ---------------------------------------------------------------------------
# Lazy resolvers: exact frozen import paths, resolved at call time (never import)
# ---------------------------------------------------------------------------


def _default_kernel(repo: Any = None) -> Any:
    """Frozen: app/research/service.py create_node/ready_nodes/resolve_node/block_node/reject_node/record_decision + admit_evidence."""
    from app.research import service as _svc

    _missing = [
        name
        for name in ("ready_nodes", "resolve_node", "block_node", "record_decision", "start_job")
        if not hasattr(_svc, name)
    ]
    if _missing:
        raise RuntimeError(f"kernel service slice unavailable (gap: service.{', '.join(_missing)} not landed)")

    class _Kernel:
        def ready_nodes(self, session_id: str) -> list[Any]:
            return _svc.ready_nodes(session_id, repo=repo)  # type: ignore[arg-type]

        def resolve_node(self, session_id: str, node_id: str) -> Any:
            try:
                return _svc.resolve_node(session_id, node_id, repo=repo)  # type: ignore[call-arg]
            except TypeError:
                return _svc.resolve_node(node_id, repo=repo)  # type: ignore[call-arg]

        def block_node(self, session_id: str, node_id: str, reason: str = "") -> Any:
            try:
                return _svc.block_node(session_id, node_id, reason, repo=repo)  # type: ignore[call-arg]
            except TypeError:
                return _svc.block_node(node_id, repo=repo)  # type: ignore[call-arg]

        def record_decision(self, session_id: str, decision_type: str, **fields: Any) -> Any:
            try:
                return _svc.record_decision(session_id, decision_type, **fields, repo=repo)
            except TypeError:
                minimal = {k: fields[k] for k in ("candidates", "probabilities", "selected") if k in fields}
                return _svc.record_decision(session_id, decision_type, **minimal, repo=repo)

        def admit_evidence(self, session_id: str, job_id: str, data: dict[str, Any]) -> Any:
            admit = getattr(_svc, "admit_evidence", None)
            if admit is not None:
                return admit(session_id, job_id, data, repo=repo)
            return _svc.record_evidence(session_id, job_id, data, repo=repo)

        def start_job(self, session_id: str, **fields: Any) -> dict[str, Any]:
            out = _svc.start_job(session_id, repo=repo, **fields)
            return dict(out) if isinstance(out, dict) else {"job_id": str(out)}

        def heartbeat_job(self, job_id: str) -> None:
            _svc.heartbeat_job(job_id, repo=repo)

        def complete_job(self, job_id: str, outcome: Any = None) -> None:
            _svc.complete_job(job_id, outcome, repo=repo)

        def fail_job(self, job_id: str, category: str, message: str) -> None:
            _svc.fail_job(job_id, category, message, repo=repo)

        def create_node(self, session_id: str, question: str, why_it_matters: str, depends_on: Any = None) -> Any:
            return _svc.create_node(session_id, question, why_it_matters, depends_on=depends_on, repo=repo)

    return _Kernel()


def _default_jev() -> Any:
    """Frozen: app/decision_client.py JevClient.select_tool/assess_result/adjudicate."""
    from app.decision_client import JevClient

    return JevClient()


def _default_reasoner() -> Any:
    """Frozen: app/reasoner_client.py ReasonerClient.decompose/analyze/expand (env-wired)."""
    import os

    from app.reasoner_client import ReasonerClient

    return ReasonerClient(
        api_key=os.environ.get("OPENCODE_API_KEY", ""),
        url=os.environ.get("OPENCODE_URL", "https://opencode.ai/zen/v1/responses"),
        model=os.environ.get("OPENCODE_MODEL", "muse-spark-1.3-contributor"),
    )


def _default_needle_generate() -> Any:
    """Frozen: needle worker op arguments.generate {op,tool,schema,objective,node,context}->{arguments}."""
    try:
        from app.needle_client import generate_arguments  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("needle arguments.generate unavailable (gap: no Python needle client yet)") from exc
    return generate_arguments


def _validate_needle_tool(jev_tool: str, needle_tool: Any) -> None:
    """Frozen: validate_needle_tool(jev_tool, needle_tool) — mismatch rejects."""
    try:
        from app.needle_client import validate_needle_tool  # type: ignore[import-not-found]

        validate_needle_tool(jev_tool, needle_tool)
        return
    except ImportError:
        pass
    if needle_tool != jev_tool:
        raise ValueError(f"needle tool {needle_tool!r} != JEV-selected tool {jev_tool!r}; rejecting")


# ---------------------------------------------------------------------------
# Registry + outcome + session helpers (existing code first)
# ---------------------------------------------------------------------------


def _manifest_params(fn: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(parameters dict, required list) from one canonical function schema."""
    params = fn.get("parameters")
    params = dict(params) if isinstance(params, dict) else {}
    required = params.get("required")
    required = [str(k) for k in required if isinstance(k, str)] if isinstance(required, list) else []
    return params, required


def _manifest_prerequisites(required: list[str]) -> str:
    """Handle-ish required params (upstream outputs) as a compact prereq string."""
    needs = [k for k in required if k in _HANDLE_PARAMS]
    return f"needs {', '.join(needs)}" if needs else ""


def build_registry() -> list[dict[str, Any]]:
    """Compact manifests for every canonical RESEARCH tool (meta tools excluded).

    JEV sees this whole registry on EVERY selection including post-tool
    transitions. Fields match decision/jev.ts ToolManifestEntry: name +
    description required; the catalog detail (use/when-NOT/conflicts/next)
    comes from TOOL_DISCOVERY_REGISTRY so JEV picks the best tool with
    highest probability; prerequisites/pitSupport are budget aids, never a
    filter — every canonical tool stays visible. ``parameters`` rides along
    for Needle (single selected tool schema in). Catalog intent/useWhen/
    avoidWhen/conflicts/nextTools ride along for JEV probability.
    """
    from app.policy import Capability
    from app.security.action_policy import TOOL_DOMAINS
    from app.tools import TOOL_DISCOVERY_REGISTRY, tools_for_capabilities

    manifests: list[dict[str, Any]] = []

    def _semi(items):
        return "; ".join(items) if items else ""

    def _comma(items):
        return ", ".join(items) if items else ""

    for tool in tools_for_capabilities(frozenset({Capability.RESEARCH})):
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in _JEV_REGISTRY_EXCLUDED:
            continue
        params, required = _manifest_params(fn)
        meta = TOOL_DISCOVERY_REGISTRY.get(name)
        domain = TOOL_DOMAINS.get(name, "unknown")
        manifests.append(
            {
                "name": name,
                "domain": domain,
                "description": fn.get("description") if isinstance(fn.get("description"), str) else "",
                "purpose": meta.summary if meta is not None else "",
                "keyInputs": f"req({', '.join(required)})" if required else "req()",
                "outputKind": meta.output_kind if meta is not None else "",
                "evidence": meta.output_kind if meta is not None else "",
                "prerequisites": _manifest_prerequisites(required),
                # ponytail: default true; only confirmed-blind session-local tools opt out.
                "pitSupport": "PIT-blind: current state only" if name in _PIT_BLIND_TOOLS else "PIT-scoped",
                "intent": meta.intent if meta is not None else "",
                "useWhen": _semi(meta.choose_when) if meta is not None else "",
                "avoidWhen": _semi(meta.reject_when) if meta is not None else "",
                "conflicts": _comma(meta.conflicts_with) if meta is not None else "",
                "nextTools": _comma(meta.related_tools) if meta is not None else "",
                "parameters": params,
            }
        )
    return manifests


def _schema_for(tool_name: str, registry: list[dict[str, Any]]) -> dict[str, Any]:
    for entry in registry:
        if entry.get("name") == tool_name:
            schema = entry.get("parameters")
            return dict(schema) if isinstance(schema, dict) else {}
    return {}


def _to_outcome(tool_name: str, result: dict[str, Any]) -> Any:
    if _contract_outcome_from_result is not None:
        return _contract_outcome_from_result(tool_name, result)
    # ponytail: contract stub path (tests monkeypatch _contract_outcome_from_result=None).
    from app.tool_render import render_tool_result

    error = result.get("error") if isinstance(result, dict) else None
    error_type = result.get("error_type") if isinstance(result, dict) else None
    try:
        from types import SimpleNamespace

        return SimpleNamespace(
            tool_name=tool_name,
            content=render_tool_result(result),
            source_handle=None,
            source_refs=None,
            error=error if isinstance(error, str) else None,
            error_type=error_type if isinstance(error_type, str) else None,
            retryable=bool(error) and error_type not in _FATAL_ERROR_TYPES,
            meta=None,
        )
    except Exception:
        return {"tool_name": tool_name, "content": str(result), "error": error, "error_type": error_type}


# Contract: outcome_summary <=2000 chars rides in every attempt; unadmitted
# successful observations stay in working state via context evidence (no new storage).
_OUTCOME_SUMMARY_MAX = 2000

# ponytail: recent-only cap; full history stays in attempts.
_OBSERVATION_TAIL = 10


def _outcome_summary(outcome: Any) -> str:
    text = _f(outcome, "content", default=None)
    if text is None:
        text = str(outcome)
    if not isinstance(text, str):
        text = str(text)
    return text[:_OUTCOME_SUMMARY_MAX]


def _tool_result_ref(result: Any, outcome: Any = None) -> str | None:
    """Persisted FINRA/WEB tool_result_id where one exists; else None (inline summary covers it)."""
    for obj in (result, outcome):
        ref = _f(obj, "tool_result_id", "tool_result_ref", default=None)
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    handle = _f(result, "source_handle", default=None)
    if isinstance(handle, dict):
        for key in ("tool_result_id", "source_handle_id", "result_id"):
            value = handle.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(result, dict):
        for key in ("source_handle_id", "result_id"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _context_evidence(evidence: list[Any], attempts: list[dict[str, Any]]) -> list[Any]:
    """Admitted evidence + recent unadmitted observation summaries (success or failure)."""
    ctx = list(evidence)
    for attempt in attempts[-_OBSERVATION_TAIL:]:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("evidence_id"):
            continue  # admitted evidence already covers it
        summary = attempt.get("outcome_summary")
        if not isinstance(summary, str) or not summary:
            continue
        error = attempt.get("error")
        ctx.append(
            {
                "tool": attempt.get("tool"),
                "job_id": attempt.get("job_id"),
                "outcome_summary": summary,
                "error": error if isinstance(error, str) and error else None,
                "error_type": attempt.get("error_type") if isinstance(attempt.get("error_type"), str) else None,
                "tool_result_ref": attempt.get("tool_result_ref"),
            }
        )
    return ctx


def _load_session(session_id: str, kernel: Any, repo: Any) -> dict[str, Any]:
    get = getattr(kernel, "get_session", None)
    if get is not None:
        found = get(session_id)
        return _node_dict(found)
    try:
        from app.research.repository import ResearchRepository

        store = kernel_store(kernel) or ResearchRepository(repo)
        return _node_dict(store.get_session(session_id))
    except Exception:
        return {"session_id": session_id, "objective": "", "query": "", "as_of": None}


def kernel_store(kernel: Any) -> Any:
    return getattr(kernel, "store", None) or getattr(kernel, "repo", None)


def _load_evidence(session_id: str, kernel: Any, repo: Any) -> list[Any]:
    for method in ("list_evidence", "evidence_for"):
        get = getattr(kernel, method, None)
        if get is not None:
            try:
                return list(get(session_id))
            except Exception:
                continue
    try:
        from app.research.repository import ResearchRepository

        store = kernel_store(kernel) or ResearchRepository(repo)
        return list(store.list_evidence(session_id))
    except Exception:
        return []


def _record(kernel: Any, session_id: str, decision_type: str, **fields: Any) -> Any:
    record = getattr(kernel, "record_decision", None)
    if record is None:
        raise RuntimeError("kernel record_decision unavailable (gap: KernelPersistence slice not landed)")
    return record(session_id, decision_type, **fields)


def _resolve(kernel: Any, session_id: str, node_id: str) -> Any:
    resolve = getattr(kernel, "resolve_node", None)
    if resolve is None:
        raise RuntimeError("kernel resolve_node unavailable (gap: KernelPersistence slice not landed)")
    try:
        return resolve(session_id, node_id)
    except TypeError:
        return resolve(node_id)


def _block(kernel: Any, session_id: str, node_id: str, reason: str) -> Any:
    block = getattr(kernel, "block_node", None)
    if block is None:
        raise RuntimeError("kernel block_node unavailable (gap: KernelPersistence slice not landed)")
    try:
        return block(session_id, node_id, reason)
    except TypeError:
        try:
            return block(session_id, node_id)
        except TypeError:
            return block(node_id)


# ---------------------------------------------------------------------------
# One tool attempt: Needle (execution-only) -> ToolRuntime
# ---------------------------------------------------------------------------


async def _attempt_tool(
    *,
    tool_name: str,
    node: Any,
    session: dict[str, Any],
    registry: list[dict[str, Any]],
    evidence: list[Any],
    attempts: list[dict[str, Any]],
    kernel: Any,
    needle_generate: Any,
    invoke: Any,
    to_outcome: Any,
    tool_session: Any,
    as_of: str | None,
) -> dict[str, Any]:
    if needle_generate is None:
        needle_generate = _default_needle_generate()  # raises a clear gap error when no client exists
    session_id = str(session.get("session_id"))
    node_id = str(_f(node, "node_id", "id"))
    domain = _source_for_tool(tool_name)
    job_source = None if domain == "OTHER" else domain
    try:
        job = kernel.start_job(session_id, type="source_agent", owner="kernel-scheduler", source=job_source)
        job_id = str(_f(job, "job_id", default=f"job:{uuid.uuid4()}"))
    except Exception:
        job_id = f"job:{uuid.uuid4()}"
    arguments: dict[str, Any] = {}
    needle_reasoning = ""
    try:
        try:
            kernel.heartbeat_job(job_id)
        except Exception:
            pass
        schema = _schema_for(tool_name, registry)
        generated = await _awaited(
            needle_generate(
                tool=tool_name,
                schema=schema,
                objective=session.get("objective") or session.get("query") or "",
                node=_node_dict(node),
                context={"evidence": evidence, "attempts": attempts, "as_of": as_of},
            )
            if _needle_takes_kwargs(needle_generate)
            else needle_generate(
                {
                    "op": "arguments.generate",
                    "tool": tool_name,
                    "schema": schema,
                    "objective": session.get("objective") or session.get("query") or "",
                    "node": _node_dict(node),
                    "context": {"evidence": evidence, "attempts": attempts, "as_of": as_of},
                }
            )
        )
        needle_tool, generated_args, needle_reasoning = _split_generated(tool_name, generated)
        _validate_needle_tool(tool_name, needle_tool)
        if not isinstance(generated_args, dict):
            raise ValueError(f"needle arguments for {tool_name!r} must be a mapping")
        arguments = dict(generated_args)
        result = await _awaited(
            invoke(
                tool_name,
                dict(arguments),
                tool_session,
                tool_call_id=f"{node_id}:{tool_name}:{uuid.uuid4().hex[:8]}",
                as_of=as_of,
                active_research_session_id=session_id,
                active_research_job_id=job_id,
            )
        )
        if not isinstance(result, dict):
            raise ValueError(f"tool runtime for {tool_name!r} must return a mapping")
        outcome = await _awaited(to_outcome(tool_name, result))
        outcome_error = _f(outcome, "error")
        return {
            "tool": tool_name,
            "arguments": arguments,
            "outcome": outcome,
            "result": result,
            "domain": domain,
            "outcome_summary": _outcome_summary(outcome),
            "error": None if outcome_error is None else str(outcome_error)[:500],
            "error_type": _f(outcome, "error_type"),
            "reasoning": needle_reasoning[:2000],
            "job_id": job_id,
            "evidence_id": None,
            "tool_result_ref": _tool_result_ref(result, outcome),
        }
    except Exception as exc:
        try:
            kernel.fail_job(job_id, "tool_error", str(exc)[:2000])
        except Exception:
            pass
        return {
            "tool": tool_name,
            "arguments": arguments,
            "outcome": None,
            "outcome_summary": "",
            "error": str(exc)[:500],
            "error_type": "tool_error",
            "reasoning": needle_reasoning[:2000],
            "job_id": job_id,
            "evidence_id": None,
            "tool_result_ref": None,
        }


def _needle_takes_kwargs(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return any(p.kind in (p.VAR_KEYWORD, p.KEYWORD_ONLY) or p.name in ("tool", "schema") for p in params.values())


def _split_generated(jev_tool: str, generated: Any) -> tuple[Any, Any, str]:
    if isinstance(generated, dict):
        reasoning = generated.get("reasoning")
        return (
            generated.get("tool", jev_tool),
            generated.get("arguments", {}),
            reasoning if isinstance(reasoning, str) else "",
        )
    return (
        _f(generated, "tool", default=jev_tool),
        _f(generated, "arguments", default={}),
        (_f(generated, "reasoning", default="") if isinstance(_f(generated, "reasoning", default=""), str) else ""),
    )


# ---------------------------------------------------------------------------
# Reasoner path: JEV escalates -> reasoner proposes -> JEV adjudicates
# ---------------------------------------------------------------------------


async def _reasoner_propose(
    reasoner: Any, session: dict[str, Any], node: Any, evidence: list[Any], attempts: list[Any]
) -> Any:
    prompt = {
        "objective": session.get("objective") or session.get("query") or "",
        "node": _node_dict(node),
        "evidence": evidence,
        "attempts": attempts,
    }
    for method in ("reason", "analyze", "decompose", "expand"):
        fn = getattr(reasoner, method, None)
        if fn is not None:
            return await _awaited(fn(prompt))
    if callable(reasoner):
        return await _awaited(reasoner(prompt))
    raise RuntimeError("reasoner has no reason/analyze/decompose/expand entrypoint")


def _expansion_proposals(proposal: Any) -> list[dict[str, Any]]:
    """Reasoner-discovered follow-ups (non-authoritative until JEV admits)."""
    if not isinstance(proposal, dict):
        return []
    raw = proposal.get("proposals")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            continue
        why = item.get("whyItMatters")
        out.append(
            {
                "question": question.strip(),
                "whyItMatters": why if isinstance(why, str) and why.strip() else "Route question.",
            }
        )
        if len(out) >= _EXPAND_CAP:
            break
    return out


async def _expand_graph(jev: Any, kernel: Any, sid: str, objective: str, node_id: str, proposal: Any) -> int:
    """Admit reasoner follow-ups via one JEV disposition round; create admitted nodes."""
    candidates = _expansion_proposals(proposal)
    if not candidates:
        return 0
    create = getattr(kernel, "create_node", None)
    if create is None:
        return 0
    try:
        decide = getattr(jev, "decide", None)
        if decide is None:
            return 0
        questions: dict[str, Any] = {}
        options: dict[str, dict[str, str]] = {}
        criteria = {
            "admit": "The follow-up materially contributes to resolving the objective.",
            "reject": "The follow-up does not materially contribute to resolving the objective.",
        }
        for i, cand in enumerate(candidates):
            qid = f"expand-{i}"
            questions[qid] = {
                "type": "choice",
                "instructions": (
                    "Does this follow-up question materially contribute to resolving the objective? "
                    f"Objective: {objective} Question: {cand['question']}"
                ),
                "criteria": criteria,
            }
            options[qid] = dict(criteria)
        state = {"objective": objective, "node_id": node_id, "proposals": candidates}
        decisions = await _awaited(
            decide(
                state,
                questions,
                decision_type="graph_expansion",
                session_id=sid,
                choice_options=options,
            )
        )
    except Exception:  # noqa: BLE001, S110 - JEV outage expands nothing; tool path below still runs
        return 0
    if not isinstance(decisions, dict):
        return 0
    created = 0
    for i, cand in enumerate(candidates):
        verdict = decisions.get(f"expand-{i}")
        choice = verdict.get("choice") if isinstance(verdict, dict) else None
        if choice == "reject":
            continue
        try:
            await _awaited(create(sid, str(cand["question"]), str(cand["whyItMatters"]), depends_on=[node_id]))
            created += 1
        except Exception:  # noqa: BLE001, S112 - one bad follow-up never blocks its siblings
            continue
    _record(
        kernel,
        sid,
        "graph_expansion",
        candidates={str(i): c["question"] for i, c in enumerate(candidates)},
        probabilities={},
        selected={"created": created, "proposed": len(candidates)},
        node_id=node_id or None,
        job_id=None,
        confidence=None,
    )
    return created


# ---------------------------------------------------------------------------
# Per-node loop
# ---------------------------------------------------------------------------


def _selection_tools(decision: Any) -> list[str]:
    if isinstance(decision, (list, tuple)):
        names = list(decision)
    elif hasattr(decision, "selected_tools"):
        # Frozen ToolDecision.selected_tools is a @property (tuple); accept a method too.
        try:
            selected = decision.selected_tools() if callable(decision.selected_tools) else decision.selected_tools
            names = list(selected)
        except Exception:
            names = _as_list(_f(decision, "tool_name", "tool", "tool_names", "tools", "selected", default=[]))
    else:
        names = _as_list(_f(decision, "tool_name", "tool", "tool_names", "tools", "selected", default=[]))
    return [str(n) for n in names if isinstance(n, str) and n and n not in _REASON_SENTINELS | _RESOLVED_SENTINELS]


def _selection_action(decision: Any) -> str:
    if isinstance(decision, (list, tuple)):
        names = [str(n) for n in decision if isinstance(n, str) and n]
        if names and all(n in _RESOLVED_SENTINELS for n in names):
            return "resolved"
        if names and all(n in _REASON_SENTINELS for n in names):
            return "reason"
        return "invoke" if names else "reason"
    action = _f(decision, "action", default=None)
    if isinstance(action, str) and action:
        return action
    names = _as_list(_f(decision, "tool_name", "tool", "tool_names", "tools", "selected", default=[]))
    if any(n in _RESOLVED_SENTINELS for n in names):
        return "resolved"
    if any(n in _REASON_SENTINELS for n in names):
        return "reason"
    return "invoke" if names else "reason"


async def run_node(node: Any, session_id: str | None = None, **hooks: Any) -> dict[str, Any]:
    """Run one ResearchNode to resolved/blocked. Terminal-safe: never raises without recording failure."""
    try:
        return await _run_node(node, session_id=session_id, **hooks)
    except Exception as exc:  # terminal-safe: record the failure, then report it
        sid = session_id or str(_f(node, "session_id", default=""))
        nid = str(_f(node, "node_id", "id", default=""))
        kernel = hooks.get("kernel")
        try:
            if kernel is not None and sid:
                _record(
                    kernel,
                    sid,
                    "node_failure",
                    candidates={},
                    probabilities={},
                    selected={"error": str(exc)[:2000]},
                    node_id=nid or None,
                    job_id=None,
                    confidence=None,
                )
        except Exception:
            pass
        return {"node_id": nid, "status": "failed", "error": str(exc)[:2000]}


async def _run_node(node: Any, session_id: str | None = None, **hooks: Any) -> dict[str, Any]:
    repo = hooks.get("repo")
    kernel = hooks.get("kernel") or _default_kernel(repo)
    jev = hooks.get("jev") or _default_jev()
    reasoner = hooks.get("reasoner")  # lazy: only the reason path needs it
    needle_generate = hooks.get("needle_generate") or hooks.get("needle")  # lazy: only the invoke path needs it
    invoke = hooks.get("invoke") or execute_agent_tool
    to_outcome = hooks.get("to_outcome") or _to_outcome
    registry = hooks.get("registry")
    if registry is None:
        try:
            registry = build_registry()
        except Exception:
            registry = []

    sid = session_id or str(_f(node, "session_id", default=""))
    if not sid:
        raise ValueError("run_node: session_id required (argument or node.session_id)")
    nid = str(_f(node, "node_id", "id", default=""))
    session = _load_session(sid, kernel, repo)
    session.setdefault("session_id", sid)
    as_of = session.get("as_of")
    as_of_str = as_of if isinstance(as_of, str) else (as_of.isoformat() if hasattr(as_of, "isoformat") else None)
    tool_session = hooks.get("tool_session") or RuntimeToolSession(session_id=f"scheduler:{sid}")

    evidence = _load_evidence(sid, kernel, repo)
    attempts: list[dict[str, Any]] = []
    admitted = 0

    for _ in range(_MAX_TOOL_ROUNDS):
        # Working state: admitted evidence + recent unadmitted observations, never dropped.
        ctx_evidence = _context_evidence(evidence, attempts)
        decision = await _awaited(
            jev.select_tool(
                session.get("objective") or session.get("query") or "",
                node,
                registry,
                ctx_evidence,
                attempts,
                session_id=sid,
                job_id=None,
            )
        )
        _record(
            kernel,
            sid,
            "tool_selection",
            candidates=dict(_f(decision, "probabilities", default={}) or {}),
            probabilities=dict(_f(decision, "probabilities", default={}) or {}),
            selected=_f(decision, "tool_name", "tool", "selected"),
            node_id=nid or None,
            job_id=None,
            confidence=_f(decision, "confidence"),
        )
        action = _selection_action(decision)

        if action == "resolved":
            _resolve(kernel, sid, nid)
            return {
                "node_id": nid,
                "status": "resolved",
                "admitted": admitted,
                "attempts": attempts,
                "incomplete_guard": False,
            }

        if action == "reason":
            active_reasoner = reasoner if reasoner is not None else _default_reasoner()
            proposal = await _reasoner_propose(active_reasoner, session, node, ctx_evidence, attempts)
            await _expand_graph(jev, kernel, sid, session.get("objective") or session.get("query") or "", nid, proposal)
            verdict = await _awaited(jev.adjudicate(proposal, node, session_id=sid, job_id=None))
            _record(
                kernel,
                sid,
                "reason_adjudication",
                candidates={},
                probabilities=dict(_f(verdict, "probabilities", default={}) or {}),
                selected=_f(verdict, "tool_name", "tool", "selected", "action"),
                node_id=nid or None,
                job_id=None,
                confidence=_f(verdict, "confidence"),
            )
            verdict_action = _selection_action(verdict)
            if verdict_action == "resolved":
                _resolve(kernel, sid, nid)
                return {
                    "node_id": nid,
                    "status": "resolved",
                    "admitted": admitted,
                    "attempts": attempts,
                    "incomplete_guard": False,
                }
            tools = _selection_tools(verdict) or _selection_tools(decision)
            if not tools:
                attempts.append(
                    {
                        "tool": None,
                        "arguments": {},
                        "outcome_summary": "",
                        "error": "adjudication named no tool; re-selecting",
                        "job_id": None,
                        "evidence_id": None,
                        "tool_result_ref": None,
                    }
                )
                continue
            decision = verdict  # JEV adjudication selects the actual tool action; fall through to invoke.

        tools = _selection_tools(decision)
        if not tools:
            attempts.append(
                {
                    "tool": None,
                    "arguments": {},
                    "outcome_summary": "",
                    "error": "selection named no tool; re-selecting",
                    "job_id": None,
                    "evidence_id": None,
                    "tool_result_ref": None,
                }
            )
            continue

        # Parallel multi-tool select: gather over the whole selected set.
        results = await asyncio.gather(
            *(
                _attempt_tool(
                    tool_name=tool,
                    node=node,
                    session=session,
                    registry=registry,
                    evidence=ctx_evidence,
                    attempts=attempts,
                    kernel=kernel,
                    needle_generate=needle_generate,
                    invoke=invoke,
                    to_outcome=to_outcome,
                    tool_session=tool_session,
                    as_of=as_of_str,
                )
                for tool in tools
            )
        )
        progressed = False
        for attempt in results:
            tool = attempt["tool"]
            if attempt["error"] is not None and attempt["outcome"] is None:
                try:
                    kernel.fail_job(attempt["job_id"], "tool_error", str(attempt["error"])[:2000])
                except Exception:  # noqa: BLE001, S110 - terminal already recorded; attempt log carries the error
                    pass
                attempts.append(
                    {
                        k: attempt.get(k)
                        for k in (
                            "tool",
                            "arguments",
                            "outcome_summary",
                            "error",
                            "error_type",
                            "confidence",
                            "reasoning",
                            "job_id",
                            "evidence_id",
                            "tool_result_ref",
                        )
                    }
                )
                continue
            outcome = attempt["outcome"]
            assessment = await _awaited(
                jev.assess_result(
                    node, outcome, _context_evidence(evidence, attempts), session_id=sid, job_id=attempt["job_id"]
                )
            )
            _record(
                kernel,
                sid,
                "result_assessment",
                candidates={},
                probabilities=dict(_f(assessment, "probabilities", default={}) or {}),
                selected=_f(assessment, "continuation", "continue", "action", "evidence_state", "decision"),
                node_id=nid or None,
                job_id=attempt["job_id"],
                confidence=_f(assessment, "confidence"),
            )
            outcome_error = _f(outcome, "error")
            if outcome_error is not None:
                try:
                    kernel.fail_job(attempt["job_id"], "tool_error", str(outcome_error)[:2000])
                except Exception:  # noqa: BLE001, S110 - terminal already recorded; attempt log carries the error
                    pass
                attempts.append(
                    {
                        k: attempt.get(k)
                        for k in (
                            "tool",
                            "arguments",
                            "outcome_summary",
                            "error",
                            "error_type",
                            "confidence",
                            "reasoning",
                            "job_id",
                            "evidence_id",
                            "tool_result_ref",
                        )
                    }
                )
                continue
            # Kernel builds the candidate from tool bytes; JEV gates relevance/state only.
            evidence_id = None
            ev_state = _f(assessment, "evidence_state", "decision", default=None)
            domain = attempt.get("domain") if isinstance(attempt.get("domain"), str) else _source_for_tool(tool)
            candidate = _evidence_candidate(tool, domain, attempt.get("result"), outcome)
            if (
                isinstance(ev_state, str)
                and ev_state in _ADMITTABLE_EVIDENCE_STATES
                and candidate is not None
                and _f(outcome, "error") is None
            ):
                try:
                    admitted_ret = kernel.admit_evidence(sid, attempt["job_id"], candidate)
                    eid = _f(admitted_ret, "evidence_id", default=None)
                    evidence_id = eid if isinstance(eid, str) and eid else None
                    admitted += 1
                    progressed = True
                except Exception as exc:
                    try:
                        kernel.complete_job(attempt["job_id"], {"tool": tool})
                    except Exception:  # noqa: BLE001, S110 - admit outcome recorded; attempt log carries the state
                        pass
                    attempts.append(
                        {
                            "tool": tool,
                            "arguments": attempt.get("arguments") if isinstance(attempt.get("arguments"), dict) else {},
                            "outcome_summary": attempt.get("outcome_summary")
                            if isinstance(attempt.get("outcome_summary"), str)
                            else "",
                            "error": f"admit failed: {exc}"[:500],
                            "reasoning": attempt.get("reasoning") if isinstance(attempt.get("reasoning"), str) else "",
                            "job_id": attempt.get("job_id"),
                            "evidence_id": None,
                            "tool_result_ref": attempt.get("tool_result_ref"),
                        }
                    )
                    continue
            try:
                kernel.complete_job(attempt["job_id"], {"tool": tool})
            except Exception:  # noqa: BLE001, S110 - terminal already recorded; attempt log carries the state
                pass
            attempts.append(
                {
                    "tool": tool,
                    "arguments": attempt.get("arguments") if isinstance(attempt.get("arguments"), dict) else {},
                    "outcome_summary": attempt.get("outcome_summary")
                    if isinstance(attempt.get("outcome_summary"), str)
                    else "",
                    "error": None if outcome_error is None else str(outcome_error)[:500],
                    "error_type": _f(outcome, "error_type"),
                    "confidence": _f(decision, "confidence"),
                    "reasoning": attempt.get("reasoning") if isinstance(attempt.get("reasoning"), str) else "",
                    "job_id": attempt.get("job_id"),
                    "evidence_id": evidence_id,
                    "tool_result_ref": attempt.get("tool_result_ref"),
                }
            )
            continuation = str(_f(assessment, "continuation", "continue", "action", default="continue_research"))
            if continuation == "resolve_node":
                _resolve(kernel, sid, nid)
                return {
                    "node_id": nid,
                    "status": "resolved",
                    "admitted": admitted,
                    "attempts": attempts,
                    "incomplete_guard": False,
                }
            if continuation == "reason_over_evidence":
                break  # fresh select round; JEV re-escalates if reasoning is still needed.
        evidence = _load_evidence(sid, kernel, repo)
        if not progressed:
            continue

    reason = f"incomplete: runtime guard ({_MAX_TOOL_ROUNDS} rounds without resolution)"
    _block(kernel, sid, nid, reason)
    return {
        "node_id": nid,
        "status": "blocked",
        "reason": reason,
        "incomplete_guard": True,
        "admitted": admitted,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Session loop: gather over ready nodes until none remain or no progress
# ---------------------------------------------------------------------------


async def run(session_id: str, **hooks: Any) -> dict[str, Any]:
    """Run every ready node (parallel gather per round) until none remain or a round stalls."""
    repo = hooks.get("repo")
    kernel = hooks.get("kernel")
    if kernel is None:
        try:
            kernel = _default_kernel(repo)
        except Exception as exc:
            return {"session_id": session_id, "status": "failed", "error": f"default kernel unavailable: {exc}"}
    ready = getattr(kernel, "ready_nodes", None)
    if ready is None:
        return {"session_id": session_id, "status": "failed", "error": "kernel ready_nodes unavailable"}
    node_results: list[dict[str, Any]] = []
    rounds = 0
    incomplete_guard = False
    guard_reason: str | None = None
    while True:
        try:
            nodes = await _awaited(ready(session_id))
        except Exception as exc:
            return {"session_id": session_id, "status": "failed", "error": str(exc)[:500], "nodes": node_results}
        nodes = list(nodes) if isinstance(nodes, (list, tuple)) else []
        if not nodes:
            break
        rounds += 1
        # ponytail: absolute emergency ceiling only; a trip is visibly incomplete, never convergence.
        if rounds > _MAX_TOOL_ROUNDS:
            incomplete_guard = True
            guard_reason = f"incomplete: runtime guard ({_MAX_TOOL_ROUNDS} session rounds without convergence)"
            break
        round_results = await asyncio.gather(
            *(run_node(node, session_id, **{**hooks, "kernel": kernel}) for node in nodes)
        )
        node_results.extend(round_results)
        round_guard = any(r.get("incomplete_guard") for r in round_results)
        if round_guard:
            incomplete_guard = True
        if not any(r.get("status") == "resolved" or r.get("admitted") for r in round_results):
            if incomplete_guard:
                reason = (
                    guard_reason
                    if guard_reason is not None
                    else "incomplete: node guard trip stalled the session (no resolved nodes, no admitted evidence)"
                )
                return {
                    "session_id": session_id,
                    "status": "incomplete_guard",
                    "rounds": rounds,
                    "nodes": node_results,
                    "incomplete_guard": True,
                    "reason": reason,
                }
            return {
                "session_id": session_id,
                "status": "stalled",
                "rounds": rounds,
                "nodes": node_results,
                "incomplete_guard": False,
                "reason": "stalled: session round made no progress (no resolved nodes, no admitted evidence)",
            }
    status = "complete" if not incomplete_guard else "incomplete_guard"
    out = {
        "session_id": session_id,
        "status": status,
        "rounds": rounds,
        "nodes": node_results,
        "incomplete_guard": incomplete_guard,
    }
    if guard_reason is not None:
        out["reason"] = guard_reason
    return out


__all__ = ["build_registry", "run", "run_node"]
