# research_cancel

Domain: research
Family: session
Intent: cancel_research
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Cancel a research session; terminal sessions return current state.

## Choose when

- Stopping a research session that is no longer needed.

## Reject when

- Not for starting a session (research_start).

## Conflicts with

- research_start

## Related tools

- research_start
- research_status

## Prerequisites

None

## Required arguments

- `session_id` (string): Research session ID.

## Optional arguments

None
