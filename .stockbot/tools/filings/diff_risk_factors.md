# diff_risk_factors

Domain: filings

Self-contained Risk Factors year-over-year diff for one ticker: what is new or changed.

## Use when

- What is new or changed in a company's risk disclosures for one ticker.

## Avoid when

- Do NOT use for full-filing diffs between accessions (diff_sec_filings).
- Do NOT use for disclosure search without change framing (search_sec_filings).
- Self-contained for one ticker; do NOT call list_sec_filings before or after.

## Related tools

- diff_sec_filings
- search_sec_filings

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

None
