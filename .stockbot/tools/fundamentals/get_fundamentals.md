# get_fundamentals

Domain: fundamentals

Reported EPS including diluted EPS, dividends, balance-sheet items, or shares outstanding for a ticker from SEC filings.

## Use when

- Asking for a specific numeric fundamental (what a company earns) such as EPS, earnings per share, dividends, or shares outstanding.

## Avoid when

- Do not use for forward analyst expectations; use get_analyst_estimates instead.

## Related tools

- get_xbrl_facts
- get_financial_statements
- get_valuation_metrics

## Prerequisites

None

## Required arguments

- `metric` (string)
- `ticker` (string)

## Optional arguments

- `as_of` (string): Point-in-time query date YYYY-MM-DD; store-backed for eps/shares_outstanding/dividends; live results are labeled data_source=live.
