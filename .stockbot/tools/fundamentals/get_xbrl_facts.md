# get_xbrl_facts

Domain: fundamentals

Single XBRL-tagged fact by concept name: revenue, net income, cash, debt, or equity.

## Use when

- One tagged line-item value by exact XBRL concept name.

## Avoid when

- Do NOT use for EPS (get_fundamentals).
- Do NOT use for full statements (get_financial_statements).

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
