"""FINRA normalizers: Query API row shapes -> short_interest / short_sale_volume.

A settlement-date snapshot is normalized as one unit: every row of the
complete snapshot shares the same ``known_at`` (the time the complete
snapshot was first archived), because FINRA does not expose a per-row
publication timestamp.  Rows without a valid short-position quantity are
dropped here, mirroring the screen's exclusion rules.
"""

from __future__ import annotations

from typing import Any, Optional

from ..storage import ids

SHORT_INTEREST_PARSER_VERSION = "finra-short-interest-v1"
SHORT_SALE_VOLUME_PARSER_VERSION = "finra-reg-sho-v1"

_SHORT_INTEREST_FIELDS = (
    "symbolCode",
    "issueName",
    "settlementDate",
    "currentShortPositionQuantity",
    "previousShortPositionQuantity",
    "averageDailyVolumeQuantity",
    "daysToCoverQuantity",
)


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_short_interest_snapshot(
    rows: list[dict],
    *,
    settlement_date: str,
    known_at: str,
    retrieved_at: str,
    content_hash: str,
    source_url: str,
    source_record_id: str,
) -> dict[str, list[dict]]:
    """FINRA consolidated short interest rows -> short_interest dataset rows."""
    short_interest: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbolCode") or "").strip().upper()
        short_position = _to_float(row.get("currentShortPositionQuantity"))
        if not symbol or short_position is None or short_position < 0:
            continue
        entity_id = ids.finra_entity_id(symbol)
        short_interest.append({
            "row_id": f"finra:row:{settlement_date}:{symbol}",
            "entity_id": entity_id,
            "security_id": None,
            "symbol_code": symbol,
            "issue_name": str(row.get("issueName") or "").strip() or None,
            "settlement_date": settlement_date,
            "short_position": short_position,
            "prev_position": _to_float(row.get("previousShortPositionQuantity")),
            "avg_daily_volume": _to_float(row.get("averageDailyVolumeQuantity")),
            "days_to_cover": _to_float(row.get("daysToCoverQuantity")),
            "source_url": source_url,
            "source_record_id": source_record_id,
            "known_at": known_at,
            "retrieved_at": retrieved_at,
            "content_hash": content_hash,
            "parser_version": SHORT_INTEREST_PARSER_VERSION,
        })
    return {"short_interest": short_interest}


def normalize_short_sale_volume(
    rows: list[dict],
    *,
    known_at: str,
    retrieved_at: str,
    content_hash: str,
    source_url: str,
    source_record_id: str,
) -> dict[str, list[dict]]:
    """Reg SHO daily rows -> short_sale_volume dataset rows.

    Kept deliberately separate from short interest: short-sale volume is
    flow (Reg SHO), short interest is a position at settlement.  Field names
    differ across Reg SHO datasets (par vs share quantities), so the
    normalizer accepts the documented alternates.
    """
    short_sale_volume: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(
            row.get("symbolCode")
            or row.get("issueSymbolIdentifier")
            or row.get("securitiesInformationProcessorSymbolIdentifier")
            or ""
        ).strip().upper()
        trade_date = str(
            row.get("tradeDate") or row.get("tradeReportDate") or row.get("shortSaleVolumeTradeDate") or ""
        )
        if not symbol or not trade_date:
            continue
        short_sale_volume.append({
            "row_id": f"finra:regsho:{trade_date}:{symbol}",
            "entity_id": ids.finra_entity_id(symbol),
            "security_id": None,
            "symbol_code": symbol,
            "trade_date": trade_date,
            "facility": str(row.get("facilityCode") or row.get("reportingFacility") or ""),
            "short_volume": _to_float(row.get("shortVolumeQuantity") or row.get("shortParQuantity")),
            "exempt_volume": _to_float(row.get("shortExemptVolumeQuantity") or row.get("shortExemptParQuantity")),
            "total_volume": _to_float(row.get("totalVolume") or row.get("totalParQuantity")),
            "source_url": source_url,
            "source_record_id": source_record_id,
            "known_at": known_at,
            "retrieved_at": retrieved_at,
            "content_hash": content_hash,
            "parser_version": SHORT_SALE_VOLUME_PARSER_VERSION,
        })
    return {"short_sale_volume": short_sale_volume}