"""Sandbox hardening regressions: no host Pi-auth mount, exact egress allowlist,
provider isolation via synthetic auth, and the Pi tool gate (see plan)."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
SPEC = ROOT / "sandbox" / "stockbot" / "spec.yaml"
EGRESS_HOSTS = ROOT / "sandbox" / "stockbot" / "egress-hosts.txt"
HOST_SETUP = ROOT / "sandbox" / "stockbot" / "HOST_SETUP.md"
ENTRYPOINT = ROOT / "sandbox" / "stockbot" / "synthetic-pi-auth.sh"
PACKAGE_JSON = ROOT / "package.json"

SENTINEL = "DOCKER_SANDBOX_MANAGED"
ALLOWED_DEST = "/root/.pi/agent/auth.json"

EXPECTED_HOSTS = [
    "chatgpt.com:443",
    "opencode.ai:443",
    "www.sec.gov:443",
    "data.sec.gov:443",
    "efts.sec.gov:443",
    "api.finra.org:443",
    "ews.fip.finra.org:443",
    "api.exa.ai:443",
    "agent.robinhood.com:443",
    "query2.finance.yahoo.com:443",
    "fc.yahoo.com:443",
    "www.slickcharts.com:443",
    "api.datacommons.org:443",
    "bigquery.googleapis.com:443",
    "cloudbilling.googleapis.com:443",
    "oauth2.googleapis.com:443",
    "www.googleapis.com:443",
]

FORBIDDEN_HOSTS = (
    "**",
    "*.googleapis.com",
    "github.com",
    "raw.githubusercontent.com",
    "pypi.org",
    "registry.npmjs.org",
)

FORBIDDEN_SRC_TOKENS = ("~/.pi", "~/.stockbot/pi-agent", "pi-agent", "auth.json")

# Bind-mount-shaped use of a host Pi-auth path: `<forbidden-src>:/<container-dest>`
# or a `-v <forbidden-src>` flag. Bare prose mentions (no mount syntax) pass.
_BIND_MOUNT_RE = re.compile(
    r"(?:^|[\s'\"`])(~\/(?:\.pi|stockbot\/pi-agent)[^\s'\"`]*"
    r"|\$\{?PI_AGENT_DIR[^}\s]*\}?"
    r"|[^\s'\"`]*auth\.json)\s*:\s*/\S+"
)
_VOLUME_FLAG_RE = re.compile(r"(?:^|\s)-v\s+\S*(?:\.pi|pi-agent|auth\.json)")
_HOST_LINE_RE = re.compile(r"^[A-Za-z0-9_.-]+:\d+$")

# High-signal real-secret shapes; must never appear in sandbox fixtures.
_REAL_SECRET_RES = (
    re.compile(r"sk-(live|test)-[A-Za-z0-9]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{10,}"),
    re.compile(r"xox[bpas]-[A-Za-z0-9-]{6,}"),
    re.compile(r"BEGIN .*PRIVATE KEY"),
)


def _assert_no_real_secrets(text: str, where: str) -> None:
    for rx in _REAL_SECRET_RES:
        assert not rx.search(text), f"real-looking secret in {where}: {rx.pattern}"


def _compose_volume_sources():
    doc = yaml.safe_load(COMPOSE.read_text())
    sources = []
    for svc in (doc.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            if isinstance(vol, str):
                sources.append(vol.split(":")[0])
            elif isinstance(vol, dict) and vol.get("source"):
                sources.append(str(vol["source"]))
    return sources


def test_no_raw_pi_mount_compose():
    for src in _compose_volume_sources():
        for token in FORBIDDEN_SRC_TOKENS:
            assert token not in src, f"host Pi-auth mount source in compose: {src!r}"


def test_no_raw_pi_mount_kit():
    doc = yaml.safe_load(SPEC.read_text())
    for mount in doc.get("mounts") or []:
        src = str((mount or {}).get("source", ""))
        for token in FORBIDDEN_SRC_TOKENS:
            assert token not in src, f"host Pi-auth mount source in kit: {src!r}"
    # No other host-source reference outside comments; the entrypoint-written
    # destination path is allowed.
    body = "\n".join(
        ln for ln in SPEC.read_text().splitlines() if not ln.lstrip().startswith("#")
    ).replace(ALLOWED_DEST, "")
    for token in FORBIDDEN_SRC_TOKENS:
        assert token not in body, f"{token!r} as host source in spec.yaml"


def test_no_raw_pi_mount_docs():
    if not HOST_SETUP.exists():
        pytest.skip("HOST_SETUP.md not yet written by sibling")
    for i, line in enumerate(HOST_SETUP.read_text().splitlines(), 1):
        probe = line.replace(ALLOWED_DEST, "")
        assert not _BIND_MOUNT_RE.search(probe), f"host Pi-auth mount at {HOST_SETUP.name}:{i}"
        assert not _VOLUME_FLAG_RE.search(probe), f"-v Pi-auth mount at {HOST_SETUP.name}:{i}"


def _read_hosts(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]


def test_egress_allowlist_exact():
    assert _read_hosts(EGRESS_HOSTS) == EXPECTED_HOSTS
    doc = yaml.safe_load(SPEC.read_text())
    assert sorted(doc["network"]["allow"]) == sorted(EXPECTED_HOSTS)
    assert doc["network"].get("mode") == "deny-all"
    for host in EXPECTED_HOSTS:
        assert _HOST_LINE_RE.match(host), f"malformed allowlist entry: {host!r}"


def test_egress_allowlist_no_forbidden():
    blob = EGRESS_HOSTS.read_text() + "\n" + SPEC.read_text()
    for forbidden in FORBIDDEN_HOSTS:
        assert forbidden not in blob, f"forbidden egress entry present: {forbidden!r}"
    assert "*" not in EGRESS_HOSTS.read_text()

def test_provider_isolation_synthetic_auth(tmp_path: Path) -> None:

    agent_dir = tmp_path / "agent"
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir),
               STOCKBOT_CODEX_ACCOUNT_ID="test-id-1")
    proc = subprocess.run([str(ENTRYPOINT), "true"], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    written = agent_dir / "auth.json"
    assert written.exists()
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert json.loads(written.read_text()) == {
        "openai-codex": {"type": "oauth", "access": SENTINEL,
                         "refresh": SENTINEL, "expires": 4102444800000,
                         "accountId": "test-id-1"}
    }
    assert ENTRYPOINT.read_text().count(SENTINEL) == 2
    _assert_no_real_secrets(written.read_text(), "synthetic auth.json")
    _assert_no_real_secrets(ENTRYPOINT.read_text(), "synthetic-pi-auth.sh")


def test_provider_isolation_noop_without_account_id(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent_dir))
    env.pop("STOCKBOT_CODEX_ACCOUNT_ID", None)
    proc = subprocess.run([str(ENTRYPOINT), "true"], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert not (agent_dir / "auth.json").exists()


def test_tool_gate_flags():
    stockbot = json.loads(PACKAGE_JSON.read_text())["scripts"]["stockbot"]
    for flag in ("--no-builtin-tools", "--no-extensions", "--no-skills",
                 "--no-prompt-templates", "--no-context-files",
                 "--extension .pi/extensions/stockbot.ts"):
        assert flag in stockbot, f"missing from stockbot script: {flag}"
