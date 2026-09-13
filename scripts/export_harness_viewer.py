#!/usr/bin/env python3
"""Export trace/eval/research SQLite projections for the read-only harness viewer (stdlib only).

Reads the authoritative ledgers (research.sqlite, research_traces.sqlite,
eval_runs.sqlite) and writes apps/harness-viewer/convex/projection.ts exporting
PROJECTION. Re-run after live runs; the viewer never writes and never migrates
the ledgers. Missing/in-progress/failed/empty states are preserved explicitly.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import get_data_root  # noqa: E402
from app.research.evals.traces import TraceHeader, get_trace_events, list_traces  # noqa: E402
from app.research.models import Job  # noqa: E402
from app.research.repository import ResearchRepository, get_research_db_path  # noqa: E402


def _research_db() -> Path:
    try:
        return get_research_db_path()
    except Exception:
        return get_data_root() / "research.sqlite"


def _all_session_ids(research_db: Path) -> list[str]:
    if not research_db.exists():
        return []
    with sqlite3.connect(research_db) as conn:
        try:
            rows = conn.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 50").fetchall()
        except sqlite3.Error:
            return []
    return [str(r[0]) for r in rows if r and r[0]]


def _ts(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _event_seq(event: dict[str, object]) -> int:
    seq = event.get("seq")
    return seq if isinstance(seq, int) else 0


def main() -> int:
    repo = ResearchRepository()
    research_db = _research_db()
    session_ids = _all_session_ids(research_db)
    research_runs: list[dict[str, object]] = []
    for sid in session_ids:
        try:
            sess = repo.get_session(sid)
        except Exception:
            continue
        jobs: list[Job] = []
        try:
            jobs = repo.list_jobs(sid)
        except Exception:
            jobs = []
        traces: list[TraceHeader] = []
        try:
            traces = list_traces(sid)
        except Exception:
            traces = []
        trace_id = traces[0].trace_id if traces else None
        trace_provider: str | None = None
        trace_model: str | None = None
        events: list[dict[str, object]] = []
        conclusion: str | None = None
        trace_status: str | None = None
        if trace_id:
            try:
                for evt in get_trace_events(trace_id):
                    events.append({"seq": evt.seq, "eventType": evt.event_type, "payload": dict(evt.payload)})
            except Exception:
                events = []
            try:
                header = traces[0]
                conclusion = header.conclusion
                trace_status = header.status
                trace_provider = header.provider
                trace_model = header.model
            except Exception:
                conclusion = None
                trace_status = None
                trace_provider = None
                trace_model = None
        evidence: list[dict[str, object]] = []
        try:
            for rec in repo.list_evidence(sid):
                evidence.append({
                    "evidenceId": str(rec.get("evidence_id", "")),
                    "subject": str(rec.get("subject", "")),
                    "knownAt": _ts(rec.get("known_at")),
                    "sourceName": str(rec.get("source_name", "")),
                    "sourceUri": _ts(rec.get("source_uri")),
                })
        except Exception:
            evidence = []
        freezes: list[dict[str, object]] = []
        try:
            for fid in sess.freeze_ids:
                try:
                    fr = repo.get_freeze(fid)
                except KeyError:
                    continue
                raw_eids: object = fr.get("evidence_ids")
                eid_list: list[str] = [e for e in raw_eids if isinstance(e, str)] if isinstance(raw_eids, list) else []
                freezes.append({"freezeId": fid, "evidenceIds": eid_list})
        except Exception:
            freezes = []
        dossiers: list[dict[str, object]] = []
        try:
            for d in repo.list_dossiers(sid):
                raw_findings: object = d.get("findings")
                clean: list[dict[str, object]] = []
                if isinstance(raw_findings, list):
                    for item in raw_findings:
                        if not isinstance(item, dict):
                            continue
                        raw_ids: object = item.get("evidence_ids")
                        id_list: list[str] = [e for e in raw_ids if isinstance(e, str)] if isinstance(raw_ids, list) else []
                        if isinstance(item.get("text"), str):
                            clean.append({"text": str(item.get("text")), "evidenceIds": id_list})
                dossiers.append({"dossierId": str(d.get("dossier_id", "")), "findings": clean})
        except Exception:
            dossiers = []
        claims: list[dict[str, object]] = []
        try:
            final = sess.final_result or {}
            raw_claims: object = final.get("claims")
            if isinstance(raw_claims, list):
                for item in raw_claims:
                    if not isinstance(item, dict):
                        continue
                    raw_cids: object = item.get("evidence_ids")
                    cid_list: list[str] = [e for e in raw_cids if isinstance(e, str)] if isinstance(raw_cids, list) else []
                    if isinstance(item.get("text"), str):
                        claims.append({"text": str(item.get("text")), "evidenceIds": cid_list})
        except Exception:
            claims = []
        job_rows: list[dict[str, object]] = []
        for job in jobs:
            diag = job.diagnostics or {}
            job_rows.append({
                "jobId": job.job_id, "parentJobId": job.parent_job_id,
                "jobType": job.job_type, "owner": job.owner, "waveId": job.wave_id,
                "status": job.status,
                "failureCategory": job.failure.category if job.failure else None,
                "failureMessage": job.failure.message if job.failure else None,
                "assignmentId": diag.get("assignment_id") if isinstance(diag.get("assignment_id"), str) else None,
                "role": diag.get("role") if isinstance(diag.get("role"), str) else None,
            })
        research_runs.append({
            "sessionId": sid, "waveId": sess.current_wave, "question": sess.query,
            "status": sess.status, "asOf": sess.as_of.isoformat() if sess.as_of is not None else None,
            "updatedAt": sess.updated_at.isoformat(),
            "traceId": trace_id, "conclusion": conclusion, "traceStatus": trace_status,
            "provider": trace_provider, "model": trace_model,
            "jobs": job_rows, "events": sorted(events, key=_event_seq),
            "claims": claims, "evidence": evidence, "freezes": freezes, "dossiers": dossiers,
            "committeeRuns": list(sess.committee_runs),
        })
    eval_runs: list[dict[str, object]] = []
    scenario_results: list[dict[str, object]] = []
    failure_records: list[dict[str, object]] = []
    experiments: list[dict[str, object]] = []
    try:
        root = get_data_root()
    except Exception:
        root = REPO_ROOT / "data"
    eval_db = root / "eval_runs.sqlite"
    if eval_db.exists():
        try:
            with sqlite3.connect(eval_db) as conn:
                for row in conn.execute("SELECT eval_run_id, model, provider, harness_version, prompt_version, git_sha, started_at, scenario_version FROM eval_runs ORDER BY started_at DESC LIMIT 20"):
                    eval_runs.append({"evalRunId": str(row[0]), "model": str(row[1]), "provider": str(row[2]), "harnessVersion": str(row[3]), "promptVersion": str(row[4]), "gitSha": str(row[5]), "startedAt": str(row[6]), "scenarioVersion": str(row[7]), "passed": 0, "failed": 0})
                for row in conn.execute("SELECT eval_run_id, scenario_name, passed, violations_json FROM eval_scenario_results ORDER BY eval_run_id, scenario_name LIMIT 200"):
                    try:
                        violations = json.loads(str(row[3]))
                    except Exception:
                        violations = [str(row[3])]
                    scenario_results.append({"evalRunId": str(row[0]), "scenarioName": str(row[1]), "passed": bool(row[2]), "violations": violations if isinstance(violations, list) else [str(violations)]})
                for row in conn.execute("SELECT failure_id, eval_run_id, scenario_name, violation FROM failure_records ORDER BY eval_run_id, scenario_name LIMIT 200"):
                    failure_records.append({"failureId": str(row[0]), "evalRunId": str(row[1]), "scenarioName": str(row[2]), "violation": str(row[3])})
        except sqlite3.Error:
            pass
    for run in eval_runs:
        results = [r for r in scenario_results if r.get("evalRunId") == run.get("evalRunId")]
        run["passed"] = sum(1 for r in results if r.get("passed"))
        run["failed"] = sum(1 for r in results if not r.get("passed"))
    projection = {
        "researchRuns": research_runs, "evalRuns": eval_runs,
        "evalScenarioResults": scenario_results, "experiments": experiments,
        "failureRecords": failure_records,
    }
    out_path = REPO_ROOT / "apps" / "harness-viewer" / "convex" / "projection.ts"
    body = (
        "import type { EvalRun, EvalScenarioResult, Experiment, FailureRecord, ResearchRun } from \"./schema\";\n"
        "export const PROJECTION: { researchRuns: ResearchRun[]; evalRuns: EvalRun[]; "
        "evalScenarioResults: EvalScenarioResult[]; experiments: Experiment[]; "
        "failureRecords: FailureRecord[] } = " + json.dumps(projection, indent=2, sort_keys=True) + ";\n"
    )
    out_path.write_text(body, encoding="utf-8")
    print(f"wrote {out_path} researchRuns={len(research_runs)} evalRuns={len(eval_runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
