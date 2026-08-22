"""Ingestion foundations: connectors, retries/backoff, pacing, checkpoints.

The pipeline boundary is always: connector fetch -> raw archive -> normalizer
-> versioned Parquet dataset -> checkpoint.  Re-running a pipeline is a
no-op for payloads that are already archived, normalized, and checkpointed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from ..storage import parquet

CHECKPOINT_PARSER_VERSION = "ingestion-checkpoint-v1"

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and response.status_code in _RETRYABLE_STATUS
    return False


retry_policy = Retrying(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


@dataclass(frozen=True)
class FetchResult:
    """One raw source payload plus the metadata needed to archive it."""

    key: str
    payload: bytes
    url: str
    kind: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Connector:
    """Base class for source connectors: discover keys and fetch raw payloads.

    Connectors contain no investment logic and no normalization.  Subclasses
    implement source-specific methods (e.g. ``fetch_tickers`` for SEC,
    ``fetch_snapshot`` for FINRA) that return :class:`FetchResult` and raise
    on failure — they never return partials.
    """

    source: str = ""


class Pacing:
    """Minimum-interval throttle shared across one pipeline run.

    SEC asks for no more than ~10 requests/second; FINRA is more tolerant,
    but a small interval keeps every pipeline polite.
    """

    def __init__(self, min_interval_seconds: float = 0.13):
        self.min_interval_seconds = min_interval_seconds
        self._lock = threading.Lock()
        self._last_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            delay = self.min_interval_seconds - (now - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.time()


def utc_now() -> str:
    """ISO-8601 UTC timestamp with microsecond precision so rapid reruns
    receive distinct known_at/checkpoint timestamps."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso_utc(value: str) -> Optional[float]:
    """Unix timestamp for ISO-8601 UTC strings with or without fractional
    seconds; None when unparseable."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


class Checkpointer:
    """Durable ingestion checkpoints in the ingestion_checkpoints dataset.

    A checkpoint is keyed by (pipeline, source, key, payload_hash): a
    completed checkpoint means that exact payload was archived, normalized,
    and written, so the pipeline can skip it on a deterministic rerun.

    Key-level helpers support two refresh models:
    - immutable cycles (FINRA settlement snapshots): a complete checkpoint
      for the key means the cycle is done forever;
    - growing sources (SEC companyfacts): a fresh checkpoint (within a TTL)
      means the last fetch is recent enough to reuse.
    """

    def __init__(self, data_root: Path, parser_version: str = CHECKPOINT_PARSER_VERSION):
        self.data_root = data_root
        self.parser_version = parser_version

    def _parquet_root(self) -> Path:
        return self.data_root / "parquet"

    def _rows(self) -> list[dict]:
        return parquet.read_table("ingestion_checkpoints", root=self._parquet_root()).to_pylist()

    def is_complete(self, pipeline: str, source: str, key: str, payload_hash: str) -> bool:
        for row in self._rows():
            if (
                row.get("pipeline") == pipeline
                and row.get("source") == source
                and row.get("key") == key
                and row.get("payload_hash") == payload_hash
                and row.get("status") == "complete"
            ):
                return True
        return False

    def is_fresh_for_key(self, pipeline: str, source: str, key: str, ttl_seconds: float) -> Optional[str]:
        """Payload hash of the NEWEST complete checkpoint for this key whose
        ``finished_at`` is within ``ttl_seconds``, or None."""
        now = time.time()
        newest_hash: Optional[str] = None
        newest_finished = ""
        for row in self._rows():
            if (
                row.get("pipeline") != pipeline
                or row.get("source") != source
                or row.get("key") != key
                or row.get("status") != "complete"
            ):
                continue
            finished = row.get("finished_at") or ""
            finished_ts = _parse_iso_utc(finished)
            if finished_ts is None:
                continue
            if now - finished_ts <= ttl_seconds and finished > newest_finished:
                newest_finished = finished
                newest_hash = row.get("payload_hash")
        return newest_hash

    def complete(
        self,
        pipeline: str,
        source: str,
        key: str,
        payload_hash: str,
        record_count: int,
        started_at: str,
    ) -> None:
        parquet.write_rows(
            "ingestion_checkpoints",
            [{
                "pipeline": pipeline,
                "source": source,
                "key": key,
                "payload_hash": payload_hash,
                "status": "complete",
                "record_count": record_count,
                "started_at": started_at,
                "finished_at": utc_now(),
                "parser_version": self.parser_version,
            }],
            root=self._parquet_root(),
        )


def summarize(status: str, skipped: int, written: int, total: int) -> dict:
    """Small pipeline result summary shared by all pipelines."""
    return {
        "status": status,
        "payloads_skipped": skipped,
        "payloads_written": written,
        "payloads_total": total,
    }