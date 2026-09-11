# diff_sec_filings

Domain: filings

Deterministic diff between two filings by accession numbers: whether one filing differs from another, e.g. amendment versus prior version.

## Use when

- Comparing two known filing accessions for amendment or restatement changes.

## Avoid when

- Not for risk-factor-only changes.

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
