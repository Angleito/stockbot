# get_sec_filing

Domain: filings

One filing's record by accession number: filer, form, dates, primary document, source URL.

## Use when

- Fetching a filing's metadata once its accession number is known.
- What is in filing.
- Filing record.

## Avoid when

- Does not discover filings; list or search for the accession when unknown.
- Answer from the filing record; do not retrieve document text unless the question asks for it.

## Related tools

- list_sec_filings
- list_sec_documents

## Prerequisites

None

## Required arguments

- `accession_no` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
