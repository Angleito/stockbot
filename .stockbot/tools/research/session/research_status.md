# research_status

Domain: research
Family: session
Intent: inspect_research
Output kind: current_snapshot
Source: local
Entity scope: single_session
Time mode: latest_or_as_of

Read a research session with its jobs and pending next action.

## Choose when

- Checking a research session and its current state.

## Reject when

- Not for starting a session (research_start).
- Not for resuming wave and budget state (research_resume).

## Conflicts with

- research_start
- research_resume

## Related tools

- research_start
- research_resume
- research_read
- research_cancel

## Prerequisites

None

## Required arguments

- `session_id` (string): Research session ID.

## Optional arguments

None
