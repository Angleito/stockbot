# list_sec_documents

Domain: sec
Family: filing-catalog
Intent: list_filing_documents
Output kind: record_series
Source: sec
Entity scope: single_filing
Time mode: as_of

Index of documents and exhibits attached to one filing, looked up by accession number.

## Choose when

- Listing documents and exhibits attached to a known filing accession.

## Reject when

- Does not return document text.

## Conflicts with

None

## Related tools

- get_sec_filing
- get_sec_document
- list_sec_filings

## Prerequisites

None

## Required arguments

- `accession_no` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
