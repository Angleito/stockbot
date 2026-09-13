# find_sec_entities

Domain: sec
Family: entity-discovery
Intent: resolve_sec_entity
Output kind: candidate_records
Source: sec
Entity scope: entity_query
Time mode: current

Resolve a company name, ticker, or CIK to verified SEC entity candidates with CIKs and tickers.

## Choose when

- Starting from a company name when the exact ticker or CIK is not known.

## Reject when

- Unneeded when the exact ticker or CIK is already known.

## Conflicts with

None

## Related tools

- list_sec_filings
- search_sec_filings

## Prerequisites

None

## Required arguments

- `query` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; former names apply only within their known/valid interval.
- `exhaustive` (boolean): Fan out over all entity routes (default false; non-exhaustive).
- `limit` (integer): Max candidates to return (default 20); higher values probe deeper.
