"""Forced Pi-timeout closure: FAILED/TIMEOUT persisted, resume stable with no dup."""
from pathlib import Path
import subprocess

import pytest

from app.research.repository import ResearchRepository
from app.research.runner import LiveModelError, run_live


def test_timeout_closes_failed_and_resume_has_no_dup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESEARCH_DB_PATH", str(tmp_path / "research.sqlite"))

    def _timeout_model(_prompt: str) -> str:
        raise subprocess.TimeoutExpired(cmd="pi", timeout=1)

    with pytest.raises(LiveModelError) as excinfo:
        run_live("timeout probe?", "probe", None, ["NVDA"], lambda n, a: {}, _timeout_model)
    repo = ResearchRepository()
    sid = excinfo.value.session_id
    assert repo.get_session(sid).status == "failed"
    jobs = repo.list_jobs(sid)
    assert len(jobs) == 1 and jobs[0].status == "failed"
    assert jobs[0].failure is not None and jobs[0].failure.category == "timeout"
    kinds = [e.event_type for e in repo.list_events(sid)]
    assert "job.failed" in kinds and "model.failed" in kinds
    assert "research.failed" in kinds and "wave.stopped" in kinds
    assert repo.resume(sid).session.status == "failed"
    assert repo.resume(sid).open_job_ids == []
    assert len(repo.list_jobs(sid)) == 1
