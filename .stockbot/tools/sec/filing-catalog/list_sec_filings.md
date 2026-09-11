# list_sec_filings

Domain: sec
Family: filing-catalog
Intent: list_entity_filings
Output kind: filing_series
Source: sec
Entity scope: single_entity
Time mode: date_range_or_as_of

List EDGAR filings for an exact ticker or CIK, filterable by form and date range.

## Choose when

- Listing filings for an exact ticker or CIK, optionally filtered by form or date.

## Reject when

- Do not guess an identifier from a bare company name; use the exact ticker when known, otherwise resolve the company's exact identifier first.
- Do NOT use for disclosure search without known identifier (search_sec_filings).

## Conflicts with

- search_sec_filings

## Related tools

- get_sec_filing
- search_sec_filings
- find_sec_entities

## Prerequisites

None

## Required arguments

- `identifier` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
- `end_date` (string)
- `forms` (array)
- `limit` (integer)
- `start_date` (string)
