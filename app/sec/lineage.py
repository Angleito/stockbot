"""XBRL fact lineage: fact -> accession -> filing -> period -> concept."""

import re

_KEYS = ("concept", "value", "period_start", "period_end", "fiscal_year",
         "fiscal_period", "filed_at", "accession", "source_url", "known_at")

_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fact_lineage(row: dict) -> dict:
    """Pure projection; missing keys -> None."""
    try:
        items = dict(row) if isinstance(row, dict) else {}
    except Exception:
        items = {}
    return {key: items.get(key) for key in _KEYS}


def period_lineage(rows: list) -> list:
    """Group by period_end; earliest filed_at is originally_reported."""
    groups: dict = {}
    for row in rows or []:
        try:
            end = row.get("period_end") if isinstance(row, dict) else None
        except Exception:
            continue
        if not end:
            continue
        groups.setdefault(end, []).append(row)
    out = []
    for end in sorted(groups):
        group = sorted(groups[end],
                       key=lambda r: (str(r.get("filed_at") or ""),
                                      str(r.get("known_at") or "")))
        original = fact_lineage(group[0])
        latest = fact_lineage(group[-1])
        out.append({"period_end": end, "originally_reported": original,
                    "latest": latest, "restated": latest != original})
    return out


def xbrl_lineage(entity_id, concept, *, as_of=None, root=None) -> list:
    """Newest period_end first; as_of gates on known_at (strict YYYY-MM-DD)."""
    from app.storage import duckdb

    clause = ""
    params: list = [entity_id, concept]
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
