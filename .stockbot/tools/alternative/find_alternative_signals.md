# find_alternative_signals

Domain: alternative

Discovery scan for rising search-term and diffusion signals worth investigating.

## Use when

- Screening for emerging trend or attention signals across terms.

## Avoid when

- Do not use for evidence on one known trend; use get_trend_evidence instead.

## Related tools

- get_trend_evidence
- investigate_social_arbitrage_candidate

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD; candidates known after it are excluded.
- `geo` (string): Geography filter, e.g. US.
- `limit` (integer): Max candidates (default 20).
- `query` (string): Substring filter over candidate terms.
