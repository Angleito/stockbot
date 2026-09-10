# get_sec_search_coverage

Domain: filings

Persisted SEC ingestion coverage and backfill-job status for a form, source, or date partition.

## Use when

- Checking whether a form or date partition is covered or still queued before searching.

## Avoid when

- Do not use to retrieve filing content; use get_sec_filing or get_sec_document.

## Related tools

- search_sec_filings

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `form` (string)
- `limit` (integer)
- `search_id` (string)
- `source` (string)
