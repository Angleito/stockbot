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

- `item` (object): Finding with content, source, and provenance fields.
- `job_id` (string): Running job ID the finding belongs to.
- `session_id` (string): Research session ID.

## Optional arguments

None
