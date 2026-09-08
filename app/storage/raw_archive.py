"""Immutable raw archive for source payloads.

Every source response is stored once, keyed by its content hash, together
with a manifest of retrieval metadata (URL, request parameters, response
headers, retrieved time, parser version).  Files are write-once: re-archiving
identical content is a no-op, and a different payload for the same source
key produces a new hash-named file.  Nothing in the archive is ever
overwritten or mutated, so ingestion can be replayed deterministically.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from ..config import get_data_root

DEFAULT_RAW_ROOT = get_data_root() / "raw"

MANIFEST_SUFFIX = ".manifest.json"
PAYLOAD_SUFFIX = ".json"


def content_hash(data: bytes) -> str:
    """sha256 of the exact payload bytes; the archive's dedup key."""
    return hashlib.sha256(data).hexdigest()


def _safe_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


@dataclass(frozen=True)
class ArchiveRecord:
    """One archived payload plus its retrieval metadata."""

    source: str
    kind: str
    key: str
    sha256: str
    payload_path: Path
    manifest_path: Path
    size: int
    retrieved_at: str
    url: str
    metadata: dict[str, object] = field(default_factory=dict)


def _manifest_path(payload_path: Path) -> Path:
    return payload_path.with_suffix(MANIFEST_SUFFIX)


def archive(
    source: str,
    kind: str,
    key: str,
    payload: bytes,
    *,
    url: str,
    retrieved_at: Optional[str] = None,
    metadata: Optional[dict[str, object]] = None,
    root: Optional[Path] = None,
) -> ArchiveRecord:
    """Store one immutable payload and its manifest.

    Idempotent: archiving the same bytes for the same (source, kind, key)
    returns the existing record without rewriting anything.
    """
    root = Path(root) if root else get_data_root() / "raw"
    digest = content_hash(payload)
    directory = root / _safe_component(source) / _safe_component(kind) / _safe_component(key)
    payload_path = directory / f"{digest[:16]}{PAYLOAD_SUFFIX}"
    manifest_path = _manifest_path(payload_path)
    if payload_path.exists() and manifest_path.exists():
        return _load_record(payload_path, manifest_path)
    directory.mkdir(parents=True, exist_ok=True)
    if not payload_path.exists():
        payload_path.write_bytes(payload)
    if not manifest_path.exists():
        manifest = {
            "source": source,
            "kind": kind,
            "key": key,
            "sha256": digest,
            "size": len(payload),
            "retrieved_at": retrieved_at or _utc_now(),
            "url": url,
            "metadata": metadata or {},
        }
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return _load_record(payload_path, manifest_path)


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_record(payload_path: Path, manifest_path: Path) -> ArchiveRecord:
    raw: object = json.loads(manifest_path.read_text())
    manifest: dict[str, object] = raw if isinstance(raw, dict) else {}
    source_value = manifest.get("source", "")
    kind_value = manifest.get("kind", "")
    key_value = manifest.get("key", "")
    sha_value = manifest.get("sha256", "")
    retrieved_value = manifest.get("retrieved_at", "")
    url_value = manifest.get("url", "")
    size_value = manifest.get("size", payload_path.stat().st_size)
    metadata_value = manifest.get("metadata")
    return ArchiveRecord(
        source=source_value if isinstance(source_value, str) else "",
        kind=kind_value if isinstance(kind_value, str) else "",
        key=key_value if isinstance(key_value, str) else "",
        sha256=sha_value if isinstance(sha_value, str) else "",
        payload_path=payload_path,
        manifest_path=manifest_path,
        size=size_value if isinstance(size_value, int) else payload_path.stat().st_size,
        retrieved_at=retrieved_value if isinstance(retrieved_value, str) else "",
        url=url_value if isinstance(url_value, str) else "",
        metadata=metadata_value if isinstance(metadata_value, dict) else {},
    )


def find(
    source: str,
    kind: str,
    key: str,
    *,
    sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> Optional[ArchiveRecord]:
    """Return the archived record for a (source, kind, key), optionally
    narrowed by content hash, or None."""
    root = Path(root) if root else get_data_root() / "raw"
    directory = root / _safe_component(source) / _safe_component(kind) / _safe_component(key)
    if not directory.is_dir():
        return None
    for payload_path in sorted(directory.glob(f"*{PAYLOAD_SUFFIX}")):
        if sha256 is not None and payload_path.stem != sha256[:16]:
            continue
        manifest_path = _manifest_path(payload_path)
        if manifest_path.is_file():
            return _load_record(payload_path, manifest_path)
    return None


def iter_archive(
    source: str,
    kind: str,
    key: str,
    *,
    root: Optional[Path] = None,
) -> Iterable[ArchiveRecord]:
    """All archived payload revisions for one source key, oldest first."""
    root = Path(root) if root else get_data_root() / "raw"
    directory = root / _safe_component(source) / _safe_component(kind) / _safe_component(key)
    if not directory.is_dir():
        return
    for payload_path in sorted(directory.glob(f"*{PAYLOAD_SUFFIX}")):
        manifest_path = _manifest_path(payload_path)
        if manifest_path.is_file():
            yield _load_record(payload_path, manifest_path)


def has_payload(
    source: str,
    kind: str,
    key: str,
    sha256: str,
    *,
    root: Optional[Path] = None,
) -> bool:
    """True when a payload with this content hash is already archived."""
    return find(source, kind, key, sha256=sha256, root=root) is not None