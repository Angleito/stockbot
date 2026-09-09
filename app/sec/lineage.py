"""XBRL fact lineage: fact -> accession -> filing -> period -> concept."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path

_KEYS = ("concept", "value", "period_start", "period_end", "fiscal_year",
         "fiscal_period", "filed_at", "accession", "source_url", "known_at")

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fact_lineage(row: Mapping[str, object]) -> dict[str, object]:
    """Pure projection; missing keys -> None.

    Rows arrive as plain dicts from the duckdb parquet views, so the
    boundary takes a Mapping and projects only the known lineage keys.
    """
    try:
        items: dict[str, object] = dict(row) if isinstance(row, dict) else {}
    except Exception:
        items = {}
    return {key: items.get(key) for key in _KEYS}


def period_lineage(
    rows: Sequence[Mapping[str, object]] | None,
) -> list[dict[str, object]]:
    """Group by period_end; earliest filed_at is originally_reported.

    Rows with a missing or non-text period_end cannot be grouped, so they
    are skipped like the missing-key rows the original code already dropped.
    """
    groups: dict[str, list[Mapping[str, object]]] = {}
    for row in rows or []:
        try:
            end = row.get("period_end")
        except Exception:
            continue
        if not isinstance(end, str) or not end:
            continue
        groups.setdefault(end, []).append(row)
    out: list[dict[str, object]] = []
    for end in sorted(groups):
        group = sorted(groups[end], key=_filed_known)
        original = fact_lineage(group[0])
        latest = fact_lineage(group[-1])
        out.append({"period_end": end, "originally_reported": original,
                    "latest": latest, "restated": latest != original})
    return out


def _filed_known(row: Mapping[str, object]) -> tuple[str, str]:
    return (str(row.get("filed_at") or ""), str(row.get("known_at") or ""))


def xbrl_lineage(
    entity_id: str,
    concept: str,
    *,
    as_of: str | None = None,
    root: Path | None = None,
) -> list[dict[str, object]]:
    """Newest period_end first; as_of gates on known_at (strict YYYY-MM-DD)."""
    from app.storage import duckdb

    clause = ""
    params: list[str] = [entity_id, concept]
    if as_of is not None:
        if not isinstance(as_of, str) or not _AS_OF_RE.match(as_of):
            raise ValueError(
                f"invalid as_of date: {as_of!r} (expected YYYY-MM-DD)")
        frag, param = duckdb.as_of_clause(as_of)
        clause = f" AND {frag}"
        params.append(param)
    rows = duckdb.query(
        "SELECT concept, value, period_start, period_end, fiscal_year, "
        "fiscal_period, filed_at, accession, source_url, known_at "
        "FROM financial_facts WHERE entity_id = ? AND concept = ?"
        f"{clause} ORDER BY period_end DESC, filed_at DESC, accession DESC",
        params=params, data_root=root or duckdb.DEFAULT_DATA_ROOT,
    )
    return [fact_lineage(row) for row in rows]
