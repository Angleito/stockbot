"""DuckDB warehouse: canonical typed tables for the versioned normalized datasets.

All analytical queries must go through :func:`query`, which enforces the
point-in-time rule: a query at ``as_of`` can never observe records whose
``known_at`` is later than ``as_of``.

Two clocks: ``known_at`` is the global public-knowledge date (the PIT
horizon — filing date, settlement date, or other source publication date);
``retrieved_at`` is the local fetch wall-clock (provenance/freshness only,
never PIT-gated). Rows with no public date keep ``known_at == retrieved_at``.

``known_at`` is stored as ISO-8601.  When ``as_of`` is a plain date
(YYYY-MM-DD), comparisons are made at day granularity (``CAST(known_at AS
DATE) <= DATE ?``), so a fact filed on the as-of date itself is visible on
that date — mirroring the screen's ``filed <= settlement_date`` semantics.
When ``as_of`` includes a time, the comparison is a full timestamp
comparison.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path

import duckdb
import polars as pl
import pyarrow as pa

from ..config import get_data_root
from ..domain.market.securities import TickerAlias
from . import mappers, parquet

DEFAULT_DATA_ROOT = get_data_root()

_DATE_GRANULARITY_AS_OF = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
TIMESTAMP_COLS: frozenset[str] = frozenset(
    {
        "known_at",
        "retrieved_at",
        "filed_at",
        "accepted_at",
        "published_at",
        "period_end",
        "period_start",
        "settlement_date",
        "trade_date",
        "coverage_date",
        "calculated_at",
        "created_at",
        "started_at",
        "finished_at",
        "completed_at",
        "valid_from",
        "valid_to",
        "declaration_date",
        "record_date",
        "payment_date",
        "ex_dividend_date",
        "transaction_date",
        "recorded_at",
        "window_start",
        "window_end",
        "as_of",
        "event_at",
        "quote_retrieved_at",
    }
)


def _column_sql_type(field: pa.Field) -> str:
    """DuckDB storage type for one dataset schema field."""
    arrow_type = field.type
    if field.name in TIMESTAMP_COLS and pa.types.is_string(arrow_type):
        return "TIMESTAMPTZ"
    if pa.types.is_string(arrow_type):
        return "VARCHAR"
    if pa.types.is_float64(arrow_type):
        return "DOUBLE"
    if pa.types.is_int64(arrow_type):
        return "BIGINT"
    if isinstance(arrow_type, pa.Decimal128Type):
        return f"DECIMAL({arrow_type.precision}, {arrow_type.scale})"
    if pa.types.is_boolean(arrow_type):
        return "BOOLEAN"
    raise TypeError(f"Unsupported arrow type for {field.name}: {arrow_type}")


def _table_ddl(name: str) -> str:
    """CREATE TABLE statement for one dataset.

    ``chr(31)``-joined unique-key values with NULL normalized to ``""``, so
    reruns dedup even when key members are NULL (DuckDB treats NULL key
    members as distinct), plus a nullable ``_tsraw`` column holding a JSON
    object of the exact input strings for timestamp columns (so date-only
    ``"2026-08-01"`` and midnight-instant ``"2026-08-01T00:00:00Z"`` both read
    back exactly as written). NOT NULL applies to known_at/retrieved_at where
    the dataset has them (events.retrieved_at stays nullable: legacy rows omit
    it); key columns stay nullable since producers omit them (e.g. insider
    security_title, 13F retrieved_at) and dedup rides on ``_dedup``.
    """
    ds = parquet.DATASETS[name]
    column_names = {field.name for field in ds.schema}
    # ponytail: NULL key members dedup via ""-normalization while the stored
    # columns stay NULL (parquet compared str(key or "") the same way).
    always_not_null = {"_dedup"}
    if name != "events":
        always_not_null |= {"known_at", "retrieved_at"} & column_names
    else:
        always_not_null |= {"known_at"} & column_names
    definitions = [
        f'"{field.name}" {_column_sql_type(field)}' + (" NOT NULL" if field.name in always_not_null else "")
        for field in ds.schema
    ]
    definitions.append('"_dedup" VARCHAR NOT NULL')
    definitions.append('"_tsraw" VARCHAR')
    return f'CREATE TABLE IF NOT EXISTS "{name}" ({", ".join(definitions)}, PRIMARY KEY ("_dedup"));'

_SCHEMA: str = "\n".join(_table_ddl(name) for name in parquet.dataset_names())


def as_of_clause(as_of: str, column: str = "known_at") -> tuple[str, str]:
    """Return (SQL fragment, parameter) enforcing ``known_at <= as_of``."""
    import re

    if re.match(_DATE_GRANULARITY_AS_OF, as_of):
        return f"CAST({column} AS DATE) <= CAST(? AS DATE)", as_of
    return f"CAST({column} AS TIMESTAMPTZ) <= CAST(? AS TIMESTAMPTZ)", as_of


def _data_roots(data_root: Path) -> tuple[Path, Path]:
    if data_root.name == "parquet":
        parquet_root = data_root
        db_root = data_root.parent
    else:
        parquet_root = data_root / "parquet"
        db_root = data_root
    return parquet_root, db_root


def _connect(data_root: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the warehouse database for a data root."""
    parquet_root, db_root = _data_roots(Path(data_root) if data_root else get_data_root())
    db_root.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_root / "warehouse.duckdb"))
    conn.execute("SET TimeZone='UTC'")
    _register_views(conn, parquet_root)
    return conn

def _register_views(conn: duckdb.DuckDBPyConnection, parquet_root: Path) -> None:
    parquet_root.mkdir(parents=True, exist_ok=True)
    for name in parquet.dataset_names():
        try:
            conn.execute(f'DROP VIEW IF EXISTS "{name}"')
        except duckdb.CatalogException:
            pass  # a table already owns the name; no legacy view left
    for statement in _SCHEMA.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
    for name in parquet.dataset_names():
        for column in ("_dedup", "_tsraw"):
            conn.execute(f'ALTER TABLE "{name}" ADD COLUMN IF NOT EXISTS "{column}" VARCHAR')


def _cell_to_storage(value: object) -> object:
    """Warehouse read-back datetimes to ISO text so copies round-trip via Arrow."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _rows_to_frame(ds: parquet.Dataset, rows: list[dict[str, object]]) -> pl.DataFrame:
    """Dicts narrowed to the dataset schema, coerced to a Polars frame."""
    if not rows:
        return pl.DataFrame()
    columns = [field.name for field in ds.schema]
    clean = [{key: _cell_to_storage(row.get(key)) for key in columns} for row in rows]
    frame = pl.from_arrow(pa.Table.from_pylist(clean, schema=ds.schema))
    assert isinstance(frame, pl.DataFrame)
    return frame


def _null_empty_timestamps(frame: pl.DataFrame, ds: parquet.Dataset) -> pl.DataFrame:
    """Empty strings read as NULL for timestamp columns (else the cast fails)."""
    string_ts_cols = [
        name
        for name in ds.schema.names
        if name in TIMESTAMP_COLS and name in frame.columns and frame.schema[name] == pl.String
    ]
    if not string_ts_cols:
        return frame
    return frame.with_columns(
        [pl.when(pl.col(name) == "").then(None).otherwise(pl.col(name)).alias(name) for name in string_ts_cols]
    )


def insert_ignore(
    table: str,
    rows: list[dict[str, object]] | pl.DataFrame,
    data_root: Path | None = None,
) -> int:
    """Insert rows ignoring primary-key conflicts; returns rows inserted.

    ``table`` is allowlisted by literal match against the dataset registry
    (unknown tables raise ``ValueError``). Empty strings in timestamp columns
    become NULL. One transaction; the count is ``COUNT(*)`` before/after in it.
    ``SELECT *`` consumers see internal ``_dedup``/``_tsraw`` columns; prefer
    explicit column lists or ignore them.
    """
    ds = parquet.dataset(table)
    dicts = rows.to_dicts() if isinstance(rows, pl.DataFrame) else rows
    if not dicts:
        return 0
    # ponytail: multi-row INSERT...ON CONFLICT DO NOTHING lands rows out of
    # order in DuckDB, so dedup happens here (first occurrence wins) and the
    # single INSERT below has no ON CONFLICT clause, preserving write order.
    ts_names = {field.name for field in ds.schema} & TIMESTAMP_COLS
    seen: set[str] = set()
    kept: list[dict[str, object]] = []
    kept_dedup: list[str] = []
    kept_tsraw: list[str | None] = []
    for row in dicts:
        key = "\x1f".join("" if row.get(k) is None else str(row.get(k)) for k in ds.unique_keys)
        if key not in seen:
            seen.add(key)
            kept.append(row)
            kept_dedup.append(key)
            raw: dict[str, str] = {}
            for name in ts_names:
                value = row.get(name)
                if isinstance(value, str) and value != "":
                    raw[name] = value
            kept_tsraw.append(json.dumps(raw, sort_keys=True) if raw else None)
    frame = _null_empty_timestamps(_rows_to_frame(ds, kept), ds).with_columns(
        pl.Series("_dedup", kept_dedup), pl.Series("_tsraw", kept_tsraw, dtype=pl.String)
    )
    conn = _connect(data_root)
    try:
        conn.register("incoming", frame)
        conn.execute("BEGIN TRANSACTION")
        try:
            before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            conn.execute(
                f'INSERT INTO "{table}" BY NAME '
                f"SELECT * FROM incoming WHERE NOT EXISTS "
                f'(SELECT 1 FROM "{table}" AS _existing WHERE _existing."_dedup" = incoming."_dedup")'
            )
            after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    assert before is not None and after is not None
    return int(after[0] - before[0])

def execute(sql: str, params: Sequence[object] = (), data_root: Path | None = None) -> None:
    """Run a write SQL statement (DELETE/UPDATE) in one committed transaction."""
    conn = _connect(data_root)
    try:
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(sql, list(params))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def query(
    sql: str,
    params: Sequence[object] = (),
    data_root: Path | None = None,
) -> list[dict[str, object]]:
    """Run a read-only SQL query over the warehouse tables; returns rows as
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


def ticker_alias_candidates(ticker: str, as_of: datetime, data_root: Path | None = None) -> list[TickerAlias]:
    """Return ticker alias rows knowable at ``as_of``, newest instant first.

    Retrieval only: the resolution semantics (validity interval, entity and
    security-id ambiguity, newest-instant selection) live in
    ``app.domain.market.identity.resolve_ticker_aliases``.  The
    ``known_at <= as_of`` filter is kept here as an efficiency prune; the
    resolver applies it again as the authoritative rule.
    """
    clause, param = as_of_clause(as_of.isoformat())
    rows = query(
        "SELECT alias_type, alias_value, entity_id, security_id, source, "
        "valid_from, valid_to, known_at, retrieved_at "
        "FROM entity_aliases "
        "WHERE alias_type = 'ticker' AND alias_value = ? AND "
        f"{clause} "
        "ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, "
        "CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST",
        params=[ticker.strip().upper(), param],
        data_root=data_root,
    )
    return [mappers.ticker_alias_from_row(row) for row in rows]
