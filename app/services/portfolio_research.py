"""Portfolio research view: SEC + FINRA enrichment per position.

Deterministic, point-in-time enrichment over the normalized datasets
(``financial_facts`` from SEC EDGAR, ``short_interest`` from FINRA), gated
by ``known_at <= as_of`` via the DuckDB query layer.  Missing data is
reported as absent (empty dicts / None), never estimated or zeroed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..analytics.screens import _resolve_as_of
from ..domain.portfolio import PortfolioSnapshot, Position
from ..storage import duckdb

SEC_CONCEPTS: tuple[str, ...] = (
    "Revenue",
    "NetIncomeLoss",
    "CashAndCashEquivalents",
    "LongTermDebt",
    "EntityCommonStockSharesOutstanding",
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parent.parent.parent / "data"


@dataclass(frozen=True)
class PortfolioResearchPosition:
    position: Position
    latest_sec_metrics: dict[str, Any]
    latest_finra_metrics: dict[str, Any]
    research_data_freshness: dict[str, Any]


def enrich_portfolio_research(
    snapshot: PortfolioSnapshot,
    *,
    as_of: date | None = None,
    data_root: Path | None = None,
) -> list[PortfolioResearchPosition]:
    """Enrich every snapshot position with its latest SEC facts and FINRA
    short-interest metrics, each restricted to ``known_at <= as_of``."""
    data_root = Path(data_root or DEFAULT_DATA_ROOT)
    as_of_str = _resolve_as_of(as_of.isoformat() if as_of is not None else None)
    results: list[PortfolioResearchPosition] = []
    for position in snapshot.positions:
        if position.entity_id is not None:
            sec_metrics = _sec_metrics(position.entity_id, as_of_str, data_root)
        else:
            sec_metrics = {}
        ticker = (position.ticker or "").strip()
        if ticker:
            finra_metrics = _finra_metrics(ticker, as_of_str, data_root)
        else:
            finra_metrics = {}
        results.append(PortfolioResearchPosition(
            position=position,
            latest_sec_metrics=sec_metrics,
            latest_finra_metrics=finra_metrics,
            research_data_freshness=_freshness(as_of_str, sec_metrics, finra_metrics),
        ))
    return results


def _sec_metrics(entity_id: str, as_of: str, data_root: Path) -> dict[str, Any]:
    """Latest knowable fact per SEC concept for an entity; missing concepts
    are simply absent from the result."""
    metrics: dict[str, Any] = {}
    for concept in SEC_CONCEPTS:
        fact = _latest_sec_fact(entity_id, concept, as_of, data_root)
        if fact is not None:
            metrics[concept] = fact
    return metrics


def _latest_sec_fact(entity_id: str, concept: str, as_of: str, data_root: Path) -> dict[str, Any] | None:
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT value, period_end, filed_at, accession, source_url "
        "FROM financial_facts "
        f"WHERE entity_id = ? AND concept = ? AND {clause} "
        "ORDER BY filed_at DESC, period_end DESC, accession DESC "
        "LIMIT 1",
        params=[entity_id, concept, param],
        data_root=data_root,
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "value": _decimal(row.get("value")),
        "period_end": str(row.get("period_end") or ""),
        "filed_at": str(row.get("filed_at") or ""),
        "accession": row.get("accession"),
        "source_url": row.get("source_url"),
    }


def _finra_metrics(ticker: str, as_of: str, data_root: Path) -> dict[str, Any]:
    """Latest knowable short-interest metrics for a ticker's newest
    eligible settlement cycle.

    The newest eligible settlement date wins first (``settlement_date <=
    as_of`` and knowable on or before ``as_of``); within that settlement,
    the newest source revision wins (mirroring ``screens._snapshot_rows``
    per-settlement semantics).  Same-instant conflicting versions yield no
    metrics (empty dict -> freshness finra fields None).
    """
    clause, param = duckdb.as_of_clause(as_of)
    rows = duckdb.query(
        "SELECT settlement_date, short_position, prev_position, "
        "avg_daily_volume, days_to_cover, known_at FROM ("
        "SELECT settlement_date, short_position, prev_position, "
        "avg_daily_volume, days_to_cover, known_at, "
        "row_number() OVER (PARTITION BY symbol_code ORDER BY CAST(settlement_date AS DATE) DESC, CAST(known_at AS TIMESTAMPTZ) DESC NULLS LAST, CAST(retrieved_at AS TIMESTAMPTZ) DESC NULLS LAST) AS _rn, "
        "count(DISTINCT list_value(CAST(short_position AS VARCHAR), CAST(prev_position AS VARCHAR), CAST(avg_daily_volume AS VARCHAR), CAST(days_to_cover AS VARCHAR), CAST(issue_name AS VARCHAR))) OVER (PARTITION BY symbol_code, CAST(settlement_date AS DATE), CAST(known_at AS TIMESTAMPTZ), CAST(retrieved_at AS TIMESTAMPTZ)) AS _variants "
        "FROM short_interest "
        "WHERE symbol_code = UPPER(?) "
        "AND CAST(settlement_date AS DATE) <= CAST(? AS DATE) "
        f"AND {clause}"
        ") WHERE _rn = 1 AND _variants = 1",
        params=[ticker, as_of, param],
        data_root=data_root,
    )
    if not rows:
        return {}
    row = rows[0]
    short_position = _decimal(row.get("short_position"))
    prev_position = _decimal(row.get("prev_position"))
    change: Decimal | None = None
    if short_position is not None and prev_position is not None:
        change = short_position - prev_position
    change_pct: Decimal | None = None
    if change is not None and prev_position != 0:
        change_pct = Decimal(100) * change / prev_position
    return {
        "short_position": short_position,
        "prev_position": prev_position,
        "short_interest_change": change,
        "short_interest_change_pct": change_pct,
        "days_to_cover": _decimal(row.get("days_to_cover")),
        "settlement_date": str(row.get("settlement_date") or ""),
        "avg_daily_volume": _decimal(row.get("avg_daily_volume")),
        "known_at": str(row.get("known_at") or ""),
    }


def _freshness(as_of: str, sec_metrics: dict[str, Any], finra_metrics: dict[str, Any]) -> dict[str, Any]:
    filed_dates = [
        d
        for d in (_parse_date(fact.get("filed_at")) for fact in sec_metrics.values())
        if d is not None
    ]
    return {
        "as_of": as_of,
        "sec_latest_filed_at": max(filed_dates, default=None),
        "finra_settlement_date": _parse_date(finra_metrics.get("settlement_date")),
        "finra_known_at": finra_metrics.get("known_at") or None,
    }


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None