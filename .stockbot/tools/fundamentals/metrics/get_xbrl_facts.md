# get_xbrl_facts

Domain: fundamentals
Family: metrics
Intent: retrieve_xbrl_concept
Output kind: fact_records
Source: sec
Entity scope: single_security
Time mode: latest

Single XBRL-tagged fact by concept name: revenue, net income, cash, debt, or equity.

## Choose when

- One tagged line-item value by exact XBRL concept name.

## Reject when

- Do NOT use for EPS (get_fundamentals).
- Do NOT use for full statements (get_financial_statements).

## Conflicts with

- get_financial_statements
- get_fundamentals

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
