# get_beneficial_ownership

Domain: ownership

Current 5%+ beneficial-ownership stakes (SC 13D/G): holder, shares, percent, voting powers.

## Use when

- Finding who owns more than 5% of a company.

## Avoid when

- Do not use for stake changes over time; use get_ownership_changes instead.
- Answer from these records; do not open filings or pull changes unless asked.

## Related tools

- get_ownership_changes
- search_sec_relationships

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
