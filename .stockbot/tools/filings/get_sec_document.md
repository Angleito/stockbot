# get_sec_document

Domain: filings

Bounded text window of one filing document by accession number for targeted excerpt reading.

## Use when

- Reading a specific section such as MD&A or risk factors from a known accession.

## Avoid when

- Do not use for what-changed questions; use get_material_events first.

## Related tools

- get_sec_filing
- get_material_events

## Prerequisites

- get_material_events

## Required arguments

- `accession_no` (string)

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
- `document_name` (string)
- `max_chars` (integer): Characters to return, 1..32000 (default 12000).
- `offset` (integer): Character offset into the document text (default 0).
