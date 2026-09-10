# get_insider_activity

Domain: insider

Executed insider transactions from Forms 3/4/5 with SEC codes mapped to buy, sell, or grant.

## Use when

- Answering insider sale questions: actual insider purchases and sales by executives and directors.

## Avoid when

- Do not use for planned but unexecuted sales; use get_planned_insider_sales.

## Related tools

- get_planned_insider_sales
- get_beneficial_ownership

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
