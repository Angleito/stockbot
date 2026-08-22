"""Normalizers: provider response shapes -> stable point-in-time schemas.

Normalizers never fetch; they map archived raw payloads into the versioned
Parquet datasets with full provenance (source, URL, retrieved time, period,
known_at, content hash, parser version).
"""