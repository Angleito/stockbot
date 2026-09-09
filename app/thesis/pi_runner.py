"""Normal-Pi launcher for automated thesis monitoring (stdlib only)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from app.config import get_data_root

_EXTENSION = ".pi/extensions/stockbot.ts"

_RECORDER_GRACE_S = 15.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _done_complete(done_p: Path) -> bool:
    """True iff done.json exists with status == "completed"."""
    try:
        raw = json.loads(done_p.read_text())
        return isinstance(raw, dict) and raw.get("status") == "completed"
    except (OSError, ValueError, AttributeError):
        return False


def _run_complete(db_path: Path, run_id: str | None) -> bool:
    """True once the recorder shows completion for this run_id."""
    if not run_id:
        return False
    try:
        if not db_path.is_file():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT status FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchall()
            return bool(rows) and all((r[0] or "") == "completed" for r in rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def run_thesis_pi(*, thesis_id: str, trigger_id: str, prompt: str,
                  data_root: Path | str | None, timeout_s: int = 170, as_of: str | None = None,
                  run_id: str | None = None) -> None:
    """Launch one bounded normal-Pi run for a pending trigger.

    Success is exit 0 plus recorder completion; nonzero exit, timeout, or
    launch failure raises. Pi persists its own findings through the thesis
    tools; nothing is parsed out of stdout here.
    """
    if shutil.which("pi") is None:
        raise RuntimeError("pi launcher: 'pi' binary not found on PATH")
    if data_root is not None and str(data_root):
        db_p = Path(str(data_root)) / "runs.sqlite"
    else:
        db_p = get_data_root() / "runs.sqlite"
    db_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pi-run-"))
    out_p = tmp / "out.log"
    err_p = tmp / "err.log"
    done_p = tmp / "done.json"
    cmd = ["pi", "-p", "--no-session", "--no-builtin-tools", "--extension", _EXTENSION, "--", prompt]
    env = dict(os.environ)
    if data_root is not None and str(data_root):
        env["STOCKBOT_DATA_DIR"] = str(data_root)
    if isinstance(as_of, str) and as_of:
        env["STOCKBOT_AS_OF"] = as_of
    if run_id:
        env["STOCKBOT_RUN_ID"] = run_id
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
    # pi -p lingers after answering: the recorder DB (agent_end) is the sole
    # success verdict. done.json is liveness only: a completed done.json
    # without a completed recorder row fails closed after a short grace.
    deadline = time.monotonic() + timeout_s
    done_seen_at: float | None = None
    while time.monotonic() < deadline:
        if _run_complete(db_p, run_id):
            break
        if _done_complete(done_p):
            if done_seen_at is None:
                done_seen_at = time.monotonic()
            if time.monotonic() - done_seen_at >= _RECORDER_GRACE_S:
                break
        elif proc.poll() is not None:
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
    if _run_complete(db_p, run_id):
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    tail = err_p.read_text()[-2000:] if err_p.is_file() else ""
    if _done_complete(done_p):
        raise RuntimeError(
            f"pi launcher: done.json completed without recorder completion on trigger {trigger_id!r}"
            f" (thesis {thesis_id!r}); durable run record missing; failing closed (logs: {tmp})")
    if rc is not None and rc != 0:
        raise RuntimeError(
            f"pi launcher: exit {rc} on trigger {trigger_id!r}"
            f" (thesis {thesis_id!r}): {tail} (logs: {tmp})")
    raise RuntimeError(
        f"pi launcher: timed out after {timeout_s}s on trigger {trigger_id!r}"
        f" (thesis {thesis_id!r}): {tail} (logs: {tmp})")
