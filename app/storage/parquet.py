"""Versioned Parquet datasets for normalized point-in-time records.

Datasets are append-only and deduplicated on write by their unique key, so
re-running an ingestion job produces no duplicate normalized facts.  Every
record carries provenance: source, source record/URL, retrieved time,
period/effective date, ``known_at``, content hash, and parser version.

Partitioning is by the year of the dataset's primary date column (hive
format), which keeps historical as-of queries cheap without hiding any
records behind filters.
"""

from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_PARQUET_ROOT = Path(__file__).resolve().parent.parent.parent / "data" / "parquet"

TEXT = pa.string()
DOUBLE = pa.float64()
INTEGER = pa.int64()


@dataclass(frozen=True)
class Dataset:
    name: str
    schema: pa.Schema
    unique_keys: tuple[str, ...]
    partition_field: Optional[str] = None


def _fields(*pairs: tuple[str, Any]) -> list[pa.Field]:
    return [pa.field(name, type_) for name, type_ in pairs]


DATASETS: dict[str, Dataset] = {
    "entities": Dataset(
        name="entities",
        schema=pa.schema(_fields(
            ("entity_id", TEXT), ("name", TEXT), ("entity_type", TEXT),
            ("sic", TEXT), ("source", TEXT), ("known_at", TEXT),
            ("retrieved_at", TEXT), ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("entity_id",),
    ),
    "entity_aliases": Dataset(
        name="entity_aliases",
        schema=pa.schema(_fields(
            ("alias_type", TEXT), ("alias_value", TEXT), ("entity_id", TEXT),
            ("security_id", TEXT), ("source", TEXT), ("valid_from", TEXT),
            ("valid_to", TEXT), ("known_at", TEXT), ("retrieved_at", TEXT),
            ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("alias_type", "alias_value", "entity_id", "source", "valid_from"),
    ),
    "securities": Dataset(
        name="securities",
        schema=pa.schema(_fields(
            ("security_id", TEXT), ("entity_id", TEXT), ("security_type", TEXT),
            ("ticker", TEXT), ("exchange", TEXT), ("source", TEXT),
            ("known_at", TEXT), ("retrieved_at", TEXT), ("content_hash", TEXT),
            ("parser_version", TEXT),
        )),
        unique_keys=("security_id",),
    ),
    "documents": Dataset(
        name="documents",
        schema=pa.schema(_fields(
            ("doc_id", TEXT), ("source", TEXT), ("kind", TEXT), ("key", TEXT),
            ("source_url", TEXT), ("accession", TEXT), ("sha256", TEXT),
            ("retrieved_at", TEXT), ("published_at", TEXT), ("known_at", TEXT),
            ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("doc_id",),
    ),
    "financial_facts": Dataset(
        name="financial_facts",
        schema=pa.schema(_fields(
            ("fact_id", TEXT), ("entity_id", TEXT), ("security_id", TEXT),
            ("concept", TEXT), ("original_concept", TEXT), ("value", DOUBLE),
            ("unit", TEXT), ("duration_type", TEXT), ("period_end", TEXT),
            ("filed_at", TEXT), ("accession", TEXT), ("frame", TEXT),
            ("known_at", TEXT), ("retrieved_at", TEXT), ("source_url", TEXT),
            ("source_record_id", TEXT), ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("fact_id",),
        partition_field="period_end",
    ),
    "short_interest": Dataset(
        name="short_interest",
        schema=pa.schema(_fields(
            ("row_id", TEXT), ("entity_id", TEXT), ("security_id", TEXT),
            ("symbol_code", TEXT), ("issue_name", TEXT), ("settlement_date", TEXT),
            ("short_position", DOUBLE), ("prev_position", DOUBLE),
            ("avg_daily_volume", DOUBLE), ("days_to_cover", DOUBLE),
            ("source_url", TEXT), ("source_record_id", TEXT), ("known_at", TEXT),
            ("retrieved_at", TEXT), ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("row_id",),
        partition_field="settlement_date",
    ),
    "short_sale_volume": Dataset(
        name="short_sale_volume",
        schema=pa.schema(_fields(
            ("row_id", TEXT), ("entity_id", TEXT), ("security_id", TEXT),
            ("symbol_code", TEXT), ("trade_date", TEXT), ("facility", TEXT),
            ("short_volume", DOUBLE), ("exempt_volume", DOUBLE),
            ("total_volume", DOUBLE), ("source_url", TEXT),
            ("source_record_id", TEXT), ("known_at", TEXT), ("retrieved_at", TEXT),
            ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("row_id",),
        partition_field="trade_date",
    ),
    "screen_runs": Dataset(
        name="screen_runs",
        schema=pa.schema(_fields(
            ("run_id", TEXT), ("screen", TEXT), ("settlement_date", TEXT),
            ("as_of", TEXT), ("created_at", TEXT), ("calc_version", TEXT),
            ("finra_rows", INTEGER), ("eligible_rows", INTEGER),
            ("valid_short_interest_rows", INTEGER), ("mapped_rows", INTEGER),
            ("unambiguous_rows", INTEGER), ("common_equity_rows", INTEGER),
            ("shares_outstanding_rows", INTEGER),
            ("exclusions_json", TEXT), ("environment", TEXT),
            ("parser_version", TEXT),
        )),
        unique_keys=("run_id",),
        partition_field="settlement_date",
    ),
    "screen_entries": Dataset(
        name="screen_entries",
        schema=pa.schema(_fields(
            ("run_id", TEXT), ("rank", INTEGER), ("entity_id", TEXT),
            ("security_id", TEXT), ("ticker", TEXT), ("issue_name", TEXT),
            ("short_shares", DOUBLE), ("shares_outstanding", DOUBLE),
            ("short_interest_percent", DOUBLE), ("sec_shares_as_of", TEXT),
            ("sec_filed_at", TEXT), ("sec_accession", TEXT),
            ("sec_source_url", TEXT),
        )),
        unique_keys=("run_id", "rank"),
    ),
    "ingestion_checkpoints": Dataset(
        name="ingestion_checkpoints",
        schema=pa.schema(_fields(
            ("pipeline", TEXT), ("source", TEXT), ("key", TEXT),
            ("payload_hash", TEXT), ("status", TEXT), ("record_count", INTEGER),
            ("started_at", TEXT), ("finished_at", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("pipeline", "source", "key", "payload_hash"),
    ),
    "portfolio_snapshots": Dataset(
        name="portfolio_snapshots",
        schema=pa.schema(_fields(
            ("snapshot_id", TEXT), ("broker", TEXT), ("created_at", TEXT),
            ("cash", DOUBLE), ("invested_value", DOUBLE), ("total_value", DOUBLE),
            ("account_count", INTEGER), ("position_count", INTEGER),
            ("priced_position_count", INTEGER), ("unresolved_position_count", INTEGER),
            ("source", TEXT), ("parser_version", TEXT), ("calculation_version", TEXT),
        )),
        unique_keys=("snapshot_id",),
    ),
    "portfolio_positions": Dataset(
        name="portfolio_positions",
        schema=pa.schema(_fields(
            ("snapshot_id", TEXT), ("position_id", TEXT), ("account_id", TEXT),
            ("security_id", TEXT), ("entity_id", TEXT), ("ticker", TEXT),
            ("quantity", DOUBLE), ("average_cost", DOUBLE), ("market_price", DOUBLE),
            ("price_type", TEXT), ("market_value", DOUBLE), ("unrealized_gain", DOUBLE),
            ("unrealized_gain_pct", DOUBLE), ("portfolio_weight", DOUBLE),
            ("source", TEXT), ("quote_retrieved_at", TEXT), ("asset_type", TEXT),
        )),
        unique_keys=("position_id",),
    ),
    "sector_mappings": Dataset(
        name="sector_mappings",
        schema=pa.schema(_fields(
            ("entity_id", TEXT), ("sector", TEXT), ("source", TEXT),
            ("known_at", TEXT), ("retrieved_at", TEXT), ("content_hash", TEXT),
            ("parser_version", TEXT),
        )),
        unique_keys=("entity_id", "sector", "source", "known_at"),
    ),
    "company_obligations": Dataset(
        name="company_obligations",
        schema=pa.schema(_fields(
            ("obligation_id", TEXT), ("ticker", TEXT), ("obligation_type", TEXT),
            ("amount_billions", DOUBLE), ("certainty", TEXT), ("status", TEXT),
            ("revenue_matched", pa.bool_()), ("default_triggered", pa.bool_()),
            ("fiscal_year", TEXT), ("excerpt", TEXT), ("source", TEXT),
            ("filed_at", TEXT), ("as_of", TEXT), ("known_at", TEXT),
            ("retrieved_at", TEXT), ("content_hash", TEXT), ("parser_version", TEXT),
        )),
        unique_keys=("obligation_id",),
        partition_field="filed_at",
    ),
}


def dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown parquet dataset: {name}") from exc


def dataset_names() -> list[str]:
    return sorted(DATASETS)


def _partition_year(date_value: Optional[str]) -> Optional[str]:
    if not date_value:
        return None
    try:
        return str(_dt.date.fromisoformat(str(date_value)[:10]).year)
    except (TypeError, ValueError):
        return None


def _exclusive_part_path(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"part-{uuid.uuid4().hex}.parquet"


def _unique_key(row: dict, keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(row.get(key) or "") for key in keys)


def read_table(name: str, root: Optional[Path] = None) -> pa.Table:
    """Read a full dataset (all partitions) as a pyarrow Table."""
    root = root or DEFAULT_PARQUET_ROOT
    ds = dataset(name)
    directory = root / ds.name
    if not directory.exists():
        return ds.schema.empty_table()
    files = sorted(p for p in directory.rglob("*.parquet") if p.is_file())
    if not files:
        return ds.schema.empty_table()
    tables = [pq.read_table(str(p)) for p in files]
    return pa.concat_tables(tables, promote_options="permissive")


def write_rows(name: str, rows: list[dict], root: Optional[Path] = None) -> int:
    """Append rows deduplicated by the dataset's unique key; returns the
    number of rows actually written (0 on a deterministic rerun)."""
    root = root or DEFAULT_PARQUET_ROOT
    ds = dataset(name)
    if not rows:
        return 0
    existing = set()
    for table in read_table(name, root).to_batches():
        existing.update(
            tuple(str(v) for v in batch)
            for batch in zip(*(table.column(key).to_pylist() for key in ds.unique_keys))
        )
    new_rows = [
        row for row in rows
        if _unique_key(row, ds.unique_keys) not in existing
    ]
    if not new_rows:
        return 0
    by_partition: dict[str, list[dict]] = {}
    for row in new_rows:
        if ds.partition_field:
            year = _partition_year(str(row.get(ds.partition_field) or "")) or "unknown"
        else:
            year = "none"
        by_partition.setdefault(year, []).append(row)
    columns = [f.name for f in ds.schema]
    for year, part_rows in by_partition.items():
        partition_col = f"{ds.partition_field}_year" if ds.partition_field else "partition"
        directory = root / ds.name / f"{partition_col}={year}"
        clean_rows = [{key: row.get(key) for key in columns} for row in part_rows]
        table = pa.Table.from_pylist(clean_rows, schema=ds.schema)
        pq.write_table(table, str(_exclusive_part_path(directory)))
    return len(new_rows)


def count_rows(name: str, root: Optional[Path] = None) -> int:
    return read_table(name, root).num_rows

