"""Protected evaluation-trace boundary: full worker records, not prose previews.

Focused on app/research/kernel_worker.py _run: the live evaluator (a sibling
module) needs the complete sanitized worker/session record, while the normal
prose path keeps its preview contracts. No network, no model calls.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from app.research import kernel_worker
from app.research.models import JSONValue

_AS_OF = "2025-06-30T00:00:00+00:00"


def _seed_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r.sqlite"))
    from app.research import service
    from app.research.journal import append_event
    from app.research.models import default_policy
    from app.research.repository import ResearchRepository

    policy = default_policy()
    policy["research_sources"] = {"mode": "allowlist", "sources": ["SEC", "FINRA"]}
    sid = service.create_research("seed question?", "seed question?", as_of=_AS_OF, policy=policy)
    store = ResearchRepository()
    fin = str(service.start_job(sid, "source_agent", source="FINRA", repo=store, wave_id=1)["job_id"])
    marker = "seed1"
    passage = f"NVDA short interest 12345 shares {marker}"
    service.persist_tool_result(
        sid,
        fin,
        "query_finra",
        f"{sid}:tr:seed1",
        {
            "records": [{"symbol": "NVDA", "shortInterest": 12345, "marker": marker}],
            "briefing": passage,
            "published_at": "2025-06-20",
        },
        repo=store,
    )
    service.record_evidence(
        sid,
        fin,
        {
            "evidence_id": f"{sid}:ev:1",
            "content": passage,
            "claim_text": passage,
            "subject": "NVDA",
            "source_name": "FINRA",
            "tool_result_id": f"{sid}:tr:seed1",
            "matching_passage": passage,
        },
        repo=store,
    )
    store.save_dossier(
        {
            "dossier_id": f"{sid}:d:1",
            "session_id": sid,
            "wave_id": 1,
            "subject": "seed",
            "coverage": {"entities": [], "sources_examined": ["SEC"], "complete": False},
            "findings": [{"text": "revenue grew", "evidence_ids": ["ev-1"], "claim_type": "observed_fact"}],
            "unknowns": ["open q"],
            "limitations": ["thin evidence"],
        }
    )
    store.save_coverage_artifact(
        {
            "artifact_id": f"{sid}:cov:1",
            "session_id": sid,
            "job_id": fin,
            "wave_id": 1,
            "claim_kind": "absence_observation",
            "claim_text": "no disclosure located",
            "search_id": "s1",
            "query": "seed query",
            "coverage": {"sources_examined": ["SEC"], "complete": True},
        }
    )
    store.save_event(append_event(sid, "test.seeded", "kernel-worker-test", "kernel-worker-test", {"note": "seed"}))
    return sid


def _run_ok(
    monkeypatch: pytest.MonkeyPatch,
    sid: str,
    req: dict[str, object],
    *,
    patch_graph: bool = True,
) -> dict[str, JSONValue]:
    """Run _run against the seeded session: no model calls, no network."""
    import app.research.scheduler as sched

    async def _ok(_sid: str) -> dict[str, object]:
        return {"session_id": _sid, "status": "complete", "nodes": []}

    monkeypatch.setattr(sched, "run", _ok)
    if patch_graph:

        def _fake_graph(objective: str, as_of: object = None) -> str:
            return sid

        monkeypatch.setattr(kernel_worker, "run_graph_prompt", _fake_graph)
    out = kernel_worker._run(req)  # type: ignore[arg-type]
    assert isinstance(out, dict)
    return out


def test_trace_carries_full_records_and_asof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _seed_session(monkeypatch, tmp_path)
    out = _run_ok(monkeypatch, sid, {"id": "r1", "op": "run", "prompt": "seed question?", "asOf": _AS_OF})
    assert out["sessionId"] == sid
    assert out["asOf"] == _AS_OF
    assert isinstance(out["session"], dict) and out["session"]["session_id"] == sid
    assert isinstance(out["evidenceRecords"], list) and out["evidenceRecords"]
    rec = out["evidenceRecords"][0]
    assert isinstance(rec, dict) and rec.get("evidence_id")
    assert "provenance" in rec and "claim_kind" in rec
    assert isinstance(out["nodeRecords"], list)
    assert isinstance(out["jobs"], list) and out["jobs"]
    assert isinstance(out["events"], list) and any(
        isinstance(e, dict) and e.get("event_type") == "test.seeded" for e in out["events"]
    )
    assert isinstance(out["dossiers"], list) and out["dossiers"]
    dossier = out["dossiers"][0]
    assert isinstance(dossier, dict)
    findings = dossier.get("findings")
    assert isinstance(findings, list) and findings
    first_finding = findings[0]
    assert isinstance(first_finding, dict) and first_finding.get("claim_type") == "observed_fact"
    assert isinstance(out["coverageArtifacts"], list) and out["coverageArtifacts"]
    assert isinstance(out["toolResults"], list) and out["toolResults"]
    assert isinstance(out["attempts"], list)


def test_prose_preview_fields_stay_lean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _seed_session(monkeypatch, tmp_path)
    out = _run_ok(monkeypatch, sid, {"id": "r1", "op": "run", "prompt": "seed question?"})
    assert isinstance(out["evidence"], list)
    for row in out["evidence"]:
        assert isinstance(row, Mapping) and set(row.keys()) == {"id", "content"}


def test_asof_non_string_stays_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.research import service

    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "r2.sqlite"))
    sid = service.create_research("q?", "q?")
    seen: list[object] = []

    def _capture(_objective: str, _as_of: object = None) -> str:
        seen.append(_as_of)
        return sid

    monkeypatch.setattr(kernel_worker, "run_graph_prompt", _capture)
    out = _run_ok(monkeypatch, sid, {"id": "r1", "op": "run", "prompt": "q?", "asOf": 42}, patch_graph=False)
    assert out["asOf"] is None
    assert seen and seen[0] is None
    assert sid


def test_run_result_json_serializable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _seed_session(monkeypatch, tmp_path)
    out = _run_ok(monkeypatch, sid, {"id": "r1", "op": "run", "prompt": "seed question?", "asOf": _AS_OF})
    json.dumps(out, default=str)
