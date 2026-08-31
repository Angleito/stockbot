"""Tests for the risk/mandate domain, storage glue, CLI command, and tool."""

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from cli import _cmd_evaluate_mandate
from app import tools
from app.domain.portfolio import PortfolioSnapshot, Position
from app.domain.risk.exposure import UNKNOWN_SECTOR, evaluate_mandate
from app.domain.risk.mandate import Mandate, RiskLimit, load_mandate
from app.services import risk as risk_service
from app.services.portfolio_sync import persist_snapshot
from app.storage import parquet


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _position(position_id, ticker, entity_id, weight, *, cash=None):
    return Position(
        position_id=position_id,
        account_id="acc-1",
        security_id="sec:equity:0000320193" if entity_id else None,
        entity_id=entity_id,
        ticker=ticker,
        quantity=Decimal("10"),
        average_cost=Decimal("95.50"),
        market_price=Decimal("116.84"),
        market_value=Decimal("1168.40"),
        unrealized_gain=Decimal("213.40"),
        unrealized_gain_pct=Decimal("0.22"),
        portfolio_weight=weight,
        source="robinhood_mcp",
        retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
        price_type="last",
        quote_retrieved_at=datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc),
    )


_MISSING = object()


def _hand_built_snapshot(weight=_MISSING) -> PortfolioSnapshot:
    resolved = _position(
        "snap-1:acc-1:WING", "WING", "sec:cik:0000320193",
        weight if weight is not _MISSING else Decimal("0.75"),
    )
    unresolved = _position(
        "snap-1:acc-1:ZZZZ", "ZZZZ", None,
        weight if weight is not _MISSING else Decimal("0.25"),
    )
    return PortfolioSnapshot(
        snapshot_id="portfolio:robinhood:2026-08-25T12:00:00+00:00",
        created_at=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        broker="robinhood",
        account_ids=("acc-1",),
        cash=Decimal("1234.56"),
        invested_value=Decimal("1228.40"),
        total_value=Decimal("2462.96"),
        positions=(resolved, unresolved),
    )


def _mandate(limits, prohibited=()) -> Mandate:
    return Mandate(limits=tuple(limits), prohibited_assets=tuple(prohibited))


def _limit(metric, operator, threshold, **overrides) -> RiskLimit:
    values = dict(metric=metric, operator=operator, threshold=Decimal(str(threshold)))
    values.update(overrides)
    return RiskLimit(**values)


def _write_mandate(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# load_mandate
# ---------------------------------------------------------------------------


def test_load_mandate_valid_json_with_defaults(tmp_path):
    path = _write_mandate(tmp_path / "mandate.json", {
        "limits": [
            {"metric": "single_position_weight", "operator": "<=", "threshold": 0.25},
            {"metric": "minimum_cash", "operator": ">=", "threshold": 0.10},
            {"metric": "sector_exposure", "target": "semiconductors", "operator": "<=", "threshold": 0.20, "severity": "critical"},
        ],
        "prohibited_assets": ["GME", "sec:cik:0000320193"],
        "extra_ignored": True,
    })
    mandate = load_mandate(path)
    assert len(mandate.limits) == 3
    assert mandate.limits[0].severity == "warning"
    assert mandate.limits[0].unit == "ratio"
    assert mandate.limits[0].threshold == Decimal("0.25")
    assert mandate.limits[1].severity == "warning"
    assert mandate.limits[2].target == "semiconductors"
    assert mandate.limits[2].severity == "critical"
    assert mandate.prohibited_assets == ("GME", "sec:cik:0000320193")


def test_load_mandate_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_mandate(tmp_path / "nope.json")


@pytest.mark.parametrize("payload", [
    [],
    {"limits": {}},
    {"limits": [{"metric": "bogus", "operator": "<=", "threshold": 1}]},
    {"limits": [{"metric": "single_position_weight", "operator": ">", "threshold": 1}]},
    {"limits": [{"metric": "single_position_weight", "operator": "<="}]},
    {"limits": [{"metric": "single_position_weight", "operator": "<=", "threshold": 0}]},
    {"limits": [{"metric": "single_position_weight", "operator": "<=", "threshold": -0.5}]},
    {"limits": [{"metric": "single_position_weight", "operator": "<=", "threshold": "abc"}]},
    {"limits": [{"metric": "sector_exposure", "operator": "<=", "threshold": 0.2}]},
    {"limits": [{"metric": "sector_exposure", "target": "", "operator": "<=", "threshold": 0.2}]},
    {"limits": [{"metric": "minimum_cash", "operator": ">=", "threshold": 0.1, "unit": "bananas"}]},
    {"limits": [{"metric": "minimum_cash", "operator": ">=", "threshold": 0.1, "severity": "fatal"}]},
    {"limits": [{"metric": "minimum_cash", "operator": ">=", "threshold": 0.1}], "prohibited_assets": "GME"},
    {"limits": [{"metric": "minimum_cash", "operator": ">=", "threshold": 0.1}], "prohibited_assets": [""]},
    {"limits": "nope"},
])
def test_load_mandate_rejects_bad_config(tmp_path, payload):
    path = _write_mandate(tmp_path / "mandate.json", payload)
    with pytest.raises(ValueError):
        load_mandate(path)


# ---------------------------------------------------------------------------
# evaluate_mandate math
# ---------------------------------------------------------------------------


def test_single_position_weight_breach_and_clean_position():
    mandate = _mandate([_limit("single_position_weight", "<=", "0.25")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert len(evaluation.breaches) == 1
    breach = evaluation.breaches[0]
    assert breach.metric == "single_position_weight"
    assert breach.actual == Decimal("0.75")
    assert breach.limit == Decimal("0.25")
    assert breach.excess == Decimal("0.50")
    assert breach.note == "WING (snap-1:acc-1:WING)"
    assert breach.severity == "warning"


def test_single_position_weight_at_threshold_no_breach():
    mandate = _mandate([_limit("single_position_weight", "<=", "0.75")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert evaluation.breaches == ()


def test_single_position_weight_breach_with_ge_operator():
    mandate = _mandate([_limit("single_position_weight", ">=", "0.80")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    breach = evaluation.breaches[0]
    assert breach.excess == Decimal("0.05")


def test_single_position_weight_none_weight_not_evaluable():
    snapshot = _hand_built_snapshot(weight=None)
    mandate = _mandate([_limit("single_position_weight", "<=", "0.25")])
    evaluation = evaluate_mandate(snapshot, mandate)
    assert evaluation.breaches == ()
    assert evaluation.not_evaluable == (
        "single_position_weight: WING (no weight)",
        "single_position_weight: ZZZZ (no weight)",
    )


def test_minimum_cash_ratio_breach_and_excess():
    mandate = _mandate([_limit("minimum_cash", ">=", "0.60")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert len(evaluation.breaches) == 1
    breach = evaluation.breaches[0]
    assert breach.metric == "minimum_cash"
    assert breach.actual == Decimal("1234.56") / Decimal("2462.96")
    assert breach.excess == Decimal("0.60") - breach.actual


def test_minimum_cash_ratio_satisfied_no_breach():
    mandate = _mandate([_limit("minimum_cash", ">=", "0.10")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert evaluation.breaches == ()


def test_minimum_cash_dollars_unit():
    mandate = _mandate([_limit("minimum_cash", ">=", "5000", unit="dollars")])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    breach = evaluation.breaches[0]
    assert breach.actual == Decimal("1234.56")
    assert breach.excess == Decimal("5000") - Decimal("1234.56")


def test_minimum_cash_unavailable_not_evaluable():
    snapshot = _hand_built_snapshot()
    snapshot = PortfolioSnapshot(
        snapshot_id=snapshot.snapshot_id,
        created_at=snapshot.created_at,
        broker=snapshot.broker,
        account_ids=snapshot.account_ids,
        cash=None,
        invested_value=snapshot.invested_value,
        total_value=snapshot.total_value,
        positions=snapshot.positions,
    )
    mandate = _mandate([_limit("minimum_cash", ">=", "0.10")])
    evaluation = evaluate_mandate(snapshot, mandate)
    assert evaluation.breaches == ()
    assert evaluation.not_evaluable == ("minimum_cash: cash unavailable",)


def test_prohibited_assets_ticker_and_entity_matches():
    mandate = _mandate([], prohibited=("wing", "sec:cik:0000320193", "NOPE"))
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert len(evaluation.breaches) == 2
    for breach in evaluation.breaches:
        assert breach.metric == "prohibited_assets"
        assert breach.severity == "warning"
        assert breach.excess is None
        assert breach.actual == "WING"
        assert breach.note == "position WING (snap-1:acc-1:WING)"
    assert {breach.target for breach in evaluation.breaches} == {"wing", "sec:cik:0000320193"}


def test_sector_exposure_buckets_unknown_and_breaches():
    mandate = _mandate([_limit("sector_exposure", "<=", "0.20", target="semiconductors")])
    evaluation = evaluate_mandate(
        _hand_built_snapshot(), mandate,
        sector_map={"sec:cik:0000320193": "semiconductors"},
    )
    assert evaluation.sector_exposures == {
        "semiconductors": Decimal("0.75"),
        UNKNOWN_SECTOR: Decimal("0.25"),
    }
    assert len(evaluation.breaches) == 1
    breach = evaluation.breaches[0]
    assert breach.metric == "sector_exposure"
    assert breach.target == "semiconductors"
    assert breach.actual == Decimal("0.75")
    assert breach.excess == Decimal("0.55")


def test_sector_exposure_missing_target_sector_is_zero():
    mandate = _mandate([_limit("sector_exposure", "<=", "0.20", target="aero")])
    evaluation = evaluate_mandate(
        _hand_built_snapshot(), mandate,
        sector_map={"sec:cik:0000320193": "semiconductors"},
    )
    assert evaluation.breaches == ()


def test_empty_mandate_zero_breaches():
    mandate = _mandate([])
    evaluation = evaluate_mandate(_hand_built_snapshot(), mandate)
    assert evaluation.breaches == ()
    assert evaluation.not_evaluable == ()
    assert evaluation.snapshot_id == "portfolio:robinhood:2026-08-25T12:00:00+00:00"
    assert evaluation.created_at == datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# load_sector_map
# ---------------------------------------------------------------------------


def _sector_row(entity_id, sector, known_at):
    return {
        "entity_id": entity_id,
        "sector": sector,
        "source": "test",
        "known_at": known_at,
        "retrieved_at": "2026-08-25T12:00:00Z",
        "content_hash": "abc",
        "parser_version": "sector-mapping-v1",
    }


def test_load_sector_map_newest_wins(data_root):
    parquet.write_rows(
        "sector_mappings",
        [
            _sector_row("sec:cik:0000320193", "aero", "2026-08-25T00:00:00Z"),
            _sector_row("sec:cik:0000320193", "defense", "2026-08-26T00:00:00Z"),
        ],
        root=data_root / "parquet",
    )
    assert risk_service.load_sector_map(data_root=data_root) == {
        "sec:cik:0000320193": "defense"
    }


def test_load_sector_map_as_of_prefers_older(data_root):
    parquet.write_rows(
        "sector_mappings",
        [
            _sector_row("sec:cik:0000320193", "aero", "2026-08-25T00:00:00Z"),
            _sector_row("sec:cik:0000320193", "defense", "2026-08-26T00:00:00Z"),
        ],
        root=data_root / "parquet",
    )
    as_of = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    assert risk_service.load_sector_map(data_root=data_root, as_of=as_of) == {
        "sec:cik:0000320193": "aero"
    }


def test_load_sector_map_empty_dataset(data_root):
    assert risk_service.load_sector_map(data_root=data_root) == {}


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _seed_snapshot_and_mandate(data_root, mandate_payload=None):
    persist_snapshot(_hand_built_snapshot(), data_root=data_root)
    parquet.write_rows(
        "sector_mappings",
        [_sector_row("sec:cik:0000320193", "semiconductors", "2026-08-25T00:00:00Z")],
        root=data_root / "parquet",
    )
    mandate_path = data_root / "mandate.json"
    return _write_mandate(
        mandate_path,
        mandate_payload if mandate_payload is not None else {
            "limits": [
                {"metric": "sector_exposure", "target": "semiconductors", "operator": "<=", "threshold": 0.20},
                {"metric": "single_position_weight", "operator": "<=", "threshold": 0.25},
            ],
            "prohibited_assets": [],
        },
    )


def test_cli_evaluate_mandate_reports(capsys, data_root):
    mandate_path = _seed_snapshot_and_mandate(data_root)
    _cmd_evaluate_mandate(mandate_path, data_root)
    out = capsys.readouterr().out
    assert f"Mandate: {mandate_path}" in out
    assert "Snapshot: portfolio:robinhood:2026-08-25T12:00:00+00:00 created 2026-08-25T12:00:00+00:00" in out
    assert "Sector exposures: semiconductors 75.0%, unknown_sector 25.0%" in out
    assert "[warning] sector_exposure semiconductors: actual 75.0%, limit 20.0%, excess 55.0%" in out
    assert "[warning] single_position_weight: actual 75.0%, limit 25.0%, excess 50.0%" in out
    assert "No breaches." not in out


def test_cli_evaluate_mandate_no_breaches(capsys, data_root):
    mandate_path = _seed_snapshot_and_mandate(
        data_root,
        {"limits": [{"metric": "single_position_weight", "operator": "<=", "threshold": 0.99}], "prohibited_assets": []},
    )
    _cmd_evaluate_mandate(mandate_path, data_root)
    out = capsys.readouterr().out
    assert "No breaches." in out


def test_cli_evaluate_mandate_missing_mandate_exits_1(capsys, data_root):
    persist_snapshot(_hand_built_snapshot(), data_root=data_root)
    with pytest.raises(SystemExit) as excinfo:
        _cmd_evaluate_mandate(data_root / "mandate.json", data_root)
    assert excinfo.value.code == 1
    assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Agent tool
# ---------------------------------------------------------------------------


def test_tool_evaluate_mandate_missing_mandate_error(data_root):
    result = tools.evaluate_mandate(data_root=data_root, mandate_path=data_root / "mandate.json")
    assert "error" in result
    assert "mandate" in result["error"].lower() or "no such file" in result["error"].lower()


def test_tool_evaluate_mandate_missing_snapshot_error(data_root):
    _write_mandate(data_root / "mandate.json", {"limits": [], "prohibited_assets": []})
    result = tools.evaluate_mandate(data_root=data_root, mandate_path=data_root / "mandate.json")
    assert "error" in result
    assert "snapshot" in result["error"].lower()


def test_tool_evaluate_mandate_happy_path(data_root):
    mandate_path = _seed_snapshot_and_mandate(data_root)
    result = tools.evaluate_mandate(data_root=data_root, mandate_path=mandate_path)
    assert result["result_type"] == "mandate_evaluation"
    assert result["snapshot_id"] == "portfolio:robinhood:2026-08-25T12:00:00+00:00"
    assert result["sector_exposures"] == {"semiconductors": "0.75", "unknown_sector": "0.25"}
    assert len(result["breaches"]) == 2
    breach = result["breaches"][0]
    assert breach["metric"] == "sector_exposure"
    assert breach["actual"] == "0.75"
    assert breach["excess"] == "0.55"
    assert result["not_evaluable"] == []
    assert result["source"] == "mandate"
