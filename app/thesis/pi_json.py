"""Pi-backed JSON gateway for thesis intake and research (stdlib only)."""

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

_EXTENSION = ".pi/extensions/stockbot.ts"
_TIMEOUT_S = 170


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _extract_largest(text: str) -> dict:
    """Parse whole text, else the largest {...} block that decodes to a dict."""
    s = text.strip()
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    best: dict | None = None
    depth = 0
    start = -1
    in_str = False
    esc = False
    spans: list[tuple[int, int]] = []
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append((start, i + 1))
                    start = -1
    for a, b in sorted(spans, key=lambda sp: sp[1] - sp[0], reverse=True):
        try:
            data = json.loads(s[a:b])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("pi gateway: stdout contained no JSON object")


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


def _read_done(done_path: Path) -> str | None:
    """Done-sentinel answer when the file holds valid JSON {status, answer}."""
    try:
        if not done_path.is_file():
            return None
        data = json.loads(done_path.read_text())
    except (OSError, ValueError):
        return None  # tolerate partial writes; keep polling
    if not isinstance(data, dict) or "status" not in data or "answer" not in data:
        return None
    answer = data["answer"]
    if isinstance(answer, str):
        return answer
    return json.dumps(answer)


def _run_pi(prompt: str, *, request_context=None, tools: list | None = None) -> str:
    if shutil.which("pi") is None:
        raise RuntimeError("pi gateway: 'pi' binary not found on PATH")
    names = [t.get("function", {}).get("name") for t in (tools or [])]
    names = [n for n in names if isinstance(n, str) and n]
    data_root = getattr(request_context, "data_root", None)
    as_of = getattr(request_context, "as_of", None)
    tmp = Path(tempfile.mkdtemp(prefix="pi-json-"))
    out_p = tmp / "out.log"
    err_p = tmp / "err.log"
    db_p = tmp / "run.db"
    done_p = tmp / "done.json"
    prefix = ""
    if data_root is not None and str(data_root):
        prefix += f"STOCKBOT_DATA_ROOT={data_root}\n"
    prefix += f"STOCKBOT_DONE_FILE={done_p}\n"
    if as_of:
        prefix += f"Point-in-time: only use data known at as_of={as_of}. "
    if names:
        prefix += f"You may call only these Stockbot tools: {', '.join(sorted(set(names)))}. "
    if data_root is not None and str(data_root):
        prefix += "The STOCKBOT_DATA_ROOT line is machine addressing for tool routing, not research content. "
    full = (prefix + prompt + "\nReturn ONLY JSON.").strip()
    cmd = ["pi", "-p", "--no-session", "--no-builtin-tools",
           "--extension", _EXTENSION]
    if names:
        cmd += ["--tools", ",".join(sorted(set(names)))]
    cmd += ["--", full]
    env = dict(os.environ)
    if data_root is not None and str(data_root):
        env["STOCKBOT_DATA_DIR"] = str(data_root)
    env["RUNS_DB_PATH"] = str(db_p)
    with open(out_p, "w") as out_f, open(err_p, "w") as err_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f,
                                stdin=subprocess.DEVNULL, env=env,
                                cwd=str(_repo_root()), start_new_session=True)
        # pi -p lingers after answering: completion comes from the
        # done sentinel (agent_end) first, the recorder DB (agent_end)
        # second, never from process exit alone.
        deadline = time.monotonic() + _TIMEOUT_S
        saw_done: str | None = None
        saw_complete = False
        while time.monotonic() < deadline:
            saw_done = _read_done(done_p)
            if saw_done is not None:
                break
            if _db_terminal(db_p):
                saw_complete = True
                break
            if proc.poll() is not None:
                break
            time.sleep(2)
        else:
            proc.poll()
        if saw_done is None:
            saw_done = _read_done(done_p)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=15)
            except Exception:
                pass
        if saw_done is not None:
            out = saw_done
            shutil.rmtree(tmp, ignore_errors=True)
            return out
        if not saw_complete and proc.returncode != 0:
            tail = err_p.read_text()[-2000:] if err_p.is_file() else ""
            raise RuntimeError(f"pi gateway: exit {proc.returncode}: {tail} (logs: {tmp})")
        if not saw_complete and not _db_terminal(db_p):
            tail = err_p.read_text()[-2000:] if err_p.is_file() else ""
            raise RuntimeError(f"pi gateway: timed out after {_TIMEOUT_S}s: {tail} (logs: {tmp})")
    out = out_p.read_text() if out_p.is_file() else ""
    shutil.rmtree(tmp, ignore_errors=True)
    return out


class PiJsonGateway:
    """Implements IntakeGateway.complete_json and ResearchGateway.complete_research."""

    def complete_json(self, prompt: str, *, request_context) -> dict:
        return _extract_largest(_run_pi(prompt, request_context=request_context))

    def complete_research(self, prompt: str, *, request_context, tools: list) -> dict:
        return _extract_largest(_run_pi(prompt, request_context=request_context, tools=tools))
