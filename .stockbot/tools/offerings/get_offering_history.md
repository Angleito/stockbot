# get_offering_history

Domain: offerings

Offering history from S-1/S-3/424B filings: offering terms with source-registration links.

## Use when

- Reviewing past offerings, shelf registrations, or IPO terms for a ticker, including share-count impact context for converts or warrants.

## Avoid when

- Not for dilution math (get_dilution_profile).

## Related tools

- get_dilution_profile

## Prerequisites

None

## Required arguments

- `ticker` (string)

## Optional arguments

- `as_of` (string)
- `limit` (integer)
