"""Launcher contract for the normal-Pi thesis runner (no `pi` binary needed)."""

import sqlite3
import subprocess

import pytest

import app.thesis.pi_runner as pi_runner
from app.thesis.pi_runner import _db_terminal, run_thesis_pi


def test_db_terminal(tmp_path):
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


def _fake_proc(mode):
    class _FakeProc:
        captured: dict = {}

        def __init__(self, cmd, **kw):
            type(self).captured = {"cmd": cmd, "env": kw.get("env", {}), "cwd": kw.get("cwd")}
            if mode in ("ok", "linger"):
                conn = sqlite3.connect(str(kw["env"]["RUNS_DB_PATH"]))
                conn.execute("CREATE TABLE agent_runs (status TEXT)")
                conn.execute("INSERT INTO agent_runs VALUES ('completed')")
                conn.commit()
                conn.close()
            # ponytail: absurd pid can never exist, so the linger SIGKILL is a
            # no-op ProcessLookupError instead of a real signal.
            self.pid, self.returncode = 2**30, (0 if mode == "ok" else 1)
            self._mode = mode

        def poll(self):
            return None if self._mode in ("hang", "linger") else self.returncode

        def wait(self, timeout=None):
            return None if self._mode in ("hang", "linger") else self.returncode

    return _FakeProc


def _run(monkeypatch, prompt, mode="ok", **kw):
    fake = _fake_proc(mode)
    monkeypatch.setattr(pi_runner.shutil, "which", lambda _name: "/bin/pi")
    monkeypatch.setattr(subprocess, "Popen", fake)
    kw.setdefault("thesis_id", "thesis:t")
    kw.setdefault("trigger_id", "trigger:1")
    kw.setdefault("prompt", prompt)
    kw.setdefault("data_root", "/tmp/root")
    out = run_thesis_pi(**kw)
    assert out is None
    return fake.captured


def test_builds_canonical_command_without_tool_restrictions(monkeypatch):
    cap = _run(monkeypatch, "Do research")
    assert cap["cmd"][:6] == ["pi", "-p", "--no-session", "--extension",
                              ".pi/extensions/stockbot.ts", "--"]
    assert cap["cmd"][6] == "Do research"
    assert "--tools" not in cap["cmd"]
    assert "--no-builtin-tools" not in cap["cmd"]
    assert cap["cwd"] == str(pi_runner._repo_root())


def test_binds_env_not_prompt(monkeypatch):
    cap = _run(monkeypatch, "Do research")
    assert cap["env"]["STOCKBOT_DONE_FILE"].endswith("done.json")
    assert cap["env"]["STOCKBOT_DATA_DIR"] == "/tmp/root"
    assert cap["env"]["RUNS_DB_PATH"].endswith("run.db")


def test_raises_on_nonzero_exit(monkeypatch):
    fake = _fake_proc("fail")
    monkeypatch.setattr(pi_runner.shutil, "which", lambda _name: "/bin/pi")
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="exit 1"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root")


def test_raises_on_timeout(monkeypatch):
    fake = _fake_proc("hang")
    monkeypatch.setattr(pi_runner.shutil, "which", lambda _name: "/bin/pi")
    monkeypatch.setattr(subprocess, "Popen", fake)
    with pytest.raises(RuntimeError, match="timed out"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root", timeout_s=1)


def test_raises_without_pi_binary(monkeypatch):
    monkeypatch.setattr(pi_runner.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="not found"):
        run_thesis_pi(thesis_id="thesis:t", trigger_id="trigger:1",
                       prompt="Do research", data_root="/tmp/root")

def test_lingering_pi_after_completion_counts_as_success(monkeypatch):
    # pi -p stays alive after answering; the SIGKILL is linger cleanup.
    _run(monkeypatch, "Do research", mode="linger", timeout_s=5)
