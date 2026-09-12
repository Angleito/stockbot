# get_sec_document

Domain: sec
Family: filing-catalog
Intent: read_filing_document
Output kind: text_window
Source: sec
Entity scope: single_document
Time mode: as_of

Bounded text window of one filing document by accession number for targeted excerpt reading.

## Choose when

- Reading a specific section such as MD&A or risk factors from a known accession.
- What the main document in a filing says; main-document text for a known accession.

## Reject when

- Do NOT use for filing metadata by accession (get_sec_filing).
- Do NOT use to list a filing's documents or exhibits (list_sec_documents).
- Not for what-changed questions.

## Conflicts with

- get_sec_filing
- list_sec_documents

## Related tools

- get_sec_filing
- get_material_events

## Prerequisites

None

## Required arguments

- `accession_no` (string): SEC accession number, e.g. 0000320193-25-000079. Named accession_no, not accession_number.

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
- `document_name` (string)
- `max_chars` (integer): Characters to return, 1..32000 (default 12000).
- `offset` (integer): Character offset into the document text (default 0).
