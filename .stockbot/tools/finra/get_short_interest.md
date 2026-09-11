# get_short_interest

Domain: finra

Biweekly short position for one ticker: FINRA short interest, days to cover, percent change.

## Use when

- One ticker's current short interest, short float, or days to cover.

## Avoid when

- Do NOT use for daily short-sale volume by venue (get_reg_sho_volume).
- Do NOT use for market-wide most-shorted screens (get_short_interest_leaderboard).
- Do NOT use for short-vs-shares context (get_short_pressure_profile).
- Do NOT use for exact source values (get_finra_datapoints).

## Related tools

- query_finra
- get_finra_datapoints
- get_reg_sho_volume
- get_short_pressure_profile
- get_short_interest_leaderboard

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `settlementDate` (string): Optional settlement date YYYY-MM-DD. Omit to return recent cycles.
