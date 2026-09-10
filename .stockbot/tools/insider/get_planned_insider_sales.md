# get_planned_insider_sales

Domain: insider

Planned insider sales from Form 144 notices: proposed sales not yet executed.

## Use when

- Seeing insider sales that are planned but may not have happened yet.
- Insiders planning to sell.
- Planned insider sales.

## Avoid when

- Do not use for completed insider trades; use get_insider_activity instead.

## Related tools

- get_insider_activity

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
