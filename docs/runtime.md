# Stockbot runtime: Oh My Pi (OMP)

Pinned dependency (package.json + bun.lock):

- `@oh-my-pi/pi-coding-agent@18.2.3`
- `@oh-my-pi/pi-tui@18.2.3`

Binary (`which omp`, `omp --version`): `omp/18.2.3`.

Launch: `bun run stockbot` runs

```text
omp --config .stockbot/omp/stockbot.yml --no-extensions --no-skills --no-rules --no-lsp --tools=task -e .stockbot/omp/index.ts
```

Native `task` stays enabled: it is the only child-agent execution mechanism
(SEC agent, SEC scouts, committee trio). Unrelated capabilities are disabled
via the `.stockbot/omp/stockbot.yml` overlay, not by removing `task`.

Architecture: OMP is the agent runtime, ResearchDirector (`.stockbot/omp/lib/research-director.ts`)
is the research orchestrator, the Python kernel (`app/research/`) is the
deterministic verifier. Thesis monitoring launches the same OMP path via
`app/thesis/omp_runner.py`.
