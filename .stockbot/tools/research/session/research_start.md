# research_start

Domain: research
Family: session
Intent: start_research
Output kind: governed_action
Source: local
Entity scope: single_session
Time mode: current

Start a research session for a question; returns the session ID with its first job and next action.

## Choose when

- Starting research on a new question.

## Reject when

- Not for checking session state (research_status).
- Not for cancelling a session (research_cancel).

## Conflicts with

- research_status
- research_cancel

## Related tools

- research_status
- research_cancel
- research_resume

## Prerequisites

None

## Required arguments

- `question` (string): Research question.

## Optional arguments

- `as_of` (string): Point-in-time cutoff (ISO-8601); omit for current state.
- `objective` (string): Objective (defaults to the question).
