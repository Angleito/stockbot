"""Point-in-time ticker resolution tests for the portfolio sync service.

The resolution rules under test:

- a ticker alias resolves to the SEC entity and security IDs;
- lookups are case-insensitive (UPPER);
- an alias whose ``known_at`` is after ``as_of`` is invisible (no
  retroactive contamination);
- two active aliases pointing at different entities resolve as ambiguous;
- unknown tickers resolve to ``resolved=False`` and never crash;
- ``provider_instrument_id`` is accepted but does not change the result.
"""

from datetime import date, datetime, timezone

import pytest

from app.services.portfolio_sync import resolve_security
from app.storage import parquet

AMD_ENTITY = "sec:cik:0000320193"
AMD_SECURITY = "sec:equity:0000320193"


@pytest.fixture
def data_root(tmp_path):
    return tmp_path / "data"


def _write(rows, name, data_root):
    parquet.write_rows(name, rows, root=data_root / "parquet")


def _seed_entity(data_root, entity_id=AMD_ENTITY, name="Advanced Micro Devices Inc"):
    _write([{
        "entity_id": entity_id,
        "name": name,
        "entity_type": "company",
        "sic": "3674",
        "source": "sec",
        "known_at": "2026-01-01T00:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "content_hash": f"entity-{entity_id}",
        "parser_version": "test",
    }], "entities", data_root)


def _seed_alias(data_root, entity_id=AMD_ENTITY, security_id=AMD_SECURITY, alias="AMD",
                known_at="2026-01-01T00:00:00Z", source="sec"):
    _write([{
        "alias_type": "ticker",
        "alias_value": alias,
        "entity_id": entity_id,
        "security_id": security_id,
        "source": source,
        "valid_from": "2026-01-01",
        "valid_to": None,
        "known_at": known_at,
        "retrieved_at": known_at,
        "content_hash": f"alias-{entity_id}-{known_at}",
        "parser_version": "test",
    }], "entity_aliases", data_root)


def _seed_security(data_root, security_id=AMD_SECURITY, entity_id=AMD_ENTITY, ticker="AMD"):
    _write([{
        "security_id": security_id,
        "entity_id": entity_id,
        "security_type": "equity-common",
        "ticker": ticker,
        "exchange": "NASDAQ",
        "source": "sec",
        "known_at": "2026-01-01T00:00:00Z",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "content_hash": f"security-{security_id}",
        "parser_version": "test",
    }], "securities", data_root)


def _seed_amd(data_root, **kwargs):
    _seed_entity(data_root)
    _seed_alias(data_root, **kwargs)
    _seed_security(data_root)


def test_ticker_resolves_to_entity_and_security_ids(data_root):
    _seed_amd(data_root)
    resolution = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is True
    assert resolution.resolution_method == "entity_alias"
    assert resolution.ticker == "AMD"
    assert resolution.entity_id == AMD_ENTITY
    assert resolution.security_id == AMD_SECURITY


def test_lookup_is_case_insensitive(data_root):
    _seed_amd(data_root)
    resolution = resolve_security("amd", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is True
    assert resolution.entity_id == AMD_ENTITY
    assert resolution.security_id == AMD_SECURITY


def test_unknown_ticker_is_unresolved_without_crashing(data_root):
    _seed_amd(data_root)
    resolution = resolve_security("NOTREAL", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is False
    assert resolution.resolution_method == "unresolved"
    assert resolution.entity_id is None
    assert resolution.security_id is None
    assert resolution.ticker == "NOTREAL"


def test_alias_known_after_as_of_is_invisible(data_root):
    _seed_amd(data_root, known_at="2026-09-01T12:00:00Z")
    early = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert early.resolved is False
    assert early.resolution_method == "unresolved"
    on_day = resolve_security("AMD", as_of=datetime(2026, 9, 1, 13, 0, tzinfo=timezone.utc), data_root=data_root)
    assert on_day.resolved is True
    assert on_day.entity_id == AMD_ENTITY


def test_ambiguous_when_multiple_entities_knowable(data_root):
    _seed_entity(data_root)
    _seed_entity(data_root, entity_id="sec:cik:0000320194", name="AMD Relabeled")
    _seed_security(data_root, security_id="sec:equity:0000320194", entity_id="sec:cik:0000320194", ticker="AMD")
    _seed_alias(data_root, known_at="2026-01-01T12:00:00Z")
    _seed_alias(
        data_root,
        entity_id="sec:cik:0000320194",
        security_id="sec:equity:0000320194",
        known_at="2026-06-01T12:00:00Z",
    )
    before_relabel = resolve_security("AMD", as_of=datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc), data_root=data_root)
    assert before_relabel.resolved is True
    assert before_relabel.entity_id == AMD_ENTITY
    assert before_relabel.security_id == AMD_SECURITY
    after_relabel = resolve_security("AMD", as_of=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc), data_root=data_root)
    assert after_relabel.resolved is False
    assert after_relabel.resolution_method == "ambiguous"
    assert after_relabel.entity_id is None
    assert after_relabel.security_id is None


def test_provider_instrument_id_does_not_change_resolution(data_root):
    _seed_amd(data_root)
    plain = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    with_instrument = resolve_security(
        "AMD",
        provider_instrument_id="instr-amd",
        as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        data_root=data_root,
    )
    assert with_instrument == plain


def test_default_as_of_is_today_and_resolves(data_root):
    _seed_amd(data_root, known_at="2026-01-01T00:00:00Z")
    resolution = resolve_security("AMD", data_root=data_root)
    assert resolution.resolved is True
    assert resolution.entity_id == AMD_ENTITY
    assert datetime.now(timezone.utc).date() >= date(2026, 1, 1)

def test_same_instant_z_and_offset_visible(data_root):
    _seed_amd(data_root, known_at="2026-08-25T12:00:00Z")
    resolution = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is True
    assert resolution.entity_id == AMD_ENTITY


def test_record_one_microsecond_after_as_of_invisible(data_root):
    _seed_amd(data_root, known_at="2026-08-25T12:00:00.000001Z")
    resolution = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is False
    assert resolution.resolution_method == "unresolved"


def test_non_utc_offset_compares_chronologically(data_root):
    _seed_amd(data_root, known_at="2026-08-25T13:00:00+01:00")
    resolution = resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc), data_root=data_root)
    assert resolution.resolved is True
    assert resolution.entity_id == AMD_ENTITY


def test_naive_as_of_rejected(data_root):
    _seed_amd(data_root)
    with pytest.raises(ValueError):
        resolve_security("AMD", as_of=datetime(2026, 8, 25, 12, 0), data_root=data_root)


def test_date_as_of_rejected(data_root):
    _seed_amd(data_root)
    with pytest.raises(TypeError):
        resolve_security("AMD", as_of=date(2026, 8, 25), data_root=data_root)


def test_newest_alias_wins_chronologically_not_lexically(data_root):
    _seed_entity(data_root)
    _seed_alias(data_root, known_at="2026-08-25T13:00:00+01:00", security_id=None)
    # Distinct source so both rows persist: entity_aliases dedups on
    # (alias_type, alias_value, entity_id, source, valid_from).
    _seed_alias(data_root, known_at="2026-08-25T12:30:00Z", security_id=AMD_SECURITY, source="control")
    _seed_security(data_root)
    resolution = resolve_security(
        "AMD", as_of=datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc), data_root=data_root
    )
    # 13:00+01:00 (= 12:00Z) sorts first lexically but is chronologically
    # older than 12:30Z — the 12:30Z alias must win.
    assert resolution.resolved is True
    assert resolution.security_id == AMD_SECURITY


def test_same_instant_conflicting_securities_are_ambiguous(data_root):
    _seed_entity(data_root)
    _seed_alias(data_root, known_at="2026-08-25T12:00:00Z", security_id=AMD_SECURITY)
    _seed_alias(data_root, known_at="2026-08-25T12:00:00Z", security_id="sec:equity:0000999999", source="control")
    _seed_security(data_root)
    resolution = resolve_security(
        "AMD", as_of=datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc), data_root=data_root
    )
    assert resolution.resolved is False
    assert resolution.resolution_method == "ambiguous"
    assert resolution.entity_id == AMD_ENTITY
    assert resolution.security_id is None


def test_same_instant_null_and_explicit_security_not_conflict(data_root):
    _seed_entity(data_root)
    _seed_alias(data_root, known_at="2026-08-25T12:00:00Z", security_id=None)
    _seed_alias(data_root, known_at="2026-08-25T12:00:00Z", security_id=AMD_SECURITY, source="control")
    _seed_security(data_root)
    resolution = resolve_security(
        "AMD", as_of=datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc), data_root=data_root
    )
    assert resolution.resolved is True
    assert resolution.security_id == AMD_SECURITY
