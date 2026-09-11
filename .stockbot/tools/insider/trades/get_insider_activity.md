# get_insider_activity

Domain: insider
Family: trades
Intent: retrieve_executed_insider_trades
Output kind: transaction_series
Source: sec
Entity scope: single_security
Time mode: latest_or_as_of

Executed insider buys/sells for one ticker: actual purchases and sales from Forms 3/4/5.

## Choose when

- One ticker's executed insider sales (buys/sells) by executives and directors (Forms 3/4/5).

## Reject when

- Do NOT use for planned but unexecuted Form 144 sales (get_planned_insider_sales).

## Conflicts with

- get_planned_insider_sales

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
