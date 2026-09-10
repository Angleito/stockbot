# thesis_show

Domain: thesis

Read a thesis: its status, assessment, and current state. Pass the thesis ID as thesis:<uuid>.

## Use when

- Checking a thesis and its current assessment.

## Avoid when

- Do not use to change a thesis; use thesis_refine instead.

## Related tools

- thesis_create
- thesis_refine
- thesis_journal

## Prerequisites

None

## Required arguments

- `id` (string): Thesis ID or slug.

## Optional arguments

- `as_of` (string): Point-in-time cutoff (ISO-8601); omit for current state.
