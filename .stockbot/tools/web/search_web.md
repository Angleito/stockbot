# search_web

Domain: web

External web news and commentary for price moves, headlines, and industry developments: what outside commentators and people are saying, business risks.

## Use when

- Explaining why a stock went up or down: jumps, falls, fallen, rallies, surges, drops, gains, spikes, or crashes.
- Finding recent news and market reaction to a price catalyst, rally, or earnings announcement over the past days or weeks.

## Avoid when

- Do not use for FINRA short data; use query_finra or get_short_interest instead.

## Related tools

- get_material_events
- query_finra

## Prerequisites

None

## Required arguments

- `query` (string): Search query: ticker/company/industry plus the research question. Never include account, portfolio, or personal identifiers.

## Optional arguments

- `category` (string): Optional category to narrow the search.
- `end_published_date` (string): Optional end publication date YYYY-MM-DD, inclusive.
- `exclude_domains` (array): Optional domains to exclude from results.
- `include_domains` (array): Optional domains to restrict results to.
- `limit` (integer): Maximum results, 1-25 (default 5).
- `search_type` (string): Optional search mode (default auto).
- `start_published_date` (string): Optional start publication date YYYY-MM-DD, inclusive.
