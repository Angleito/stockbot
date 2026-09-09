"""Runtime narrowing for untyped JSON/BigQuery/SQLite boundaries."""
from __future__ import annotations
import json

def result_rows(result: dict[str, object]) -> list[object]:
    raw = result.get("rows")
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    raise TypeError("rows must be a list")

def as_dict(value: object, *, what: str) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{what} must be a dict[str, object]")
            out[k] = v
        return out
    raise TypeError(f"{what} must be a dict[str, object]")

def as_list(value: object, *, what: str) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    raise TypeError(f"{what} must be a list")

def as_str_list(value: object, *, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for e in value:
            if not isinstance(e, str):
                raise TypeError(f"{what} must be a list[str]")
            out.append(e)
        return out
    raise TypeError(f"{what} must be a list[str]")

def as_str(value: object, *, what: str) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"{what} must be a str")

def as_int(value: object, *, what: str) -> int:
    # Mirrors the int() coercion the cast sites relied on (bool/int/str/float),
    # raising TypeError on anything else; ValueError from int() propagates.
    if isinstance(value, bool | int | str | float):
        return int(value)
    raise TypeError(f"{what} must be an int")

def json_from_text(raw: object, *, what: str) -> object | None:
    """Parse a JSON-text boundary value. None/blank -> None, bad JSON -> None."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"{what} must be a JSON string")
    if not raw.strip():
        return None
    try:
        parsed: object = json.loads(raw)
        return parsed
    except ValueError:
        return None
