"""Sandbox hardening regressions: no host Pi-auth mount, exact egress allowlist,
single-provider docs, and the Pi tool gate (see plan)."""

import json
import os
import re
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

EXPECTED_HOSTS = [
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


def _iter_source_values(node: object):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("source", "src") and isinstance(value, str):
                yield value
            else:
                yield from _iter_source_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_source_values(value)


def test_no_raw_pi_mount_compose():
    for src in _compose_volume_sources():
        for token in FORBIDDEN_SRC_TOKENS:
            assert token not in src, f"host Pi-auth mount source in compose: {src!r}"


def test_no_raw_pi_mount_kit():
    doc = yaml.safe_load(SPEC.read_text())
    for src in _iter_source_values(doc):
        for token in FORBIDDEN_SRC_TOKENS:
            assert token not in src, f"host Pi-auth mount source in kit: {src!r}"
    # No other host-source reference outside comments.
    body = "\n".join(
        ln for ln in SPEC.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    for token in FORBIDDEN_SRC_TOKENS:
        assert token not in body, f"{token!r} as host source in spec.yaml"


def test_no_raw_pi_mount_docs():
    if not HOST_SETUP.exists():
        pytest.skip("HOST_SETUP.md not yet written by sibling")
    for i, line in enumerate(HOST_SETUP.read_text().splitlines(), 1):
        probe = line
        assert not _BIND_MOUNT_RE.search(probe), f"host Pi-auth mount at {HOST_SETUP.name}:{i}"
        assert not _VOLUME_FLAG_RE.search(probe), f"-v Pi-auth mount at {HOST_SETUP.name}:{i}"


def _read_hosts(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]


def test_egress_allowlist_exact():
    assert _read_hosts(EGRESS_HOSTS) == EXPECTED_HOSTS
    doc = yaml.safe_load(SPEC.read_text())
    assert doc["schemaVersion"] == "2"
    for legacy in ("network", "mounts", "build", "env"):
        assert legacy not in doc, f"legacy v1 key in spec.yaml: {legacy!r}"
    assert sorted(doc["permissions"]["network"]["allow"]) == sorted(EXPECTED_HOSTS)
    for host in EXPECTED_HOSTS:
        assert _HOST_LINE_RE.match(host), f"malformed allowlist entry: {host!r}"


def test_egress_allowlist_no_forbidden():
    blob = EGRESS_HOSTS.read_text() + "\n" + SPEC.read_text()
    for forbidden in FORBIDDEN_HOSTS:
        assert forbidden not in blob, f"forbidden egress entry present: {forbidden!r}"
    assert "*" not in EGRESS_HOSTS.read_text()


def test_tool_gate_flags():
    stockbot = json.loads(PACKAGE_JSON.read_text())["scripts"]["stockbot"]
    for flag in ("--no-builtin-tools", "--no-extensions", "--no-skills",
                 "--no-prompt-templates", "--no-context-files",
                 "--extension .pi/extensions/stockbot.ts"):
        assert flag in stockbot, f"missing from stockbot script: {flag}"

def test_kit_create_docs():
    text = HOST_SETUP.read_text()
    assert "--name stockbot-runtime" in text
    assert "./sandbox/stockbot/" in text
    assert "-e THESIS_ID" in text
    assert "--kit sandbox/stockbot/spec.yaml" not in text
    assert "sbx run --sandbox stockbot-runtime -- bun run stockbot" not in text
    spec = SPEC.read_text()
    for token in ("openai-codex", "STOCKBOT_CODEX_ACCOUNT_ID", "synthetic-pi-auth", "service: openai"):
        assert token not in spec
    assert "codex" not in (spec + text).lower()


def test_opencode_secret_rejects_indirection(tmp_path: Path) -> None:
    helper = ROOT / "scripts" / "host" / "pi-opencode-go-secret"
    agent_dir = tmp_path / ".pi" / "agent"
    agent_dir.mkdir(parents=True)
    cases = (("$FOO", False), ("${FOO}", False), ("!cmd", False), ("opencode-live-key-1", True))
    for key, ok in cases:
        (agent_dir / "auth.json").write_text(json.dumps({"opencode-go": {"type": "api_key", "key": key}}))
        env = dict(os.environ, HOME=str(tmp_path))
        proc = subprocess.run([str(helper)], env=env, capture_output=True, text=True, timeout=30)
        if ok:
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == key
        else:
            assert proc.returncode != 0, key
