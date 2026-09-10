# get_trend_evidence

Domain: alternative

Evidence for one known trend: search interest, rising queries, and geography.

## Use when

- Backing a specific trend claim with search-interest evidence.
- Backing a trend picked up in a geography such as the US around a date, with search-interest evidence.
- Trends picked up.

## Avoid when

- Do not use to discover new signals; use find_alternative_signals instead.

## Related tools

- find_alternative_signals

## Prerequisites

None

## Required arguments

None

## Optional arguments

- `end_date` (string): Range end YYYY-MM-DD. Optional; both omitted defaults to trailing 7 days ending today UTC.
- `geo` (string): Single geography shorthand for geos.
- `geos` (array): Geographies, e.g. [US].
- `limit` (integer): Max rows (default 100).
- `start_date` (string): Range start YYYY-MM-DD. Optional; both omitted defaults to trailing 7 days ending today UTC.
- `term` (string): Optional substring filter over collected terms.
- `week_end` (string): Interest-week end YYYY-MM-DD; omitted defaults to the trailing 14-day week window ending at end_date.
- `week_start` (string): Interest-week start YYYY-MM-DD; omitted defaults to the trailing 14-day week window ending at end_date.
