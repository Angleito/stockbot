# get_short_interest

Domain: finra
Family: short-interest
Intent: current_reported_short_position
Output kind: current_snapshot
Source: finra
Entity scope: single_security
Time mode: latest_or_as_of

Biweekly short position for one ticker: FINRA short interest, days to cover, percent change.

## Choose when

- One ticker's current short interest, short float, or days to cover.

## Reject when

- Do NOT use for daily short-sale volume by venue (get_reg_sho_volume).
- Do NOT use for market-wide most-shorted screens (get_short_interest_leaderboard).
- Do NOT use for short-vs-shares context (get_short_pressure_profile).
- Do NOT use for exact source values (get_finra_datapoints).
- Do NOT use for analyzed briefings or trends over a dataset (query_finra).

## Conflicts with

- get_finra_datapoints
- get_reg_sho_volume
- get_short_pressure_profile
- query_finra

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
