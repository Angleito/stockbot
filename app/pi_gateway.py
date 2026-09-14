"""Pi tool gateway: Pi-driven tool-call security gates.

Pi is the reasoning model; every tool call it makes still passes these gates:
permit filter, argument validation, intent firewall, egress/private-args
checks, ingress scan, LOCAL_CONTEXT-only execution, DLP, budget + recorder.
Rival pattern to avoid: calling execute_tool directly from the bridge
(drops all gates).

One deliberate divergence: the ingress scan uses envelope_for_tool +
prepare_context directly on rendered evidence — Pi itself reads it, and no
source text is promoted into canonical facts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from .policy import LOCAL_CONTEXT, Capability, RequestContext
from .redact import redact_json, redact_text
from .security.action_policy import (
    TOOL_DOMAINS,
    authorize_egress,
    authorize_tool_call,
    private_pattern_hit,
)
from .security.context import (
    RunSecurityContext,
    Sensitivity,
    SessionAuthorization,
    SessionSecurityState,
    classify_intent,
)
from .security.context_gateway import (
    QuarantinedContext,
    envelope_for_tool,
    prepare_context,
)
from .security.response_guard import guard_response
from .runtime import ExecutionBudget, ToolResultMeta
from .storage.runs import get_current_recorder
from .tool_render import render_tool_result
from .tools import (
    TOOLS,
    TOOL_REGISTRY_VERSION,
    _invalid_args_error,
    _resolve_company_to_ticker,
    _tool_function,
    _unknown_tool_error,
    _validate_tool_arguments,
    execute_tool,
    tool_is_permitted,
    tools_for_capabilities,
)
from app.research.stage import DISCOVERY_TOOLS, DISPATCH_TOOLS, RESEARCH_TOOL_NAMES, check_stage_tool, stage_for_session

_CALL_TOOL_FORBIDDEN = frozenset({"call_tool", "browse_tools", "search_tools", "list_tool_domains", "describe_tool"})

# Bounded local thesis lifecycle sinks (sqlite store only; never egress).
# Private-pattern arg scanning would false-positive on user thesis content,
# while real exfiltration vectors (search_web, external research) stay scanned.
_THESIS_LOCAL_TOOLS = frozenset({
    "thesis_create", "thesis_show", "thesis_refine", "thesis_watch", "thesis_journal",
})

logger = logging.getLogger(__name__)

# Model label recorded for Pi-driven tool calls. Handlers ignore it
# (no nested completions remain on the Pi path); it exists for provenance.
PI_MODEL = "pi"

_UNAVAILABLE_HEADER = (
    "The requested data is unavailable: one or more tool calls failed or "
    "returned no data, so the exact values cannot be provided. No values "
    "are estimated, derived, or substituted."
)

_UNAVAILABLE_NEXT_STEP = (
    "Next step: correct the request (dataset, fields, filters, or "
    "credentials) and retry, or use a different dataset/source. The error "
    "above states exactly what failed."
)

_BUDGET_EXHAUSTED_RESPONSE = (
    "The research budget was exhausted before a final answer could be "
    "produced. Retry with a narrower question or fewer tool calls."
)

_NON_DATA_LIST_KEYS = frozenset({"source_records", "warnings", "metrics", "trends"})


def _is_failed_result(result: object) -> bool:
    """A tool result is a failure when it carries an explicit error."""
    return isinstance(result, dict) and bool(result.get("error"))


def _unavailable_data_response(failed: list[tuple[str, dict[str, object]]]) -> str:
    """Deterministic user-facing response when any tool call failed.

    Built from the rendered error context of each failed tool (name,
    dataset/source, HTTP status, sanitized FINRA response, environment)
    plus a next step. The model is never consulted, so nothing can be
    invented, derived, or substituted to fill the gap.
    """
    lines = [_UNAVAILABLE_HEADER, ""]
    for name, result in failed:
        lines.append(f"Tool: {name}")
        for line in render_tool_result(result).splitlines():
            lines.append("  " + line)
        lines.append("")
    lines.append(_UNAVAILABLE_NEXT_STEP)
    return "\n".join(lines)


_SOURCE_REF_ID_KEYS = (
    "accession_no", "accession_number", "accession", "record_id",
    "document_name", "filing_id",
)
_SOURCE_REF_URL_KEYS = ("url", "source_url", "source", "filing_url", "document_url", "link")
_SOURCE_REF_DATE_KEYS = (
    "known_at", "accepted_at", "acceptanceDatetime", "acceptedDate",
    "filed_at", "filingDate", "filed", "published_at", "publishedAt", "published",
)


def _is_uri_like(value: str) -> bool:
    text = value.strip()
    return "://" in text or "/" in text or "." in text


def _extract_source_refs(result: object) -> dict[str, object]:
    """First actual record reference; top-level labels never qualify."""
    if not isinstance(result, dict):
        return {}
    candidates: list[dict[str, object]] = [result]
    for value in result.values():
        if isinstance(value, dict):
            candidates.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    candidates.append(item)
    all_refs: list[dict[str, str]] = []
    for item in candidates:
        ref: dict[str, str] = {}
        for key in _SOURCE_REF_ID_KEYS:
            raw_id = item.get(key)
            if isinstance(raw_id, (str, int)) and str(raw_id).strip():
                ref["record_id"] = str(raw_id).strip()
                break
        for key in _SOURCE_REF_URL_KEYS:
            raw_url = item.get(key)
            if isinstance(raw_url, str) and raw_url.strip() and _is_uri_like(raw_url):
                ref["uri"] = raw_url.strip()
                break
        for key in _SOURCE_REF_DATE_KEYS:
            raw_date = item.get(key)
            if isinstance(raw_date, str) and raw_date.strip():
                ref["known_at"] = raw_date.strip()
                break
        if ref and ref not in all_refs:
            all_refs.append(ref)
    if not all_refs:
        return {}
    primary: dict[str, object] = dict(next((r for r in all_refs if "record_id" in r), all_refs[0]))
    if len(all_refs) > 1:
        primary["all"] = all_refs
    return primary


def _tool_result_meta(result: object) -> ToolResultMeta:
    """Best-effort telemetry envelope for a tool result: row counts,
    truncation, source name, and freshness."""
    if not isinstance(result, dict):
        return ToolResultMeta(0, None, False, None, [], {})
    source = result.get("source") or result.get("dataset_id") or result.get("dataset")
    source_names = [str(source)] if source is not None else []
    freshness_value = next(
        (result[k] for k in ("retrieved_at", "data_freshness", "freshness", "as_of_date", "as_of")
         if result.get(k) is not None),
        None)
    source_freshness = (
        {str(source): str(freshness_value)} if source is not None and freshness_value is not None else {})
    as_of = next((result[k] for k in ("as_of_date", "as_of")
                  if result.get(k) is not None), None)
    returned_count = result.get("returned_count") if isinstance(result.get("returned_count"), int) else None
    row_count = result.get("row_count") if isinstance(result.get("row_count"), int) else max(
        (len(v) for k, v in result.items() if isinstance(v, list) and k not in _NON_DATA_LIST_KEYS),
        default=0)
    total = result.get("total_records")
    truncated = (
        bool(result.get("truncated"))
        or result.get("may_have_more") is True
        or (returned_count is not None and isinstance(total, int) and returned_count < total))
    return ToolResultMeta(row_count, returned_count, truncated,
                          str(as_of) if as_of is not None else None, source_names, source_freshness)


@dataclass
class PiSessionContext:
    """Per-Pi-session grant + labels + budget. Deny-by-default."""

    session_id: str
    active_research_session_id: str | None = None
    active_research_job_id: str | None = None
    authorization: SessionAuthorization = field(default_factory=SessionAuthorization)
    security_state: SessionSecurityState = field(default_factory=SessionSecurityState)
    run_security: RunSecurityContext = field(init=False)
    budget: ExecutionBudget = field(init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.security_state.authorization = self.authorization
        if self.security_state.private_context_seen:
            data_labels = {"private"}
        else:
            data_labels = set()
        self.run_security = RunSecurityContext(
            original_intent=classify_intent([]),
            capabilities=frozenset(cap.name for cap in LOCAL_CONTEXT.capabilities),
            authorization=self.authorization,
            data_labels=data_labels,
        )
        limits = LOCAL_CONTEXT.run_limits
        self.budget = ExecutionBudget(
            max_tool_calls=limits.max_tool_calls,
            max_runtime=limits.max_runtime,
            max_evidence_tokens=limits.max_evidence_tokens,
        )


def _record_security(
    session: PiSessionContext,
    source: str,
    payload: str,
    decision: str,
    reason: str | None,
    *,
    score: int | None = None,
    verdict: str | None = None,
    rule_ids: list[str] | None = None,
) -> None:
    with session._lock:
        session.run_security.security_events.append(
            {
                "source": source,
                "decision": decision,
                "reason": reason,
            }
        )
    recorder = get_current_recorder()
    if recorder is not None:
        recorder.record_security_event(
            source=source,
            sha256=hashlib.sha256(payload.encode()).hexdigest(),
            score=score,
            verdict=verdict,
            rule_ids=rule_ids or [],
            decision=decision,
            reason=reason,
        )


def _args_json(arguments: dict[str, object]) -> str:
    return json.dumps(arguments, sort_keys=True)


def _override_context(data_root: str | Path | None = None, as_of: str | None = None):
    """LOCAL_CONTEXT with data_root overridden; invalid roots fall back."""
    def _with_root(root: Path) -> RequestContext:
        return RequestContext(
            principal_id=LOCAL_CONTEXT.principal_id,
            capabilities=LOCAL_CONTEXT.capabilities,
            tool_policy=LOCAL_CONTEXT.tool_policy,
            data_root=root,
            run_limits=LOCAL_CONTEXT.run_limits,
            as_of=as_of,
        )
    if data_root is None or not str(data_root):
        if as_of is None:
            return LOCAL_CONTEXT
        return _with_root(LOCAL_CONTEXT.data_root)
    try:
        root = Path(str(data_root))
    except Exception:
        return _with_root(LOCAL_CONTEXT.data_root) if as_of is not None else LOCAL_CONTEXT
    if not root.is_absolute():
        return _with_root(LOCAL_CONTEXT.data_root) if as_of is not None else LOCAL_CONTEXT
    return _with_root(root)

def execute_pi_tool(
    name: str,
    arguments: dict[str, object],
    session: PiSessionContext,
    *,
    tool_call_id: str | None = None,
    protocol_id: str | None = None,
    bridge_queue_ms: float = 0.0,
    data_root: str | Path | None = None,
    as_of: str | None = None,
    active_research_session_id: str | None = None,
    active_research_job_id: str | None = None,
) -> dict[str, object]:
    """Run one Pi-requested tool through all gates. Never raises."""
    try:
        return _execute_pi_tool(
            name,
            arguments,
            session,
            tool_call_id=tool_call_id,
            protocol_id=protocol_id,
            bridge_queue_ms=bridge_queue_ms,
            data_root=data_root,
            as_of=as_of,
            active_research_session_id=active_research_session_id,
            active_research_job_id=active_research_job_id,
        )
    except Exception as exc:  # never break the bridge loop
        logger.exception("Pi tool gateway failed for '%s'", name)
        return {"error": f"Pi tool gateway failed for tool '{name}': {exc}"}


def _execute_pi_tool(
    name: str,
    arguments: dict[str, object],
    session: PiSessionContext,
    *,
    tool_call_id: str | None = None,
    protocol_id: str | None = None,
    bridge_queue_ms: float = 0.0,
    data_root: str | Path | None = None,
    as_of: str | None = None,
    active_research_session_id: str | None = None,
    active_research_job_id: str | None = None,
) -> dict[str, object]:
    # Generic dispatch: call_tool validates then tail-calls the inner tool once.
    # The outer wrapper consumes no budget slot and writes no recorder row.
    if name == "call_tool":
        raw_inner = arguments.get("name") if isinstance(arguments, dict) else None
        raw_args = arguments.get("arguments") if isinstance(arguments, dict) else None
        if raw_args is None:
            raw_inner_args: object = {}
        else:
            raw_inner_args = raw_args
        if not isinstance(raw_inner, str) or not raw_inner.strip():
            invalid_outer = _validate_tool_arguments("call_tool", arguments if isinstance(arguments, dict) else {})
            msg = invalid_outer if invalid_outer is not None else "call_tool: 'name' must be a non-empty string"
            return _invalid_args_error("call_tool", msg)
        inner_name = raw_inner.strip()
        if not isinstance(raw_inner_args, dict):
            invalid_inner_args = _validate_tool_arguments("call_tool", arguments if isinstance(arguments, dict) else {})
            msg_inner = invalid_inner_args if invalid_inner_args is not None else "call_tool: 'arguments' must be an object"
            return _invalid_args_error("call_tool", msg_inner)
        if inner_name in _CALL_TOOL_FORBIDDEN:
            return _invalid_args_error("call_tool", f"Tool '{inner_name}' cannot be called via call_tool; call browse_tools to find the exact canonical name, then call_tool with a research tool name")
        if not any(_tool_function(t).get("name") == inner_name for t in TOOLS):
            return _unknown_tool_error(inner_name)
        return _execute_pi_tool(inner_name, raw_inner_args, session, tool_call_id=tool_call_id, protocol_id=protocol_id, bridge_queue_ms=bridge_queue_ms, data_root=data_root, as_of=as_of, active_research_session_id=active_research_session_id, active_research_job_id=active_research_job_id)
    # Single-dispatch company-name support, schema-driven: any tool whose
    # schema declares company_name alongside a ticker/entity identifier
    # fills the missing identifier before validation. Cards keep the
    # identifier required (visible signal) while name-only dispatches
    # still execute; tools without company_name are untouched.
    if isinstance(arguments, dict):
        _fn = next(
            (_tool_function(t) for t in TOOLS if _tool_function(t).get("name") == name),
            None,
        )
        _fparams = _fn.get("parameters") if isinstance(_fn, dict) else None
        _fprops = _fparams.get("properties") if isinstance(_fparams, dict) else None
        _freq = _fparams.get("required") if isinstance(_fparams, dict) else None
        _req_names: list[str] = [str(_r) for _r in _freq] if isinstance(_freq, list) else []
        _prop_names: set[str] = set(_fprops.keys()) if isinstance(_fprops, dict) else set()
        _id_key = next(
            (_k for _k in ("ticker", "entity") if _k in _req_names and _k in _prop_names),
            next((_k for _k in ("ticker", "entity") if _k in _prop_names), None),
        )
        if _id_key is not None and "company_name" in _prop_names:
            _raw_id = arguments.get(_id_key)
            _raw_cname = arguments.get("company_name")
            if (not isinstance(_raw_id, str) or not _raw_id.strip()) and isinstance(_raw_cname, str) and _raw_cname.strip():
                try:
                    _resolved = _resolve_company_to_ticker(_raw_cname)
                except Exception:
                    _resolved = None
                if _resolved:
                    arguments = {**arguments, _id_key: _resolved}
    recorder = get_current_recorder()
    run_id = recorder.run_id if recorder is not None else f"pi-{session.session_id}"
    args_for_hash = (
        _args_json(arguments) if isinstance(arguments, dict) else json.dumps(str(arguments))
    )

    # Staged context: immutable request-local IDs. Explicit canonical
    # arguments win, then the captured bridge pair, then session fields.
    # Resolved IDs never enter arguments/recorder/schema/handlers.
    _explicit_sid: str | None = None
    _explicit_jid: str | None = None
    if isinstance(arguments, dict):
        _raw_explicit_sid = arguments.get("session_id")
        if isinstance(_raw_explicit_sid, str) and _raw_explicit_sid:
            _explicit_sid = _raw_explicit_sid
        _raw_explicit_jid = arguments.get("job_id")
        if isinstance(_raw_explicit_jid, str) and _raw_explicit_jid:
            _explicit_jid = _raw_explicit_jid
    _cap_sid = active_research_session_id if isinstance(active_research_session_id, str) and active_research_session_id else None
    _cap_jid = active_research_job_id if isinstance(active_research_job_id, str) and active_research_job_id else None
    with session._lock:
        _ctx_sid = session.active_research_session_id
        _ctx_jid = session.active_research_job_id
    if not isinstance(_ctx_sid, str) or not _ctx_sid:
        _ctx_sid = None
    if not isinstance(_ctx_jid, str) or not _ctx_jid:
        _ctx_jid = None
    _resolved_sid = _explicit_sid or _cap_sid or _ctx_sid
    _resolved_jid = _explicit_jid or _cap_jid or _ctx_jid
    _sid_from_explicit = _explicit_sid is not None
    # One shared research store rooted at the effective request data root.
    # Never pass the data-root directory to service _repo (it treats a path as a DB file).
    _store = None
    if isinstance(_resolved_sid, str) and _resolved_sid:
        from app.research.repository import ResearchRepository as _RR
        _store = _RR(data_root=_override_context(data_root, as_of).data_root)
    # Attached staged session must exist for every target (fail closed);
    # discovery only skips the stage check, never existence.
    if _store is not None:
        _sid: str = _resolved_sid  # type: ignore[assignment]
        try:
            _found = _store.get_session(_sid)
        except KeyError:
            if not _sid_from_explicit:
                return {"error": f"Unknown research session '{_sid}'", "error_type": "invalid_research_context"}
            _found = None  # type: ignore[assignment]
        else:
            if name not in DISCOVERY_TOOLS:
                _st = stage_for_session(_found, _store.list_jobs(_sid))
                try:
                    check_stage_tool(_st, name)
                except ValueError as exc:
                    return {"error": str(exc)}
    # Gate 1: RESEARCH-only permit filter; unlisted tools are denied.
    if name not in RESEARCH_TOOL_NAMES or not tool_is_permitted(name, LOCAL_CONTEXT):
        _record_security(
            session, name, args_for_hash, "action_blocked", f"tool not permitted: {name}"
        )
        return {"error": f"Tool is not permitted: {name}"}

    # Gate 2: schema validation + 8KB arg-bytes cap.
    invalid = _validate_tool_arguments(name, arguments)
    if invalid is not None:
        return _invalid_args_error(name, invalid)
    if len(json.dumps(arguments)) > LOCAL_CONTEXT.tool_policy.max_arguments_bytes:
        return {
            "error": (
                "Tool arguments exceed the maximum size "
                f"({LOCAL_CONTEXT.tool_policy.max_arguments_bytes} bytes): {name}"
            ),
            "error_type": "invalid_tool_arguments",
        }

    # Gate 8 (reserve): attached staged data dispatches consume one persisted
    # slot via the kernel; bookkeeping (discovery/evidence/reads/finalize)
    # never touches persisted counters and keeps the per-Pi-run budget below.
    # The outer call_tool wrapper returns before this point, so only the
    # inner call consumes.
    _dispatch_consumed = False
    if name in DISPATCH_TOOLS and _store is not None:
        if not isinstance(_resolved_jid, str) or not _resolved_jid:
            return {"error": f"Active research job is required for tool '{name}'", "error_type": "invalid_research_context"}
        try:
            from app.research import service as _svc
            _svc.authorize_and_consume_dispatch(_resolved_sid, _resolved_jid, name, repo=_store)  # type: ignore[arg-type]
        except ValueError as exc:
            _cmsg = str(exc).lower()
            if "budget" in _cmsg or "exhaust" in _cmsg or "quota" in _cmsg:
                return {"error": _BUDGET_EXHAUSTED_RESPONSE, "error_type": "budget_exhausted"}
            return {"error": str(exc)}
        except KeyError as exc:
            return {"error": str(exc), "error_type": "invalid_research_context"}
        _dispatch_consumed = True
    # one budget slot per call before any external work.
    # search_web draws from its dedicated pool, not the generic tool pool.
    # Session lock held only for the reserve; the handler below runs unlocked.
    reserved = True
    if not _dispatch_consumed:
        with session._lock:
            if name != "search_web":
                reserved = session.budget.reserve_tool_call()
            else:
                reserved = session.budget.reserve_search_call()
    if not reserved:
        return {"error": _BUDGET_EXHAUSTED_RESPONSE, "error_type": "budget_exhausted"}

    # Gate 3: intent firewall. No approval callback in this plan, so
    # portfolio-shaped calls are always denied (RESEARCH-only).
    with session._lock:
        intent_allowed, intent_reason = authorize_tool_call(name, arguments, session.run_security)
    if not intent_allowed:
        if TOOL_DOMAINS.get(name) == "portfolio_read":
            _record_security(
                session, name, args_for_hash, "action_blocked", intent_reason
            )
            return {
                "error": "Portfolio access is not authorized for this session",
                "error_type": "authorization_denied",
                "soft": True,
            }
        _record_security(session, name, args_for_hash, "action_blocked", intent_reason)
        return {
            "error": "Tool call exceeds original user intent",
            "error_type": "intent_denied",
            "soft": True,
        }

    # Gate 4: search_web egress; every other tool's private-pattern args check.
    if name == "search_web":
        with session._lock:
            decision = authorize_egress("exa", arguments, session.run_security)
        if not decision.allowed:
            _record_security(
                session, name, args_for_hash, "egress_blocked", decision.reason
            )
            return {
                "error": "Egress blocked: private data must not leave Stockbot",
                "error_type": "egress_denied",
                "soft": True,
            }
    elif name not in _THESIS_LOCAL_TOOLS:
        hit = private_pattern_hit(args_for_hash)
        if hit:
            _record_security(session, name, args_for_hash, "action_blocked", hit)
            return {
                "error": "Tool arguments contain private data that must not be transmitted",
                "error_type": "private_args_denied",
                "soft": True,
            }

    # Gate 6: LOCAL_CONTEXT only, never a broker context, in this plan.
    # Handler + rendering run OUTSIDE the session lock so calls overlap.
    # Gates above stay LOCAL_CONTEXT-based; only the final execute_tool
    # context carries the validated data_root override.
    t0_iso = datetime.now(timezone.utc).isoformat()
    handler_t0 = time.perf_counter()
    result = execute_tool(name, arguments, PI_MODEL, context=_override_context(data_root, as_of))
    handler_ms = (time.perf_counter() - handler_t0) * 1000.0

    # Top-level cache metadata only, for the recorder; protocol IDs and
    # timings stay out of model-visible content.
    raw_cache_hit = result.get("cache_hit") if isinstance(result, dict) else None
    cache_hit = raw_cache_hit if isinstance(raw_cache_hit, bool) else None
    raw_cache_type = result.get("cache_type") if isinstance(result, dict) else None
    cache_type = raw_cache_type if isinstance(raw_cache_type, str) else None

    failed = _is_failed_result(result)
    soft = failed and result.get("soft") is True
    denied = failed and "not permitted" in str(result.get("error", ""))
    status = "completed" if not failed else ("denied" if denied else "failed")
    error_type: str | None = None
    error_message: str | None = None
    if failed:
        raw_error_type = result.get("error_type") or (
            "permission_denied" if denied else "tool_error"
        )
        error_type = raw_error_type if isinstance(raw_error_type, str) else None
        error_message = redact_text(str(result.get("error")))[:2000]
    meta = _tool_result_meta(result)
    if tool_call_id is not None:
        resolved_tc_id = f"{run_id}:tc:{tool_call_id}"
        if recorder is not None:
            recorder.next_tool_seq()  # keep the run's tool-call count truthful
    elif recorder is not None:
        resolved_tc_id = f"{run_id}:tc:{recorder.next_tool_seq()}"
    else:
        resolved_tc_id = f"{run_id}:tc:0"
    if recorder is not None:
        recorder.record_tool_call(
            tool_call_id=resolved_tc_id,
            round=0,
            tool_name=name,
            arguments_json=json.dumps(arguments),
            started_at=t0_iso,
            completed_at=datetime.now(timezone.utc).isoformat(),
            status=status,
            result_row_count=meta.row_count,
            returned_count=meta.returned_count,
            truncated=meta.truncated,
            result_bytes=len(json.dumps(result)),
            result_hash=hashlib.sha256(
                json.dumps(result, sort_keys=True).encode()
            ).hexdigest(),
            source_names=json.dumps(meta.source_names),
            source_freshness=json.dumps(meta.source_freshness),
            as_of=meta.as_of,
            error_type=error_type,
            error_message=error_message,
            protocol_id=protocol_id,
            bridge_queue_ms=bridge_queue_ms,
            handler_ms=handler_ms,
            cache_hit=cache_hit,
            cache_type=cache_type,
        )

    if failed:
        if soft:
            return result
        # Gate 7 (hard failure): deterministic unavailable-data shape, no model call.
        return {
            "error": _unavailable_data_response([(name, result)]),
            "error_type": error_type or "tool_error",
        }

    # Gate 5: ingress scan on the rendered evidence; quarantined or blocked
    # results are withheld from Pi with a fixed placeholder.
    rendered = render_tool_result(
        result, max_bytes=LOCAL_CONTEXT.run_limits.max_tool_result_bytes
    )
    envelope = envelope_for_tool(name, result)
    outcome = prepare_context(envelope, rendered)
    if isinstance(outcome, QuarantinedContext):
        with session._lock:
            session.run_security.quarantined_items += 1
        _record_security(
            session,
            envelope.source,
            rendered,
            "quarantined" if outcome.verdict == "QUARANTINE" else "blocked",
            "; ".join(outcome.reasons) if outcome.reasons else None,
            score=outcome.score,
            verdict=outcome.verdict,
            rule_ids=list(outcome.rule_ids),
        )
        return {
            "error": (
                "Tool result withheld by Stockbot security gateway. "
                "No usable evidence was provided."
            ),
            "error_type": "ingress_blocked",
            "soft": True,
        }

    # Gate 7 (success path): DLP over what Pi receives, then record evidence.
    # Session lock covers guard_response + label/budget mutations only.
    with session._lock:
        final_text = guard_response(outcome.text, session.run_security, run_id)
        if name == "search_web":
            session.run_security.data_labels.add("external")
        if envelope.sensitivity is Sensitivity.PRIVATE:
            session.run_security.data_labels.add("private")
        evidence_allowed = session.budget.add_evidence_tokens(len(final_text) // 4)
    if not evidence_allowed:
        return {"error": _BUDGET_EXHAUSTED_RESPONSE, "error_type": "budget_exhausted"}
    if recorder is not None:
        evidence_id = f"{run_id}:evid:{recorder.next_evidence_seq():04d}"
        recorder.record_evidence(
            evidence_id=evidence_id,
            run_id=run_id,
            tool_call_id=resolved_tc_id,
            round=0,
            tool_name=name,
            rendered_hash=hashlib.sha256(final_text.encode()).hexdigest(),
            rendered_bytes=len(final_text.encode("utf-8")),
            estimated_tokens=len(final_text) // 4,
            source_names=json.dumps(meta.source_names),
            source_freshness=json.dumps(meta.source_freshness),
            as_of=meta.as_of,
            rendered_text=redact_text(final_text),
        )
        recorder.record_security_event(
            source=envelope.source,
            sha256=hashlib.sha256(final_text.encode()).hexdigest(),
            score=None,
            verdict=None,
            rule_ids=[
                envelope.source,
                envelope.sensitivity.value,
                envelope.integrity.value,
            ],
            decision="allowed",
            reason=None,
        )
    safe_meta: dict[str, object] = {
        "row_count": meta.row_count,
        "returned_count": meta.returned_count,
        "truncated": meta.truncated,
        "source": envelope.source,
        "sensitivity": envelope.sensitivity.value,
        "integrity": envelope.integrity.value,
        "status": status,
        "as_of": meta.as_of,
    }
    source_refs = _extract_source_refs(result)
    if source_refs:
        safe_meta["source_refs"] = source_refs
    if name in ("search_tools", "browse_tools") and isinstance(result, dict):
        # Deferred loading: the TS extension activates these schemas additively.
        # Names are already model-visible in content; meta carries them structured.
        raw_matches = result.get("matches")
        if isinstance(raw_matches, list):
            safe_meta["matches"] = [
                m.get("name") for m in raw_matches
                if isinstance(m, dict) and isinstance(m.get("name"), str)
            ]
        else:
            # Pre-discovery search shape: full schemas under "schemas".
            raw_schemas = result.get("schemas")
            if isinstance(raw_schemas, list):
                names: list[str] = []
                for schema in raw_schemas:
                    fn = schema.get("function") if isinstance(schema, dict) else None
                    tool_name = fn.get("name") if isinstance(fn, dict) else None
                    if isinstance(tool_name, str):
                        names.append(tool_name)
                safe_meta["matches"] = names
        raw_tools = result.get("tools")
        if isinstance(raw_tools, list):
            safe_meta["tools"] = [
                m.get("name") for m in raw_tools
                if isinstance(m, dict) and isinstance(m.get("name"), str)
            ]

    return {"content": final_text, "meta": safe_meta}
