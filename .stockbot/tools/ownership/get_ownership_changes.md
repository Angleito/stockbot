# get_ownership_changes

Domain: ownership

Deterministic diffs between a holder's consecutive 13D/G filings: share and percent changes, changed stakes and positions.

## Use when

- Tracking how one holder's stake increased or decreased between filings.
- Changed their stakes.

## Avoid when

- Do not use for the current snapshot of holders; use get_beneficial_ownership.

## Related tools

- get_beneficial_ownership

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
