# research_submit_source_result

Domain: research
Family: session
Intent: submit_source_result
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Complete one running source job with validated coverage; evidence stays mutation-only.

## Choose when

- Completing a source investigation with validated coverage.

## Reject when

- Not for session state overviews (research_status).

## Conflicts with

None

## Related tools

- research_status

## Prerequisites

None

## Required arguments

- `coverage` (object): Coverage with useful_for_question sufficient|insufficient (required).
- `evidence_ids` (array): Evidence IDs grounding a sufficient result (empty only with insufficient).
- `job_id` (string): Running source job ID to complete.
- `session_id` (string): Research session ID.
- `unresolved_questions` (array): Open questions left by the source run.

## Optional arguments

None
