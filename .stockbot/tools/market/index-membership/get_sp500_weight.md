# get_sp500_weight

Domain: market
Family: index-membership
Intent: retrieve_sp500_weight
Output kind: current_snapshot
Source: slickcharts
Entity scope: single_security
Time mode: latest

A company's current weight and rank in the S&P 500 index from the constituent list.

## Choose when

- Answering what percent of the S&P 500 a ticker represents.

## Reject when

- Do not use for valuation or short-positioning questions.

## Conflicts with

None

## Related tools

- get_analyst_estimates

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

None
