# Needle Harness

Local routing (Cactus Needle 3), remote reasoning (Muse Spark via OpenCode Go API), Bun orchestration, Next.js terminal UI.

## 1. Requirements

- Bun 1.4.2, Python 3 with `cactus-needle`, repo-root `needle3.cact` weights
- `SEC_EDGAR_IDENTITY="Name email@example.com"` for SEC tools (in `.env` or shell)

## 2. Setup

```bash
bash scripts/setup-needle.sh
bun install
export OPENCODE_API_KEY="$(../scripts/host/pi-opencode-go-secret)"
export SEC_EDGAR_IDENTITY="Name email@example.com"
```

## 3. Env vars

See ../.env.example (single repo-root .env). Shell exports override .env. OPENCODE_API_KEY may come from either; never commit .env.

## 4. How to run

```bash
cp ../.env.example ../.env  # once from repo root, then fill values
bun run needle  # from repo root (or needle-harness/) → http://localhost:3000
# staged logs per step; browser misbehaves? first check: curl -s localhost:3000/api/health
bun run lint && bun run typecheck && bun run build
BENCHMARK=1 bun run needle   # write benchmarks/*.json per run
```

## 5. Architecture

```
prompt → Needle 3 (local routing, JSONL bridge) → Bun loop (max 8 steps)
  → Tools (web_search, fetch_url, get_sec_filings, get_current_time)
  → Evidence[] → Muse Spark 1.3 (single reasoning call) → answer + metrics
Next.js streams AgentEvents as SSE to the terminal UI.
```

Needle routes, Bun orchestrates, tools retrieve, Muse reasons, Next.js displays.

## 6. Benchmarks

With `BENCHMARK=1`, each run writes `benchmarks/<YYYYMMDD-HHmmss>-<6rand>.json`
with prompt, needle decisions, tool calls, full evidence, Muse usage, answer, metrics.
Never committed (gitignored).
