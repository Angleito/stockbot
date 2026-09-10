# search_sec_filings

Domain: filings

What was disclosed about risk factors in recent filings: full-text EDGAR SEC search and retrieval across entity, EFTS, and 10-K/10-Q routes, with disclosure language and mentions.

## Use when

- Searching filing text or mentions when the exact accession number is unknown.
- Risk-factor language used in recent SEC filings.
- Risk-factor language in SEC filings.

## Avoid when

- Do not use to list filings for a known ticker; use list_sec_filings instead.
- Use the given ticker directly; no entity lookup is needed.

## Related tools

- list_sec_filings
- get_sec_filing
- find_sec_entities

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
