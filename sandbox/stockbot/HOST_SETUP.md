# Stockbot sandbox host setup (equipped host only)

Run everything below on the host, from the repo root. Nothing here executes
inside the sandbox. Requires `sbx` installed; static-only machines (no `sbx`)
stop here — `bun run sandbox-doctor` failing at "`sbx` available" is expected
there and proves fail-loud.

Prereqs: host `~/.pi/agent/auth.json` holds a working `opencode-go` (API key)
entry. Real secrets never enter the sandbox: the OpenCode key stays
Docker-proxy-managed. Local models run through one llama-server router at
`http://127.0.0.1:8080` (host `~/.pi/agent/models.json` providers `liquid-local`, `minicpm-local`); IDs are the router preset IDs (`LiquidAI/LFM2.5-1.2B-Thinking-GGUF:Q4_K_M`, `openbmb/MiniCPM5-1B-GGUF:Q4_K_M`) — one port for all models — and need no sandbox egress.
Router presets live in `~/.config/llama-server/presets.ini` (start the router with `--models-preset` for them to apply). Qwen3.6-35B removed entirely: ~20G weights exceeded this host's 4GiB iGPU heap (`vk::Queue::submit: ErrorDeviceLost`), files deleted from the HF cache, no Pi provider points at it.

## 1. Register host credentials

```sh
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
sbx create --name stockbot-runtime --clone --no-share-skills ./sandbox/stockbot/ .
```

Pass the data volume at create (`stockbot-data` → `/data`; see
`sbx create --help` — the kit declares no volumes). Before create, build,
push, and pin the image (`docker build -t <registry>/stockbot-sandbox:<tag> .`,
push, write the digest into `spec.yaml` `sandbox.image`); tag alone is not
accepted.

## 5. Launch (thesis ID only, never tokens)

```sh
export THESIS_ID="<thesis-id>"
sbx exec -e THESIS_ID="$THESIS_ID" stockbot-runtime -- bun run stockbot
```

`THESIS_ID` is the only host-derived value. `OPENCODE_API_KEY` and
`PI_AGENT_DIR` must never be set in this shell nor in `.env`. If
`sbx secret set --command` snapshots at set-time instead of re-resolving per
use, re-run the step-1 `opencode-go` command after every host key rotation.

## 6. Verify: policy matrix

```sh
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
group is policy-blocked.

```sh
for h in opencode.ai www.sec.gov data.sec.gov efts.sec.gov \
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

## 8. Verify: isolation + opencode turn

```sh
mount | grep -Ei 'pi|stockbot' || true
env | grep -Ei 'OPENAI_API_KEY|OPENCODE_API_KEY|PI_AGENT_DIR' || true
echo "SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-<empty>}"
test ! -s /home/agent/.pi/agent/auth.json   # absent or empty: nothing writes credentials anymore
bun run sandbox-doctor
```

Then `bun run stockbot`: switch to an `opencode-go/*` model (Ctrl+P),
complete a turn plus one Stockbot research tool.

## Never

Never mount `$HOME`, `~/.ssh`, `~/.pi`, `~/.config`, the Docker socket, or
browser profiles; never enable SSH-agent forwarding; never add a wildcard or
`github.com` / registry / `api.openai.com` / `auth.openai.com` host — new
hosts go in `egress-hosts.txt` + kit + test together, exact `host:port` only.
