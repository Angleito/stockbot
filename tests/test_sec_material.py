"""Offline tests for RegulatoryEvent + material 8-K events (no network)."""

from types import SimpleNamespace

import pytest

from app.sec import events8k, material
from app.sec import filings as filings_mod
from app.sec.material import EIGHT_K_ITEM_EVENTS
from app.sec.models import EVENT_TYPES, CurrentReportEvent, RegulatoryEvent


def test_event_vocabulary_covers_mapped_items():
    assert len(EVENT_TYPES) > 0
    assert set(EIGHT_K_ITEM_EVENTS.values()) <= set(EVENT_TYPES)


def test_regulatory_event_rejects_bad_inputs():
    with pytest.raises(ValueError):
        RegulatoryEvent("e", "Acme", "nope", None, "k", ("a",))
    with pytest.raises(ValueError):
        RegulatoryEvent("e", "Acme", "earnings", None, "k", ())


def test_material_events_from_8k_mapping_and_drops():
    acc = "ACC"
    evts = events8k.parse_8k_events(acc, {
        "Item 1.03": "bankruptcy text",
        "9.01": "exhibits only",
        "7.01": "reg FD chatter",
    }, event_date="2024-01-15")
    out = material.material_events_from_8k(acc, evts, issuer="Acme")
    assert [(e.event_id, e.event_type, e.severity) for e in out] == [
        ("ACC:1.03", "bankruptcy", "critical"),
    ]
    assert out[0].source_accessions == ("ACC",)
    assert out[0].structured_data["item_number"] == "1.03"
    assert out[0].to_dict()["source_accessions"] == ["ACC"]


def test_material_events_missing_dates_never_crash():
    evt = CurrentReportEvent("ACC", "2.02", "Results", None, "t", ())
    (one,) = material.material_events_from_8k("ACC", [evt], issuer="Acme")
    assert one.effective_date is None and one.known_at == "unknown"
    assert (one.event_type, one.severity) == ("earnings", "notable")


class _Report:
    def __init__(self, texts):
        self._texts = texts

    def items(self):
        return list(self._texts)

    def __getitem__(self, key):
        return self._texts[key]


def test_get_material_events_sorted_and_skips_bad_load(monkeypatch):
    seen = {}
    fakes = [
        SimpleNamespace(accession_no="ACC1", filer_name="Acme"),
        SimpleNamespace(accession_no="ACC2", filer_name="Acme"),
        SimpleNamespace(accession_no="BAD", filer_name="Acme"),
    ]

    def fake_list(ticker_or_cik, **kwargs):
        seen.update(kwargs)
        seen["ticker_or_cik"] = ticker_or_cik
        return fakes

    def fake_load(accession_no):
        if accession_no == "ACC1":
            return _Report({"Item 2.02": "beat"}), "2024-02-01", "2024-02-02"
        if accession_no == "ACC2":
            return _Report({"Item 1.03": "bust"}), "2024-01-15", "2024-01-16"
        raise ValueError("no report")

    monkeypatch.setattr(filings_mod, "list_sec_filings", fake_list)
    monkeypatch.setattr(material, "load_report", fake_load)
    out = material.get_material_events("ACME", "2024-01-01")
    assert [(e.event_id, e.severity) for e in out] == [
        ("ACC2:1.03", "critical"),
        ("ACC1:2.02", "notable"),
    ]
    assert seen["forms"] == ["8-K", "8-K/A"] and seen["start_date"] == "2024-01-01"


def test_get_material_events_rejects_bad_since(monkeypatch):
    with pytest.raises(ValueError):
        material.get_material_events("ACME", "2024/01/01")
    with pytest.raises(ValueError):
        material.get_material_events("ACME", "2024-13-40")
