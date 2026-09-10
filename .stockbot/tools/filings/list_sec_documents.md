# list_sec_documents

Domain: filings

Index of documents and exhibits attached to one filing, looked up by accession number.

## Use when

- Seeing which exhibits a filing contains before reading any document text.
- What documents are attached to filing.

## Avoid when

- Do not use to read document text; use get_sec_document instead.

## Related tools

- get_sec_filing
- get_sec_document

## Prerequisites

- list_sec_filings

## Required arguments

- `accession_no` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
