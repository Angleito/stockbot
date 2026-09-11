# get_insider_activity

Domain: insider

Executed insider buys/sells for one ticker: actual purchases and sales from Forms 3/4/5.

## Use when

- One ticker's executed insider buys/sells by executives and directors (Forms 3/4/5).

## Avoid when

- Do NOT use for planned but unexecuted Form 144 sales (get_planned_insider_sales).

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
