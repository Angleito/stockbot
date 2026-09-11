# describe_finra_dataset

Domain: finra
Family: catalog
Intent: inspect_finra_dataset_schema
Output kind: schema
Source: finra
Entity scope: single_dataset
Time mode: current

One FINRA dataset's fields, types, filter values, and supported methods.

## Choose when

- Learning a named FINRA dataset's fields, types, filters, and coverage before querying.

## Reject when

- Not for analyzed briefings.

## Conflicts with

None

## Related tools

- list_finra_datasets
- query_finra
- get_finra_datapoints

## Prerequisites

None

## Required arguments

- `dataset_id` (string): Canonical group/name (e.g. otcMarket/consolidatedShortInterest). Legacy bare names are accepted when unambiguous.

## Optional arguments

None
