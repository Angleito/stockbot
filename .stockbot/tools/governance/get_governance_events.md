# get_governance_events

Domain: governance

Proxy and governance filing context (DEF 14A, meetings, votes) with retrieval pointers.

## Use when

- Finding shareholder-meeting, proxy-vote, or board-compensation records.

## Avoid when

- Do not use for merger-deal status; use get_transaction_status instead.

## Related tools

- get_transaction_status

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
- `since` (string)
