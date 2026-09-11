# get_material_events

Domain: events
Family: company-events
Intent: retrieve_recent_material_events
Output kind: event_series
Source: sec
Entity scope: single_security
Time mode: since_or_as_of

Deterministic 8-K-derived recent event feed with accession citations for what changed since a date.

## Choose when

- Finding what changed recently: recent 8-K-derived events for a company since a date.

## Reject when

- Does not cover market reaction or news commentary.
- Answer from the event feed; do not open filing documents unless the question needs document text.

## Conflicts with

None

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
