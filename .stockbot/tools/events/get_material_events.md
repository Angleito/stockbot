# get_material_events

Domain: events

Deterministic 8-K-derived recent event feed with accession citations for what changed since a date.

## Use when

- Answering what changed or what is new for a company since a date, including 8-K events behind a move.
- Linking an 8-K event to a stock move over the past days or weeks, including jumps, falls, fallen, or rallies after earnings.

## Avoid when

- Does not cover market reaction or news commentary.
- Answer from the event feed; do not open filing documents unless the question needs document text.

## Related tools

- get_sec_document
- search_web
- get_recent_ownership_filings

## Prerequisites

None

## Required arguments

- `since` (string)
- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
