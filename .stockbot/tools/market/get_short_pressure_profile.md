# get_short_pressure_profile

Domain: market

Short-positioning context combining FINRA data with SEC shares outstanding and their ratio.

## Use when

- Getting short-positioning context relative to shares outstanding for one ticker.

## Avoid when

- Do not use for short interest over time; use query_finra or get_short_interest.

## Related tools

- get_short_interest
- query_finra

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

None
