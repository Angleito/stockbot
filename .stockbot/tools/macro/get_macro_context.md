# get_macro_context

Domain: macro

Macro statistics for a geography such as California: population (how many people live there), unemployment, inflation, GDP, rates.

## Use when

- Retrieving population, labor, inflation, GDP, or rate statistics for a geography.

## Avoid when

- Not for company-specific facts.

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
