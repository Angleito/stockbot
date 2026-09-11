# list_finra_datasets

Domain: finra

Catalog of public FINRA datasets with canonical ids, groups, and ticker/date support.

## Use when

- Finding which FINRA dataset covers a question before querying.

## Avoid when

- Does not return dataset fields or schemas.

## Related tools

- describe_finra_dataset

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `group` (string): Optional dataset group filter (e.g. otcMarket, fixedIncomeMarket, finra).
- `search` (string): Optional substring match on name/description.
