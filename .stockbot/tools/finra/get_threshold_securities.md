# get_threshold_securities

Domain: finra

FINRA OTC Regulation SHO threshold securities, optionally filtered by ticker and date.

## Use when

- Checking whether a ticker sits on the Reg SHO threshold list.
- On the threshold list.

## Avoid when

- Do not use for ordinary short interest levels; use get_short_interest instead.
- Call directly for the current list; no FINRA discovery chain is needed.

## Related tools

- get_short_interest

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `ticker` (string)
- `tradeDate` (string): Optional trade date YYYY-MM-DD.
