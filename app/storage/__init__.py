"""Canonical analytical storage: immutable raw archive + versioned Parquet
datasets queried through DuckDB.

This is the point-in-time foundation described in FUTURE_ARCHITECTURE.md
Phase 0.  Connectors archive source payloads under data/raw/; normalizers
write versioned Parquet datasets under data/parquet/; analytics queries those
datasets through DuckDB with mandatory ``known_at <= as_of`` filters.
"""