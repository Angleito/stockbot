# thesis_status

Domain: thesis
Family: lifecycle
Intent: change_thesis_status
Output kind: governed_action
Source: local
Entity scope: single_thesis
Time mode: current

Pause, resume, or close thesis monitoring. Pass the thesis ID as thesis:<uuid>.

## Choose when

- Pausing monitoring without deleting rules, resuming a paused thesis, or closing a thesis.

## Reject when

- Not for reading thesis state (thesis_show).
- Not for editing claims or rules (thesis_refine, thesis_watch).

## Conflicts with

None

## Related tools

- thesis_show

## Prerequisites

None

## Required arguments

- `action` (string): Status change to apply.
- `id` (string): Thesis ID or slug.

## Optional arguments

None
