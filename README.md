# Stockbot

Stockbot is a local-first AI investment research system designed to give individual investors hedge-fund-style research infrastructure. Today it is the research and data foundation for that direction, not an automated trading or consumer portfolio-management product.

## Current capabilities

- SEC EDGAR research, filing extraction, and deterministic financial calculations
- FINRA datasets, short-interest screens, and Regulation SHO analysis
- Point-in-time `known_at` data handling, provenance, raw-data archiving, normalized Parquet datasets, and DuckDB analytics
- Obligation and valuation analysis
- A tool-using research agent backed by Pi
- Analyst-consensus and market-data adapters
- Optional Robinhood portfolio, quote, option, and saved-scanner **read** integration with portfolio analytics
- Optional Exa-backed external web research (`search_web`): bounded current-evidence search with highlights; results are research evidence, not canonical financial records

## Architecture principles

Stockbot keeps raw source data and normalized analytical data separate, preserves when data became knowable, and favors deterministic calculations over model arithmetic. The LLM selects bounded tools and explains evidence; it is not the primary database or calculator.

## Requirements

- Python 3.14
- An SEC EDGAR identity for SEC requests
- A working Pi setup for model inference (Stockbot shells to `pi`)
- Bun (task runner) — `package.json` scripts are zero-dependency wrappers around Python tooling; required for `bun run setup`, `bun run test`, `bun run verify`.

## Local setup

```bash
cp .env.example .env
# Fill only the credentials for integrations you intend to use.
bun run setup
```

Offline tests do not need FINRA, Robinhood, or SEC credentials.

## Environment variables

See [`.env.example`](.env.example) for the current list and whether each setting is required, optional, or testing-only. `SEC_EDGAR_IDENTITY` is required only for SEC features. FINRA and Robinhood are optional integrations.

## Running research

```bash
bun run stockbot
```

Pi owns model selection and the agent loop; Stockbot exposes its tools and
security gates over the stdio bridge. `cli.py` remains for admin tasks only
(`runs`, `inspect`, `refresh-data`, `log-server`, `robinhood-login`).

## Running tests

```bash
bun run test
bun run typecheck
bun run test-collect
# Fresh-environment verification:
bun run verify
```

The default suite is offline. FINRA and Robinhood smoke tests are opt-in
bun scripts (smoke-mock, smoke-prod, smoke-robinhood).

## Optional Robinhood integration

Set `BROKER_ENABLED=true` and complete the local OAuth login flow (`cli.py robinhood-login`) before using the integration. OAuth and MCP transport are pinned to `https://agent.robinhood.com/mcp/trading`; persisted tokens/client registration are bound to that HTTPS origin. Stockbot only permits explicitly allowlisted read operations and blocks unknown operations. It blocks order placement, order cancellation/replacement, option exercise, withdrawals, deposits, and transfers. It does not support trading or modifying saved scanners.

## Optional Exa integration

Set `EXA_ENABLED=true` and `EXA_API_KEY` to enable external web research via
the `search_web` tool. It returns bounded highlights for current qualitative
evidence: news, announcements, competitive/industry developments,
publications, specialist commentary, and counterevidence. Canonical
SEC/FINRA/Robinhood/local-warehouse data remains authoritative — Exa never
writes canonical datasets, and exact financial facts, portfolio state, and
deterministic screens must come from the canonical tools. Searches appear in
run traces (`cli.py inspect`) with their evidence.

## Data storage

Runtime data is local and ignored by Git under `data/`. Stockbot archives raw source responses and stores normalized datasets as Parquet for DuckDB analytics. Robinhood OAuth state is intended to stay local at `~/.stockbot/robinhood/oauth.json` with restrictive permissions.

## Privacy and external services

Stockbot runs locally, but selected functionality calls external APIs including SEC EDGAR, FINRA, Robinhood MCP, and market-data sources. When Pi runs inference, prompts and selected research/tool context—including applicable portfolio research context—are sent to the user's configured Pi model for inference. Raw Robinhood OAuth credentials remain local; Stockbot omits brokerage account identifiers from the model-facing portfolio payload.

The analyst-consensus adapter uses Yahoo Finance's unofficial `quoteSummary` endpoint with a cookie/crumb workflow. It is isolated in `app/analyst_client.py`, is not required for startup, and fails gracefully; it is not a guaranteed supported API.

## Current limitations

- No hosted, multi-user, or deployment environment
- No brokerage trading, money movement, or scanner writes
- Some research adapters depend on external source availability
- Yahoo Finance consensus data is unofficial and best-effort
- The broader thesis-monitoring and mandate product is not implemented

## Roadmap

See [docs/architecture-roadmap.md](docs/architecture-roadmap.md). It describes intended architecture; some components are not implemented.

## Disclaimer

Stockbot is for research and informational purposes only. It is not investment, legal, tax, or financial advice. Verify source data and make your own decisions.

## License

[AGPL-3.0-only](LICENSE)
