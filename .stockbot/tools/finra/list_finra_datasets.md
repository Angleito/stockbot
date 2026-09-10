# list_finra_datasets

Domain: finra

Catalog of public FINRA datasets with canonical ids, groups, and ticker/date support.

## Use when

- Finding which FINRA dataset covers a question before querying.

## Avoid when

- Do not use to read dataset fields; use describe_finra_dataset instead.

## Related tools

- describe_finra_dataset

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `group` (string): Optional dataset group filter (e.g. otcMarket, fixedIncomeMarket, finra).
- `search` (string): Optional substring match on name/description.
