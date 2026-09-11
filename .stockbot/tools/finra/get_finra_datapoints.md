# get_finra_datapoints

Domain: finra

Exact raw rows from any named FINRA dataset: only the requested fields, up to 25 rows.

## Use when

- Exact fields and values from a named FINRA dataset for an explicit data request.

## Avoid when

- Do NOT use for analyzed briefings or trends (query_finra).

## Related tools

- describe_finra_dataset
- query_finra
- list_finra_datasets

## Prerequisites

None

## Required arguments

- `dataset` (string): Canonical id group/name (e.g. otcMarket/consolidatedShortInterest). Legacy bare names accepted when unambiguous.
- `fields` (array): Exact field names to return (e.g. settlementDate, symbolCode, currentShortPositionQuantity for short interest).

## Optional arguments

- `end_date` (string): YYYY-MM-DD.
- `filters` (array): Extra compare filters (field names must exist on the dataset — when unknown, call describe_finra_dataset first).
- `limit` (integer): Max rows to return (clamped to 1..25; default 10).
- `sort_fields` (array): FINRA sortFields syntax: '+field' ascending, '-field' descending, e.g. ["-settlementDate"] returns newest first. Use for 'latest five' / 'last five' / 'most recent' data requests. Fields must exist on the dataset.
- `sort_order` (string): Convenience: sort by the dataset's date field ('desc' = newest first, for 'latest five' requests). Rejected when the dataset has no date field — use sort_fields instead.
- `start_date` (string): YYYY-MM-DD. Combined with end_date as a range.
- `ticker` (string): Issue symbol when the dataset is symbol-level.
