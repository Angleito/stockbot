# get_sec_filing

Domain: sec
Family: filing-catalog
Intent: retrieve_filing_metadata
Output kind: record
Source: sec
Entity scope: single_filing
Time mode: as_of

One filing's record by accession number: filer, form, dates, primary document, source URL.

## Choose when

- Fetching filing metadata after its accession number is known.

## Reject when

- Does not discover filings; list or search for the accession when unknown.
- Do NOT use for document text windows (get_sec_document).
- Do NOT use to list a filing's documents or exhibits (list_sec_documents).

## Conflicts with

- get_sec_document
- list_sec_documents

## Related tools

- list_sec_filings
- list_sec_documents

## Prerequisites

None

## Required arguments

- `accession_no` (string): SEC accession number, e.g. 0000320193-25-000079. Named accession_no, not accession_number.

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
