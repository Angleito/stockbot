# get_ownership_changes

Domain: ownership

Deterministic diffs between a holder's consecutive 13D/G filings: share and percent changes, changed stakes and positions.

## Use when

- Comparing consecutive 13D/G filings for changes in a holder's stake.

## Avoid when

- Not for the current snapshot of holders.

## Related tools

- get_beneficial_ownership

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
