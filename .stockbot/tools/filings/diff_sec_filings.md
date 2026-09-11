# diff_sec_filings

Domain: filings

Self-contained full-filing diff for one ticker or two accessions: amendment versus prior version.

## Use when

- Comparing a ticker's latest amendment filing versus its predecessor filing.
- Comparing two known filing accessions for amendment or restatement changes.

## Avoid when

- Do NOT use for risk-factor-only year-over-year diffs (diff_risk_factors).
- Do NOT call list_sec_filings first; ticker resolution is internal.

## Related tools

- diff_risk_factors
- get_sec_filing

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; filings known after it are excluded.
- `current_accession` (string)
- `forms` (array)
- `previous_accession` (string)
- `section` (string)
- `ticker` (string)
