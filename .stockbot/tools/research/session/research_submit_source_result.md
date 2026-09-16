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
- Sufficient coverage means major EDGAR-visible channels and counterparties investigated with no material open questions or branches — never just N evidence rows.

## Reject when

- Not for session state overviews (research_status).

## Conflicts with

None

## Related tools

- research_status

## Prerequisites

None

## Required arguments

- `coverage` (object): Coverage with useful_for_question sufficient|insufficient (required). New sufficiency keys (major_entities_investigated, relationship_types_checked, forms_examined, exhibits_examined, material_open_questions, major_entities_missing, remaining_branches, routes_unsearched) ride alongside existing resolved/partially_resolved/unresolved/source_limitations/dates/partitions/docs/gaps; when any sufficiency key is present, sufficient requires non-empty investigated entities/relationships/forms/exhibits and empty material opens/missing entities/remaining branches/routes, else the legacy envelope applies.
- `evidence_ids` (array): Evidence IDs grounding a sufficient result (empty only with insufficient).
- `job_id` (string): Running source job ID to complete.
- `session_id` (string): Research session ID.
- `unresolved_questions` (array): Open questions left by the source run.

## Optional arguments

None
