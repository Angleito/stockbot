# search_sec_filings

Domain: filings

General EDGAR full-text disclosure search across entity, EFTS, and 10-K/10-Q routes, with mentions.

## Use when

- Searching SEC filing text or mentions when the accession number is unknown.

## Avoid when

- Not a filing lister for a known ticker (list_sec_filings).
- Do NOT use for year-over-year risk-factor changes (diff_risk_factors).

## Related tools

- list_sec_filings
- get_sec_filing
- find_sec_entities
- diff_risk_factors

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `accession_no` (string)
- `as_of` (string): Point-in-time date YYYY-MM-DD; records known after it are excluded.
- `cik` (string)
- `company_name` (string)
- `domain` (string)
- `end_date` (string)
- `exhaustive` (boolean): Fan out over all routes (default false; non-exhaustive).
- `forms` (array)
- `limit` (integer)
- `person_name` (string)
- `query` (string)
- `security_identifier` (string): Ticker, CUSIP, ISIN, or class title; never treated as issuer identity.
- `start_date` (string)
- `ticker` (string)
