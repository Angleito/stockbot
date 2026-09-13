# get_threshold_securities

Domain: finra
Family: threshold-securities
Intent: retrieve_threshold_status
Output kind: status_series
Source: finra
Entity scope: single_security_or_market
Time mode: date_or_latest

FINRA OTC Regulation SHO threshold securities, optionally filtered by ticker and date.

## Choose when

- Checking whether securities appear on the Reg SHO threshold list.

## Reject when

- Not for ordinary short interest levels.

## Conflicts with

None

## Related tools

- get_short_interest

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `ticker` (string)
- `tradeDate` (string): Optional trade date YYYY-MM-DD.
