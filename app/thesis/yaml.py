"""Smallest safe YAML primitives for thesis persistence (stdlib + PyYAML only)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol, Self, TypeVar

import yaml

try:
    import fcntl  # Linux-only; thesis locking is explicitly Linux-only.
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from app.thesis.models import SCHEMA_VERSION

_M = TypeVar("_M", bound="YamlModel")


class YamlModel(Protocol):
    """Structural thesis-model surface consumed by load_yaml (all models share from_dict)."""

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: str = ..., /) -> Self:
        ...


def _root_resolved(root: Path | str) -> Path:
    return Path(os.path.realpath(root))


def _resolve_inside(root: Path | str, path: Path | str) -> Path:
    """Resolve ``path`` and reject anything escaping ``root``.

    Catches absolute child escapes, ``..`` traversal, and symlink escapes
    (via realpath on both sides). Raises ``ValueError`` naming the path.
    """
    root_r = _root_resolved(root)
    p = Path(path)
    dest = p if p.is_absolute() else (root_r / p)
    dest_r = Path(os.path.realpath(dest))
    try:
        dest_r.relative_to(root_r)
    except ValueError:
        raise ValueError(f"refusing path outside thesis root: {path!s} (root {root_r})") from None
    return dest_r


def load_yaml(path: Path | str, model_type: type[_M]) -> _M:
    """Safe-load a mapping YAML file, require schema v1, validate and return."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{p}: file not found") from None
    except OSError as exc:
        raise ValueError(f"{p}: cannot read file: {exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"{p}: malformed YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{p}: root must be a mapping, got {type(data).__name__}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{p}: unknown schema_version {data.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    return model_type.from_dict(data, str(p))


def load_raw_yaml(path: Path | str) -> dict[str, Any]:
    """Safe-load + mapping/schema check without model validation."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{p}: malformed YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{p}: root must be a mapping, got {type(data).__name__}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{p}: unknown schema_version {data.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    return data


def atomic_write_yaml(path: Path | str, value: dict[str, Any], root: Path | str) -> Path:
    """Validate-then-replace ``path`` atomically under ``root``.

    Writes a same-directory temp file, flushes + fsyncs, loads it back to
    validate, then ``os.replace``. On error only the temp file is removed;
    the previous valid destination is left unchanged.
    """
    if not isinstance(value, dict):
        raise ValueError(f"{path}: value must be a mapping, got {type(value).__name__}")
    dest = _resolve_inside(root, path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(value, fh, sort_keys=True, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            back = yaml.safe_load(Path(tmp).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{dest}: staged write failed validation (malformed YAML): {exc}") from None
        if not isinstance(back, dict):
            raise ValueError(f"{dest}: staged write failed validation (non-mapping root)")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return dest


def atomic_write_json(path: Path | str, value: dict[str, Any], root: Path | str) -> Path:
    """Same temp/flush/fsync/atomic-replace discipline as YAML, for JSON intents."""
    if not isinstance(value, dict):
        raise ValueError(f"{path}: value must be a mapping, got {type(value).__name__}")
    dest = _resolve_inside(root, path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            back = json.loads(Path(tmp).read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"{dest}: staged write failed validation (malformed JSON): {exc}") from None
        if not isinstance(back, dict):
            raise ValueError(f"{dest}: staged write failed validation (non-mapping root)")
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return dest


def atomic_write_text(path: Path | str, content: str, root: Path | str) -> Path:
    """Same temp/flush/fsync/atomic-replace discipline as YAML, for journal Markdown."""
    if not isinstance(content, str):
        raise ValueError(f"{path}: content must be text, got {type(content).__name__}")
    dest = _resolve_inside(root, path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return dest


@contextlib.contextmanager
def thesis_lock(thesis_dir: Path | str):
    """Hold one ``fcntl.flock`` lock file per thesis (Linux-only, stdlib only)."""
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("thesis_lock requires Linux fcntl.flock")
    lock_path = Path(thesis_dir) / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
