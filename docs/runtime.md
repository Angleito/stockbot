# Stockbot runtime: Oh My Pi (OMP)

Pinned dependency (package.json + bun.lock):

- `@oh-my-pi/pi-coding-agent@18.2.3`
- `@oh-my-pi/pi-tui@18.2.3`

Binary (`which omp`, `omp --version`): `omp/18.2.3`.

Launch: `bun run stockbot` runs

```text
omp --config .stockbot/omp/stockbot.yml --no-extensions --no-skills --no-rules --no-lsp --tools=task -e .stockbot/omp
```

Native `task` stays enabled: it is the only child-agent execution mechanism
(SEC agent, SEC scouts, committee trio). Unrelated capabilities are disabled
via the `.stockbot/omp/stockbot.yml` overlay, not by removing `task`.

Architecture: OMP is the agent runtime, ResearchDirector (`.stockbot/omp/lib/research-director.ts`)
is the research orchestrator, the Python kernel (`app/research/`) is the
deterministic verifier. Thesis monitoring launches the same OMP path via
`app/thesis/omp_runner.py`.

`app/research/runner.py` is a deterministic eval harness only (retired live
loop kept as a test helper): OMP owns orchestration, the kernel owns evidence,
PIT, freeze, and stages. Production and eval orchestration never call
`run_live`/`resume_live`.

Live golden (opt-in only, never CI): `bun run verify:hedgefund-live` runs one
golden scenario through the production OMP path with real SEC/FINRA/Exa
credentials (`HEDGEFUND_LIVE=1`, `SEC_EDGAR_IDENTITY`, `FINRA_CLIENT_ID` /
`FINRA_CLIENT_SECRET`, `EXA_ENABLED=1` + `EXA_API_KEY`) and re-exports the
read-only harness-viewer projection. Without the opt-in flag it exits 2.
