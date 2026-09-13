# research_resume

Domain: research
Family: session
Intent: resume_research
Output kind: current_snapshot
Source: local
Entity scope: single_session
Time mode: latest_or_as_of

Resume a research session: read-only snapshot with wave, budgets, open jobs, and next action.

## Choose when

- Resuming or re-entering an existing research session.

## Reject when

- Not for checking jobs and next action without wave state (research_status).

## Conflicts with

- research_status

## Related tools

- research_status
- research_start

## Prerequisites

None

## Required arguments

- `session_id` (string): Research session ID.

## Optional arguments

None
