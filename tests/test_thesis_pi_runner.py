"""Launcher contract for the normal-Pi thesis runner (no `pi` binary needed)."""

import sqlite3
import subprocess

from pathlib import Path

import pytest

import app.thesis.pi_runner as pi_runner
from app.thesis.pi_runner import _db_terminal, run_thesis_pi


def test_db_terminal(tmp_path: Path) -> None:
    db = tmp_path / "run.db"
    assert _db_terminal(db) is False
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE agent_runs (status TEXT)")
    conn.execute("INSERT INTO agent_runs VALUES ('running')")
    conn.commit()
    conn.close()
    assert _db_terminal(db) is False
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE agent_runs SET status='completed'")
    conn.commit()
    conn.close()
    assert _db_terminal(db) is True


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
            if mode in ("ok", "linger"):
                db_path = env.get("RUNS_DB_PATH")
                assert isinstance(db_path, (str, Path))
                conn = sqlite3.connect(str(db_path))
                conn.execute("CREATE TABLE agent_runs (status TEXT)")
                conn.execute("INSERT INTO agent_runs VALUES ('completed')")
                conn.commit()
                conn.close()
            # ponytail: absurd pid can never exist, so the linger SIGKILL is a
            # no-op ProcessLookupError instead of a real signal.
            self.pid, self.returncode = 2**30, (0 if mode == "ok" else 1)
            self._mode = mode

        def poll(self) -> int | None:
            return None if self._mode in ("hang", "linger") else self.returncode

        def wait(self, timeout: float | None = None) -> int | None:
            return None if self._mode in ("hang", "linger") else self.returncode

    return _FakeProc


def _run(monkeypatch: pytest.MonkeyPatch, prompt: str, mode: str = "ok", timeout_s: int = 170) -> dict[str, object]:
    fake = _fake_proc(mode)
    def _which(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which)
    monkeypatch.setattr(subprocess, "Popen", fake)
    out = run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1", prompt=prompt,
                        data_root="/tmp/root", timeout_s=timeout_s)
    assert out is None
    return _as_dict(fake.captured)


def test_builds_canonical_command_without_tool_restrictions(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _run(monkeypatch, "Do research")
    cmd = _as_list(cap["cmd"])
    assert cmd[:6] == ["pi", "-p", "--no-session", "--extension",
                       ".pi/extensions/stockbot.ts", "--"]
    assert cmd[6] == "Do research"
    assert "--tools" not in cmd
    assert "--no-builtin-tools" not in cmd
    assert cap["cwd"] == str(pi_runner._repo_root())


def test_binds_env_not_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _run(monkeypatch, "Do research")
    env = _as_dict(cap["env"])
    done_file = env["STOCKBOT_DONE_FILE"]
    assert isinstance(done_file, str)
    assert done_file.endswith("done.json")
    assert env["STOCKBOT_DATA_DIR"] == "/tmp/root"
    runs_db = env["RUNS_DB_PATH"]
    assert isinstance(runs_db, str)
    assert runs_db.endswith("run.db")


def test_raises_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_proc("fail")
    def _which_ok(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which_ok)
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="exit 1"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root")


def test_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_proc("hang")
    def _which_ok2(_name: str) -> str | None:
        return "/bin/pi"
    monkeypatch.setattr(pi_runner.shutil, "which", _which_ok2)
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="timed out"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root", timeout_s=1)


def test_raises_without_pi_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which_missing(_name: str) -> str | None:
        return None
    monkeypatch.setattr(pi_runner.shutil, "which", _which_missing)
    with pytest.raises(RuntimeError, match="not found"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root")

def test_lingering_pi_after_completion_counts_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # pi -p stays alive after answering; the SIGKILL is linger cleanup.
    _run(monkeypatch, "Do research", mode="linger", timeout_s=5)
