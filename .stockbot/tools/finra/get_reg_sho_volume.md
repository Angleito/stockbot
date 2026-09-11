# get_reg_sho_volume

Domain: finra

FINRA daily Reg SHO short-sale volume by reporting facility for a ticker, rolling 12 months.

## Use when

- Checking daily short-sale volume breakdowns for one ticker.

## Avoid when

- Not for biweekly short interest positions.

## Related tools

- get_short_interest
- query_finra

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `tradeDate` (string): Optional trade date YYYY-MM-DD.
