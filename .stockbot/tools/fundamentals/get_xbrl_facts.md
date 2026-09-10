# get_xbrl_facts

Domain: fundamentals

XBRL-tagged financial facts such as revenue, net income, cash, debt, or equity for a ticker. Concept names look like NetIncomeLoss.

## Use when

- Fetching a tagged line-item value such as revenue or total debt.

## Avoid when

- Do not use for EPS; use get_fundamentals with metric eps instead.

## Related tools

- get_fundamentals
- get_financial_statements

## Prerequisites

None

## Required arguments

- `concept` (string)
- `ticker` (string)

## Optional arguments

None
