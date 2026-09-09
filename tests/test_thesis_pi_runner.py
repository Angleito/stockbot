"""Launcher contract for the normal-Pi thesis runner (no `pi` binary needed)."""

import json
import sqlite3
import subprocess

from pathlib import Path

import pytest

import app.thesis.pi_runner as pi_runner
from app.thesis.pi_runner import _done_complete, _run_complete, run_thesis_pi


def _which_pi(_name: str) -> str | None:
    return "/bin/pi"


def test_completion_signals(tmp_path: Path) -> None:
    db = tmp_path / "runs.sqlite"
    done = tmp_path / "done.json"
    assert _run_complete(db, "run:x") is False
    assert _done_complete(done) is False
    done.write_text(json.dumps({"status": "running"}))
    assert _done_complete(done) is False
    done.write_text(json.dumps({"status": "completed", "answer": "ok"}))
    assert _done_complete(done) is True
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE agent_runs (run_id TEXT, status TEXT)")
    conn.execute("INSERT INTO agent_runs VALUES ('run:x', 'completed')")
    conn.execute("INSERT INTO agent_runs VALUES ('run:other', 'completed')")
    conn.commit()
    conn.close()
    assert _run_complete(db, "run:x") is True
    assert _run_complete(db, "run:missing") is False
    assert _run_complete(db, None) is False


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _fake_proc(mode: str) -> type:
    class _FakeProc:
        captured: dict[str, object] = {}

        def __init__(self, cmd: list[str], **kw: object) -> None:
            env = kw.get("env")
            assert isinstance(env, dict)
            type(self).captured = {"cmd": cmd, "env": env, "cwd": kw.get("cwd")}
            if mode in ("ok", "linger", "done_only", "done_failed_row"):
                done_file = env.get("STOCKBOT_DONE_FILE")
                assert isinstance(done_file, str)
                done_p = Path(done_file)
                done_p.parent.mkdir(parents=True, exist_ok=True)
                done_p.write_text(json.dumps({"status": "completed", "answer": "ok"}))
                # done_only writes no recorder row; done_failed_row writes a failed row.
                status = {"ok": "completed", "linger": "completed",
                          "done_failed_row": "failed"}.get(mode)
                if status is not None:
                    db_path = env.get("RUNS_DB_PATH")
                    assert isinstance(db_path, (str, Path))
                    db_p = Path(str(db_path))
                    db_p.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(str(db_p))
                    conn.execute("CREATE TABLE IF NOT EXISTS agent_runs (run_id TEXT, status TEXT)")
                    passed_run_id = env.get("STOCKBOT_RUN_ID")
                    if isinstance(passed_run_id, str) and passed_run_id:
                        conn.execute("INSERT INTO agent_runs VALUES (?, ?)", (passed_run_id, status))
                    conn.commit()
                    conn.close()
            # ponytail: absurd pid can never exist, so the linger SIGKILL is a
            # no-op ProcessLookupError instead of a real signal.
            self.pid, self.returncode = 2**30, (0 if mode in ("ok", "done_only", "done_failed_row") else 1)
            self._mode = mode

        def poll(self) -> int | None:
            return None if self._mode in ("hang", "linger") else self.returncode

        def wait(self, timeout: float | None = None) -> int | None:
            return None if self._mode in ("hang", "linger") else self.returncode

    return _FakeProc

def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prompt: str, mode: str = "ok", timeout_s: int = 170,
         run_id: str | None = "run:test", data_root: Path | None = None) -> dict[str, object]:
    fake = _fake_proc(mode)
    def _which(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which)
    monkeypatch.setattr(subprocess, "Popen", fake)
    root = data_root if data_root is not None else tmp_path / "data"
    out = run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1", prompt=prompt,
                        data_root=root, timeout_s=timeout_s, run_id=run_id)
    assert out is None
    return _as_dict(fake.captured)


def test_builds_canonical_command_with_tool_restrictions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cap = _run(monkeypatch, tmp_path, "Do research")
    cmd = _as_list(cap["cmd"])
    assert cmd[:7] == ["pi", "-p", "--no-session", "--no-builtin-tools", "--extension",
                       ".pi/extensions/stockbot.ts", "--"]
    assert cmd[7] == "Do research"
    assert "--tools" not in cmd
    assert "--no-builtin-tools" in cmd
    assert cap["cwd"] == str(pi_runner._repo_root())


def test_binds_env_not_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cap = _run(monkeypatch, tmp_path, "Do research")
    env = _as_dict(cap["env"])
    done_file = env["STOCKBOT_DONE_FILE"]
    assert isinstance(done_file, str)
    assert done_file.endswith("done.json")
    assert env["STOCKBOT_DATA_DIR"] == str(tmp_path / "data")
    runs_db = env["RUNS_DB_PATH"]
    assert isinstance(runs_db, str)
    assert runs_db.endswith("runs.sqlite")
    assert env["STOCKBOT_RUN_ID"] == "run:test"


def test_no_run_id_leaves_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Fail closed: without a run_id no recorder row can correlate, so even a
    # completed done.json is a failure, not success.
    fake = _fake_proc("ok")
    monkeypatch.setattr(pi_runner.shutil, "which", _which_pi)
    monkeypatch.setattr(subprocess, "Popen", fake)
    monkeypatch.setattr(pi_runner, "_RECORDER_GRACE_S", 0.0)
    with pytest.raises(RuntimeError, match="without recorder"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=tmp_path / "data", run_id=None)
    env = _as_dict(_as_dict(fake.captured)["env"])
    assert "STOCKBOT_RUN_ID" not in env


def test_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proc("fail")
    def _which_ok(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which_ok)
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="exit 1"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=tmp_path / "data", run_id="run:test")


def test_raises_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _fake_proc("hang")
    def _which_ok2(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which_ok2)
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="timed out"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=tmp_path / "data", timeout_s=1, run_id="run:test")


def test_raises_without_pi_binary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _which_missing(_name: str) -> str | None:
        return None
    monkeypatch.setattr(pi_runner.shutil, "which", _which_missing)
    with pytest.raises(RuntimeError, match="not found"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=tmp_path / "data")

def test_lingering_pi_after_completion_counts_as_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # pi -p stays alive after answering; the SIGKILL is linger cleanup.
    _run(monkeypatch, tmp_path, "Do research", mode="linger", timeout_s=5)


def test_failed_run_after_completed_run_still_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    _run(monkeypatch, tmp_path, "Do research", mode="ok", run_id="run:one", data_root=data_root)

    fake = _fake_proc("fail")
    def _which_ok(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which_ok)
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="exit 1"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=data_root, run_id="run:two")


@pytest.mark.parametrize("mode", ["done_only", "done_failed_row"])
def test_done_completed_without_recorder_is_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    # Regression: completed done.json with an absent (done_only) or failed
    # (done_failed_row) recorder row must fail, never count as success.
    fake = _fake_proc(mode)
    monkeypatch.setattr(pi_runner.shutil, "which", _which_pi)
    monkeypatch.setattr(subprocess, "Popen", fake)
    monkeypatch.setattr(pi_runner, "_RECORDER_GRACE_S", 0.0)
    with pytest.raises(RuntimeError, match="without recorder|durable"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root=tmp_path / "data", run_id="run:test")
