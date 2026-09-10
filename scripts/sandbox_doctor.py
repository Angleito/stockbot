#!/usr/bin/env python3
"""Stockbot sandbox doctor: fail-loud preflight for the Docker Sandbox path.

Stdlib only. Prints `ok <name>` lines, exits nonzero on the first failure,
and never prints secret values (names, exit codes, and hostnames only).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX_NAME = "stockbot-runtime"
REQUIRED_ENV_ABSENT = ("OPENAI_API_KEY", "OPENCODE_API_KEY", "PI_AGENT_DIR")
# Denied hosts are checked with the :443 port to match the allowlist format.
DENIED_HOSTS = ("github.com:443", "example.com:443", "pypi.org:443")
# Substrings that must never appear in a host-source (mount) line of the
# compose file or kit. Comment lines are skipped so the kit's own
# "Never mount ..." safety comment does not trip the check.
FORBIDDEN_MOUNT_SUBSTRS = ("~/.pi", "~/.stockbot", "auth.json", ".pi/agent")
MOUNT_CHECK_FILES = ("docker-compose.yml", "sandbox/stockbot/spec.yaml")


def ok(name: str) -> None:
    print(f"\u2713 {name}")

def fail(name: str, detail: str) -> NoReturn:
    print(f"\u2717 {name}: {detail}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        fail(" ".join(cmd[1:]), f"'{cmd[0]}' not found on PATH")
    except subprocess.TimeoutExpired:
        fail(" ".join(cmd[1:]), f"'{' '.join(cmd)}' timed out after 60s")


def check_sbx() -> None:
    name = "sbx available"
    if shutil.which("sbx") is None:
        fail(name, "sbx not found on PATH; run on an sbx-equipped host")
    ok(name)


def check_policy() -> None:
    name = "effective policy Locked Down for stockbot-runtime"
    proc = run(["sbx", "policy", "ls", SANDBOX_NAME])
    if proc.returncode != 0:
        fail(name, f"'sbx policy ls {SANDBOX_NAME}' failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if "locked down" not in proc.stdout.lower():
        fail(name, f"'sbx policy ls {SANDBOX_NAME}' does not report Locked Down")
    ok(name)


def check_credentials() -> None:
    name = "openai + opencode-go host credentials configured"
    proc = run(["sbx", "secret", "ls"])
    if proc.returncode != 0:
        fail(name, f"'sbx secret ls' failed (exit {proc.returncode}): {proc.stderr.strip()}")
    missing = [svc for svc in ("openai", "opencode-go") if svc not in proc.stdout]
    if missing:
        fail(name, f"missing host credentials: {', '.join(missing)}")
    ok(name)


def check_ssh_forwarding() -> None:
    name = "ssh.agentForwardingEnabled false"
    proc = run(["sbx", "settings", "get", "ssh.agentForwardingEnabled"])
    if proc.returncode != 0:
        fail(name, f"'sbx settings get ssh.agentForwardingEnabled' failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if proc.stdout.strip().lower() != "false":
        fail(name, "ssh.agentForwardingEnabled is not false; run 'sbx settings set ssh.agentForwardingEnabled false'")
    ok(name)


def check_no_pi_mount() -> None:
    name = "no host Pi auth mount in compose/kit"
    for rel in MOUNT_CHECK_FILES:
        path = REPO_ROOT / rel
        if not path.is_file():
            fail(name, f"{rel} not found")
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if any(sub in line for sub in FORBIDDEN_MOUNT_SUBSTRS):
                fail(name, f"host Pi auth source in {rel}:{lineno}")
    ok(name)


def check_calling_env() -> None:
    name = "no provider secrets in calling env"
    present = [key for key in REQUIRED_ENV_ABSENT if os.environ.get(key)]
    if present:
        fail(name, f"unset before launching: {', '.join(present)}")
    ok(name)


def check_network() -> None:
    name = "required hosts allowed, others denied"
    allowlist_path = REPO_ROOT / "sandbox" / "stockbot" / "egress-hosts.txt"
    if not allowlist_path.is_file():
        fail(name, "sandbox/stockbot/egress-hosts.txt not found")
    required = [ln.strip() for ln in allowlist_path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not required:
        fail(name, "sandbox/stockbot/egress-hosts.txt is empty")
    for host in required:
        proc = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, host])
        if proc.returncode != 0:
            fail(name, f"{host} should be allowed (exit {proc.returncode}): {proc.stderr.strip()}")
    for host in DENIED_HOSTS:
        proc = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, host])
        if proc.returncode == 0:
            fail(name, f"{host} should be denied")
    ok(f"{name} ({len(required)} allowed, {len(DENIED_HOSTS)} denied)")


def main() -> None:
    check_sbx()
    check_policy()
    check_credentials()
    check_ssh_forwarding()
    check_no_pi_mount()
    check_calling_env()
    check_network()


if __name__ == "__main__":
    main()
