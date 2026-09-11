# search_sec_relationships

Domain: ownership

Ownership and transaction relationship links for an entity: 13D/G owners, 13F holdings, deal links.

## Use when

- Mapping who owns, holds, or transacts with an entity in either direction.

## Avoid when

- Not for current 5%+ stake sizes.

## Related tools

- get_beneficial_ownership
- get_ownership_changes

## Prerequisites

None

## Required arguments

- `entity` (string): CIK, ticker, entity id, or candidate dict.

## Optional arguments

- `as_of` (string): Point-in-time date YYYY-MM-DD.
- `exhaustive` (boolean): Exhaust all applicable relationship indexes and SEC routes; the returned model context remains bounded.
- `limit` (integer)
- `relationship_types` (array): Optional open-vocabulary type filter (e.g. beneficial_owner, holding_manager).
