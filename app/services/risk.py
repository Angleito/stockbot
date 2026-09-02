"""Storage-touching glue for mandate evaluation.

Loads the mandate file, the latest persisted portfolio snapshot, and the
newest-per-entity sector mappings, then delegates the pure math to the
domain evaluator.  Nothing is persisted; evaluations are computed on
demand.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..domain.risk.evaluation import RiskEvaluation, evaluate_mandate
from ..storage import duckdb
from .mandate import load_mandate_file
from .portfolio_sync import read_latest_snapshot


def load_sector_map(
    data_root: Path | None = None, as_of: datetime | None = None
) -> dict[str, str]:
    """Newest-per-entity sector from sector_mappings, knowable on/before as_of;
    same-instant conflicting sectors drop the entity (unknown exposure)."""
    clause, param = duckdb.as_of_clause(as_of.isoformat()) if as_of else ("1 = 1", None)
    params = [param] if param is not None else []
    rows = duckdb.query(
        "SELECT entity_id, sector FROM ("
        "SELECT entity_id, sector, "
        "row_number() OVER (PARTITION BY entity_id ORDER BY CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST) AS _rn, "
        "count(DISTINCT sector) OVER (PARTITION BY entity_id, CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
        f"FROM sector_mappings WHERE {clause}"
        ") WHERE _rn = 1 AND _variants = 1",
        params=params,
        data_root=data_root,
    )
    return {str(row["entity_id"]): str(row["sector"]) for row in rows}


def evaluate_latest_mandate(
    mandate_path: Path, data_root: Path | None = None
) -> RiskEvaluation:
    """Load mandate + latest snapshot + sector map and evaluate.

    Raises FileNotFoundError (mandate missing, no snapshot) or ValueError
    (bad mandate).
    """
    mandate = load_mandate_file(mandate_path)
    snapshot = read_latest_snapshot(data_root=data_root)
    if snapshot is None:
        raise FileNotFoundError(
            "no persisted portfolio snapshot; run a portfolio sync "
            "(get_portfolio_snapshot with refresh) first"
        )
    sector_map = load_sector_map(data_root=data_root, as_of=snapshot.created_at)
    return evaluate_mandate(snapshot, mandate, sector_map)
