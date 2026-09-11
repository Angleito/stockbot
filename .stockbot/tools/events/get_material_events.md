# get_material_events

Domain: events

Deterministic 8-K-derived recent event feed with accession citations for what changed since a date.

## Use when

- Finding recent 8-K-derived events for a company since a date.

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
