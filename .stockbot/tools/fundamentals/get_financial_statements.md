# get_financial_statements

Domain: fundamentals

Parsed income statement, balance sheet, and cash flow from 10-K or 10-Q filings: revenue, expenses, profit.

## Use when

- Reading full financial statements rather than one numeric metric.

## Avoid when

- Not for a single metric like EPS.
- Answer from the statements; do not re-pull single metrics.

## Related tools

- get_fundamentals
- get_xbrl_facts

## Prerequisites

None

## Required arguments

- `statement_type` (string)
- `ticker` (string)

## Optional arguments

None
