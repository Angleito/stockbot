# research_add_evidence

Domain: research
Family: session
Intent: add_research_evidence
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Record one finding on a research job; provenance, point-in-time, and IDs are kernel-validated.

## Choose when

- Recording a finding from a dispatched research job.

## Reject when

- Not for session state overviews (research_status).

## Conflicts with

None

## Related tools

- research_status

## Prerequisites

None

## Required arguments

- `item` (object): Finding whose provenance must match its claim_kind: raw filing passage for observed_fact, search scope for absence_observation.
- `job_id` (string): Running job ID the finding belongs to.
- `session_id` (string): Research session ID.

## Optional arguments

None
