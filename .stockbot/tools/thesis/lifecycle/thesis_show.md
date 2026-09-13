# thesis_show

Domain: thesis
Family: lifecycle
Intent: retrieve_thesis
Output kind: current_snapshot
Source: local
Entity scope: single_thesis
Time mode: latest_or_as_of

Read a thesis: its status, assessment, and current state. Pass the thesis ID as thesis:<uuid>.

## Choose when

- Checking a thesis and its current assessment.

## Reject when

- Not for changing a thesis.
- Not for listing or adding monitoring rules and alerts (thesis_watch).

## Conflicts with

- thesis_watch

## Related tools

- thesis_create
- thesis_refine
- thesis_journal
- thesis_watch

## Prerequisites

None

## Required arguments

- `id` (string): Thesis ID or slug.

## Optional arguments

- `as_of` (string): Point-in-time cutoff (ISO-8601); omit for current state.
