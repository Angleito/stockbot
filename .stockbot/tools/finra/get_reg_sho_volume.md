# get_reg_sho_volume

Domain: finra

Self-contained daily short-sale volume by venue for one ticker: FINRA Reg SHO volume, rolling 12 months.

## Use when

- Daily short-sale volume or venue breakdowns for one ticker.

## Avoid when

- Do NOT use for biweekly short interest positions (get_short_interest).
- Do NOT call describe_finra_dataset or get_finra_datapoints; dataset and fields resolve internally.

## Related tools

- get_short_interest
- query_finra

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `tradeDate` (string): Optional trade date YYYY-MM-DD.
