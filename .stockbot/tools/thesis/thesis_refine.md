# thesis_refine

Domain: thesis

Update a thesis with clarifications and deltas. Pass the thesis ID as thesis:<uuid>.

## Use when

- Revising a thesis after new evidence or feedback.
- Update thesis.

## Avoid when

- Not for routine notes.

## Related tools

- thesis_show
- thesis_journal

## Prerequisites

None

## Required arguments

- `clarification` (string): New information or correction in the user's own words.
- `id` (string): Thesis ID or slug.

## Optional arguments

- `assumptions` (array)
- `claims` (array)
- `expressions` (array)
- `invalidators` (array)
- `questions` (array)
- `scope` (string): Ticker scope (e.g. NVDA) or 'unknown'.
- `unknowns` (array)
