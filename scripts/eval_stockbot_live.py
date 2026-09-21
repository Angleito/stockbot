#!/usr/bin/env python3
"""Stockbot live-evaluation CLI: temporary Next dev server + real /api/agent route.

Default one-command path: pick a free loopback port, generate an evaluation
token, start ``next dev`` from ``needle-harness`` with ``STOCKBOT_EVAL_TOKEN``
set, wait on ``GET /api/health``, run every case via protected
``POST /api/agent`` (``prompt``/``asOf``/``includeTrace`` + bearer auth), print
the compact report, write one timestamped JSON artifact atomically, then
terminate the server.

``STOCKBOT_EVAL_BASE_URL`` plus ``STOCKBOT_EVAL_TOKEN`` reuses an
already-running server instead. Exit 0 only for a completed all-hard-pass
suite, 1 for completed hard failures, 2 for infrastructure/transport/
malformed judge/case/server/artifact errors. Partial case results are
preserved in the artifact when a later case infrastructure-fails, if a
writable target exists.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.research.evals.live_quality import (
    LiveEvalInfraError,
    RunArtifact,
    artifact_exit_code,
    format_report,
    run_live_suite,
    write_artifact,
)

DEFAULT_CASES = Path("evals/stockbot_live_cases.json")
HEALTH_PATH = "/api/health"
STARTUP_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.5


def _default_output() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path("artifacts") / f"stockbot-eval-{stamp}.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stockbot live evaluation (real /api/agent route)")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="cases JSON path")
    parser.add_argument("--output", default=None, help="artifact path (default: timestamped artifacts/)")
    parser.add_argument("--base-url", default=None, help="reuse a running server (needs STOCKBOT_EVAL_TOKEN)")
    parser.add_argument("--token", default=None, help="eval token for a reused server")
    parser.add_argument("--timeout-s", type=float, default=600.0, help="per-case HTTP timeout seconds")
    args = parser.parse_args(argv)
    if not isinstance(args.timeout_s, float) or not args.timeout_s > 0:
        parser.error("--timeout-s must be a positive number of seconds")
    return args


def _load_cases(path: Path) -> list[object]:
    try:
        from app.research.evals.quality_models import load_cases  # type: ignore[import-not-found]
    except ImportError:
        return _load_cases_fallback(path)
    try:
        cases = load_cases(path)
    except FileNotFoundError as exc:
        raise LiveEvalInfraError(f"cases file not found: {path}") from exc
    except (OSError, ValueError) as exc:
        raise LiveEvalInfraError(f"cannot read cases file {path}: {exc}") from exc
    selected: list[object] = list(cases)
    if not selected:
        raise LiveEvalInfraError(f"no cases in {path}")
    return selected


def _load_cases_fallback(path: Path) -> list[object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LiveEvalInfraError(f"cases file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveEvalInfraError(f"cannot read cases file {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise LiveEvalInfraError(f"no cases in {path}")
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise LiveEvalInfraError(f"malformed case entry in {path}")
        cid = entry.get("id")
        if not isinstance(cid, str) or not cid.strip():
            raise LiveEvalInfraError(f"malformed case id in {path}")
        if cid in seen:
            raise LiveEvalInfraError(f"duplicate case id {cid!r} in {path}")
        seen.add(cid)
    return list(raw)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _npm_cmd() -> str:
    for candidate in ("bun", "npm"):
        if shutil.which(candidate):
            return candidate
    raise LiveEvalInfraError("no bun/npm executable found to start the Next dev server")


def _health_ok(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + HEALTH_PATH, timeout=5) as resp:
            return int(getattr(resp, "status", 200)) == 200
    except Exception:
        return False


def _wait_healthy(base_url: str, deadline_s: float) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline_s:
        if _health_ok(base_url):
            return
        time.sleep(POLL_INTERVAL_S)
    raise LiveEvalInfraError(f"dev server at {base_url} never became healthy")


class _DevServer:
    """Temporary local Next dev server; always terminated on context exit."""

    def __init__(self, repo_root: Path, token: str) -> None:
        self.repo_root = repo_root
        self.token = token
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> str:
        harness = self.repo_root / "needle-harness"
        if not (harness / "package.json").exists():
            raise LiveEvalInfraError(f"needle-harness not found at {harness}")
        env = dict(os.environ)
        env["STOCKBOT_EVAL_TOKEN"] = self.token
        env["PORT"] = str(self.port)
        cmd = _npm_cmd()
        args = ["run", "dev", "--", "-p", str(self.port)] if cmd == "npm" else ["run", "dev", "--port", str(self.port)]
        try:
            self.proc = subprocess.Popen(
                [cmd, *args], cwd=str(harness), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
            )
        except OSError as exc:
            raise LiveEvalInfraError(f"cannot start dev server: {exc}") from exc
        _wait_healthy(self.base_url, STARTUP_TIMEOUT_S)
        return self.base_url

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)


def _resolve_server(args: argparse.Namespace, repo_root: Path) -> tuple[str, str, _DevServer | None]:
    base_url = args.base_url or os.environ.get("STOCKBOT_EVAL_BASE_URL", "").strip()
    token = args.token or os.environ.get("STOCKBOT_EVAL_TOKEN", "").strip()
    if base_url:
        if not token:
            raise LiveEvalInfraError("STOCKBOT_EVAL_TOKEN (or --token) is required with --base-url")
        return base_url.rstrip("/"), token, None
    generated = secrets.token_urlsafe(32)
    server = _DevServer(repo_root, generated)
    return server.__enter__(), generated, server


def _artifact_target(output_arg: str | None) -> tuple[Path, bool]:
    if output_arg:
        return Path(output_arg), False
    return _default_output(), True


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent
    try:
        cases = _load_cases(Path(args.cases))
    except LiveEvalInfraError as exc:
        print(f"stockbot live eval: case error: {exc}", file=sys.stderr)
        return 2
    dest, timestamped = _artifact_target(args.output)
    if dest.exists():
        print(f"stockbot live eval: refuse to overwrite existing artifact: {dest}", file=sys.stderr)
        return 2
    server: _DevServer | None = None
    artifact: RunArtifact | None = None
    try:
        base_url, token, server = _resolve_server(args, repo_root)
        try:
            # Env-only judge wiring: quality_judge reads STOCKBOT_EVAL_JUDGE_* itself.
            artifact = run_live_suite(list(cases), base_url, token, timeout_s=float(args.timeout_s))
        except LiveEvalInfraError as exc:
            if exc.partial_artifact is not None and isinstance(exc.partial_artifact, RunArtifact):
                artifact = exc.partial_artifact
            else:
                print(f"stockbot live eval: infrastructure error: {exc}", file=sys.stderr)
                return 2
        if artifact is None:
            print("stockbot live eval: infrastructure error: empty suite result", file=sys.stderr)
            return 2
        if timestamped:
            dest = _default_output()
            if dest.exists():
                print(f"stockbot live eval: refuse to overwrite existing artifact: {dest}", file=sys.stderr)
                return 2
        try:
            written = write_artifact(artifact, dest)
        except LiveEvalInfraError as exc:
            print(f"stockbot live eval: artifact error: {exc}", file=sys.stderr)
            return 2
        print(format_report(artifact), end="")
        print(f"Artifact: {written}")
        return artifact_exit_code(artifact)
    except LiveEvalInfraError as exc:
        if artifact is not None:
            try:
                written = write_artifact(artifact, dest)
                print(f"stockbot live eval: partial artifact preserved at {written}", file=sys.stderr)
            except LiveEvalInfraError as write_exc:
                print(f"stockbot live eval: artifact error: {write_exc}", file=sys.stderr)
        print(f"stockbot live eval: infrastructure error: {exc}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            server.close()


if __name__ == "__main__":
    sys.exit(main())
