# search_company_patents

Domain: patents

Company patent search: publications, assignees, counts, and classifications.

## Use when

- Finding patents a company filed or patented lately, with publication counts and classifications.

## Avoid when

- Do not use for financial or filing questions; use the matching fundamentals tool.
- Answer from patent records; do not run a web search for fresher filings.
- Use company names directly; no entity lookup is needed.

## Related tools

None

## Prerequisites

None

## Required arguments

- `assignees` (array): Documented assignee aliases (verified, never inferred from matching text).
- `company_id` (string): Documented assignee name from existing company evidence.

## Optional arguments

- `end_date` (string): Range end YYYY-MM-DD.
- `limit` (integer): Max publications (default 20).
- `start_date` (string): Range start YYYY-MM-DD.
