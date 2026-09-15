"""Live wave-1 + targeted wave-2 runner: real dispatch/model with persistence.

Wave 1: create session -> SEC scouts (real tools) -> validate dossier ->
freeze E1 -> committee trio -> disagreement -> gate (decide_wave2). When the
committee requests material SEC follow-up within budget, one targeted wave-2
runs: fetch -> freeze E2 -> committee on E2 -> final synthesis over E2 with
disagreement from both waves. Each step persists to ResearchRepository and
appends the journal; validators (budgets/PIT/freeze/dossier) stay
authoritative and are never bypassed.

Resume: ``resume_live`` continues an interrupted session (source/freeze/
one-committee) to a final result without duplicating completed work. Fetch
is skipped when wave evidence exists, freeze is skipped when its id already
resolves, and the committee trio always reruns because analyses are not
persisted. FAILED/COMPLETED/CANCELLED sessions are never silently reopened.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from time import monotonic

from app.research import freeze as _freeze
from app.research import jobs as _jobs
from app.research import session as _session
from app.research.agents import ResearchRequest
from app.research.agents.bearbot import BearAnalysis, run_bearbot
from app.research.agents.bullbot import BullAnalysis, run_bullbot
from app.research.agents.scout import ScoutAssignment, ScoutResult
from app.research.agents.sec_agent import run_sec_assignment
from app.research.agents.source_agent import is_sec_tool
from app.research.agents.stockbot import StockbotAnalysis, run_stockbot
from app.research.director import (
    DirectorBudgets,
    DirectorDeps,
    Wave1Result,
    decide_wave2,
    run_wave1,
    synthesize_wave1,
)
from app.research.dossiers.sec import SECDossier, dossier_to_dict, validate_dossier
from app.research.evals.traces import (
    TraceRecorder,
    create_trace,
    get_trace_events,
    list_traces,
)
from app.research.evidence import (
    Evidence,
    EvidenceLedger,
    EvidenceRejectedError,
    evidence_content_hash,
    evidence_from_dict,
    evidence_to_dict,
    ingest_evidence,
)
from app.research.journal import append_event, hydrate
from app.research.models import (
    Failure,
    FailureCategory,
    Job,
    JobType,
    JournalEvent,
    JSONValue,
    ResearchSession,
    SessionStatus,
    default_policy,
    utcnow,
)
from app.research.repository import ResearchRepository
from app.research.synthesis.committee import CommitteeDisagreement, compute_disagreement

__all__ = ["LiveModelError", "normalize_query", "resume_live", "run_live"]


def normalize_query(query: str) -> str:
    """Canonical query key: lowercase + whitespace-collapse for dedup."""
    return " ".join(query.lower().split())

_KNOWN_AT_KEYS = (
    "known_at", "accepted_at", "acceptanceDatetime", "acceptedDate",
    "published_at", "publishedAt", "published",
    "filingDate", "filedAt", "filed",
    "date", "timestamp",
)


def _known_at_scopes(raw: Mapping[str, object]) -> list[object]:
    """Every known_at candidate in priority order: top, record, meta, source_refs."""
    scopes: list[object] = [raw.get(k) for k in _KNOWN_AT_KEYS]
    record = raw.get("record")
    if isinstance(record, dict):
        scopes.extend(record.get(k) for k in _KNOWN_AT_KEYS)
    meta = raw.get("meta")
    if isinstance(meta, dict):
        scopes.extend(meta.get(k) for k in _KNOWN_AT_KEYS)
        refs = meta.get("source_refs")
        if isinstance(refs, dict):
            scopes.append(refs.get("known_at"))
    return scopes

def _coerce_known_at(value: object) -> datetime | None:
    """One candidate: datetime UTC-converted, ISO string parsed; anything else skipped."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    if isinstance(value, _dt):
        return value.replace(tzinfo=_tz.utc) if value.tzinfo is None else value.astimezone(_tz.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = _dt.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=_tz.utc) if parsed.tzinfo is None else parsed.astimezone(_tz.utc)
    return None


def _extract_known_at(raw: Mapping[str, object]) -> datetime | None:
    """Source-provided timestamp or None; never invented, never as_of."""
    for value in _known_at_scopes(raw):
        parsed = _coerce_known_at(value)
        if parsed is not None:
            return parsed
    return None


_SOURCE_URI_KEYS = (
    "source_uri", "uri", "url", "source_url", "source", "document_url",
    "filing_url", "link", "path",
)
_SOURCE_ID_KEYS = (
    "source_record_id", "record_id", "accession_no", "accession",
    "accessionNumber", "accession_number", "document", "document_id",
    "filing_id", "id",
)


def _json_dumps(value: object, _json: object) -> str:
    dumps = getattr(_json, "dumps", None)
    if not callable(dumps):
        return str(value)
    out: object = dumps(value, sort_keys=True, default=str)
    return out if isinstance(out, str) else str(value)


def _flatten_trace_value(value: object, _rj: object, _json: object) -> str | int | float | bool | None:
    """One complex trace value: redacted JSON, str() fallback on encoder failure."""
    try:
        redact = _rj if callable(_rj) else None
        rendered = _json_dumps(value, _json)
        cleaned: object = redact(rendered) if redact is not None else rendered
        text = cleaned if isinstance(cleaned, str) else rendered
        return text[:2000]
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return str(value)[:2000]


def _is_uri_like(value: str) -> bool:
    text = value.strip()
    return "://" in text or "/" in text or "." in text

def _source_ref_scopes(raw: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Every reference scope in priority order: top, record, meta, source_refs."""
    scopes: list[Mapping[str, object]] = [raw]
    record = raw.get("record")
    if isinstance(record, dict):
        scopes.append(record)
    meta = raw.get("meta")
    if isinstance(meta, dict):
        scopes.append(meta)
        refs = meta.get("source_refs")
        if isinstance(refs, dict):
            scopes.append(refs)
    return scopes


def _scope_uri(scope: Mapping[str, object]) -> str | None:
    """First URI-like string in one scope; None when absent."""
    for key in _SOURCE_URI_KEYS:
        value = scope.get(key)
        if isinstance(value, str) and value.strip() and _is_uri_like(value):
            return value.strip()
    return None


def _scope_ref(scope: Mapping[str, object]) -> str | None:
    """First record id in one scope; None when absent."""
    for key in _SOURCE_ID_KEYS:
        value = scope.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _extract_source_ref(raw: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Real SEC reference from the tool result; (None, None) when absent."""
    uri: str | None = None
    ref: str | None = None
    for scope in _source_ref_scopes(raw):
        if uri is None:
            uri = _scope_uri(scope)
        if ref is None:
            ref = _scope_ref(scope)
        if uri is not None and ref is not None:
            break
    return uri, ref



def _merge_disagreement(
    first: CommitteeDisagreement, second: CommitteeDisagreement,
) -> CommitteeDisagreement:
    """Combine both waves' disagreements; wave-2 freeze owns the merged record."""
    seen: dict[str, ResearchRequest] = {}
    for request in (*first.requested_research, *second.requested_research):
        prior: ResearchRequest | None = seen.get(request.question)
        if prior is None:
            seen[request.question] = request
        else:
            for name in request.requesting_agents:
                if name not in prior.requesting_agents:
                    prior.requesting_agents.append(name)
    return CommitteeDisagreement(
        session_id=second.session_id,
        wave_id=second.wave_id,
        freeze_id=second.freeze_id,
        agreement=list(dict.fromkeys([*first.agreement, *second.agreement]))[:20],
        disagreement=list(dict.fromkeys([*first.disagreement, *second.disagreement]))[:20],
        critical_uncertainties=list(dict.fromkeys(
            [*first.critical_uncertainties, *second.critical_uncertainties]))[:20],
        requested_research=list(seen.values()),
    )

class BudgetLedger:
    """One authoritative run budget: atomic consume, read-only total (None = unlimited)."""
    def __init__(self, limit: int | None) -> None:
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()
    def consume_research_dispatch(self) -> bool:
        """Increment once before a real research dispatch; False only on an explicit int limit."""
        with self._lock:
            if self._limit is not None and self._used >= self._limit:
                return False
            self._used += 1
            return True
    def hydrate(self, used: int) -> None:
        """Seed cumulative total on resume; never rewinds below current."""
        with self._lock:
            if isinstance(used, bool):
                return
            if isinstance(used, int) and used > self._used:
                self._used = used
    @property
    def used(self) -> int:
        with self._lock:
            return self._used


class LiveModelError(RuntimeError):
    """A live Pi model call failed after the failure was persisted.

    Carries the session/stage so the CLI can exit nonzero with the same
    message plus the session id. The job is already FAILED (TIMEOUT),
    the journal holds ``model.failed`` + ``wave.stopped``, and the
    session carries the failure — resume stays safe, never duplicated.
    """

    def __init__(self, session_id: str, stage: str, message: str) -> None:
        super().__init__(message)
        self.session_id: str = session_id
        self.stage: str = stage
        self.message: str = message


class _LiveRun:
    """Shared wave-1/wave-2 machinery for run_live (new) and resume_live (existing).

    Owns the store, in-memory ledger, dossier/job bookkeeping, and every
    stage step. run_live drives it from session creation; resume_live preloads
    it from persisted state and skips already-done fetch/freeze stages.
    """

    def __init__(
        self,
        store: ResearchRepository,
        question: str,
        objective: str,
        as_of: str | None,
        as_of_str: str,
        scoped: list[str],
        dispatch: Callable[[str, dict[str, object]], dict[str, object]],
        model: Callable[[str], str],
        limits: DirectorBudgets,
        wave_id: int = 1,
        actor: str = "run_live",
        provider: str = "fake",
        model_name: str | None = None,
    ) -> None:
        self.store = store
        self.question = question
        self.objective = objective
        self.as_of = as_of
        self.as_of_str = as_of_str
        self.scoped = scoped
        self.dispatch = dispatch
        self.model = model
        self.limits = limits
        self.wave_id = wave_id
        self._actor = actor
        self.ledger: EvidenceLedger = EvidenceLedger()
        self.dossier_ids: list[str] = []
        self.source_jobs: list[str] = []
        self.budget = BudgetLedger(limits.max_tool_calls)
        self.executed_queries: set[str] = set()
        self.t0: float = monotonic()
        self._lock = threading.Lock()
        self.trace: TraceRecorder | None = None
        self.trace_id: str | None = None
        self._scout_deadline: datetime | None = None
        self.provider = provider or "fake"
        _mn = model_name.strip() if isinstance(model_name, str) and model_name.strip() else getattr(model, "__name__", "live") or "live"
        self.model_name = str(_mn)[:120]
    def _emit(self, session_id: str, event_type: str, payload: Mapping[str, object]) -> None:
        with self._lock:
            prior: list[JournalEvent] = self.store.list_events(session_id)
            hydrate(session_id, prior)
            event: JournalEvent = append_event(session_id, event_type, "runner", self._actor, dict(payload))
            self.store.save_event(event)
        self._trace_record(event_type, payload)

    def _save_budget_used(self, session_id: str, *, strict: bool = False) -> None:
        """Persist cumulative research dispatches so resume hydrates the same total."""
        if strict:
            with self._lock:
                used = self.budget.used
                sess = self.store.get_session(session_id)
                budget = dict(sess.budget)
                budget["tool_calls_used"] = used
                self.store.save_session(replace(sess, budget=budget, updated_at=utcnow()))
            return
        try:
            with self._lock:
                used = self.budget.used
                sess = self.store.get_session(session_id)
                budget = dict(sess.budget)
                budget["tool_calls_used"] = used
                self.store.save_session(replace(sess, budget=budget, updated_at=utcnow()))
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
    def _flatten_trace_payload(self, payload: Mapping[str, object] | None) -> dict[str, str | int | float | bool | None]:
        """Redacted flat payload for the trace recorder; complex values JSON-encoded."""
        import json as _json

        from app.redact import redact_json as _rj
        flat: dict[str, str | int | float | bool | None] = {}
        for k, v in (payload or {}).items():
            if v is None or isinstance(v, (str, int, float, bool)):
                flat[k] = _rj(str(v)) if isinstance(v, str) else v
            else:
                flat[k] = _flatten_trace_value(v, _rj, _json)
        return flat


    def _trace_record(self, event_type: str, payload: Mapping[str, object] | None = None, duration_ms: float | None = None) -> None:
        tr = self.trace
        if tr is None:
            return
        try:
            flat = self._flatten_trace_payload(payload)
            with self._lock:
                tr.record(event_type, flat, duration_ms)
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _fail_open_job(self, session_id: str, job_id: str, stage: str, message: str, exc: Exception) -> None:
        """Fail one queued/running job; silent when already closed or missing."""
        with self._lock:
            try:
                open_job = self.store.get_job(job_id)
            except KeyError:
                return
        if open_job is not None and open_job.status in ("queued", "running"):
            with self._lock:
                self.store.save_job(_jobs.fail_job(open_job, FailureCategory.TIMEOUT, message))
            self._emit(session_id, "job.failed", {
                "job_id": job_id, "stage": stage,
                "failure_category": FailureCategory.TIMEOUT.value, "error": str(exc)[:2000],
            })


    def _fail_session(self, session_id: str, message: str) -> None:
        """Stamp the session TIMEOUT failure and move it to FAILED; never reopens terminals."""
        with self._lock:
            sess = self.store.get_session(session_id)
            sess = replace(
                sess,
                failure=Failure(category=FailureCategory.TIMEOUT.value, message=message),
                updated_at=utcnow(),
            )
            if sess.status not in ("failed", "completed", "cancelled"):
                sess = _session.transition_session(sess, SessionStatus.FAILED)
            self.store.save_session(sess)


    def _persist_model_failure(self, session_id: str, job_id: str, stage: str, exc: Exception) -> str:
        """Fail one RUNNING job (TIMEOUT) + journal + session failure; return the message."""
        detail: str = f"{type(exc).__name__}: {exc}"
        message: str = f"{stage}: {detail}"[:2000]
        self._fail_open_job(session_id, job_id, stage, message, exc)
        self._emit(session_id, "model.failed", {
            "stage": stage, "job_id": job_id,
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
        })
        self._fail_session(session_id, message)
        self._emit(session_id, "research.failed", {
            "stage": stage, "failure_category": FailureCategory.TIMEOUT.value,
            "reason": "timeout:model-call",
        })
        self._emit(session_id, "wave.stopped", {"reason": "timeout:model-call", "stage": stage})
        try:
            if self.trace is not None:
                self.trace.finish(message[:2000], "failed")
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        return detail

    def _model_at_stage(self, stage: str, session_id: str, job_id: str) -> Callable[[str], str]:
        """Wrap the live model so a failure persists before it propagates."""
        def _call(prompt: str) -> str:
            from time import perf_counter as _pc

            from app.redact import redact_text as _rt
            _t0 = _pc()
            try:
                out = self.model(prompt)
                _dur = (_pc() - _t0) * 1000.0
                self._trace_record("model.completed", {"provider": self.provider, "model": self.model_name, "stage": stage, "job_id": job_id, "prompt": _rt(prompt)[:2000], "output": _rt(out)[:2000]}, _dur)
                return out
            except Exception as exc:
                _dur2 = (_pc() - _t0) * 1000.0
                self._trace_record("model.failed", {"provider": self.provider, "model": self.model_name, "stage": stage, "job_id": job_id, "error": str(exc)[:2000]}, _dur2)
                detail: str = self._persist_model_failure(session_id, job_id, stage, exc)
                raise LiveModelError(session_id, stage, detail) from exc
        return _call

    def _attach_trace(self, session_id: str) -> None:
        """Create the eval trace and stamp its id on the session budget; raises on failure."""
        import subprocess as _sp
        try:
            _sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=2).strip() or "unknown"
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            _sha = "unknown"
        tr = create_trace(session_id=session_id, wave_id=self.wave_id, provider=self.provider, model=self.model_name, prompt_version="v1", git_sha=_sha)
        self.trace, self.trace_id = tr, tr.trace_id
        try:
            with self._lock:
                cur = self.store.get_session(session_id)
                b = dict(cur.budget)
                b["trace_id"] = tr.trace_id
                self.store.save_session(replace(cur, budget=b, updated_at=utcnow()))
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        self._trace_record("trace.opened", {"trace_id": tr.trace_id, "session_id": session_id})


    def _create_session(self, q: str, aof: str, interrupt_after: str | None = None) -> str:
        _ = aof
        policy: dict[str, JSONValue] = default_policy()
        if interrupt_after is not None:
            policy["interrupt_after"] = interrupt_after
        section: JSONValue | None = policy.get("research")
        if isinstance(section, dict):
            section["max_total_jobs"] = self.limits.max_jobs
            section["max_waves"] = self.limits.max_waves
        eff_as_of: str | None = self.as_of if self.as_of and self.as_of.strip() else None
        sess = _session.create_session(q, self.objective or q, as_of=eff_as_of, policy=policy)
        sess = replace(sess, current_wave=self.wave_id)
        self.store.save_session(sess)
        try:
            self._attach_trace(sess.session_id)
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        self._emit(sess.session_id, "session.created", {"question": q, "wave_id": self.wave_id})
        sess = _session.transition_session(sess, SessionStatus.PLANNING)
        self.store.save_session(sess)
        sess = _session.transition_session(sess, SessionStatus.RESEARCHING)
        self.store.save_session(sess)
        self._open_source_job(sess.session_id, self.wave_id, self.question)
        return sess.session_id

    def _open_source_job(self, session_id: str, wave: int, question: str | None = None) -> str:
        """Create + start one SEC source_agent job for a wave; persist both sides."""
        sess = self.store.get_session(session_id)
        existing: list[Job] = self.store.list_jobs(session_id)
        updated, job = _jobs.create_job(
            sess, existing, job_type=JobType.SOURCE_AGENT, owner="runner",
            wave_id=wave, source_domain="SEC",
        )
        q = question if isinstance(question, str) and question.strip() else self.question
        tickers_json: list[JSONValue] = [t for t in self.scoped]
        job = replace(job, diagnostics={"tickers": tickers_json, "question": q[:500], "as_of": self.as_of_str})
        self.store.save_session(updated)
        self.store.save_job(job)
        self._emit(session_id, "job.created", {"job_id": job.job_id, "job_type": job.job_type})
        self.store.save_job(_jobs.start_job(job))
        self.source_jobs.append(job.job_id)
        return job.job_id

    @staticmethod
    def _match_names(items: object) -> list[dict[str, object]]:
        """Names from one match list: bare strings and {"name"} dicts only."""
        names: list[dict[str, object]] = []
        if not isinstance(items, list):
            return names
        for item in items:
            if isinstance(item, str) and item:
                names.append({"name": item})
            elif isinstance(item, dict):
                cand: object = item.get("name")
                if isinstance(cand, str) and cand:
                    names.append({"name": cand})
        return names


    def _catalog_dispatch(self, name: str, args: dict[str, object]) -> dict[str, object]:
        """Discovery passthrough: timed dispatch, normalized match names for catalog tools."""
        from time import perf_counter as _pc2
        _t1 = _pc2()
        raw: dict[str, object] = self.dispatch(name, args)
        _d1 = (_pc2() - _t1) * 1000.0
        self._trace_record("discovery.completed", {"tool": name, "args": args, "matches": raw.get("matches")}, _d1)
        if name == "search_tools" or name == "browse_tools":
            found: object = raw.get("matches")
            if isinstance(found, list):
                return {"matches": self._match_names(found)}
            meta: object = raw.get("meta")
            inner: object = meta.get("matches") if isinstance(meta, dict) else None
            return {"matches": self._match_names(inner)}
        return raw

    def _query_key(self, args: dict[str, object]) -> str:
        """Normalized search key: tool args query lower + whitespace-collapse."""
        inner_args = args.get("arguments")
        query = inner_args.get("query") if isinstance(inner_args, dict) else args.get("query")
        text = query if isinstance(query, str) else ""
        return normalize_query(text)

    def _duplicate_query(self, args: dict[str, object]) -> bool:
        key = self._query_key(args)
        if not key:
            return False
        if key in self.executed_queries:
            return True
        self.executed_queries.add(key)
        return False


    def _guarded_tool_call(self, sid: str, inner: str, name: str, args: dict[str, object]) -> tuple[dict[str, object], float]:
        """Policy + loop gates, timed dispatch, deadline and error checks; returns (raw, duration_ms).

        Tool volume is unlimited by default; an explicit int BudgetLedger limit
        still rejects via policy_rejection. Exact query repeats soft-skip as
        duplicate_research_action (telemetry, no re-execution).
        """
        if not is_sec_tool(inner):
            self._emit(sid, "policy.denied", {"tool": inner, "reason": "POLICY_DENIED"})
            raise ValueError(f"POLICY_DENIED: non-SEC tool {inner!r}")
        if not self.budget.consume_research_dispatch():
            self._emit(sid, "budget.exhausted", {"tool": inner, "reason": "policy_rejection"})
            raise ValueError("policy_rejection: explicit tool limit reached")
        if inner == "search_sec_filings" and self._duplicate_query(args):
            self._emit(sid, "tool.skipped", {"tool": inner, "reason": "duplicate_research_action"})
            return {"evidence_ids": []}, 0.0
        from time import perf_counter as _pc
        _t0 = _pc()
        try:
            raw: dict[str, object] = self.dispatch(name, args)
        except Exception as exc:
            _dur_e = (_pc() - _t0) * 1000.0
            self._trace_record("tool.failed", {"tool": inner, "args": args, "error": str(exc)[:2000]}, _dur_e)
            raise
        _dur = (_pc() - _t0) * 1000.0
        self._raise_if_past_deadline(sid, inner, args, _dur)
        if "error" in raw:
            self._emit(sid, "tool.failed", {"tool": inner, "error": str(raw.get("error"))[:2000]})
            self._trace_record("tool.failed", {"tool": inner, "args": args, "error": str(raw.get("error"))[:2000]}, _dur)
            raise ValueError(f"TOOL_ERROR: {inner} failed: {raw.get('error')}")
        return raw, _dur


    def _raise_if_past_deadline(self, sid: str, inner: str, args: dict[str, object], _dur: float) -> None:
        """Timeout when the scout deadline passed during dispatch; tolerant of naive datetimes."""
        try:
            _sdl = self._scout_deadline
            if _sdl is not None:
                from datetime import datetime as _dts
                from datetime import timezone as _tzs
                _nows = _dts.now(_tzs.utc)
                _sdlc = _sdl if _sdl.tzinfo is not None else _sdl.replace(tzinfo=_tzs.utc)
                if _nows >= _sdlc:
                    self._emit(sid, "tool.failed", {"tool": inner, "error": "scout deadline exceeded"})
                    self._trace_record("tool.failed", {"tool": inner, "args": args, "error": "scout deadline exceeded"}, _dur)
                    raise TimeoutError("scout deadline exceeded after dispatch")
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _build_evidence_record(self, sid: str, wave: int, q: str, src_job_id: str, inner: str, raw: dict[str, object], eid: str) -> Evidence:
        """One Evidence record from a successful tool result; timestamps never invented."""
        content_obj: object = raw.get("content")
        content: str = content_obj.strip() if isinstance(content_obj, str) and content_obj.strip() else json.dumps(raw, sort_keys=True, default=str)
        if not content:
            content = "{}"
        uri, ref = _extract_source_ref(raw)
        return Evidence(
            evidence_id=eid, session_id=sid, wave_id=wave,
            source_type="sec", source_name=inner, subject=q.strip()[:120] if q.strip() else "sec evidence",
            claim_text=f"{inner} finding for {', '.join(self.scoped) if self.scoped else 'universe'}"[:500], content=content,
            content_hash=evidence_content_hash(content),
            retrieved_at=utcnow(), source_uri=uri, source_record_id=ref,
            published_at=None, known_at=_extract_known_at(raw), effective_at=None,
            job_id=src_job_id or None, agent_id="sec_scout",
            supports=(), contradicts=(), confidence=None, quality=None,
            metadata={"tool": inner, "tickers": ", ".join(self.scoped)},
            superseded_by=None,
        )


    def _ingest_tool_evidence(self, sid: str, inner: str, args: dict[str, object], record: Evidence, eid: str, _dur: float) -> dict[str, object]:
        """Ledger + store persist for one tool record; rejected evidence yields empty ids."""
        try:
            ingest_evidence(self.ledger, record, as_of=self.as_of, on_reject=lambda t, p: self._emit(sid, t, p))
        except EvidenceRejectedError as exc:
            self._trace_record("tool.failed", {"tool": inner, "args": args, "error": f"evidence.rejected:{exc.reason if hasattr(exc, 'reason') else exc}"[:2000]}, _dur)
            return {"evidence_ids": []}
        try:
            with self._lock:
                self.store.save_evidence(evidence_to_dict(record))
        except ValueError:
            pass
        self._emit(sid, "evidence.ingested", {"evidence_id": eid, "tool": inner})
        self._trace_record("tool.completed", {"tool": inner, "args": args, "evidence_id": eid}, _dur)
        known_at = record.known_at
        return {"evidence_ids": [{"evidence_id": eid, "known_at": known_at.isoformat() if known_at else None, "claim_text": record.claim_text[:500], "content_snippet": record.content[:500]}]}
    @staticmethod
    def _finding_text(item: Mapping[str, object]) -> str | None:
        """Stripped finding text or None when blank."""
        txt = item.get("text")
        return txt.strip()[:500] if isinstance(txt, str) and txt.strip() else None


    @staticmethod
    def _finding_ids(item: Mapping[str, object]) -> list[str] | None:
        """String evidence ids or None when absent."""
        ids = item.get("evidence_ids")
        if not isinstance(ids, list) or not ids:
            return None
        eids = [e for e in ids if isinstance(e, str) and e]
        return eids or None


    @staticmethod
    def _valid_finding(item: object) -> object | None:
        """One valid finding dict or None: non-empty text plus a string evidence id."""
        from app.research.agents import GroundedClaim as _GC
        if not isinstance(item, Mapping):
            return None
        text = _LiveRun._finding_text(item)
        eids = _LiveRun._finding_ids(item)
        if text is None or eids is None:
            return None
        return _GC(text=text, evidence_ids=eids)


    @staticmethod
    def _coerce_reused_findings(raw_findings: object) -> list[object]:
        """Valid finding dicts only: non-empty text plus at least one string evidence id."""
        if not isinstance(raw_findings, list):
            return []
        out: list[object] = []
        for item in raw_findings:
            if (found := _LiveRun._valid_finding(item)) is not None:
                out.append(found)
        return out


    @staticmethod
    def _valid_request(item: object) -> ResearchRequest | None:
        """One follow-up request or None; malformed rows never raise."""
        if not isinstance(item, Mapping):
            return None
        try:
            raw_agents: object = item.get("requesting_agents")
            agents: list[str] = [a for a in raw_agents if isinstance(a, str)] if isinstance(raw_agents, list) else []
            question_raw: object = item.get("question", "")
            material_raw: object = item.get("why_material", "")
            domain_raw: object = item.get("requested_source_domain", "SEC")
            gain_raw: object = item.get("expected_gain", "medium")
            return ResearchRequest(question=str(question_raw), why_material=str(material_raw), requested_source_domain=str(domain_raw), expected_gain=str(gain_raw), requesting_agents=agents)
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            return None


    @staticmethod
    def _coerce_reused_requests(req_raw: object) -> list[ResearchRequest]:
        """Follow-up requests with string agents; malformed rows skipped."""
        if not isinstance(req_raw, list):
            return []
        out: list[ResearchRequest] = []
        for item in req_raw:
            req = _LiveRun._valid_request(item)
            if req is not None:
                out.append(req)
        return out


    def _reused_scout_result(self, sid: str, assignment: ScoutAssignment, child: Job, res: Mapping[str, object]) -> ScoutResult:
        """Rebuild a ScoutResult from a completed child job; emits scout.reused."""
        from app.research.agents import GroundedClaim as _GC2
        unk_raw = res.get("unknowns")
        lim_raw = res.get("limitations")
        self._emit(sid, "scout.reused", {"job_id": child.job_id, "assignment_id": assignment.assignment_id})
        self._trace_record("scout.reused", {"job_id": child.job_id, "assignment_id": assignment.assignment_id})
        raw_findings: list[object] = _LiveRun._coerce_reused_findings(res.get("findings"))
        findings: list[_GC2] = [f for f in raw_findings if isinstance(f, _GC2)]
        return ScoutResult(
            assignment_id=assignment.assignment_id, session_id=sid, coverage=str(res.get("coverage", "reused")),
            findings=findings,
            unknowns=[e for e in unk_raw if isinstance(e, str)] if isinstance(unk_raw, list) else [],
            limitations=[e for e in lim_raw if isinstance(e, str)] if isinstance(lim_raw, list) else [],
            follow_up_requests=_LiveRun._coerce_reused_requests(res.get("follow_up_requests")),
        )


    def _find_completed_scout(self, sid: str, src_job_id: str, assignment: ScoutAssignment, existing_jobs: list[Job]) -> ScoutResult | None:
        """Completed child with the same assignment id; None when the scout must run."""
        for child in existing_jobs:
            if child.parent_job_id != src_job_id or child.job_type != JobType.SCOUT.value:
                continue
            res = child.result or {}
            if res.get("assignment_id") == assignment.assignment_id and child.status == "completed" and "findings" in res:
                return self._reused_scout_result(sid, assignment, child, res)
        return None
    def _start_reused_scout(self, sid: str, reusable: Job, assignment_id: str = "") -> Job:
        """Start a queued scout child and journal the retry."""
        if reusable.status == "queued":
            self.store.save_job(_jobs.start_job(reusable))
            self._emit(sid, "job.started", {"job_id": reusable.job_id})
        aid = assignment_id or str((reusable.diagnostics or {}).get("assignment_id"))
        self._emit(sid, "scout.retried", {"job_id": reusable.job_id, "assignment_id": aid})
        self._trace_record("scout.retried", {"job_id": reusable.job_id, "assignment_id": aid})
        return reusable


    def _create_scout_job(self, sid: str, wave: int, src_job_id: str, assignment: ScoutAssignment, existing_jobs: list[Job]) -> Job:
        """Create + start a fresh scout child for one assignment."""
        from datetime import timedelta
        sess_now = self.store.get_session(sid)
        deadline = (utcnow() + timedelta(seconds=assignment.time_budget_s)).isoformat()
        sess_upd, scout_job = _jobs.create_job(sess_now, existing_jobs, job_type=JobType.SCOUT, owner="runner", wave_id=wave, parent_job_id=src_job_id, source_domain="SEC", tool_budget=assignment.max_tool_calls, deadline=deadline)
        tickers_j: list[JSONValue] = [t for t in assignment.tickers]
        scout_job = replace(scout_job, diagnostics={"assignment_id": assignment.assignment_id, "role": assignment.role, "question": assignment.question[:500], "tickers": tickers_j, "as_of": assignment.as_of, "max_tool_calls": assignment.max_tool_calls, "time_budget_s": assignment.time_budget_s, "allowed_domain": assignment.allowed_domain, "session_id": assignment.session_id})
        self.store.save_session(sess_upd)
        self.store.save_job(scout_job)
        self._emit(sid, "job.created", {"job_id": scout_job.job_id, "job_type": scout_job.job_type, "assignment_id": assignment.assignment_id})
        self.store.save_job(_jobs.start_job(scout_job))
        return scout_job


    def _open_or_reuse_scout_job(self, sid: str, wave: int, src_job_id: str, assignment: ScoutAssignment, existing_jobs: list[Job]) -> Job:
        """Reuse a queued/running scout child or create + start a fresh one."""
        reusable = next((c for c in existing_jobs if c.parent_job_id == src_job_id and c.job_type == JobType.SCOUT.value and (c.diagnostics or {}).get("assignment_id") == assignment.assignment_id and c.status in ("queued", "running")), None)
        if reusable is not None:
            return self._start_reused_scout(sid, reusable, assignment.assignment_id)
        return self._create_scout_job(sid, wave, src_job_id, assignment, existing_jobs)


    def _raise_if_deadline_exceeded(self, what: str, name: str = "") -> None:
        """Timeout when the scout deadline already passed; tolerant of naive datetimes."""
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        _dl = self._scout_deadline
        if _dl is None:
            return
        try:
            _dlc = _dl if _dl.tzinfo is not None else _dl.replace(tzinfo=_tz.utc)
            if _dt.now(_tz.utc) >= _dlc:
                raise TimeoutError(f"scout deadline exceeded {what}{name}")
        except TimeoutError:
            raise
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _seed_scout_deadline(self, scout_job: Job, assignment: ScoutAssignment) -> None:
        """Seed the scout deadline from the job, falling back to now + budget."""
        try:
            _dl = scout_job.deadline
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            _dl = None
        if _dl is None:
            from datetime import timedelta as _td
            _dl = utcnow() + _td(seconds=assignment.time_budget_s)
        self._scout_deadline = _dl


    @staticmethod
    def _categorize_scout_error(exc: Exception, origin: str) -> FailureCategory:
        """Category for a scout failure: pre-tagged wins, else keyword ladder."""
        pre_cat = getattr(exc, "_failure_category", None)
        if isinstance(pre_cat, FailureCategory):
            return pre_cat
        low = (type(exc).__name__ + " " + str(exc)).lower()
        if (hit := _LiveRun._ladder_category(low)) is not None:
            return hit
        if origin == "model":
            return FailureCategory.MODEL_ERROR
        return FailureCategory.TOOL_ERROR


    def _cancel_scout_job(self, sid: str, scout_job: Job, exc: Exception, pre_cat: object) -> None:
        """Cancel one scout child on cancellation; tags the exception for the outer handler."""
        try:
            cur = self.store.get_job(scout_job.job_id)
            if cur.status in ("queued", "running"):
                self.store.save_job(_jobs.cancel_job(cur))
            self._emit(sid, "job.cancelled", {"job_id": scout_job.job_id})
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        setattr(exc, "_failure_category", pre_cat if isinstance(pre_cat, FailureCategory) else FailureCategory.TOOL_ERROR)  # noqa: B010 - dynamic boundary, no stubs; getattr keeps checker green
        setattr(exc, "_scout_cancelled", True)  # noqa: B010 - dynamic boundary, no stubs; getattr keeps checker green


    def _fail_scout_job(self, sid: str, scout_job: Job, exc: Exception, cat: FailureCategory, msg: str) -> None:
        """Fail one scout child and journal it; tags the exception category."""
        try:
            cur2 = self.store.get_job(scout_job.job_id)
            if cur2.status in ("queued", "running"):
                self.store.save_job(_jobs.fail_job(cur2, cat, f"scout:{type(exc).__name__}:{msg}"[:2000]))
            self._emit(sid, "job.failed", {"job_id": scout_job.job_id, "failure_category": cat.value})
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        setattr(exc, "_failure_category", cat)  # noqa: B010 - dynamic boundary, no stubs; getattr keeps checker green


    @staticmethod
    def _is_scout_cancelled(exc: Exception) -> bool:
        """True when the scout error signals cancellation."""
        low = (type(exc).__name__ + " " + str(exc)).lower()
        return "cancel" in type(exc).__name__.lower() or "cancelled" in low or "canceled" in low or bool(getattr(exc, "_scout_cancelled", False))


    def _complete_scout_job(self, sid: str, assignment: ScoutAssignment, scout_job: Job, result: ScoutResult) -> None:
        """Persist one scout result onto its job and journal completion."""
        req_out: list[JSONValue] = []
        for req in result.follow_up_requests:
            agents_out: list[JSONValue] = [a for a in req.requesting_agents]
            req_out.append({"question": req.question, "why_material": req.why_material, "requested_source_domain": req.requested_source_domain, "expected_gain": req.expected_gain, "requesting_agents": agents_out})
        findings_out: list[JSONValue] = [{"text": c.text, "evidence_ids": list(c.evidence_ids)} for c in result.findings]
        self.store.save_job(_jobs.complete_job(self.store.get_job(scout_job.job_id), result={"assignment_id": assignment.assignment_id, "findings": findings_out, "coverage": result.coverage, "unknowns": list(result.unknowns), "limitations": list(result.limitations), "follow_up_requests": req_out}))
        self._emit(sid, "job.completed", {"job_id": scout_job.job_id})


    def _scout_model_call(self, sid: str, prefix: str, scout_job: Job, scout_calls: list[int], prompt: str) -> str:
        """One scout model call with pre/post deadline guards; tags model origin on error."""
        _ = sid
        scout_calls[0] += 1
        from time import perf_counter as _pc

        from app.redact import redact_text as _rt
        _t0 = _pc()
        try:
            self._raise_if_deadline_exceeded("before model call")
            out = self.model(prompt)
            self._raise_if_deadline_exceeded("after model call")
            _dur = (_pc() - _t0) * 1000.0
            self._trace_record("model.completed", {"provider": self.provider, "model": self.model_name, "stage": f"{prefix}scout", "job_id": scout_job.job_id, "prompt": _rt(prompt)[:2000], "output": _rt(out)[:2000]}, _dur)
            return out
        except Exception as exc:
            _dur2 = (_pc() - _t0) * 1000.0
            self._trace_record("model.failed", {"provider": self.provider, "model": self.model_name, "stage": f"{prefix}scout", "job_id": scout_job.job_id, "error": str(exc)[:2000]}, _dur2)
            setattr(exc, "_scout_origin", "model")  # noqa: B010 - dynamic boundary, no stubs; getattr keeps checker green
            raise


    def _run_scout_assignment(self, sid: str, assignment: ScoutAssignment, scout_job: Job, dispatch_fn: Callable[[str, dict[str, object]], dict[str, object]], model_fn: Callable[[str], str], journal_fn: Callable[[str, dict[str, object]], None] | None) -> ScoutResult:
        """Run one scout with deadline reset; cancels or fails the child on error."""
        from app.research.agents.scout import run_scout as _run_one
        try:
            try:
                return _run_one(assignment, dispatch=dispatch_fn, model=model_fn, journal=journal_fn)
            finally:
                self._scout_deadline = None
        except Exception as exc:
            origin = getattr(exc, "_scout_origin", "")
            pre_cat = getattr(exc, "_failure_category", None)
            msg = str(exc)[:2000] or type(exc).__name__
            if self._is_scout_cancelled(exc):
                self._cancel_scout_job(sid, scout_job, exc, pre_cat)
                raise
            cat = self._categorize_scout_error(exc, origin)
            self._fail_scout_job(sid, scout_job, exc, cat, msg)
            raise


    @staticmethod
    def _ladder_category(low: str) -> FailureCategory | None:
        """Shared keyword ladder: timeout, policy, and distinct loop arms; None falls through."""
        if "timeout" in low or "expired" in low or "timed_out" in low:
            return FailureCategory.TIMEOUT
        if "research_loop_detected" in low or "research_loop" in low:
            return FailureCategory.RESEARCH_LOOP_DETECTED
        if "duplicate_research_action" in low:
            return FailureCategory.DUPLICATE_RESEARCH_ACTION
        if "policy_rejection" in low:
            return FailureCategory.POLICY_REJECTION
        if "policy" in low or "denied" in low:
            return FailureCategory.POLICY_DENIED
        return None


    @staticmethod
    def _categorize_fetch_error(exc: Exception) -> FailureCategory:
        """Category for a fetch failure: pre-tagged wins, else keyword ladder."""
        cat_raw = getattr(exc, "_failure_category", None)
        if isinstance(cat_raw, FailureCategory):
            return cat_raw
        low_all = (type(exc).__name__ + " " + str(exc)).lower()
        return _LiveRun._ladder_category(low_all) or FailureCategory.TOOL_ERROR


    def _fail_source_job(self, sid: str, src_job_id: str, cat: FailureCategory, message: str, exc: Exception, cancelled: bool) -> None:
        """Cancel or fail the source job on fetch error; silent when already closed."""
        try:
            src_job = self.store.get_job(src_job_id)
            if src_job.status in ("queued", "running"):
                if cancelled:
                    self.store.save_job(_jobs.cancel_job(src_job))
                    self._emit(sid, "job.cancelled", {"job_id": src_job_id})
                else:
                    self.store.save_job(_jobs.fail_job(src_job, cat, message))
                    self._emit(sid, "job.failed", {"job_id": src_job_id, "stage": "source-scout", "failure_category": cat.value, "error": str(exc)[:2000]})
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _fail_fetch_session(self, sid: str, cat: FailureCategory, message: str) -> None:
        """Stamp the fetch failure on the session and move it to FAILED."""
        try:
            sess_fail = self.store.get_session(sid)
            sess_fail = replace(sess_fail, failure=Failure(category=cat.value, message=message), updated_at=utcnow())
            if sess_fail.status not in ("failed", "completed", "cancelled"):
                sess_fail = _session.transition_session(sess_fail, SessionStatus.FAILED)
            self.store.save_session(sess_fail)
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _raise_fetch_failed(self, sid: str, src_job_id: str, exc: Exception) -> None:
        """Persist a fetch failure on every surface and raise LiveModelError."""
        cat = self._categorize_fetch_error(exc)
        cancelled = bool(getattr(exc, "_scout_cancelled", False))
        detail = f"{type(exc).__name__}: {exc}"[:2000]
        message = f"source-scout: {detail}"[:2000]
        self._fail_source_job(sid, src_job_id, cat, message, exc, cancelled)
        self._emit(sid, "model.failed", {"stage": "source-scout", "job_id": src_job_id, "error_type": type(exc).__name__, "error": str(exc)[:2000]})
        self._fail_fetch_session(sid, cat, message)
        self._emit(sid, "research.failed", {"stage": "source-scout", "failure_category": cat.value, "reason": "timeout:model-call" if cat == FailureCategory.TIMEOUT else f"scout:{cat.value}"})
        self._emit(sid, "wave.stopped", {"reason": "timeout:model-call" if cat == FailureCategory.TIMEOUT else "failed:source-scout", "stage": "source-scout"})
        self._save_budget_used(sid)
        try:
            if self.trace is not None:
                self.trace.finish(message[:2000], "failed")
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        raise LiveModelError(sid, "source-scout", detail) from exc


    def _persist_fetch_success(self, sid: str, src_job_id: str, dossier_obj: object) -> list[str]:
        """Validate + persist the dossier, close the source job, return evidence ids."""
        ids: list[str] = []
        did: str = ""
        if isinstance(dossier_obj, SECDossier):
            ids = list(dossier_obj.supporting_evidence_ids)
            did = dossier_obj.dossier_id
            validate_dossier(dossier_obj, self.ledger.ids())
            try:
                self.store.save_dossier(dossier_to_dict(dossier_obj))
            except ValueError:
                pass
        else:
            raise ValueError(f"runner: unexpected dossier type {type(dossier_obj).__name__} (SECDossier required)")  # noqa: TRY004 - public error contract pins ValueError, tests are oracle
        sess = self.store.get_session(sid)
        sess = replace(
            sess,
            evidence_ids=list(dict.fromkeys([*sess.evidence_ids, *ids])),
            dossier_ids=list(dict.fromkeys([*sess.dossier_ids, did])),
            updated_at=utcnow(),
        )
        self.store.save_session(sess)
        self._emit(sid, "dossier.created", {"dossier_id": did, "evidence_ids": ids})
        if src_job_id:
            job = self.store.get_job(src_job_id)
            self.store.save_job(_jobs.complete_job(job, result={"dossier_id": did, "evidence_ids": ids}))
            self._emit(sid, "job.completed", {"job_id": src_job_id})
        self.dossier_ids.append(did)
        self._save_budget_used(sid)
        return ids


    def _fetch_wave(self, sid: str, wave: int, q: str, src_job_id: str, prefix: str) -> list[str]:
        existing_wave = sum(1 for e in self.ledger.list_session(sid) if e.wave_id == wave)
        counter: list[int] = [existing_wave]


        def _live_dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
            if name == "call_tool":
                inner_obj: object = args.get("name")
                inner: str = inner_obj if isinstance(inner_obj, str) and inner_obj else "unknown_tool"
                raw, _dur = self._guarded_tool_call(sid, inner, name, args)
                counter[0] += 1
                eid: str = f"{sid}:{wave}:sec:{counter[0]}"
                record = self._build_evidence_record(sid, wave, q, src_job_id, inner, raw, eid)
                return self._ingest_tool_evidence(sid, inner, args, record, eid, _dur)
            return self._catalog_dispatch(name, args)
        scout_calls: list[int] = [0]
        def _scout_journal(event_type: str, payload: dict[str, object]) -> None:
            self._emit(sid, event_type, payload)
        def _scout_model(prompt: str) -> str:
            scout_calls[0] += 1
            stage: str = f"{prefix}scout-{scout_calls[0]}"
            return self._model_at_stage(stage, sid, src_job_id)(prompt)


        def _spawn_scout(assignment: ScoutAssignment) -> ScoutResult:
            existing_jobs: list[Job] = self.store.list_jobs(sid)
            reused = self._find_completed_scout(sid, src_job_id, assignment, existing_jobs)
            if reused is not None:
                return reused
            scout_job = self._open_or_reuse_scout_job(sid, wave, src_job_id, assignment, existing_jobs)
            self._seed_scout_deadline(scout_job, assignment)
            def _tagged_dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
                try:
                    self._raise_if_deadline_exceeded("before dispatch ", name)
                    return _live_dispatch(name, args)
                except Exception as exc:
                    setattr(exc, "_scout_origin", "tool")  # noqa: B010 - dynamic boundary, no stubs; getattr keeps checker green
                    raise
            def _tagged_model(prompt: str) -> str:
                return self._scout_model_call(sid, prefix, scout_job, scout_calls, prompt)
            result = self._run_scout_assignment(sid, assignment, scout_job, _tagged_dispatch, _tagged_model, _scout_journal)
            self._complete_scout_job(sid, assignment, scout_job, result)
            return result
        try:
            dossier_obj: object = run_sec_assignment(
                q, session_id=sid, wave_id=wave, as_of=self.as_of_str or "unbounded",
                tickers=self.scoped, dispatch=_live_dispatch, model=_scout_model, journal=_scout_journal, spawn=_spawn_scout,
            )
        except Exception as exc:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            self._raise_fetch_failed(sid, src_job_id, exc)
            raise AssertionError("unreachable: _raise_fetch_failed always raises")
        return self._persist_fetch_success(sid, src_job_id, dossier_obj)

    def _fetch(self, session_id: str) -> list[str]:
        src: str = self.source_jobs[0] if self.source_jobs else ""
        return self._fetch_wave(session_id, self.wave_id, self.question, src, "")

    def _freeze_wave(self, session_id: str, wave: int) -> str:
        sess = self.store.get_session(session_id)
        if sess.status in (SessionStatus.RESEARCHING.value, SessionStatus.TARGETED_RESEARCH.value):
            sess = _session.transition_session(sess, SessionStatus.FREEZING)
            self.store.save_session(sess)
        recs: list[Evidence] = [e for e in self.ledger.list_session(session_id) if e.wave_id <= wave]
        fid: str = f"{session_id}:{wave}:freeze"
        frozen = _freeze.create_freeze(
            freeze_id=fid, session_id=session_id, wave_id=wave, records=recs, as_of=self.as_of,
        )
        try:
            self.store.save_freeze(_freeze.freeze_to_dict(frozen))
        except ValueError:
            pass
        cur = self.store.get_session(session_id)
        if fid not in cur.freeze_ids:
            cur = replace(cur, freeze_ids=[*cur.freeze_ids, fid], updated_at=utcnow())
            self.store.save_session(cur)
        self._emit(session_id, "freeze.created", {"freeze_id": fid, "evidence_ids": list(frozen.evidence_ids)})
        return fid

    def _freeze_evidence_text(self, session_id: str, ev_ids: Sequence[str]) -> str:
        """One shared serialization of the exact freeze records for all committee members."""
        by_id = {e.evidence_id: e for e in self.ledger.list_session(session_id)}
        lines: list[str] = []
        for eid in ev_ids:
            rec = by_id.get(eid)
            if rec is None:
                continue
            known = rec.known_at.isoformat() if rec.known_at else "unknown"
            content = rec.content if len(rec.content) <= 1000 else rec.content[:1000] + " […truncated]"
            lines.append(f"[{rec.evidence_id}] {rec.subject} | {known} | {rec.source_name} {rec.source_uri or ''}".rstrip())
            lines.append(f"claim: {rec.claim_text}")
            lines.append(f"content: {content}")
        return "\n".join(lines)

    def _create_freeze(self, session_id: str) -> str:
        return self._freeze_wave(session_id, self.wave_id)

    @staticmethod
    def _categorize_committee_error(exc: Exception) -> FailureCategory:
        """Category for a committee failure: pre-tagged wins, else keyword ladder."""
        pre = getattr(exc, "_failure_category", None)
        if isinstance(pre, FailureCategory):
            return pre
        msg = str(exc)[:1500] or type(exc).__name__
        low = (type(exc).__name__ + " " + msg).lower()
        if (hit := _LiveRun._ladder_category(low)) is not None and hit != FailureCategory.TIMEOUT:
            return hit
        if "model_output" in low or "unknown evidence" in low or "uncited" in low:
            return FailureCategory.MODEL_OUTPUT_FAILURE
        if "tool_error" in low or "tool failed" in low:
            return FailureCategory.TOOL_ERROR
        return FailureCategory.TIMEOUT


    def _fail_committee_jobs(self, session_id: str, ran: list[str], exc: Exception, cat: FailureCategory, msg: str) -> None:
        """Fail every still-open committee job, then journal each one."""
        for jid in ran:
            with self._lock:
                leftover: Job = self.store.get_job(jid)
                if leftover.status in ("queued", "running"):
                    self.store.save_job(_jobs.fail_job(
                        leftover, cat, f"committee:{type(exc).__name__}:{msg}"[:2000]))
            self._emit(session_id, "job.failed", {"job_id": jid, "failure_category": cat.value})


    def _open_committee_jobs(self, session_id: str, wave: int, pending: list[Job], progressed: ResearchSession, ran: list[str]) -> ResearchSession:
        """Create + start the stock/bull/bear trio; returns the progressed session."""
        prog: ResearchSession = progressed
        for kind in (JobType.STOCKBOT, JobType.BULLBOT, JobType.BEARBOT):
            updated: tuple[ResearchSession, Job] = _jobs.create_job(
                prog, pending, job_type=kind, owner="runner", wave_id=wave,
            )
            prog, job = updated
            self.store.save_session(prog)
            self.store.save_job(job)
            self._emit(session_id, "job.created", {"job_id": job.job_id, "job_type": job.job_type})
            started = _jobs.start_job(job)
            self.store.save_job(started)
            pending.append(started)
            ran.append(started.job_id)
        return prog

    def _run_member_stock(self, session_id: str, run_fn: Callable[[], StockbotAnalysis], job_id: str, fid: str) -> StockbotAnalysis:
        """Run the stockbot member and mark its job complete; exceptions propagate."""
        analysis = run_fn()
        with self._lock:
            self.store.save_job(_jobs.complete_job(self.store.get_job(job_id), result={"freeze_id": fid}))
        return analysis

    def _run_member_bull(self, session_id: str, run_fn: Callable[[], BullAnalysis], job_id: str, fid: str) -> BullAnalysis:
        """Run the bullbot member and mark its job complete; exceptions propagate."""
        analysis = run_fn()
        with self._lock:
            self.store.save_job(_jobs.complete_job(self.store.get_job(job_id), result={"freeze_id": fid}))
        return analysis

    def _run_member_bear(self, session_id: str, run_fn: Callable[[], BearAnalysis], job_id: str, fid: str) -> BearAnalysis:
        """Run the bearbot member and mark its job complete; exceptions propagate."""
        analysis = run_fn()
        with self._lock:
            self.store.save_job(_jobs.complete_job(self.store.get_job(job_id), result={"freeze_id": fid}))
        return analysis


    def _run_trio(self, stock_fn: Callable[[], StockbotAnalysis], bull_fn: Callable[[], BullAnalysis], bear_fn: Callable[[], BearAnalysis]) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        """Run the three member closures in parallel; first exception wins."""
        with ThreadPoolExecutor(max_workers=3) as _pool:
            stock_f = _pool.submit(stock_fn)
            bull_f = _pool.submit(bull_fn)
            bear_f = _pool.submit(bear_fn)
            return stock_f.result(), bull_f.result(), bear_f.result()


    def _committee_wave(self, session_id: str, wave: int, prefix: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        sess = self.store.get_session(session_id)
        if sess.status == SessionStatus.FREEZING.value:
            sess = _session.transition_session(sess, SessionStatus.ANALYZING)
            self.store.save_session(sess)
        fid: str = f"{session_id}:{wave}:freeze"
        frozen_ids: object = self.store.get_freeze(fid).get("evidence_ids")
        ev_ids: list[str] = [e for e in frozen_ids if isinstance(e, str)] if isinstance(frozen_ids, list) else []
        pending: list[Job] = list(self.store.list_jobs(session_id))
        ran: list[str] = []
        self._open_committee_jobs(session_id, wave, pending, self.store.get_session(session_id), ran)
        shared_text = self._freeze_evidence_text(session_id, ev_ids)
        def _run_stock() -> StockbotAnalysis:
            return self._run_member_stock(session_id, lambda: run_stockbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-stockbot", session_id, ran[0]),
                evidence_text=shared_text,
            ), ran[0], fid)
        def _run_bull() -> BullAnalysis:
            return self._run_member_bull(session_id, lambda: run_bullbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-bullbot", session_id, ran[1]),
                evidence_text=shared_text,
            ), ran[1], fid)
        def _run_bear() -> BearAnalysis:
            return self._run_member_bear(session_id, lambda: run_bearbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-bearbot", session_id, ran[2]),
                evidence_text=shared_text,
            ), ran[2], fid)
        try:
            stock, bull, bear = self._run_trio(_run_stock, _run_bull, _run_bear)
        except Exception as exc:
            cat = self._categorize_committee_error(exc)
            msg = str(exc)[:1500] or type(exc).__name__
            self._fail_committee_jobs(session_id, ran, exc, cat, msg)
            raise
        self._emit(session_id, "committee.completed", {"freeze_id": fid, "evidence_ids": ev_ids})
        entry_jobs: list[JSONValue] = list(ran)
        entry: dict[str, JSONValue] = {"freeze_id": fid, "wave_id": wave, "jobs": entry_jobs}
        latest = self.store.get_session(session_id)
        latest = replace(latest, committee_runs=[*latest.committee_runs, entry], updated_at=utcnow())
        self.store.save_session(latest)
        return (stock, bull, bear)

    def _run_committee(self, session_id: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        return self._committee_wave(session_id, self.wave_id, "")

    def _record_stop(self, session_id: str, reason: str) -> None:
        self._emit(session_id, "wave.stopped", {"reason": reason})

    def _store_final(self, session_id: str, freeze_id: str, answer: str, claims: Sequence[object]) -> None:
        """Persist the synthesis answer, then close the session as completed."""
        from app.research.agents import GroundedClaim as _GC
        claims_json: list[JSONValue] = []
        for claim in claims:
            if isinstance(claim, _GC):
                claims_json.append({"text": claim.text, "evidence_ids": list(claim.evidence_ids)})
        final: dict[str, JSONValue] = {"answer": answer, "freeze_id": freeze_id, "claims": claims_json}
        cur = self.store.get_session(session_id)
        cur = replace(cur, final_result=final, updated_at=utcnow())
        self.store.save_session(cur)
        if cur.status == SessionStatus.SYNTHESIZING.value:
            cur = _session.transition_session(cur, SessionStatus.COMPLETED)
            self.store.save_session(cur)

    def _store_empty_terminal(self, session_id: str) -> None:
        """Persist completed no-evidence result (limitations answer, empty claims)."""
        try:
            empty: dict[str, JSONValue] = {
                "answer": ("No PIT-eligible SEC evidence was found; the question cannot be answered "
                           "from SEC filings within the session scope. Limitations: SEC-only, "
                           "as_of-filtered corpus."),
                "freeze_id": "",
                "claims": [],
            }
            cur = self.store.get_session(session_id)
            cur = replace(cur, final_result=empty, updated_at=utcnow())
            if cur.status not in ("failed", "completed", "cancelled"):
                cur = _session.transition_session(cur, SessionStatus.COMPLETED)
            self.store.save_session(cur)
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass
        try:
            if self.trace is not None:
                self.trace.finish("complete:empty-with-limitations", "completed")
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass

    def _run_wave2(self, wave1: Wave1Result, targeted: str) -> dict[str, object] | None:
        """One targeted SEC wave: fetch next-gen freeze -> committee; None when empty."""
        sid: str = wave1.session_id
        d1: CommitteeDisagreement | None = wave1.disagreement
        if d1 is None:
            return None
        cur = self.store.get_session(sid)
        nxt = max(cur.current_wave, len(cur.freeze_ids), wave1.wave_id) + 1
        sess = self.store.get_session(sid)
        if sess.status == SessionStatus.ANALYZING.value:
            sess = _session.transition_session(sess, SessionStatus.TARGETED_RESEARCH)
            self.store.save_session(sess)
        sess = replace(self.store.get_session(sid), current_wave=nxt, updated_at=utcnow())
        self.store.save_session(sess)
        self._emit(sid, "wave.started", {"wave_id": nxt, "targeted_question": targeted})
        src2: str = self._open_source_job(sid, nxt, targeted or self.question)
        e2: list[str] = self._fetch_wave(sid, nxt, targeted or self.question, src2, "w2-")
        if not e2:
            self._emit(sid, "wave.stopped", {"reason": "complete:empty-with-limitations"})
            return None
        fid2: str = self._freeze_wave(sid, nxt)
        s2, b2, r2 = self._committee_wave(sid, nxt, "w2-")
        d2: CommitteeDisagreement = compute_disagreement(s2, b2, r2)
        merged: CommitteeDisagreement = _merge_disagreement(d1, d2)
        w2result = Wave1Result(
            session_id=sid, wave_id=nxt, freeze_id=fid2, evidence_ids=e2,
            stock=s2, bull=b2, bear=r2, disagreement=merged,
        )
        return {
            "freeze_id": fid2, "evidence_ids": e2,
            "dossier_id": self.dossier_ids[-1] if self.dossier_ids else "",
            "stock": s2, "bull": b2, "bear": r2,
            "disagreement": merged, "result": w2result, "targeted": targeted,
        }

    def _deps(self, interrupt_after: str | None = None) -> DirectorDeps:
        return DirectorDeps(
            create_session=lambda q, aof: self._create_session(q, aof, interrupt_after),
            fetch_wave_evidence=self._fetch,
            create_freeze=self._create_freeze,
            run_committee=self._run_committee,
            record_stop=self._record_stop,
        )

    def _advance_to_synthesizing(self, session_id: str) -> None:
        """Move an ANALYZING session to SYNTHESIZING; other states pass through."""
        sess = self.store.get_session(session_id)
        if sess.status == SessionStatus.ANALYZING.value:
            sess = _session.transition_session(sess, SessionStatus.SYNTHESIZING)
            self.store.save_session(sess)


    def _close_trace(self, conclusion: object, fallback: str) -> None:
        """Finish the eval trace with the synthesis answer; never raises."""
        try:
            if self.trace is not None:
                text = conclusion if isinstance(conclusion, str) and conclusion else fallback
                self.trace.finish(text[:2000], "completed")
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
            pass


    def _complete_wave1_tail(self, result: Wave1Result, did_out: str, gate: str) -> dict[str, object]:
        """Synthesis tail for the wave-1-only close: persist, stop, trace."""
        self._advance_to_synthesizing(result.session_id)
        synth = synthesize_wave1(self.question, self.as_of_str or "unbounded", result)
        if synth is not None:
            self._store_final(result.session_id, synth.freeze_id, synth.answer, synth.claims)
        self._emit(result.session_id, "wave.stopped", {"reason": "complete:wave1"})
        self._close_trace(synth.answer if synth is not None else None, "complete:wave1")
        return {
            "session_id": result.session_id, "wave_id": result.wave_id,
            "freeze_id": result.freeze_id, "evidence_ids": list(result.evidence_ids),
            "dossier_id": did_out, "stock": result.stock, "bull": result.bull,
            "bear": result.bear, "disagreement": result.disagreement,
            "stop_reason": "complete:wave1", "wave2_decision": gate,
        }


    def _complete_wave2_tail(self, result: Wave1Result, did_out: str, gate: str, w2: dict[str, object]) -> dict[str, object]:
        """Synthesis tail for the wave-2 close: persist over E2, stop, trace."""
        w2result_obj: object = w2["result"]
        assert isinstance(w2result_obj, Wave1Result)
        synth2 = synthesize_wave1(self.question, self.as_of_str or "unbounded", w2result_obj)
        self._advance_to_synthesizing(result.session_id)
        if synth2 is not None:
            self._store_final(result.session_id, synth2.freeze_id, synth2.answer, synth2.claims)
        self._emit(result.session_id, "wave.stopped", {"reason": "complete:wave2"})
        self._close_trace(synth2.answer if synth2 is not None else None, "complete:wave2")
        return {
            "session_id": result.session_id, "wave_id": result.wave_id,
            "freeze_id": result.freeze_id, "evidence_ids": list(result.evidence_ids),
            "dossier_id": did_out, "stock": result.stock, "bull": result.bull,
            "bear": result.bear, "disagreement": result.disagreement,
            "stop_reason": "complete:wave2", "wave2_decision": gate,
            "wave2_targeted": w2["targeted"],
            "wave2_freeze_id": w2["freeze_id"], "wave2_evidence_ids": w2["evidence_ids"],
            "wave2_dossier_id": w2["dossier_id"],
            "wave2_stock": w2["stock"], "wave2_bull": w2["bull"], "wave2_bear": w2["bear"],
            "wave2_disagreement": w2["disagreement"],
        }


    def _finish_completed(self, result: Wave1Result, did_out: str) -> dict[str, object]:
        """Shared wave-2 gate + synthesis tail for run and resume (complete paths)."""
        deps = self._deps()
        decision = decide_wave2(
            result, deps=deps, budgets=self.limits,
            waves_used=1,
            jobs_used=len(self.store.list_jobs(result.session_id)),
            tool_calls_used=self.budget.used,
            elapsed_s=monotonic() - self.t0,
        )
        gate: str = f"{decision.stop_reason}:{decision.reason_detail}"
        w2: dict[str, object] | None = None
        if decision.authorized:
            w2 = self._run_wave2(result, decision.targeted_question)
        if w2 is None:
            return self._complete_wave1_tail(result, did_out, gate)
        return self._complete_wave2_tail(result, did_out, gate, w2)


def _empty_terminal_result(session_id: str, wave_id: int, eids: list[str], dossier_id: str, reason: str) -> dict[str, object]:
    """Terminal no-evidence result payload shared by run paths."""
    return {
        "session_id": session_id, "wave_id": wave_id, "freeze_id": "",
        "evidence_ids": eids, "dossier_id": dossier_id,
        "stock": None, "bull": None, "bear": None, "disagreement": None,
        "stop_reason": reason,
    }


def _run_one_committee(run: _LiveRun, store: ResearchRepository, question: str, as_of_str: str, wave_id: int) -> dict[str, object]:
    """Interrupted single-member path: fetch, freeze, stock-only analysis, stop."""
    sid = run._create_session(question, as_of_str, "one-committee")
    eids = run._fetch(sid)
    if not eids:
        run._emit(sid, "wave.stopped", {"reason": "complete:empty-with-limitations"})
        run._store_empty_terminal(sid)
        return _empty_terminal_result(sid, wave_id, eids, run.dossier_ids[0] if run.dossier_ids else "", "complete:empty-with-limitations")
    fid = run._create_freeze(sid)
    sess = store.get_session(sid)
    if sess.status == SessionStatus.FREEZING.value:
        sess = _session.transition_session(sess, SessionStatus.ANALYZING)
        store.save_session(sess)
    existing_jobs = store.list_jobs(sid)
    sess, one = _jobs.create_job(
        sess, existing_jobs, job_type=JobType.STOCKBOT, owner="runner", wave_id=wave_id,
    )
    store.save_session(sess)
    store.save_job(one)
    run._emit(sid, "job.created", {"job_id": one.job_id, "job_type": one.job_type})
    store.save_job(_jobs.start_job(one))
    stock = run_stockbot(
        question, session_id=sid, wave_id=wave_id, freeze_id=fid,
        evidence_ids=eids, as_of=as_of_str or "unbounded",
        model=run._model_at_stage("committee-stockbot", sid, one.job_id),
        evidence_text=run._freeze_evidence_text(sid, eids),
    )
    store.save_job(_jobs.complete_job(store.get_job(one.job_id), result={"freeze_id": fid}))
    one_jobs: list[JSONValue] = [one.job_id]
    one_entry: dict[str, JSONValue] = {"freeze_id": fid, "wave_id": wave_id, "jobs": one_jobs}
    cur = store.get_session(sid)
    cur = replace(cur, committee_runs=[*cur.committee_runs, one_entry], updated_at=utcnow())
    store.save_session(cur)
    run._emit(sid, "committee.completed", {"freeze_id": fid, "ran": ["stockbot"]})
    run._emit(sid, "wave.stopped", {"reason": "interrupted:one-committee"})
    return {
        "session_id": sid, "wave_id": wave_id, "freeze_id": fid,
        "evidence_ids": eids, "dossier_id": run.dossier_ids[0] if run.dossier_ids else "",
        "stock": stock, "bull": None, "bear": None, "disagreement": None,
        "stop_reason": "interrupted:one-committee",
    }


def _close_wave1_result(run: _LiveRun, result: Wave1Result, did_out: str, interrupt_after: str | None) -> dict[str, object]:
    """Close a director wave-1: completed tail, interrupt payload, or empty terminal."""
    if (
        result.stock is not None
        and result.bull is not None
        and result.bear is not None
        and result.disagreement is not None
    ):
        return run._finish_completed(result, did_out)
    if interrupt_after in ("source", "freeze"):
        return {
            "session_id": result.session_id, "wave_id": result.wave_id,
            "freeze_id": result.freeze_id, "evidence_ids": list(result.evidence_ids),
            "dossier_id": did_out, "stock": None, "bull": None, "bear": None,
            "disagreement": None, "stop_reason": f"interrupted:{interrupt_after}",
        }
    run._store_empty_terminal(result.session_id)
    return _empty_terminal_result(result.session_id, result.wave_id, list(result.evidence_ids), did_out, "complete:empty-with-limitations")


def run_live(
    question: str,
    objective: str,
    as_of: str | None,
    tickers: Sequence[str],
    dispatch: Callable[[str, dict[str, object]], dict[str, object]],
    model: Callable[[str], str],
    repo: ResearchRepository | None = None,
    budgets: DirectorBudgets | None = None,
    *,
    interrupt_after: str | None = None,
    wave_id: int = 1,
    provider: str = "fake",
    model_name: str | None = None,
) -> dict[str, object]:
    """Run one live wave-1 and persist every step; resume never duplicates."""
    if interrupt_after not in (None, "source", "freeze", "one-committee"):
        raise ValueError(f"runner: 'interrupt_after' must be source/freeze/one-committee, got {interrupt_after!r}")
    store: ResearchRepository = repo if repo is not None else ResearchRepository()
    limits: DirectorBudgets = budgets if budgets is not None else DirectorBudgets()
    as_of_str: str = as_of.strip() if isinstance(as_of, str) and as_of.strip() else ""
    scoped: list[str] = list(tickers)
    run = _LiveRun(store, question, objective, as_of, as_of_str, scoped, dispatch, model, limits, wave_id, provider=provider, model_name=model_name)
    if interrupt_after == "one-committee":
        return _run_one_committee(run, store, question, as_of_str, wave_id)
    deps = run._deps(interrupt_after)
    result = run_wave1(
        question, as_of_str or "unbounded", deps=deps,
        tickers=scoped, wave_id=wave_id, interrupt_after=interrupt_after,
    )
    return _close_wave1_result(run, result, run.dossier_ids[0] if run.dossier_ids else "", interrupt_after)



def _job_diagnostics_of(job: Job) -> dict[str, JSONValue]:
    return dict(job.diagnostics)


def _job_tickers(job: Job) -> list[str]:
    """Tickers from one job's diagnostics; empty when absent or malformed."""
    diag = _job_diagnostics_of(job)
    raw_tick: object = diag.get("tickers")
    if isinstance(raw_tick, list) and raw_tick:
        tickers = [t.strip() for t in raw_tick if isinstance(t, str) and t.strip()]
        if tickers:
            return tickers
    return []


def _scoped_from_jobs(store: ResearchRepository, session_id: str, wave: int) -> list[str]:
    """Tickers from the wave's source-job diagnostics; empty when absent."""
    for job in store.list_jobs(session_id):
        if job.job_type != JobType.SOURCE_AGENT.value or job.wave_id != wave:
            continue
        if found := _job_tickers(job):
            return found
    return []


def _scoped_from_evidence(store: ResearchRepository, session_id: str) -> list[str]:
    """Tickers from persisted evidence metadata; empty when absent."""
    for rec_dict in store.list_evidence(session_id):
        meta = rec_dict.get("metadata")
        if isinstance(meta, dict):
            tickers_raw = meta.get("tickers")
            if isinstance(tickers_raw, str) and tickers_raw.strip():
                return [t.strip() for t in tickers_raw.split(",") if t.strip()]
    return []


def _resolve_resume_scoped(store: ResearchRepository, session_id: str, wave: int) -> list[str]:
    """Tickers for resume: source-job diagnostics first, evidence metadata fallback."""
    try:
        if found := _scoped_from_jobs(store, session_id, wave):
            return found
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    return _scoped_from_evidence(store, session_id)


def _resume_trace_id(session_id: str, sess: ResearchSession) -> str | None:
    """Trace id for resume: session budget first, newest session trace fallback."""
    trace_id: object = sess.budget.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id:
        existing = list_traces(session_id)
        trace_id = existing[0].trace_id if existing else None
    return trace_id if isinstance(trace_id, str) and trace_id else None


def _attach_resumed_trace(run: _LiveRun, session_id: str, wave: int, trace_id: str) -> None:
    """Reattach an existing trace with its event count as the sequence base."""
    import time as _time
    from pathlib import Path as _Path

    from app.config import get_data_root as _gdr
    try:
        events = get_trace_events(trace_id)
        seq = len(events)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        seq = 0
    try:
        root = _gdr()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        root = _Path("data")
    from app.research.evals.traces import TRACE_DB_NAME as _tdb
    try:
        db_path = root / _tdb
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        db_path = _Path("data") / _tdb
    run.trace = TraceRecorder(trace_id=trace_id, session_id=session_id, wave_id=wave, db_path=db_path, jsonl_path=root / "traces" / f"{trace_id}.jsonl", _seq=seq, _start_perf=_time.perf_counter(), _start_iso="", _closed=False)
    run.trace_id = trace_id
    run._trace_record("trace.resumed", {"trace_id": trace_id, "session_id": session_id, "wave_id": wave})


def _attach_fresh_trace(run: _LiveRun, session_id: str, wave: int) -> None:
    """Fresh trace for resume when no prior trace resolves."""
    import subprocess as _sp
    try:
        _sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=2).strip() or "unknown"
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        _sha = "unknown"
    tr = create_trace(session_id=session_id, wave_id=wave, provider=run.provider, model=run.model_name, prompt_version="v1", git_sha=_sha)
    run.trace, run.trace_id = tr, tr.trace_id
    run._trace_record("trace.opened", {"trace_id": tr.trace_id, "session_id": session_id, "resume": True})


def _resume_trace(run: _LiveRun, session_id: str, wave: int, sess: ResearchSession) -> None:
    """Reattach the eval trace on resume: existing trace resumed, else a fresh one."""
    trace_id = _resume_trace_id(session_id, sess)
    if trace_id is not None:
        _attach_resumed_trace(run, session_id, wave, trace_id)
    else:
        _attach_fresh_trace(run, session_id, wave)

def _append_resume_evidence(run: _LiveRun, rec_dict: Mapping[str, object]) -> None:
    """Append one persisted evidence record; corrupt and duplicate rows skipped."""
    try:
        ev = evidence_from_dict(rec_dict)
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return
    if ev.evidence_id in run.ledger:
        return
    try:
        run.ledger.append(ev)
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass


def _preload_resume_ledger(run: _LiveRun, store: ResearchRepository, session_id: str) -> None:
    """Seed ledger + dossier ids from persisted state; duplicates skipped."""
    for rec_dict in store.list_evidence(session_id):
        _append_resume_evidence(run, rec_dict)
    run.dossier_ids = []
    for d in store.list_dossiers(session_id):
        _did = d.get("dossier_id")
        if isinstance(_did, str) and _did:
            run.dossier_ids.append(_did)


def _completed_fetch_ids(run: _LiveRun, store: ResearchRepository, session_id: str, wave: int, eids: list[str]) -> tuple[list[str], str] | None:
    """Completed-source fast path: dossier + completed source mean fetch is done."""
    dossier_id = f"{session_id}:{wave}:sec"
    all_jobs = store.list_jobs(session_id)
    src_jobs = [j for j in all_jobs if j.job_type == JobType.SOURCE_AGENT.value and j.wave_id == wave]
    if any(j.status == "completed" for j in src_jobs) and dossier_id in set(run.dossier_ids):
        return eids, run.dossier_ids[0] if run.dossier_ids else ""
    return None


def _reuse_fetch_question(reuse_job: Job, question: str) -> str:
    """Fetch question for a reused source job: its diagnostics question wins."""
    try:
        diag_q: object = dict(reuse_job.diagnostics).get("question")
        if isinstance(diag_q, str) and diag_q.strip():
            return diag_q
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    return question


def _restart_queued_fetch(run: _LiveRun, store: ResearchRepository, session_id: str, reuse_job: Job) -> None:
    """Start a queued source job and journal the reuse."""
    if reuse_job.status == "queued":
        store.save_job(_jobs.start_job(reuse_job))
        run._emit(session_id, "job.started", {"job_id": reuse_job.job_id})
    run._trace_record("source.reused", {"job_id": reuse_job.job_id, "wave_id": reuse_job.wave_id})


def _reuse_fetch_job(run: _LiveRun, store: ResearchRepository, session_id: str, wave: int, question: str) -> tuple[str, str] | None:
    """Running-source path: restart a queued job, reuse its question, return (src_id, fetch_q)."""
    src_jobs = [j for j in store.list_jobs(session_id) if j.job_type == JobType.SOURCE_AGENT.value and j.wave_id == wave]
    reuse_job = next((j for j in src_jobs if j.status in ("queued", "running")), None)
    if reuse_job is None:
        return None
    _restart_queued_fetch(run, store, session_id, reuse_job)
    return reuse_job.job_id, _reuse_fetch_question(reuse_job, question)


def _wave2_targeted_question(store: ResearchRepository, session_id: str) -> str | None:
    """Targeted question from the wave.started journal; None when absent."""
    for evt in store.list_events(session_id):
        payload = evt.to_dict().get("payload")
        if isinstance(payload, dict) and evt.event_type == "wave.started":
            tq = payload.get("targeted_question")
            if isinstance(tq, str) and tq.strip():
                return tq
    return None


def _fresh_fetch_question(store: ResearchRepository, session_id: str, wave: int, question: str) -> str:
    """Fresh-source path: wave-2 reuses the targeted question from the journal."""
    if wave != 2:
        return question
    try:
        return _wave2_targeted_question(store, session_id) or question
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return question


def _fetch_or_reuse_wave(run: _LiveRun, store: ResearchRepository, session_id: str, wave: int, question: str, eids: list[str]) -> tuple[list[str], str]:
    """Fetch-or-reuse phase: skip fetch when the dossier + completed source exist."""
    done = _completed_fetch_ids(run, store, session_id, wave, eids)
    if done is not None:
        return done
    reused = _reuse_fetch_job(run, store, session_id, wave, question)
    if reused is not None:
        src_id, fetch_q = reused
    else:
        fetch_q = _fresh_fetch_question(store, session_id, wave, question)
        src_id = run._open_source_job(session_id, wave, fetch_q)
    return run._fetch_wave(session_id, wave, fetch_q, src_id, ""), run.dossier_ids[0] if run.dossier_ids else ""


def _freeze_or_reuse(run: _LiveRun, store: ResearchRepository, session_id: str, wave: int, eids: list[str]) -> tuple[str, list[str]]:
    """Freeze-or-reuse phase: existing freeze ids win, else create a fresh freeze."""
    fid: str = f"{session_id}:{wave}:freeze"
    try:
        existing_freeze = store.get_freeze(fid)
        frozen_ids = existing_freeze.get("evidence_ids")
        if isinstance(frozen_ids, list) and all(isinstance(e, str) for e in frozen_ids):
            return fid, [e for e in frozen_ids if isinstance(e, str)]
        return fid, eids
    except KeyError:
        return run._freeze_wave(session_id, wave), eids



def _hydrate_resume_budget(run: _LiveRun, store: ResearchRepository, session_id: str, sess: ResearchSession) -> None:
    """Seed the run budget from persisted totals; evidence count fallback; never raises."""
    try:
        persisted: object = sess.budget.get("tool_calls_used")
        if isinstance(persisted, int) and persisted >= 0:
            run.budget.hydrate(persisted)
        else:
            run.budget.hydrate(len(store.list_evidence(session_id)))
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass


def _close_resumed_wave(run: _LiveRun, store: ResearchRepository, session_id: str, wave: int, eids: list[str]) -> dict[str, object]:
    """Committee + wave-2 tail for a resumed wave with evidence."""
    fid, eids_for_committee = _freeze_or_reuse(run, store, session_id, wave, eids)
    # Analyses are never persisted, so the trio always reruns on the freeze —
    # even after one-committee (stock-only) interruptions. Same freeze id, new jobs.
    stock, bull, bear = run._committee_wave(session_id, wave, "")
    disagreement = compute_disagreement(stock, bull, bear)
    result = Wave1Result(
        session_id=session_id, wave_id=wave, freeze_id=fid,
        evidence_ids=list(eids_for_committee),
        stock=stock, bull=bull, bear=bear, disagreement=disagreement,
    )
    did_out: str = run.dossier_ids[0] if run.dossier_ids else ""
    return run._finish_completed(result, did_out)


def _open_resume_run(store: ResearchRepository, sess: ResearchSession, session_id: str, dispatch: Callable[[str, dict[str, object]], dict[str, object]], model: Callable[[str], str], limits: DirectorBudgets, provider: str, model_name: str | None) -> tuple[_LiveRun, str, int]:
    """Build the resume run: question/scope/wave from persisted session. Returns (run, question, wave)."""
    question: str = sess.query
    objective: str = sess.objective
    as_of_obj = sess.as_of
    as_of_str: str = as_of_obj.isoformat() if as_of_obj is not None else ""
    as_of: str | None = as_of_str or None
    cur_wave = sess.current_wave
    wave: int = cur_wave if isinstance(cur_wave, int) and cur_wave >= 1 else 1
    scoped = _resolve_resume_scoped(store, session_id, wave)
    run = _LiveRun(store, question, objective, as_of, as_of_str, scoped, dispatch, model, limits, wave, actor="resume_live", provider=provider, model_name=model_name)
    return run, question, wave


def _merge_session_dossiers(run: _LiveRun, sess: ResearchSession) -> None:
    """Merge session-level dossier ids into the run without duplicates."""
    for _did in sess.dossier_ids:
        if isinstance(_did, str) and _did and _did not in run.dossier_ids:
            run.dossier_ids.append(_did)



def resume_live(
    session_id: str,
    dispatch: Callable[[str, dict[str, object]], dict[str, object]],
    model: Callable[[str], str],
    repo: ResearchRepository | None = None,
    budgets: DirectorBudgets | None = None,
    *,
    provider: str = "fake",
    model_name: str | None = None,
) -> dict[str, object]:
    """Continue an interrupted session to a final result without duplicating work.

    Preloads the evidence ledger (skipping duplicates) and dossier ids, seeds
    the evidence counter from the existing wave count so ids are never reused,
    skips fetch when wave evidence exists, skips freeze when its id already
    resolves, then always reruns the full committee trio on the freeze because
    analyses are not persisted. Closes with the wave-2 gate + synthesis tail.
    FAILED/COMPLETED/CANCELLED sessions raise instead of silently reopening.
    """
    store: ResearchRepository = repo if repo is not None else ResearchRepository()
    limits: DirectorBudgets = budgets if budgets is not None else DirectorBudgets()
    state = store.resume(session_id)
    sess = state.session
    if sess.status in ("failed", "completed", "cancelled"):
        raise LiveModelError(
            session_id, "resume",
            f"resume: session {session_id} is {sess.status}"
            " (retry of failed waves is out of scope; start a new session)",
        )
    run, question, wave = _open_resume_run(store, sess, session_id, dispatch, model, limits, provider, model_name)
    try:
        _resume_trace(run, session_id, wave, sess)
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts
        pass
    _hydrate_resume_budget(run, store, session_id, sess)
    _preload_resume_ledger(run, store, session_id)
    _merge_session_dossiers(run, sess)
    eids: list[str] = [e.evidence_id for e in run.ledger.list_session(session_id) if e.wave_id == wave]
    eids, _ = _fetch_or_reuse_wave(run, store, session_id, wave, question, eids)
    if not eids:
        run._record_stop(session_id, "complete:empty-with-limitations")
        run._store_empty_terminal(session_id)
        return {
            "session_id": session_id, "wave_id": wave, "freeze_id": "",
            "evidence_ids": eids, "dossier_id": run.dossier_ids[0] if run.dossier_ids else "",
            "stock": None, "bull": None, "bear": None, "disagreement": None,
            "stop_reason": "complete:empty-with-limitations",
        }
    return _close_resumed_wave(run, store, session_id, wave, eids)
