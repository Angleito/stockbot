# get_short_interest

Domain: finra

FINRA consolidated short interest amounts sold short for a ticker: position, days to cover, and percent change.

## Use when

- Answering current short interest or days to cover for one ticker.
- Sold short.

## Avoid when

- Not for change-over-time trends.
- Answer from this result; do not pull positioning context unless asked.
- Not for exact source values or figures.

## Related tools

- query_finra
- get_finra_datapoints
- get_reg_sho_volume

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `settlementDate` (string): Optional settlement date YYYY-MM-DD. Omit to return recent cycles.
