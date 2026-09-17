"""Normal-Pi launcher for automated thesis monitoring (stdlib only)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
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
    except OSError, ValueError, AttributeError:
        return False


def _run_rows(db_path: Path, run_id: str) -> list[tuple[object, ...]] | None:
    """Recorder status rows for one run; None when the DB is missing."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT status FROM agent_runs WHERE run_id = ?", (run_id,)).fetchall()
    finally:
        conn.close()


def _run_complete(db_path: Path, run_id: str | None) -> bool:
    """True once the recorder shows completion for this run_id."""
    if not run_id:
        return False
    try:
        rows = _run_rows(db_path, run_id)
    except sqlite3.Error:
        return False
    return rows is not None and bool(rows) and all((r[0] or "") == "completed" for r in rows)


@dataclass(frozen=True)
class _PiLaunch:
    """Ready-to-spawn Pi run: command, env, log paths, and recorder DB."""

    cmd: list[str]
    env: dict[str, str]
    tmp: Path
    out_p: Path
    err_p: Path
    done_p: Path
    db_p: Path


def _pi_db_path(data_root: Path | str | None) -> Path:
    """Recorder DB path for a data root (default root when unset)."""
    if data_root is not None and str(data_root):
        db_p = Path(str(data_root)) / "runs.sqlite"
    else:
        db_p = get_data_root() / "runs.sqlite"
    db_p.parent.mkdir(parents=True, exist_ok=True)
    return db_p


def _pi_cmd(prompt: str) -> list[str]:
    """Pi CLI argv with optional provider/model flags."""
    provider = os.environ.get("STOCKBOT_PI_PROVIDER", "").strip()
    model = os.environ.get("STOCKBOT_PI_MODEL", "").strip()
    flags = (["--provider", provider] if provider else []) + (["--model", model] if model else [])
    return [
        "pi",
        "-p",
        "--no-session",
        "--no-builtin-tools",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--extension",
        _EXTENSION,
        *flags,
        "--",
        prompt,
    ]


def _pi_data_env(env: dict[str, str], data_root: Path | str | None) -> None:
    """Overlay the data-dir when set."""
    if data_root is not None and str(data_root):
        env["STOCKBOT_DATA_DIR"] = str(data_root)


def _pi_run_env(env: dict[str, str], as_of: str | None, run_id: str | None) -> None:
    """Overlay as_of/run selectors when set."""
    if isinstance(as_of, str) and as_of:
        env["STOCKBOT_AS_OF"] = as_of
    if run_id:
        env["STOCKBOT_RUN_ID"] = run_id


def _pi_env(
    data_root: Path | str | None, as_of: str | None, run_id: str | None, done_p: Path, db_p: Path
) -> dict[str, str]:
    """Child env for one Pi run."""
    env = dict(os.environ)
    _pi_data_env(env, data_root)
    _pi_run_env(env, as_of, run_id)
    env["STOCKBOT_DONE_FILE"] = str(done_p)
    env["RUNS_DB_PATH"] = str(db_p)
    return env


def _prepare_launch(prompt: str, data_root: Path | str | None, as_of: str | None, run_id: str | None) -> _PiLaunch:
    """Gate the pi binary, lay out tmp/log paths, command, and env."""
    if shutil.which("pi") is None:
        raise RuntimeError("pi launcher: 'pi' binary not found on PATH")
    db_p = _pi_db_path(data_root)
    tmp = Path(tempfile.mkdtemp(prefix="pi-run-"))
    done_p = tmp / "done.json"
    return _PiLaunch(
        cmd=_pi_cmd(prompt),
        env=_pi_env(data_root, as_of, run_id, done_p, db_p),
        tmp=tmp,
        out_p=tmp / "out.log",
        err_p=tmp / "err.log",
        done_p=done_p,
        db_p=db_p,
    )


def _spawn_pi(launch: _PiLaunch, trigger_id: str) -> subprocess.Popen[bytes]:
    """Spawn the Pi child with logs plumbed; file handles close immediately."""
    with contextlib.ExitStack() as stack:
        try:
            out_f = stack.enter_context(open(launch.out_p, "w"))
            err_f = stack.enter_context(open(launch.err_p, "w"))
        except OSError as exc:
            raise RuntimeError(f"pi launcher: cannot open logs for trigger {trigger_id!r}: {exc}") from exc
        try:
            return subprocess.Popen(
                launch.cmd,
                stdout=out_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                env=launch.env,
                cwd=str(_repo_root()),
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"pi launcher: cannot start pi for trigger {trigger_id!r}: {exc}") from exc


def _await_loop(launch: _PiLaunch, proc: subprocess.Popen[bytes], run_id: str | None, deadline: float) -> None:
    """Wait steps until the deadline or the stop sentinel."""
    done_seen_at: float | None = None
    while time.monotonic() < deadline:
        done_seen_at = _wait_step(launch, proc, run_id, deadline, done_seen_at)
        if done_seen_at is not None and done_seen_at < 0:
            break


def _await_pi(launch: _PiLaunch, proc: subprocess.Popen[bytes], run_id: str | None, timeout_s: int) -> None:
    """Wait for recorder completion, done.json grace, or child exit (then reap)."""
    # pi -p lingers after answering: the recorder DB (agent_end) is the sole
    # success verdict. done.json is liveness only: a completed done.json
    # without a completed recorder row fails closed after a short grace.
    _await_loop(launch, proc, run_id, time.monotonic() + timeout_s)
    _reap_pi(proc)


def _wait_step(
    launch: _PiLaunch, proc: subprocess.Popen[bytes], run_id: str | None, deadline: float, done_seen_at: float | None
) -> float | None:
    """One wait-loop step; negative sentinel means stop waiting."""
    if _run_complete(launch.db_p, run_id):
        return -1.0
    if _done_complete(launch.done_p):
        return _done_step(done_seen_at)
    if proc.poll() is not None:
        return -1.0
    time.sleep(min(2.0, max(0.05, deadline - time.monotonic())))
    return done_seen_at


def _done_step(done_seen_at: float | None) -> float:
    """Done.json grace step; negative sentinel once the grace expires."""
    now = time.monotonic()
    if done_seen_at is None:
        return now
    return -1.0 if now - done_seen_at >= _RECORDER_GRACE_S else done_seen_at


def _kill_lingering(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL a lingering child; lookup races are cleanup, never failure."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError, PermissionError:
        pass


def _wait_reaped(proc: subprocess.Popen[bytes]) -> None:
    """Reap the child; wait races are cleanup, never failure."""
    try:
        proc.wait(timeout=15)
    except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
        pass


def _reap_pi(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL a lingering child; linger cleanup, never failure."""
    if proc.poll() is None:
        _kill_lingering(proc)
        _wait_reaped(proc)


def _verdict(
    launch: _PiLaunch,
    proc: subprocess.Popen[bytes],
    thesis_id: str,
    trigger_id: str,
    run_id: str | None,
    timeout_s: int,
) -> None:
    """Recorder completion is the verdict; every other outcome raises."""
    # Recorder completion is the verdict; the SIGKILL above is linger cleanup
    # (pi -p stays alive after answering), never failure.
    if _run_complete(launch.db_p, run_id):
        shutil.rmtree(launch.tmp, ignore_errors=True)
        return
    tail = launch.err_p.read_text()[-2000:] if launch.err_p.is_file() else ""
    _raise_unrecorded(launch, thesis_id, trigger_id, tail)
    _raise_exit(launch, proc, thesis_id, trigger_id, tail)
    raise RuntimeError(
        f"pi launcher: timed out after {timeout_s}s on trigger {trigger_id!r}"
        f" (thesis {thesis_id!r}): {tail} (logs: {launch.tmp})"
    )


def _raise_unrecorded(launch: _PiLaunch, thesis_id: str, trigger_id: str, tail: str) -> None:
    """Fail closed when done.json completed without a recorder row."""
    _ = tail
    if _done_complete(launch.done_p):
        raise RuntimeError(
            f"pi launcher: done.json completed without recorder completion on trigger {trigger_id!r}"
            f" (thesis {thesis_id!r}); durable run record missing; failing closed (logs: {launch.tmp})"
        )


def _raise_exit(launch: _PiLaunch, proc: subprocess.Popen[bytes], thesis_id: str, trigger_id: str, tail: str) -> None:
    """Fail closed on a nonzero child exit."""
    rc = proc.poll()
    if rc is not None and rc != 0:
        raise RuntimeError(
            f"pi launcher: exit {rc} on trigger {trigger_id!r} (thesis {thesis_id!r}): {tail} (logs: {launch.tmp})"
        )


def run_thesis_pi(
    *,
    thesis_id: str,
    trigger_id: str,
    prompt: str,
    data_root: Path | str | None,
    timeout_s: int = 170,
    as_of: str | None = None,
    run_id: str | None = None,
) -> None:
    """Launch one bounded normal-Pi run for a pending trigger.

    Success is exit 0 plus recorder completion; nonzero exit, timeout, or
    launch failure raises. Pi persists its own findings through the thesis
    tools; nothing is parsed out of stdout here.
    """
    launch = _prepare_launch(prompt, data_root, as_of, run_id)
    proc = _spawn_pi(launch, trigger_id)
    _await_pi(launch, proc, run_id, timeout_s)
    _verdict(launch, proc, thesis_id, trigger_id, run_id, timeout_s)
