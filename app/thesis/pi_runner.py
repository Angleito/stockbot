"""Normal-Pi launcher for automated thesis monitoring (stdlib only)."""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

_EXTENSION = ".pi/extensions/stockbot.ts"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _db_terminal(db_path: Path) -> bool:
    """True once the recorder shows a completed run (agent_end processed)."""
    # Same check as scripts/verify_pi_tools.db_terminal; copied so app code
    # does not import from scripts/.
    try:
        if not db_path.is_file():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute("SELECT status FROM agent_runs").fetchall()
            return bool(rows) and all((r[0] or "") == "completed" for r in rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def run_thesis_pi(*, thesis_id: str, trigger_id: str, prompt: str,
                  data_root: Path | str | None, timeout_s: int = 170, as_of: str | None = None) -> None:
    """Launch one bounded normal-Pi run for a pending trigger.

    Success is exit 0 plus recorder completion; nonzero exit, timeout, or
    launch failure raises. Pi persists its own findings through the thesis
    tools; nothing is parsed out of stdout here.
    """
    if shutil.which("pi") is None:
        raise RuntimeError("pi launcher: 'pi' binary not found on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="pi-run-"))
    out_p = tmp / "out.log"
    err_p = tmp / "err.log"
    db_p = tmp / "run.db"
    done_p = tmp / "done.json"
    cmd = ["pi", "-p", "--no-session", "--extension", _EXTENSION, "--", prompt]
    env = dict(os.environ)
    if data_root is not None and str(data_root):
        env["STOCKBOT_DATA_DIR"] = str(data_root)
    if isinstance(as_of, str) and as_of:
        env["STOCKBOT_AS_OF"] = as_of
    env["STOCKBOT_DONE_FILE"] = str(done_p)
    env["RUNS_DB_PATH"] = str(db_p)
    try:
        out_f = open(out_p, "w")
        err_f = open(err_p, "w")
    except OSError as exc:
        raise RuntimeError(
            f"pi launcher: cannot open logs for trigger {trigger_id!r}: {exc}") from exc
    try:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f,
                                stdin=subprocess.DEVNULL, env=env,
                                cwd=str(_repo_root()), start_new_session=True)
    except OSError as exc:
        raise RuntimeError(
            f"pi launcher: cannot start pi for trigger {trigger_id!r}: {exc}") from exc
    finally:
        out_f.close()
        err_f.close()
    # pi -p lingers after answering: completion comes from the recorder DB
    # (agent_end), never from process exit alone.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _db_terminal(db_p):
            break
        if proc.poll() is not None:
            break
        time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
    rc = proc.poll()
    # Recorder completion is the verdict; the SIGKILL above is linger cleanup
    # (pi -p stays alive after answering), never failure.
    if _db_terminal(db_p):
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    tail = err_p.read_text()[-2000:] if err_p.is_file() else ""
    if rc is not None and rc != 0:
        raise RuntimeError(
            f"pi launcher: exit {rc} on trigger {trigger_id!r}"
            f" (thesis {thesis_id!r}): {tail} (logs: {tmp})")
    raise RuntimeError(
        f"pi launcher: timed out after {timeout_s}s on trigger {trigger_id!r}"
        f" (thesis {thesis_id!r}): {tail} (logs: {tmp})")
