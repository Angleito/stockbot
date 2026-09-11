# thesis_watch

Domain: thesis

Add a monitoring rule that alerts when a thesis condition triggers.

## Use when

- Setting an alert on a thesis invalidator or trigger.

## Avoid when

- Not for logging notes.

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
