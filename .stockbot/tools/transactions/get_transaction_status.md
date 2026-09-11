# get_transaction_status

Domain: transactions

M&A filing context: tender offers, 14D-9 recommendations, S-4s, and merger proxies.

## Use when

- Checking merger, acquisition, or tender-offer filing context for a ticker.

## Avoid when

- Not for governance or proxy votes.

## Related tools

- get_governance_events
- get_sec_document

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
