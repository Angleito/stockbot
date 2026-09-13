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
from app.research.agents.sec_agent import run_sec_assignment
from app.research.agents.scout import ScoutAssignment, ScoutResult
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
from app.research.synthesis.committee import CommitteeDisagreement, compute_disagreement
from app.research.dossiers.sec import SECDossier, dossier_to_dict, validate_dossier
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
    SessionStatus,
    default_policy,
    utcnow,
)
from app.research.repository import ResearchRepository
from app.research.evals.traces import TraceRecorder, create_trace, get_trace_events, list_traces

__all__ = ["LiveModelError", "resume_live", "run_live"]


_KNOWN_AT_KEYS = (
    "known_at", "acceptanceDatetime", "acceptedDate", "filingDate",
    "filedAt", "filed", "publishedAt", "published_at", "published",
    "date", "timestamp",
)


def _extract_known_at(raw: Mapping[str, object]) -> datetime | None:
    """Source-provided timestamp or None; never invented, never as_of."""
    from datetime import datetime as _dt
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
    for value in scopes:
        if isinstance(value, _dt):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return _dt.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                continue
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


def _is_uri_like(value: str) -> bool:
    text = value.strip()
    return "://" in text or "/" in text or "." in text

def _extract_source_ref(raw: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Real SEC reference from the tool result; (None, None) when absent."""
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
    uri: str | None = None
    ref: str | None = None
    for scope in scopes:
        if uri is None:
            for key in _SOURCE_URI_KEYS:
                value = scope.get(key)
                if isinstance(value, str) and value.strip() and _is_uri_like(value):
                    uri = value.strip()
                    break
        if ref is None:
            for key in _SOURCE_ID_KEYS:
                value = scope.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    ref = str(value).strip()
                    break
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
    """One authoritative run budget: atomic consume, read-only total."""
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0
        self._lock = threading.Lock()
    def consume_research_dispatch(self) -> bool:
        """Increment once before a real research dispatch; False when exhausted."""
        with self._lock:
            if self._used >= self._limit:
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
        except Exception:
            pass
    def _trace_record(self, event_type: str, payload: Mapping[str, object] | None = None, duration_ms: float | None = None) -> None:
        tr = self.trace
        if tr is None:
            return
        try:
            from app.redact import redact_json as _rj
            import json as _json
            flat: dict[str, str | int | float | bool | None] = {}
            for k, v in (payload or {}).items():
                if v is None or isinstance(v, (str, int, float, bool)):
                    flat[k] = _rj(str(v)) if isinstance(v, str) else v
                else:
                    try:
                        flat[k] = _rj(_json.dumps(v, sort_keys=True, default=str))[:2000]
                    except Exception:
                        flat[k] = str(v)[:2000]
            with self._lock:
                tr.record(event_type, flat, duration_ms)
        except Exception:
            pass


    def _persist_model_failure(self, session_id: str, job_id: str, stage: str, exc: Exception) -> str:
        """Fail one RUNNING job (TIMEOUT) + journal + session failure; return the message."""
        detail: str = f"{type(exc).__name__}: {exc}"
        message: str = f"{stage}: {detail}"[:2000]
        open_job: Job | None = None
        with self._lock:
            try:
                open_job = self.store.get_job(job_id)
            except KeyError:
                open_job = None
        if open_job is not None and open_job.status in ("queued", "running"):
            with self._lock:
                self.store.save_job(_jobs.fail_job(open_job, FailureCategory.TIMEOUT, message))
            self._emit(session_id, "job.failed", {
                "job_id": job_id, "stage": stage,
                "failure_category": FailureCategory.TIMEOUT.value, "error": str(exc)[:2000],
            })
        self._emit(session_id, "model.failed", {
            "stage": stage, "job_id": job_id,
            "error_type": type(exc).__name__, "error": str(exc)[:2000],
        })
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
        self._emit(session_id, "research.failed", {
            "stage": stage, "failure_category": FailureCategory.TIMEOUT.value,
            "reason": "timeout:model-call",
        })
        self._emit(session_id, "wave.stopped", {"reason": "timeout:model-call", "stage": stage})
        try:
            if self.trace is not None:
                self.trace.finish(message[:2000], "failed")
        except Exception:
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
            import subprocess as _sp
            try:
                _sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=2).strip() or "unknown"
            except Exception:
                _sha = "unknown"
            tr = create_trace(session_id=sess.session_id, wave_id=self.wave_id, provider=self.provider, model=self.model_name, prompt_version="v1", git_sha=_sha)
            self.trace, self.trace_id = tr, tr.trace_id
            try:
                with self._lock:
                    cur = self.store.get_session(sess.session_id)
                    b = dict(cur.budget)
                    b["trace_id"] = tr.trace_id
                    self.store.save_session(replace(cur, budget=b, updated_at=utcnow()))
            except Exception:
                pass
            self._trace_record("trace.opened", {"trace_id": tr.trace_id, "session_id": sess.session_id})
        except Exception:
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

    def _fetch_wave(self, sid: str, wave: int, q: str, src_job_id: str, prefix: str) -> list[str]:
        existing_wave = sum(1 for e in self.ledger.list_session(sid) if e.wave_id == wave)
        counter: list[int] = [existing_wave]

        def _on_reject(event_type: str, payload: dict[str, object]) -> None:
            self._emit(sid, event_type, payload)

        def _live_dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
            if name == "call_tool":
                inner_obj: object = args.get("name")
                inner: str = inner_obj if isinstance(inner_obj, str) and inner_obj else "unknown_tool"
                if not is_sec_tool(inner):
                    self._emit(sid, "policy.denied", {"tool": inner, "reason": "POLICY_DENIED"})
                    raise ValueError(f"POLICY_DENIED: non-SEC tool {inner!r}")
                if not self.budget.consume_research_dispatch():
                    self._emit(sid, "budget.exhausted", {"tool": inner, "reason": "TOOL_BUDGET_EXHAUSTED"})
                    raise ValueError("TOOL_BUDGET_EXHAUSTED: run tool budget exhausted")
                self._save_budget_used(sid, strict=True)
                from time import perf_counter as _pc
                _t0 = _pc()
                try:
                    raw: dict[str, object] = self.dispatch(name, args)
                except Exception as exc:
                    _dur_e = (_pc() - _t0) * 1000.0
                    self._trace_record("tool.failed", {"tool": inner, "args": args, "error": str(exc)[:2000]}, _dur_e)
                    raise
                _dur = (_pc() - _t0) * 1000.0
                try:
                    _sdl = self._scout_deadline
                    if _sdl is not None:
                        from datetime import datetime as _dts, timezone as _tzs
                        _nows = _dts.now(_tzs.utc)
                        _sdlc = _sdl if _sdl.tzinfo is not None else _sdl.replace(tzinfo=_tzs.utc)
                        if _nows >= _sdlc:
                            self._emit(sid, "tool.failed", {"tool": inner, "error": "scout deadline exceeded"})
                            self._trace_record("tool.failed", {"tool": inner, "args": args, "error": "scout deadline exceeded"}, _dur)
                            raise TimeoutError("scout deadline exceeded after dispatch")
                except TimeoutError:
                    raise
                except Exception:
                    pass
                if "error" in raw:
                    self._emit(sid, "tool.failed", {"tool": inner, "error": str(raw.get("error"))[:2000]})
                    self._trace_record("tool.failed", {"tool": inner, "args": args, "error": str(raw.get("error"))[:2000]}, _dur)
                    raise ValueError(f"TOOL_ERROR: {inner} failed: {raw.get('error')}")
                counter[0] += 1
                eid: str = f"{sid}:{wave}:sec:{counter[0]}"
                content_obj: object = raw.get("content")
                if isinstance(content_obj, str) and content_obj.strip():
                    content: str = content_obj.strip()
                else:
                    content = json.dumps(raw, sort_keys=True, default=str)
                if not content:
                    content = "{}"
                retrieved = utcnow()
                known_at = _extract_known_at(raw)
                src_uri, src_ref = _extract_source_ref(raw)
                subject: str = q.strip()[:120] if q.strip() else "sec evidence"
                claim: str = f"{inner} finding for {', '.join(self.scoped) if self.scoped else 'universe'}"[:500]
                record = Evidence(
                    evidence_id=eid, session_id=sid, wave_id=wave,
                    source_type="sec", source_name=inner, subject=subject,
                    claim_text=claim, content=content,
                    content_hash=evidence_content_hash(content),
                    retrieved_at=retrieved, source_uri=src_uri, source_record_id=src_ref,
                    published_at=None, known_at=known_at, effective_at=None,
                    job_id=src_job_id or None, agent_id="sec_scout",
                    supports=(), contradicts=(), confidence=None, quality=None,
                    metadata={"tool": inner, "tickers": ", ".join(self.scoped)},
                    superseded_by=None,
                )
                try:
                    ingest_evidence(self.ledger, record, as_of=self.as_of, on_reject=_on_reject)
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
                return {"evidence_ids": [{"evidence_id": eid, "known_at": known_at.isoformat() if known_at else None, "claim_text": claim[:500], "content_snippet": content[:500]}]}
            from time import perf_counter as _pc2
            _t1 = _pc2()
            raw: dict[str, object] = self.dispatch(name, args)
            _d1 = (_pc2() - _t1) * 1000.0
            self._trace_record("discovery.completed", {"tool": name, "args": args, "matches": raw.get("matches")}, _d1)
            if name == "search_tools" or name == "browse_tools":
                found: object = raw.get("matches")
                names: list[dict[str, object]] = []
                if isinstance(found, list):
                    for item in found:
                        if isinstance(item, str) and item:
                            names.append({"name": item})
                        elif isinstance(item, dict):
                            cand: object = item.get("name")
                            if isinstance(cand, str) and cand:
                                names.append({"name": cand})
                else:
                    meta: object = raw.get("meta")
                    if isinstance(meta, dict):
                        inner_meta: object = meta.get("matches")
                        if isinstance(inner_meta, list):
                            for item in inner_meta:
                                if isinstance(item, str) and item:
                                    names.append({"name": item})
                                elif isinstance(item, dict):
                                    cand2: object = item.get("name")
                                    if isinstance(cand2, str) and cand2:
                                        names.append({"name": cand2})
                return {"matches": names}
            return raw
        scout_calls: list[int] = [0]
        def _scout_journal(event_type: str, payload: dict[str, object]) -> None:
            self._emit(sid, event_type, payload)
        def _scout_model(prompt: str) -> str:
            scout_calls[0] += 1
            stage: str = f"{prefix}scout-{scout_calls[0]}"
            return self._model_at_stage(stage, sid, src_job_id)(prompt)
        def _spawn_scout(assignment: ScoutAssignment) -> ScoutResult:
            from datetime import timedelta
            from app.research.agents import ResearchRequest as _RR
            from app.research.agents.scout import run_scout as _run_one
            existing_jobs: list[Job] = self.store.list_jobs(sid)
            for child in existing_jobs:
                if child.parent_job_id != src_job_id or child.job_type != JobType.SCOUT.value:
                    continue
                res = child.result or {}
                if res.get("assignment_id") == assignment.assignment_id and child.status == "completed" and "findings" in res:
                    from app.research.agents import GroundedClaim as _GC
                    raw_findings = res.get("findings")
                    findings_list: list[_GC] = []
                    if isinstance(raw_findings, list):
                        for item in raw_findings:
                            if not isinstance(item, dict):
                                continue
                            txt = item.get("text")
                            ids = item.get("evidence_ids")
                            if not isinstance(txt, str) or not txt.strip():
                                continue
                            if not isinstance(ids, list) or not ids:
                                continue
                            eids = [e for e in ids if isinstance(e, str) and e]
                            if not eids:
                                continue
                            findings_list.append(_GC(text=txt.strip()[:500], evidence_ids=eids))
                    unk_raw = res.get("unknowns")
                    lim_raw = res.get("limitations")
                    req_raw = res.get("follow_up_requests")
                    unk_list = [e for e in unk_raw if isinstance(e, str)] if isinstance(unk_raw, list) else []
                    lim_list = [e for e in lim_raw if isinstance(e, str)] if isinstance(lim_raw, list) else []
                    req_list: list[_RR] = []
                    if isinstance(req_raw, list):
                        for item in req_raw:
                            if not isinstance(item, dict):
                                continue
                            try:
                                raw_agents: object = item.get("requesting_agents")
                                agents: list[str] = [a for a in raw_agents if isinstance(a, str)] if isinstance(raw_agents, list) else []
                                req_list.append(_RR(question=str(item.get("question", "")), why_material=str(item.get("why_material", "")), requested_source_domain=str(item.get("requested_source_domain", "SEC")), expected_gain=str(item.get("expected_gain", "medium")), requesting_agents=agents))
                            except Exception:
                                continue
                    self._emit(sid, "scout.reused", {"job_id": child.job_id, "assignment_id": assignment.assignment_id})
                    self._trace_record("scout.reused", {"job_id": child.job_id, "assignment_id": assignment.assignment_id})
                    return ScoutResult(assignment_id=assignment.assignment_id, session_id=sid, coverage=str(res.get("coverage", "reused")), findings=findings_list, unknowns=unk_list, limitations=lim_list, follow_up_requests=req_list)
            reusable = next((c for c in existing_jobs if c.parent_job_id == src_job_id and c.job_type == JobType.SCOUT.value and (c.diagnostics or {}).get("assignment_id") == assignment.assignment_id and c.status in ("queued", "running")), None)
            if reusable is not None:
                scout_job = reusable
                if scout_job.status == "queued":
                    self.store.save_job(_jobs.start_job(scout_job))
                    self._emit(sid, "job.started", {"job_id": scout_job.job_id})
                self._emit(sid, "scout.retried", {"job_id": scout_job.job_id, "assignment_id": assignment.assignment_id})
                self._trace_record("scout.retried", {"job_id": scout_job.job_id, "assignment_id": assignment.assignment_id})
            else:
                sess_now = self.store.get_session(sid)
                deadline = (utcnow() + timedelta(seconds=assignment.time_budget_s)).isoformat()
                sess_upd, scout_job = _jobs.create_job(sess_now, existing_jobs, job_type=JobType.SCOUT, owner="runner", wave_id=wave, parent_job_id=src_job_id, source_domain="SEC", tool_budget=assignment.max_tool_calls, deadline=deadline)
                tickers_j: list[JSONValue] = [t for t in assignment.tickers]
                scout_job = replace(scout_job, diagnostics={"assignment_id": assignment.assignment_id, "role": assignment.role, "question": assignment.question[:500], "tickers": tickers_j, "as_of": assignment.as_of, "max_tool_calls": assignment.max_tool_calls, "time_budget_s": assignment.time_budget_s, "allowed_domain": assignment.allowed_domain, "session_id": assignment.session_id})
                self.store.save_session(sess_upd)
                self.store.save_job(scout_job)
                self._emit(sid, "job.created", {"job_id": scout_job.job_id, "job_type": scout_job.job_type, "assignment_id": assignment.assignment_id})
                self.store.save_job(_jobs.start_job(scout_job))
            try:
                _dl = scout_job.deadline
            except Exception:
                _dl = None
            if _dl is None:
                from datetime import timedelta as _td
                _dl = utcnow() + _td(seconds=assignment.time_budget_s)
            self._scout_deadline = _dl
            def _tagged_dispatch(name: str, args: dict[str, object]) -> dict[str, object]:
                try:
                    _dl2 = self._scout_deadline
                    if _dl2 is not None:
                        try:
                            from datetime import datetime as _dt2, timezone as _tz2
                            _now2 = _dt2.now(_tz2.utc)
                            _dlc = _dl2 if _dl2.tzinfo is not None else _dl2.replace(tzinfo=_tz2.utc)
                            if _now2 >= _dlc:
                                raise TimeoutError(f"scout deadline exceeded before dispatch {name}")
                        except TimeoutError:
                            raise
                        except Exception:
                            pass
                    return _live_dispatch(name, args)
                except Exception as exc:
                    setattr(exc, "_scout_origin", "tool")
                    raise
            def _tagged_model(prompt: str) -> str:
                scout_calls[0] += 1
                from time import perf_counter as _pc
                from app.redact import redact_text as _rt
                _t0 = _pc()
                try:
                    _dl3 = self._scout_deadline
                    if _dl3 is not None:
                        try:
                            from datetime import datetime as _dt3, timezone as _tz3
                            if _dt3.now(_tz3.utc) >= (_dl3 if _dl3.tzinfo is not None else _dl3.replace(tzinfo=_tz3.utc)):
                                raise TimeoutError("scout deadline exceeded before model call")
                        except TimeoutError:
                            raise
                        except Exception:
                            pass
                    out = self.model(prompt)
                    try:
                        _dl4 = self._scout_deadline
                        if _dl4 is not None:
                            from datetime import datetime as _dt4, timezone as _tz4
                            if _dt4.now(_tz4.utc) >= (_dl4 if _dl4.tzinfo is not None else _dl4.replace(tzinfo=_tz4.utc)):
                                raise TimeoutError("scout deadline exceeded after model call")
                    except TimeoutError:
                        raise
                    except Exception:
                        pass
                    _dur = (_pc() - _t0) * 1000.0
                    self._trace_record("model.completed", {"provider": self.provider, "model": self.model_name, "stage": f"{prefix}scout", "job_id": scout_job.job_id, "prompt": _rt(prompt)[:2000], "output": _rt(out)[:2000]}, _dur)
                    return out
                except Exception as exc:
                    _dur2 = (_pc() - _t0) * 1000.0
                    self._trace_record("model.failed", {"provider": self.provider, "model": self.model_name, "stage": f"{prefix}scout", "job_id": scout_job.job_id, "error": str(exc)[:2000]}, _dur2)
                    setattr(exc, "_scout_origin", "model")
                    raise
            try:
                try:
                    result = _run_one(assignment, dispatch=_tagged_dispatch, model=_tagged_model, journal=_scout_journal)
                finally:
                    self._scout_deadline = None
            except Exception as exc:
                origin = getattr(exc, "_scout_origin", "")
                pre_cat = getattr(exc, "_failure_category", None)
                msg = str(exc)[:2000] or type(exc).__name__
                low = (type(exc).__name__ + " " + msg).lower()
                cancelled = "cancel" in type(exc).__name__.lower() or "cancelled" in low or "canceled" in low or bool(getattr(exc, "_scout_cancelled", False))
                if cancelled:
                    try:
                        cur = self.store.get_job(scout_job.job_id)
                        if cur.status in ("queued", "running"):
                            self.store.save_job(_jobs.cancel_job(cur))
                        self._emit(sid, "job.cancelled", {"job_id": scout_job.job_id})
                    except Exception:
                        pass
                    setattr(exc, "_failure_category", pre_cat if isinstance(pre_cat, FailureCategory) else FailureCategory.TOOL_ERROR)
                    setattr(exc, "_scout_cancelled", True)
                    raise
                if isinstance(pre_cat, FailureCategory):
                    cat = pre_cat
                elif "timeout" in low or "timed_out" in low or "expired" in low:
                    cat = FailureCategory.TIMEOUT
                elif "budget" in low:
                    cat = FailureCategory.TOOL_BUDGET_EXHAUSTED
                elif "policy" in low or "denied" in low:
                    cat = FailureCategory.POLICY_DENIED
                else:
                    cat = FailureCategory.MODEL_ERROR if origin == "model" else FailureCategory.TOOL_ERROR
                try:
                    cur2 = self.store.get_job(scout_job.job_id)
                    if cur2.status in ("queued", "running"):
                        self.store.save_job(_jobs.fail_job(cur2, cat, f"scout:{type(exc).__name__}:{msg}"[:2000]))
                    self._emit(sid, "job.failed", {"job_id": scout_job.job_id, "failure_category": cat.value})
                except Exception:
                    pass
                setattr(exc, "_failure_category", cat)
                raise
            req_out: list[JSONValue] = []
            for req in result.follow_up_requests:
                agents_out: list[JSONValue] = [a for a in req.requesting_agents]
                req_out.append({"question": req.question, "why_material": req.why_material, "requested_source_domain": req.requested_source_domain, "expected_gain": req.expected_gain, "requesting_agents": agents_out})
            findings_out: list[JSONValue] = [{"text": c.text, "evidence_ids": list(c.evidence_ids)} for c in result.findings]
            self.store.save_job(_jobs.complete_job(self.store.get_job(scout_job.job_id), result={"assignment_id": assignment.assignment_id, "findings": findings_out, "coverage": result.coverage, "unknowns": list(result.unknowns), "limitations": list(result.limitations), "follow_up_requests": req_out}))
            self._emit(sid, "job.completed", {"job_id": scout_job.job_id})
            return result
        try:
            dossier_obj: object = run_sec_assignment(
                q, session_id=sid, wave_id=wave, as_of=self.as_of_str or "unbounded",
                tickers=self.scoped, dispatch=_live_dispatch, model=_scout_model, journal=_scout_journal, spawn=_spawn_scout,
            )
        except Exception as exc:
            cat_raw = getattr(exc, "_failure_category", None)
            if isinstance(cat_raw, FailureCategory):
                cat = cat_raw
            else:
                low_all = (type(exc).__name__ + " " + str(exc)).lower()
                if "timeout" in low_all or "expired" in low_all or "timed_out" in low_all:
                    cat = FailureCategory.TIMEOUT
                elif "budget" in low_all:
                    cat = FailureCategory.TOOL_BUDGET_EXHAUSTED
                elif "policy" in low_all or "denied" in low_all:
                    cat = FailureCategory.POLICY_DENIED
                else:
                    cat = FailureCategory.TOOL_ERROR
            cancelled = bool(getattr(exc, "_scout_cancelled", False))
            detail = f"{type(exc).__name__}: {exc}"[:2000]
            message = f"source-scout: {detail}"[:2000]
            try:
                src_job = self.store.get_job(src_job_id)
                if src_job.status in ("queued", "running"):
                    if cancelled:
                        self.store.save_job(_jobs.cancel_job(src_job))
                        self._emit(sid, "job.cancelled", {"job_id": src_job_id})
                    else:
                        self.store.save_job(_jobs.fail_job(src_job, cat, message))
                        self._emit(sid, "job.failed", {"job_id": src_job_id, "stage": "source-scout", "failure_category": cat.value, "error": str(exc)[:2000]})
            except Exception:
                pass
            self._emit(sid, "model.failed", {"stage": "source-scout", "job_id": src_job_id, "error_type": type(exc).__name__, "error": str(exc)[:2000]})
            try:
                sess_fail = self.store.get_session(sid)
                sess_fail = replace(sess_fail, failure=Failure(category=cat.value, message=message), updated_at=utcnow())
                if sess_fail.status not in ("failed", "completed", "cancelled"):
                    sess_fail = _session.transition_session(sess_fail, SessionStatus.FAILED)
                self.store.save_session(sess_fail)
            except Exception:
                pass
            self._emit(sid, "research.failed", {"stage": "source-scout", "failure_category": cat.value, "reason": "timeout:model-call" if cat == FailureCategory.TIMEOUT else f"scout:{cat.value}"})
            self._emit(sid, "wave.stopped", {"reason": "timeout:model-call" if cat == FailureCategory.TIMEOUT else f"failed:source-scout", "stage": "source-scout"})
            self._save_budget_used(sid)
            try:
                if self.trace is not None:
                    self.trace.finish(message[:2000], "failed")
            except Exception:
                pass
            raise LiveModelError(sid, "source-scout", detail) from exc
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
            raise ValueError(f"runner: unexpected dossier type {type(dossier_obj).__name__} (SECDossier required)")
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

    def _committee_wave(self, session_id: str, wave: int, prefix: str) -> tuple[StockbotAnalysis, BullAnalysis, BearAnalysis]:
        sess = self.store.get_session(session_id)
        if sess.status == SessionStatus.FREEZING.value:
            sess = _session.transition_session(sess, SessionStatus.ANALYZING)
            self.store.save_session(sess)
        fid: str = f"{session_id}:{wave}:freeze"
        frozen_ids: object = self.store.get_freeze(fid).get("evidence_ids")
        ev_ids: list[str] = [e for e in frozen_ids if isinstance(e, str)] if isinstance(frozen_ids, list) else []
        existing: list[Job] = self.store.list_jobs(session_id)
        pending: list[Job] = list(existing)
        ran: list[str] = []
        progressed = self.store.get_session(session_id)
        for kind in (JobType.STOCKBOT, JobType.BULLBOT, JobType.BEARBOT):
            progressed, job = _jobs.create_job(
                progressed, pending, job_type=kind, owner="runner", wave_id=wave,
            )
            self.store.save_session(progressed)
            self.store.save_job(job)
            self._emit(session_id, "job.created", {"job_id": job.job_id, "job_type": job.job_type})
            started = _jobs.start_job(job)
            self.store.save_job(started)
            pending.append(started)
            ran.append(started.job_id)
        shared_text = self._freeze_evidence_text(session_id, ev_ids)
        def _run_stock() -> StockbotAnalysis:
            analysis = run_stockbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-stockbot", session_id, ran[0]),
                evidence_text=shared_text,
            )
            with self._lock:
                self.store.save_job(_jobs.complete_job(self.store.get_job(ran[0]), result={"freeze_id": fid}))
            return analysis
        def _run_bull() -> BullAnalysis:
            analysis = run_bullbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-bullbot", session_id, ran[1]),
                evidence_text=shared_text,
            )
            with self._lock:
                self.store.save_job(_jobs.complete_job(self.store.get_job(ran[1]), result={"freeze_id": fid}))
            return analysis
        def _run_bear() -> BearAnalysis:
            analysis = run_bearbot(
                self.question, session_id=session_id, wave_id=wave, freeze_id=fid,
                evidence_ids=ev_ids, as_of=self.as_of_str or "unbounded",
                model=self._model_at_stage(f"{prefix}committee-bearbot", session_id, ran[2]),
                evidence_text=shared_text,
            )
            with self._lock:
                self.store.save_job(_jobs.complete_job(self.store.get_job(ran[2]), result={"freeze_id": fid}))
            return analysis
        try:
            with ThreadPoolExecutor(max_workers=3) as _pool:
                stock_f = _pool.submit(_run_stock)
                bull_f = _pool.submit(_run_bull)
                bear_f = _pool.submit(_run_bear)
                stock = stock_f.result()
                bull = bull_f.result()
                bear = bear_f.result()
        except Exception as exc:
            pre = getattr(exc, "_failure_category", None)
            cat = pre if isinstance(pre, FailureCategory) else FailureCategory.TIMEOUT
            msg = str(exc)[:1500] or type(exc).__name__
            low = (type(exc).__name__ + " " + msg).lower()
            if not isinstance(pre, FailureCategory):
                if "budget" in low:
                    cat = FailureCategory.TOOL_BUDGET_EXHAUSTED
                elif "policy" in low or "denied" in low:
                    cat = FailureCategory.POLICY_DENIED
                elif "model_output" in low or "unknown evidence" in low or "uncited" in low:
                    cat = FailureCategory.MODEL_OUTPUT_FAILURE
                elif "tool_error" in low or "tool failed" in low:
                    cat = FailureCategory.TOOL_ERROR
            for jid in ran:
                with self._lock:
                    leftover: Job = self.store.get_job(jid)
                    if leftover.status in ("queued", "running"):
                        self.store.save_job(_jobs.fail_job(
                            leftover, cat, f"committee:{type(exc).__name__}:{msg}"[:2000]))
                    self._emit(session_id, "job.failed", {"job_id": jid, "failure_category": cat.value})
            raise
        self._emit(session_id, "committee.completed", {"freeze_id": fid, "evidence_ids": ev_ids})
        entry_jobs: list[JSONValue] = []
        for jid in ran:
            entry_jobs.append(jid)
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
        """Persist completed no-evidence result (empty claims) and finish trace."""
        try:
            empty: dict[str, JSONValue] = {"answer": "", "freeze_id": "", "claims": []}
            cur = self.store.get_session(session_id)
            cur = replace(cur, final_result=empty, updated_at=utcnow())
            if cur.status not in ("failed", "completed", "cancelled"):
                cur = _session.transition_session(cur, SessionStatus.COMPLETED)
            self.store.save_session(cur)
        except Exception:
            pass
        try:
            if self.trace is not None:
                self.trace.finish("no_questions:empty-wave1", "completed")
        except Exception:
            pass

    def _run_wave2(self, wave1: Wave1Result, targeted: str) -> dict[str, object] | None:
        """One targeted SEC wave: fetch E2 -> freeze -> committee; None when E2 is empty."""
        sid: str = wave1.session_id
        d1: CommitteeDisagreement | None = wave1.disagreement
        if d1 is None:
            return None
        sess = self.store.get_session(sid)
        if sess.status == SessionStatus.ANALYZING.value:
            sess = _session.transition_session(sess, SessionStatus.TARGETED_RESEARCH)
            self.store.save_session(sess)
        sess = replace(self.store.get_session(sid), current_wave=2, updated_at=utcnow())
        self.store.save_session(sess)
        self._emit(sid, "wave.started", {"wave_id": 2, "targeted_question": targeted})
        src2: str = self._open_source_job(sid, 2, targeted or self.question)
        e2: list[str] = self._fetch_wave(sid, 2, targeted or self.question, src2, "w2-")
        if not e2:
            self._emit(sid, "wave.stopped", {"reason": "no_questions:empty-wave2"})
            return None
        fid2: str = self._freeze_wave(sid, 2)
        s2, b2, r2 = self._committee_wave(sid, 2, "w2-")
        d2: CommitteeDisagreement = compute_disagreement(s2, b2, r2)
        merged: CommitteeDisagreement = _merge_disagreement(d1, d2)
        w2result = Wave1Result(
            session_id=sid, wave_id=2, freeze_id=fid2, evidence_ids=e2,
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
            sess = self.store.get_session(result.session_id)
            if sess.status == SessionStatus.ANALYZING.value:
                sess = _session.transition_session(sess, SessionStatus.SYNTHESIZING)
                self.store.save_session(sess)
            synth = synthesize_wave1(self.question, self.as_of_str or "unbounded", result)
            if synth is not None:
                self._store_final(result.session_id, synth.freeze_id, synth.answer, synth.claims)
            self._emit(result.session_id, "wave.stopped", {"reason": "complete:wave1"})
            try:
                if self.trace is not None:
                    _conclusion = synth.answer[:2000] if synth is not None and synth.answer else "complete:wave1"
                    self.trace.finish(_conclusion, "completed")
            except Exception:
                pass
            return {
                "session_id": result.session_id, "wave_id": result.wave_id,
                "freeze_id": result.freeze_id, "evidence_ids": list(result.evidence_ids),
                "dossier_id": did_out, "stock": result.stock, "bull": result.bull,
                "bear": result.bear, "disagreement": result.disagreement,
                "stop_reason": "complete:wave1", "wave2_decision": gate,
            }
        w2result_obj: object = w2["result"]
        assert isinstance(w2result_obj, Wave1Result)
        synth2 = synthesize_wave1(self.question, self.as_of_str or "unbounded", w2result_obj)
        sess2 = self.store.get_session(result.session_id)
        if sess2.status == SessionStatus.ANALYZING.value:
            sess2 = _session.transition_session(sess2, SessionStatus.SYNTHESIZING)
            self.store.save_session(sess2)
        if synth2 is not None:
            self._store_final(result.session_id, synth2.freeze_id, synth2.answer, synth2.claims)
        self._emit(result.session_id, "wave.stopped", {"reason": "complete:wave2"})
        try:
            if self.trace is not None:
                _conclusion2 = synth2.answer[:2000] if synth2 is not None and synth2.answer else "complete:wave2"
                self.trace.finish(_conclusion2, "completed")
        except Exception:
            pass
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
        sid = run._create_session(question, as_of_str, interrupt_after)
        eids = run._fetch(sid)
        if not eids:
            run._emit(sid, "wave.stopped", {"reason": "no_questions:empty-wave1"})
            run._store_empty_terminal(sid)
            return {
                "session_id": sid, "wave_id": wave_id, "freeze_id": "",
                "evidence_ids": eids, "dossier_id": run.dossier_ids[0] if run.dossier_ids else "",
                "stock": None, "bull": None, "bear": None, "disagreement": None,
                "stop_reason": "no_questions:empty-wave1",
            }
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

    deps = run._deps(interrupt_after)
    result = run_wave1(
        question, as_of_str or "unbounded", deps=deps,
        tickers=scoped, wave_id=wave_id, interrupt_after=interrupt_after,
    )
    did_out: str = run.dossier_ids[0] if run.dossier_ids else ""
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
    return {
        "session_id": result.session_id, "wave_id": result.wave_id,
        "freeze_id": result.freeze_id, "evidence_ids": list(result.evidence_ids),
        "dossier_id": did_out, "stock": None, "bull": None, "bear": None,
        "disagreement": None, "stop_reason": "no_questions:empty-wave1",
    }


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
    question: str = sess.query
    objective: str = sess.objective
    as_of_str: str = sess.as_of.isoformat() if sess.as_of is not None else ""
    as_of: str | None = as_of_str or None
    wave: int = sess.current_wave if isinstance(sess.current_wave, int) and sess.current_wave >= 1 else 1
    scoped: list[str] = []
    try:
        for job in store.list_jobs(session_id):
            if job.job_type != JobType.SOURCE_AGENT.value or job.wave_id != wave:
                continue
            diag = job.diagnostics or {}
            raw_tick: object = diag.get("tickers")
            if isinstance(raw_tick, list) and raw_tick:
                tickers = [t.strip() for t in raw_tick if isinstance(t, str) and t.strip()]
                if tickers:
                    scoped = tickers
                    break
    except Exception:
        pass
    if not scoped:
        for rec_dict in store.list_evidence(session_id):
            meta = rec_dict.get("metadata")
            if isinstance(meta, dict):
                tickers_raw = meta.get("tickers")
                if isinstance(tickers_raw, str) and tickers_raw.strip():
                    scoped = [t.strip() for t in tickers_raw.split(",") if t.strip()]
                    break
    run = _LiveRun(store, question, objective, as_of, as_of_str, scoped, dispatch, model,
                   limits, wave, actor="resume_live", provider=provider, model_name=model_name)
    try:
        from pathlib import Path as _Path
        import time as _time
        from app.config import get_data_root as _gdr
        trace_id = sess.budget.get("trace_id")
        if not isinstance(trace_id, str) or not trace_id:
            existing = list_traces(session_id)
            trace_id = existing[0].trace_id if existing else None
        if isinstance(trace_id, str) and trace_id:
            try:
                events = get_trace_events(trace_id)
                seq = len(events)
            except Exception:
                seq = 0
            try:
                root = _gdr()
            except Exception:
                root = _Path("data")
            from app.research.evals.traces import TRACE_DB_NAME as _tdb
            try:
                db_path = root / _tdb
            except Exception:
                db_path = _Path("data") / _tdb
            run.trace = TraceRecorder(trace_id=trace_id, session_id=session_id, wave_id=wave, db_path=db_path, jsonl_path=root / "traces" / f"{trace_id}.jsonl", _seq=seq, _start_perf=_time.perf_counter(), _start_iso="", _closed=False)
            run.trace_id = trace_id
            run._trace_record("trace.resumed", {"trace_id": trace_id, "session_id": session_id, "wave_id": wave})
        else:
            import subprocess as _sp
            try:
                _sha = _sp.check_output(["git", "rev-parse", "--short", "HEAD"], text=True, timeout=2).strip() or "unknown"
            except Exception:
                _sha = "unknown"
            tr = create_trace(session_id=session_id, wave_id=wave, provider=run.provider, model=run.model_name, prompt_version="v1", git_sha=_sha)
            run.trace, run.trace_id = tr, tr.trace_id
            run._trace_record("trace.opened", {"trace_id": tr.trace_id, "session_id": session_id, "resume": True})
    except Exception:
        pass
    try:
        persisted = sess.budget.get("tool_calls_used")
        if isinstance(persisted, int) and persisted >= 0:
            run.budget.hydrate(persisted)
        else:
            run.budget.hydrate(len(store.list_evidence(session_id)))
    except Exception:
        pass
    for rec_dict in store.list_evidence(session_id):
        try:
            ev = evidence_from_dict(rec_dict)
        except Exception:
            continue
        if ev.evidence_id in run.ledger:
            continue
        try:
            run.ledger.append(ev)
        except Exception:
            continue
    run.dossier_ids = []
    for d in store.list_dossiers(session_id):
        _did = d.get("dossier_id")
        if isinstance(_did, str) and _did:
            run.dossier_ids.append(_did)
    for _did in sess.dossier_ids:
        if isinstance(_did, str) and _did and _did not in run.dossier_ids:
            run.dossier_ids.append(_did)
    eids: list[str] = [e.evidence_id for e in run.ledger.list_session(session_id) if e.wave_id == wave]
    dossier_id = f"{session_id}:{wave}:sec"
    all_jobs = store.list_jobs(session_id)
    src_jobs = [j for j in all_jobs if j.job_type == JobType.SOURCE_AGENT.value and j.wave_id == wave]
    src_completed = any(j.status == "completed" for j in src_jobs)
    dossier_exists = dossier_id in set(run.dossier_ids)
    if src_completed and dossier_exists:
        pass
    else:
        reuse_job = next((j for j in src_jobs if j.status in ("queued", "running")), None)
        fetch_q = question
        if reuse_job is not None:
            if reuse_job.status == "queued":
                store.save_job(_jobs.start_job(reuse_job))
                run._emit(session_id, "job.started", {"job_id": reuse_job.job_id})
            run._trace_record("source.reused", {"job_id": reuse_job.job_id, "wave_id": wave})
            try:
                diag_q = (reuse_job.diagnostics or {}).get("question")
                if isinstance(diag_q, str) and diag_q.strip():
                    fetch_q = diag_q
            except Exception:
                pass
            src_id = reuse_job.job_id
        else:
            if wave == 2:
                try:
                    for evt in store.list_events(session_id):
                        payload = evt.to_dict().get("payload")
                        if isinstance(payload, dict) and evt.event_type == "wave.started":
                            tq = payload.get("targeted_question")
                            if isinstance(tq, str) and tq.strip():
                                fetch_q = tq
                                break
                except Exception:
                    pass
            src_id = run._open_source_job(session_id, wave, fetch_q)
        eids = run._fetch_wave(session_id, wave, fetch_q, src_id, "")
    if not eids:
        run._record_stop(session_id, "no_questions:empty-wave1")
        run._store_empty_terminal(session_id)
        return {
            "session_id": session_id, "wave_id": wave, "freeze_id": "",
            "evidence_ids": eids, "dossier_id": run.dossier_ids[0] if run.dossier_ids else "",
            "stock": None, "bull": None, "bear": None, "disagreement": None,
            "stop_reason": "no_questions:empty-wave1",
        }
    fid: str = f"{session_id}:{wave}:freeze"
    try:
        existing_freeze = store.get_freeze(fid)
        frozen_ids = existing_freeze.get("evidence_ids")
        if isinstance(frozen_ids, list) and all(isinstance(e, str) for e in frozen_ids):
            eids_for_committee: list[str] = [e for e in frozen_ids if isinstance(e, str)]
        else:
            eids_for_committee = eids
    except KeyError:
        fid = run._freeze_wave(session_id, wave)
        eids_for_committee = eids
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
