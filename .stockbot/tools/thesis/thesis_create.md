# thesis_create

Domain: thesis

Start a new investment thesis proposal with scope, claims, and open questions.

## Use when

- Creating a new investment thesis to track and test.

## Avoid when

- Do not use to read an existing thesis; use thesis_show instead.
- Create directly; do not call thesis_show or thesis_refine first.

## Related tools

- thesis_show
- thesis_refine

## Prerequisites

None

## Required arguments

- `user_thesis` (string): The user's investment thesis in their own words.

## Optional arguments

- `assumptions` (array)
- `claims` (array)
- `expressions` (array)
- `invalidators` (array)
- `questions` (array)
- `scope` (string): Ticker scope (e.g. NVDA) or 'unknown'.
- `unknowns` (array)
