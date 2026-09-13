# get_analyst_estimates

Domain: analyst
Family: estimates
Intent: retrieve_forward_consensus
Output kind: forecast_snapshot
Source: yahoo_finance
Entity scope: single_security
Time mode: latest

Forward sell-side consensus expectations: targets, ratings, forward EPS/revenue, revisions.

## Choose when

- What analysts expect for one ticker: targets, consensus EPS, or estimate revisions.

## Reject when

- Do NOT use for reported historical EPS (get_fundamentals).
- Do NOT use for cheap-vs-expensive multiples (get_valuation_metrics).

## Conflicts with

- get_fundamentals
- get_valuation_metrics

## Related tools

- get_valuation_metrics
- get_fundamentals

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

None
