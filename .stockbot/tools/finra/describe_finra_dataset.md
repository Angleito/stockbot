# describe_finra_dataset

Domain: finra

One FINRA dataset's fields, types, filter values, and supported methods.

## Use when

- Learning a dataset's schema and field names before querying it.
- What is in the dataset.
- Describing what is in a named FINRA dataset such as short interest: fields, types, and filter values.

## Avoid when

- Do not use for analyzed briefings; use query_finra instead.

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
