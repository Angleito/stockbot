"""FINRA ingestion pipeline: consolidated short interest and Reg SHO.

A settlement-date snapshot is fetched page by page, every page is archived
raw, the complete snapshot is normalized as one unit, and the cycle is
checkpointed.  FINRA can correct published data, so a completed cycle is
re-checked after ``FINRA_REFRESH_TTL_SECONDS``: a corrected payload (new
content hash) archives as a new raw revision and normalizes as a new source
version with a new ``known_at``.  Identical payloads remain no-ops, so
reruns never duplicate normalized facts.

``known_at`` for a snapshot is the time the complete snapshot was first
archived (FINRA does not expose per-row publication timestamps); this is
recorded on every row and enforced by the DuckDB as-of layer, which picks
the newest version known at the requested as-of.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .. import finra_client
from ..normalization.finra import (
    SHORT_INTEREST_PARSER_VERSION,
    normalize_short_interest_snapshot,
    normalize_short_sale_volume,
)
from ..storage import parquet, raw_archive
from .base import Checkpointer, Pacing, summarize, utc_now

SHORT_INTEREST_PIPELINE = "finra_short_interest"
REG_SHO_PIPELINE = "finra_reg_sho"
SHORT_INTEREST_DATASET = "otcMarket/consolidatedShortInterest"
REG_SHO_DATASET = "otcMarket/regShoDaily"
# Re-check a completed cycle after this long to discover corrected payloads.
FINRA_REFRESH_TTL_SECONDS = 24 * 3600

_SNAPSHOT_FIELDS = (
    "symbolCode",
    "issueName",
    "settlementDate",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
)


def _snapshot_hash(rows: list[dict]) -> str:
    """Stable content hash for a complete snapshot, independent of the
    pagination that produced it."""
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _resolve_short_interest_spec():
    entry = finra_client._resolve_dataset(SHORT_INTEREST_DATASET)
    spec = finra_client._get_dataset_spec(entry)
    if entry.supports_record_offset is False:
        raise ValueError("FINRA consolidated short interest does not support required pagination.")
    return entry, spec


def fetch_snapshot_pages(
    settlement_date: str,
    pacing: Optional[Pacing] = None,
    archive_root: Optional[Path] = None,
) -> tuple[list[dict], list[Any]]:
    """Page one exact settlement-date partition, returning (all_rows,
    page_archive_records).  Every page is archived raw — under
    ``archive_root`` when given — before parsing."""
    entry, spec = _resolve_short_interest_spec()
    fields = list(_SNAPSHOT_FIELDS)
    if spec.field_names and not set(fields) <= spec.field_names:
        missing = sorted(set(fields) - spec.field_names)
        raise ValueError(
            "FINRA metadata for consolidated short interest is missing required "
            f"fields: {', '.join(missing)}."
        )
    pacing = pacing or Pacing(min_interval_seconds=0.2)
    offset = 0
    all_rows: list[dict] = []
    page_records: list[Any] = []
    total: Optional[int] = None
    path_name = finra_client._dataset_path_name(spec)
    url = f"{finra_client.FINRA_API_BASE}/data/group/{spec.group}/name/{path_name}"
    while True:
        pacing.wait()
        payload = finra_client._build_payload(
            spec, entry, None, settlement_date, settlement_date,
            finra_client.MAX_LIMIT, None, offset=offset, fields=fields,
        )
        content, rows, headers = finra_client.ingestion_post_query(spec.group, path_name, payload)
        record = raw_archive.archive(
            "finra", "data_page",
            f"{spec.dataset_id}:{settlement_date}:offset{offset}",
            content,
            url=url,
            metadata={"payload": payload, "headers": headers},
            root=archive_root,
        )
        page_records.append(record)
        raw_total = headers.get("record-total")
        if raw_total is None:
            raise ValueError("FINRA omitted Record-Total; cannot prove the short-interest snapshot is complete.")
        try:
            page_total = int(raw_total)
        except (TypeError, ValueError) as exc:
            raise ValueError("FINRA returned an invalid Record-Total header.") from exc
        if total is None:
            total = page_total
        elif total != page_total:
            raise ValueError("FINRA Record-Total changed while paging the snapshot.")
        page_rows = [row for row in rows if isinstance(row, dict)]
        if rows and not page_rows:
            raise ValueError("FINRA returned malformed rows while paging the short-interest snapshot.")
        all_rows.extend(page_rows)
        if len(all_rows) >= total:
            break
        if not page_rows:
            raise ValueError("FINRA pagination ended before the complete short-interest snapshot was retrieved.")
        offset += len(page_rows)
    if len(all_rows) != total:
        raise ValueError("FINRA returned more rows than its Record-Total; snapshot was not published.")
    return all_rows, page_records


def ingest_short_interest_snapshot(
    settlement_date: str,
    data_root: Path,
    archive_root: Optional[Path] = None,
    pacing: Optional[Pacing] = None,
) -> dict:
    """Fetch, archive, normalize, and checkpoint one FINRA short-interest
    settlement cycle.  Identical reruns within the refresh TTL are no-ops;
    corrected payloads are ingested as new source versions."""
    archive_root = archive_root or data_root / "raw"
    checkpointer = Checkpointer(data_root)
    if checkpointer.is_fresh_for_key(
        SHORT_INTEREST_PIPELINE, "finra", settlement_date, FINRA_REFRESH_TTL_SECONDS
    ):
        return summarize("complete", skipped=1, written=0, total=1)
    started_at = utc_now()
    rows, _page_records = fetch_snapshot_pages(
        settlement_date, pacing=pacing, archive_root=archive_root
    )
    snapshot_hash = _snapshot_hash(rows)
    entry, spec = _resolve_short_interest_spec()
    path_name = finra_client._dataset_path_name(spec)
    source_url = f"{finra_client.FINRA_API_BASE}/data/group/{spec.group}/name/{path_name}"
    datasets = normalize_short_interest_snapshot(
        rows,
        settlement_date=settlement_date,
        known_at=started_at,
        retrieved_at=started_at,
        content_hash=snapshot_hash,
        source_url=source_url,
        source_record_id=f"{spec.dataset_id}:{settlement_date}",
    )
    written = sum(
        parquet.write_rows(name, rows_, root=data_root / "parquet")
        for name, rows_ in datasets.items()
    )
    checkpointer.complete(
        SHORT_INTEREST_PIPELINE, "finra", settlement_date, snapshot_hash,
        record_count=written, started_at=started_at,
    )
    return summarize("complete", skipped=0, written=1, total=1)


def ingest_reg_sho_snapshot(
    trade_date: str,
    data_root: Path,
    archive_root: Optional[Path] = None,
    pacing: Optional[Pacing] = None,
) -> dict:
    """Fetch, archive, normalize, and checkpoint one Reg SHO trade date.
    Identical reruns within the refresh TTL are no-ops; corrected payloads
    are ingested as new source versions."""
    archive_root = archive_root or data_root / "raw"
    checkpointer = Checkpointer(data_root)
    if checkpointer.is_fresh_for_key(
        REG_SHO_PIPELINE, "finra", trade_date, FINRA_REFRESH_TTL_SECONDS
    ):
        return summarize("complete", skipped=1, written=0, total=1)
    started_at = utc_now()
    entry = finra_client._resolve_dataset(REG_SHO_DATASET)
    spec = finra_client._get_dataset_spec(entry)
    path_name = finra_client._dataset_path_name(spec)
    source_url = f"{finra_client.FINRA_API_BASE}/data/group/{spec.group}/name/{path_name}"
    pacing = pacing or Pacing(min_interval_seconds=0.2)
    pacing.wait()
    payload = finra_client._build_payload(
        spec, entry, None, trade_date, trade_date,
        finra_client.MAX_LIMIT, None, offset=0, fields=None,
    )
    content, rows, _headers = finra_client.ingestion_post_query(spec.group, path_name, payload)
    raw_archive.archive(
        "finra", "data_page", f"{spec.dataset_id}:{trade_date}:offset0",
        content, url=source_url,
        metadata={"payload": payload},
        root=archive_root,
    )
    rows = [row for row in rows if isinstance(row, dict)]
    snapshot_hash = _snapshot_hash(rows)
    datasets = normalize_short_sale_volume(
        rows,
        known_at=started_at,
        retrieved_at=started_at,
        content_hash=snapshot_hash,
        source_url=source_url,
        source_record_id=f"{spec.dataset_id}:{trade_date}",
    )
    written = sum(
        parquet.write_rows(name, rows_, root=data_root / "parquet")
        for name, rows_ in datasets.items()
    )
    checkpointer.complete(
        REG_SHO_PIPELINE, "finra", trade_date, snapshot_hash,
        record_count=written, started_at=started_at,
    )
    return summarize("complete", skipped=0, written=1, total=1)