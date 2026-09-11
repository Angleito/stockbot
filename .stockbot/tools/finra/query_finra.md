# query_finra

Domain: finra

Analyzed briefing over a FINRA dataset: coverage, deterministic metrics, trends, prose. Dataset IDs look like otcMarket/consolidatedShortInterest.

## Use when

- Analyzing short-interest or other FINRA data moves and changes over time.
- Changed lately.

## Avoid when

- Do not use when exact source values are requested; use get_finra_datapoints.

## Related tools

- describe_finra_dataset
- get_finra_datapoints
- get_short_interest
- list_finra_datasets

## Prerequisites

None

## Required arguments

- `dataset` (string): Canonical id group/name (e.g. fixedIncomeMarket/treasuryDailyAggregates). Legacy bare names accepted when unambiguous.

## Optional arguments

- `analysis_goal` (string): Optional: what the user needs answered (e.g. 'trend over the last 12 months'). Guides the briefing; deterministic metrics are always computed.
- `end_date` (string): YYYY-MM-DD.
- `filters` (array): Extra compare filters (field names must exist on the dataset — when unknown, call describe_finra_dataset first).
- `limit` (integer): Max records to return (clamped to 1..1000).
- `offset` (integer): 0-based record offset for pagination (FINRA max 500000). Rejected for datasets whose catalog entry has supportsRecordOffset=false.
- `start_date` (string): YYYY-MM-DD. Combined with end_date as a range.
- `ticker` (string): Issue symbol when the dataset is symbol-level.
