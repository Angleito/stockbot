# get_macro_context

Domain: macro

Macro statistics for a geography such as California: population (how many people live there), unemployment, inflation, GDP, rates.

## Use when

- Answering how many people live in a state, its unemployment rate, inflation, or other economic backdrop.
- Tracking how unemployment or inflation moves when a rate changes.

## Avoid when

- Do not use for company-specific facts; use the company tool for that domain.

## Related tools

- search_web

## Prerequisites

None

## Required arguments

- `geos` (array): Geography DCIDs, e.g. [geoId/06].
- `variables` (array): Statistical variable IDs, e.g. [Count_Person].

## Optional arguments

- `end_date` (string): Range end YYYY-MM-DD.
- `limit` (integer): Max observations (default 100).
- `start_date` (string): Range start YYYY-MM-DD.
