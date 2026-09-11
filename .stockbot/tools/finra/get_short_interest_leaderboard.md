# get_short_interest_leaderboard

Domain: finra

Market-wide most-shorted screen: ranked stocks by short interest as a percent of SEC shares.

## Use when

- Screening which stocks are the most shorted across the market.

## Avoid when

- Do NOT use for one ticker's short interest (get_short_interest).

## Related tools

- get_short_interest

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `as_of` (string): Optional knowledge horizon (YYYY-MM-DD). Only data knowable on or before this date is used. Defaults to today; pass an explicit date for a historical screen.
- `limit` (integer): Number of ranked stocks to return; default 10, maximum 25.
- `settlement_date` (string): Optional FINRA settlement date (YYYY-MM-DD). Omit for the latest published FINRA cycle.
