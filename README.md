# Stockbot

Stockbot is a local-first AI investment research system designed to give individual investors hedge-fund-style research infrastructure. Today it is the research and data foundation for that direction, not an automated trading or consumer portfolio-management product.

## Current capabilities

- SEC EDGAR research, filing extraction, and deterministic financial calculations
- FINRA datasets, short-interest screens, and Regulation SHO analysis
- Point-in-time `known_at` data handling, provenance, raw-data archiving, normalized Parquet datasets, and DuckDB analytics
- Obligation and valuation analysis
- A tool-using research agent using OpenRouter-backed models
- Analyst-consensus and market-data adapters
- Optional Robinhood portfolio, quote, option, and saved-scanner **read** integration with portfolio analytics

## Architecture principles

Stockbot keeps raw source data and normalized analytical data separate, preserves when data became knowable, and favors deterministic calculations over model arithmetic. The LLM selects bounded tools and explains evidence; it is not the primary database or calculator.

## Requirements

- Python 3.12+ (the pinned dependencies are tested locally)
- An SEC EDGAR identity for SEC requests
- An OpenRouter API key for chat or LLM-generated summaries

## Local setup

```bash
cp .env.example .env
# Fill only the credentials for integrations you intend to use.
make setup
```

Offline tests do not need FINRA, Robinhood, SEC, or OpenRouter credentials.

## Environment variables

See [`.env.example`](.env.example) for the current list and whether each setting is required, optional, or testing-only. `SEC_EDGAR_IDENTITY` and `OPENROUTER_API_KEY` are required only for the features that use them. FINRA and Robinhood are optional integrations.

## Running the CLI

```bash
venv/bin/python cli.py
# or choose a model explicitly
venv/bin/python cli.py --model provider/model-name
```

## Running the API

```bash
venv/bin/uvicorn app.main:app --reload
```

The API exposes `POST /chat` and `GET /health` locally.

## Running tests

```bash
make test
make typecheck
make test-collect
# Fresh-environment verification:
make verify
```

The default suite is offline. FINRA and Robinhood smoke tests are opt-in Make targets.

## Optional Robinhood integration

Set `ROBINHOOD_ENABLED=true` and complete the local OAuth login flow before using the integration. Stockbot only permits explicitly allowlisted read operations and blocks unknown operations. It blocks order placement, order cancellation/replacement, option exercise, withdrawals, deposits, and transfers. It does not support trading or modifying saved scanners.

## Data storage

Runtime data is local and ignored by Git under `data/`. Stockbot archives raw source responses and stores normalized datasets as Parquet for DuckDB analytics. Robinhood OAuth state is intended to stay local at `~/.stockbot/robinhood/oauth.json` with restrictive permissions.

## Privacy and external services

Stockbot runs locally, but selected functionality calls external APIs including SEC EDGAR, FINRA, Robinhood MCP, and market-data sources. When an OpenRouter-backed model is used, prompts and selected research/tool context—including applicable portfolio research context—may be sent to OpenRouter for inference. Raw Robinhood OAuth credentials remain local; Stockbot omits brokerage account identifiers from the model-facing portfolio payload.

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

[MIT](LICENSE)
