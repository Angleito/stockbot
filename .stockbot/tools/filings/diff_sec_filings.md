# diff_sec_filings

Domain: filings

Deterministic diff between two filings by accession numbers: whether one filing differs from another, e.g. amendment versus prior version.

## Use when

- Comparing an amendment or restatement against its prior filing version.
- What changed between filings.

## Avoid when

- Do not use for risk-factor-only changes; use diff_risk_factors instead.

## Related tools

- diff_risk_factors
- get_sec_filing

## Prerequisites

- list_sec_filings

## Required arguments

- `current_accession` (string)
- `previous_accession` (string)

## Optional arguments

- `section` (string)
