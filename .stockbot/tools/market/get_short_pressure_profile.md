# get_short_pressure_profile

Domain: market

Short pressure vs shares outstanding for one ticker: FINRA positioning plus SEC shares and ratio.

## Use when

- Short positioning relative to shares outstanding for one ticker.

## Avoid when

- Do NOT use for biweekly short position alone (get_short_interest).
- Do NOT use for daily short-sale volume (get_reg_sho_volume).

## Related tools

- get_short_interest
- query_finra
- get_reg_sho_volume

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

None
