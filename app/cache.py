"""SQLite cache for parsed filings and summaries. stdlib only."""

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import get_data_root

DB_PATH = str(get_data_root() / "cache.db")

_local = threading.local()


def _db_path() -> str:
    return str(get_data_root() / "cache.db")


def _open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.commit()
    return conn


def _conn() -> sqlite3.Connection:
    db_path = _db_path()
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != db_path:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001, S110 - intentional best-effort boundary, never aborts; intentional silent skip
            pass
        conn = _open_db(db_path)
        _local.conn = conn
        _local.path = db_path
    return conn


def _is_expired(created_at: float, ttl: float | None) -> bool:
    return ttl is not None and (time.time() - created_at) > ttl


def get(key: str, ttl: float | None = None) -> object | None:
    """Return the cached JSON value for key, or None. If ttl (seconds) is given,
    entries older than ttl are treated as misses."""
    row = _conn().execute("SELECT value, created_at FROM cache WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    value, created_at = row
    if _is_expired(created_at, ttl):
        return None
    decoded: object = json.loads(value)
    return decoded


def set(key: str, value: object) -> None:
    """Store value (JSON-serialized) under key."""
    _conn().execute(
        "INSERT OR REPLACE INTO cache (key, value, created_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), time.time()),
    )
    _conn().commit()
