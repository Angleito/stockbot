# get_fundamentals

Domain: fundamentals

Single reported fundamental for one ticker: EPS, dividends, balance-sheet item, or shares outstanding.

## Use when

- One specific numeric fundamental for one ticker: EPS, dividends, or shares outstanding.

## Avoid when

- Do NOT use for full statements (get_financial_statements).
- Do NOT use for XBRL facts by concept (get_xbrl_facts).
- Do NOT use for cheap-vs-expensive multiples (get_valuation_metrics).
- Do NOT use for forward consensus (get_analyst_estimates).

## Related tools

- get_xbrl_facts
- get_financial_statements
- get_valuation_metrics
- get_analyst_estimates

## Prerequisites

None

## Required arguments

- `metric` (string)
- `ticker` (string)

## Optional arguments

- `as_of` (string): Point-in-time query date YYYY-MM-DD; store-backed for eps/shares_outstanding/dividends; live results are labeled data_source=live.
