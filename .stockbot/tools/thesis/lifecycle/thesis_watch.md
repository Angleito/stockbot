# thesis_watch

Domain: thesis
Family: lifecycle
Intent: add_thesis_watch
Output kind: governed_action
Source: local
Entity scope: single_thesis
Time mode: current

Add a monitoring rule that alerts when a thesis condition triggers.

## Choose when

- Setting an alert on a thesis invalidator or trigger.

## Reject when

- Not for logging notes.

## Conflicts with

None

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
