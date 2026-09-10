# Stockbot sandbox host setup (equipped host only)

Run everything below on the host, from the repo root. Nothing here executes
inside the sandbox. Requires `sbx` installed; static-only machines (no `sbx`)
stop here — `bun run sandbox-doctor` failing at "`sbx` available" is expected
there and proves fail-loud.

Prereqs: host `~/.pi/agent/auth.json` holds working `openai-codex` (OAuth) and
`opencode-go` (API key) entries. Real secrets never enter the sandbox: the
Codex entry arrives as the `DOCKER_SANDBOX_MANAGED` sentinel (entrypoint
writes it), the OpenCode key stays Docker-proxy-managed.

## 1. Register host credentials

```sh
sbx secret set openai --oauth
sbx secret set opencode-go --command "$PWD/scripts/host/pi-opencode-go-secret"
```

## 2. Lock the policy down (first time only)

Only run `init` if policy was never initialized. Never silently reset an
existing Balanced/Open policy — instead stop and require Locked Down:

```sh
sbx policy ls stockbot-runtime
sbx policy init deny-all   # ONLY if never initialized; otherwise do NOT run
```

Required end state: effective policy for `stockbot-runtime` is Locked Down.

## 3. Disable SSH agent forwarding

```sh
sbx settings set ssh.agentForwardingEnabled false
sbx daemon restart
```

## 4. Create the sandbox (clone, no shared skills)

`--clone` snapshots the repo into the sandbox so the host checkout is not
writable from inside and no GitHub access is needed at runtime (github.com
stays off the allowlist by design):

```sh
sbx create --kit sandbox/stockbot/spec.yaml --name stockbot-runtime \
  --clone --no-share-skills
```

## 5. Launch (account ID only, never tokens)

```sh
export STOCKBOT_CODEX_ACCOUNT_ID="$("./scripts/host/pi-codex-account-id")"
export THESIS_ID="<thesis-id>"
sbx run --sandbox stockbot-runtime -- bun run stockbot
```

`STOCKBOT_CODEX_ACCOUNT_ID` is the only host-derived value and is
non-authenticating (Pi's `chatgpt-account-id` header). `OPENAI_API_KEY`,
`OPENCODE_API_KEY`, and `PI_AGENT_DIR` must never be set in this shell nor in
`.env`. If `sbx secret set --command` snapshots at set-time instead of
re-resolving per use, re-run the step-1 `opencode-go` command after every host
key rotation.

## 6. Verify: policy matrix

```sh
sbx policy check network --sandbox stockbot-runtime chatgpt.com:443
sbx policy check network --sandbox stockbot-runtime opencode.ai:443
sbx policy check network --sandbox stockbot-runtime www.sec.gov:443
sbx policy check network --sandbox stockbot-runtime data.sec.gov:443
sbx policy check network --sandbox stockbot-runtime efts.sec.gov:443
sbx policy check network --sandbox stockbot-runtime api.finra.org:443
sbx policy check network --sandbox stockbot-runtime ews.fip.finra.org:443
sbx policy check network --sandbox stockbot-runtime api.exa.ai:443
sbx policy check network --sandbox stockbot-runtime agent.robinhood.com:443
sbx policy check network --sandbox stockbot-runtime query2.finance.yahoo.com:443
sbx policy check network --sandbox stockbot-runtime fc.yahoo.com:443
sbx policy check network --sandbox stockbot-runtime www.slickcharts.com:443
sbx policy check network --sandbox stockbot-runtime api.datacommons.org:443
sbx policy check network --sandbox stockbot-runtime bigquery.googleapis.com:443
sbx policy check network --sandbox stockbot-runtime cloudbilling.googleapis.com:443
sbx policy check network --sandbox stockbot-runtime oauth2.googleapis.com:443
sbx policy check network --sandbox stockbot-runtime www.googleapis.com:443
# Each must allow; each of these must deny:
sbx policy check network --sandbox stockbot-runtime github.com:443
sbx policy check network --sandbox stockbot-runtime example.com:443
sbx policy check network --sandbox stockbot-runtime pypi.org:443
```

## 7. Verify: curl matrix (inside the sandbox)

First group connects (HTTP 2xx/4xx from the host, not a policy error); second
group is policy-blocked. `auth.openai.com` stays blocked by design — Pi's own
refresh lives there, and the far-future sentinel expiry means Pi never calls
it; Docker owns refresh on the host.

```sh
for h in chatgpt.com opencode.ai www.sec.gov data.sec.gov efts.sec.gov \
    api.finra.org ews.fip.finra.org api.exa.ai agent.robinhood.com \
    query2.finance.yahoo.com fc.yahoo.com www.slickcharts.com \
    api.datacommons.org bigquery.googleapis.com cloudbilling.googleapis.com \
    oauth2.googleapis.com www.googleapis.com; do
  curl -sS -o /dev/null -w "$h %{http_code}\n" --max-time 10 "https://$h/"
done
for h in github.com raw.githubusercontent.com example.com pypi.org \
    registry.npmjs.org api.openai.com auth.openai.com; do
  curl -sS -o /dev/null -w "$h %{http_code}\n" --max-time 10 "https://$h/"
done
```

## 8. Verify: isolation + both providers in one session

```sh
mount | grep -Ei 'pi|stockbot' || true
env | grep -Ei 'OPENAI_API_KEY|OPENCODE_API_KEY|PI_AGENT_DIR' || true
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-<empty>}"
cat /root/.pi/agent/auth.json   # access/refresh must be DOCKER_SANDBOX_MANAGED only
bun run sandbox-doctor
```

Then `bun run stockbot`: use both providers in one Pi session (a Codex turn
and an OpenCode turn) plus one Stockbot research tool. Restart the sandbox and
repeat this section to prove host-owned refresh survives.

## Never

Never mount `$HOME`, `~/.ssh`, `~/.pi`, `~/.config`, the Docker socket, or
browser profiles; never enable SSH-agent forwarding; never add a wildcard or
`github.com` / registry / `api.openai.com` / `auth.openai.com` host — new
hosts go in `egress-hosts.txt` + kit + test together, exact `host:port` only.
