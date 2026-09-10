# thesis_journal

Domain: thesis

Append an operator note or journal entry to a thesis log. Pass the thesis ID as thesis:<uuid>.

## Use when

- Logging a dated note or observation against a thesis.
- Add that to thesis.

## Avoid when

- Do not use to revise claims; use thesis_refine instead.

## Related tools

- thesis_show
- thesis_refine

## Prerequisites

- thesis_show

## Required arguments

- `body` (string): Note body (Markdown).
- `id` (string): Thesis ID or slug.

## Optional arguments

- `known_at` (string): PIT cutoff this entry is known at (ISO-8601).
- `run_id` (string): Live run this entry completes (trigger-linked only).
- `title` (string)
- `trigger_id` (string): Trigger this entry completes (omit for ordinary notes).
