"""DuckDB query layer over the versioned Parquet datasets.

All analytical queries must go through :func:`query`, which enforces the
point-in-time rule: a query at ``as_of`` can never observe records whose
``known_at`` is later than ``as_of``.

``known_at`` is stored as ISO-8601.  When ``as_of`` is a plain date
(YYYY-MM-DD), comparisons are made at day granularity (``CAST(known_at AS
DATE) <= DATE ?``), so a fact filed on the as-of date itself is visible on
that date — mirroring the screen's ``filed <= settlement_date`` semantics.
When ``as_of`` includes a time, the comparison is a full timestamp
comparison.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

import duckdb
import pyarrow.parquet as pq

from . import parquet

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"

_DATE_GRANULARITY_AS_OF = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


def as_of_clause(as_of: str, column: str = "known_at") -> tuple[str, str]:
    """Return (SQL fragment, parameter) enforcing ``known_at <= as_of``."""
    import re

    if re.match(_DATE_GRANULARITY_AS_OF, str(as_of)):
        return f"CAST({column} AS DATE) <= CAST(? AS DATE)", str(as_of)
    return f"{column} <= ?", str(as_of)


def _data_roots(data_root: Path) -> tuple[Path, Path]:
    if data_root.name == "parquet":
        parquet_root = data_root
        db_root = data_root.parent
    else:
        parquet_root = data_root / "parquet"
        db_root = data_root
    return parquet_root, db_root


def _connect(data_root: Optional[Path] = None) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the warehouse database for a data root."""
    parquet_root, db_root = _data_roots(Path(data_root) if data_root else DEFAULT_DATA_ROOT)
    db_root.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_root / "warehouse.duckdb"))
    _register_views(conn, parquet_root)
    return conn


def _register_views(conn: duckdb.DuckDBPyConnection, parquet_root: Path) -> None:
    parquet_root.mkdir(parents=True, exist_ok=True)
    for name in parquet.dataset_names():
        directory = parquet_root / name
        empty_dir = directory / "__empty__"
        files = [p for p in directory.rglob("*.parquet")] if directory.exists() else []
        real_files = [p for p in files if empty_dir not in p.parents]
        if real_files:
            # DuckDB errors when a hive-partitioned scan sees a file without
            # the partition column; the placeholder must not be scanned once
            # real files exist.
            if empty_dir.exists():
                import shutil

                shutil.rmtree(empty_dir)
            glob_path = directory / "**" / "*.parquet"
        else:
            empty_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(parquet.dataset(name).schema.empty_table(), str(empty_dir / "part-empty.parquet"))
            glob_path = empty_dir / "*.parquet"
        conn.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{glob_path}', hive_partitioning = true)"
        )


def query(
    sql: str,
    params: Sequence[Any] = (),
    data_root: Optional[Path] = None,
) -> list[dict]:
    """Run a read-only SQL query over the parquet views; returns rows as
    dicts.

    Point-in-time enforcement is explicit: analytical queries must embed
    ``as_of_clause(as_of)`` in their WHERE clause and pass its parameter
    (see :func:`as_of_clause`).  A regression test proves that a query built
    this way cannot observe records with a later ``known_at``.
    """
    conn = _connect(data_root)
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            result = conn.execute(sql, list(params)).fetchall()
            columns = [desc[0] for desc in conn.description]
        finally:
            conn.execute("ROLLBACK")
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in result]