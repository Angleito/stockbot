# diff_sec_filings

Domain: filings

Full-filing diff between two accessions: amendment versus prior version, all sections in context.

## Use when

- Comparing two known filing accessions for amendment or restatement changes.

## Avoid when

- Do NOT use for risk-factor-only year-over-year diffs (diff_risk_factors).

## Related tools

- diff_risk_factors
- get_sec_filing
- list_sec_filings

## Prerequisites

None

## Required arguments

- `current_accession` (string)
- `previous_accession` (string)

## Optional arguments

- `section` (string)
