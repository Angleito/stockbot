# thesis_watch

Domain: thesis

Add a monitoring rule that alerts when a thesis condition triggers.

## Use when

- Setting an alert on a thesis invalidator or trigger.
- What am I watching for.

## Avoid when

- Do not use to log notes; use thesis_journal instead.

## Related tools

- thesis_show

## Prerequisites

None

## Required arguments

- `id` (string): Thesis ID or slug.

## Optional arguments

- `claim_ids` (array)
- `expression_ids` (array)
- `rule_type` (string): Semantic monitor name to add (omit to only list rules).
