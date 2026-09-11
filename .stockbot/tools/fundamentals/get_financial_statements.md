# get_financial_statements

Domain: fundamentals

Full parsed statements for one ticker: income statement, balance sheet, and cash flow.

## Use when

- Full financial statements rather than one numeric metric.

## Avoid when

- Do NOT use for a single metric like EPS (get_fundamentals).
- Do NOT use for a single XBRL fact (get_xbrl_facts).

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
