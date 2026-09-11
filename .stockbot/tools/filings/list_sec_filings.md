# list_sec_filings

Domain: filings

List EDGAR filings for an exact ticker or CIK, filterable by form and date range.

## Use when

- Listing 10-K, 10-Q, or 8-K filings once the exact ticker or CIK is verified, including the latest 10-K or 10-Q.
- Filing history: what was filed with the SEC lately, recent filings included.

## Avoid when

- Do not guess an identifier from a bare company name; use the exact ticker when known, otherwise resolve the company's exact identifier first.

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
