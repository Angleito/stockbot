# research_finalize

Domain: research
Family: session
Intent: finalize_research
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Persist the trio-joined synthesis and complete a research session with frozen-evidence claims.

## Choose when

- Finalizing a trio-complete research session with grounded claims.
- Delivering the substantive structured answer in the same turn — Bottom line through evidence refs plus the searched-source scope line; a bare finalized-status note is not a completion.

## Reject when

- Not for session state overviews (research_status).

## Conflicts with

None

## Related tools

- research_status

## Prerequisites

None

## Required arguments

- `answer` (string): Final synthesis prose.
- `claims` (array): Grounded findings; each claim needs non-empty text and non-empty evidence IDs from the freeze.
- `session_id` (string): Research session ID.

## Optional arguments

None
