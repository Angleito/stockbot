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
- Answer from the filing record; do not retrieve document text unless the question asks for it.

## Conflicts with

None

## Related tools

- list_sec_filings
- list_sec_documents

## Prerequisites

None

## Required arguments

- `accession_no` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
