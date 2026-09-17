#!/usr/bin/env python3
"""Stockbot sandbox doctor: fail-loud preflight for the Docker Sandbox path.

Stdlib only. Prints `ok <name>` lines, exits nonzero on the first failure,
and never prints secret values (names, exit codes, and hostnames only).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
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
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        fail(" ".join(cmd[1:]), f"'{cmd[0]}' not found on PATH")
    except subprocess.TimeoutExpired:
        fail(" ".join(cmd[1:]), f"'{' '.join(cmd)}' timed out after 60s")


def check_sbx() -> None:
    name = "sbx available"
    if shutil.which("sbx") is None:
        fail(name, "sbx not found on PATH; run on an sbx-equipped host")
    ok(name)


def _policy_output_locked_down(stdout: str) -> bool:
    return "locked down" in stdout.lower()


def _behavioral_policy_ok() -> bool:
    allow = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, "opencode.ai:443"])
    deny = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, "github.com:443"])
    return allow.returncode == 0 and deny.returncode != 0


def check_policy() -> None:
    name = "effective policy Locked Down for stockbot-runtime"
    proc = run(["sbx", "policy", "ls", SANDBOX_NAME])
    if proc.returncode != 0:
        fail(name, f"'sbx policy ls {SANDBOX_NAME}' failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if _policy_output_locked_down(proc.stdout):
        ok(name)
        return
    # Newer sbx prints rules without a profile name; prove default-deny
    # behaviorally: a kit host allows, a non-kit host denies.
    if not _behavioral_policy_ok():
        fail(name, "effective policy is not default-deny with kit allows")
    ok(name)


def check_credentials() -> None:
    name = "opencode-go host credential configured"
    proc = run(["sbx", "secret", "ls"])
    if proc.returncode != 0:
        fail(name, f"'sbx secret ls' failed (exit {proc.returncode}): {proc.stderr.strip()}")
    if "opencode-go" not in proc.stdout:
        fail(name, "missing host credential: opencode-go")
    ok(name)


def _ssh_value_ok(stdout: str) -> bool:
    return stdout.strip().lower() == "false"


def check_ssh_forwarding() -> None:
    name = "ssh.agentForwardingEnabled false"
    proc = run(["sbx", "settings", "get", "ssh.agentForwardingEnabled"])
    if proc.returncode != 0:
        fail(
            name,
            f"'sbx settings get ssh.agentForwardingEnabled' failed (exit {proc.returncode}): {proc.stderr.strip()}",
        )
    if not _ssh_value_ok(proc.stdout):
        fail(name, "ssh.agentForwardingEnabled is not false; run 'sbx settings set ssh.agentForwardingEnabled false'")
    ok(name)


def is_mount_violation(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return any(sub in line for sub in FORBIDDEN_MOUNT_SUBSTRS)


def _check_mount_file(rel: str, name: str) -> None:
    path = REPO_ROOT / rel
    if not path.is_file():
        fail(name, f"{rel} not found")
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        if is_mount_violation(line):
            fail(name, f"host Pi auth source in {rel}:{lineno}")


def check_no_pi_mount() -> None:
    name = "no host Pi auth mount in compose/kit"
    for rel in MOUNT_CHECK_FILES:
        _check_mount_file(rel, name)
    ok(name)


def missing_env_keys(env: Mapping[str, str]) -> list[str]:
    return [key for key in REQUIRED_ENV_ABSENT if env.get(key)]


def check_calling_env() -> None:
    name = "no provider secrets in calling env"
    present = missing_env_keys(os.environ)
    if present:
        fail(name, f"unset before launching: {', '.join(present)}")
    ok(name)


def parse_allowlist(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def read_allowlist() -> list[str]:
    name = "required hosts allowed, others denied"
    allowlist_path = REPO_ROOT / "sandbox" / "stockbot" / "egress-hosts.txt"
    if not allowlist_path.is_file():
        fail(name, "sandbox/stockbot/egress-hosts.txt not found")
    required = parse_allowlist(allowlist_path.read_text())
    if not required:
        fail(name, "sandbox/stockbot/egress-hosts.txt is empty")
    return required


def _check_host_allowed(host: str, name: str) -> None:
    proc = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, host])
    if proc.returncode != 0:
        fail(name, f"{host} should be allowed (exit {proc.returncode}): {proc.stderr.strip()}")


def _check_host_denied(host: str, name: str) -> None:
    proc = run(["sbx", "policy", "check", "network", "--sandbox", SANDBOX_NAME, host])
    if proc.returncode == 0:
        fail(name, f"{host} should be denied")


def check_network() -> None:
    name = "required hosts allowed, others denied"
    required = read_allowlist()
    for host in required:
        _check_host_allowed(host, name)
    for host in DENIED_HOSTS:
        _check_host_denied(host, name)
    ok(f"{name} ({len(required)} allowed, {len(DENIED_HOSTS)} denied)")


CHECKS: dict[str, Callable[[], None]] = {
    "sbx": check_sbx,
    "policy": check_policy,
    "credentials": check_credentials,
    "ssh": check_ssh_forwarding,
    "mount": check_no_pi_mount,
    "env": check_calling_env,
    "network": check_network,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="append", choices=sorted(CHECKS), help="run only this check (repeatable; default: all)"
    )
    return parser.parse_args(argv)


def run_all(checks: list[Callable[[], None]]) -> int:
    failures = 0
    for check in checks:
        try:
            check()
        except SystemExit as exc:
            if exc.code:
                failures += 1
        except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
            failures += 1
    return failures


def selected_checks(args: argparse.Namespace) -> list[Callable[[], None]]:
    return [CHECKS[name] for name in args.check or list(CHECKS)]


def main(argv: list[str] | None = None) -> int:
    for check in selected_checks(parse_args(argv)):
        check()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
