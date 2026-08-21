"""SQLite cache for parsed filings and summaries. stdlib only."""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cache.db")

_local = threading.local()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.commit()
        _local.conn = conn
    return conn


def get(key: str, ttl: Optional[float] = None) -> Optional[Any]:
    """Return the cached JSON value for key, or None. If ttl (seconds) is given,
    entries older than ttl are treated as misses."""
    row = _conn().execute("SELECT value, created_at FROM cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value, created_at = row
    if ttl is not None and (time.time() - created_at) > ttl:
        return None
    return json.loads(value)


def set(key: str, value: Any) -> None:
    """Store value (JSON-serialized) under key."""
    _conn().execute(
        "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), time.time()),
    )
    _conn().commit()
