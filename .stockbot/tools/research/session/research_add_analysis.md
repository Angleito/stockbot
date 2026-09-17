# research_add_analysis

Domain: research
Family: session
Intent: add_committee_analysis
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Record one committee analysis (stockbot, bullbot, or bearbot); claim refs are validated against frozen evidence.

## Choose when

- Recording a trio analysis grounded in the frozen evidence.

## Reject when

- Not for session state overviews (research_status).

## Conflicts with

None

## Related tools

- research_status

## Prerequisites

None

## Required arguments

- `analysis` (object): Committee output with claims grounded in frozen evidence IDs.
- `job_id` (string): Running job ID the analysis belongs to.
- `role` (string): Committee role authoring the analysis.
- `session_id` (string): Research session ID.

## Optional arguments

None
