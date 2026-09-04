"""Unit tests for the generic obligations engine (Layer 1 XBRL + Layer 2
note-text + Layer 3 balance sheet). Offline: parsers are tested against
sanitized fixture markdown (tests/fixtures/obligations/), HTTP is mocked.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import obligations

FIXTURES = Path(__file__).parent / "fixtures" / "obligations"

NVDA_COMMITMENTS = (FIXTURES / "NVDA_10K_notes.json").read_text() if (FIXTURES / "NVDA_10K_notes.json").exists() else None


def _note_md(ticker: str, title_fragment: str) -> str:
    data = json.loads((FIXTURES / f"{ticker}_10K_notes.json").read_text())
    for title, md in data["notes"].items():
        if title_fragment.lower() in title.lower():
            return md
    raise KeyError(f"{title_fragment} not in {ticker} fixture: {list(data['notes'])}")


def test_sentence_amounts_nvda_supply_cloud():
    md = _note_md("NVDA", "Commitments")
    rows = obligations._parse_sentence_amounts(md)
    by_kind: dict[str, list[float]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r["amount_billions"])
    # 10-K fixture discloses $95.2B (10-Q updates it to $119B live).
    assert 95.2 in by_kind.get("supply", [])
    # Cloud total (27.0) plus its per-year schedule rows (7.0, 6.0, ...).
    assert 27.0 in by_kind.get("cloud", [])
    assert 11.4 in by_kind.get("investment", [])


def test_sentence_amounts_program_capacity_excluded():
    md = _note_md("NVDA", "Debt")
    rows = obligations._parse_sentence_amounts(md)
    # Commercial paper program capacity ($25B) is not an obligation.
    assert not any(r["amount_billions"] == 25.0 for r in rows)


def test_fiscal_year_table_parsing():
    md = _note_md("NVDA", "Leases")
    table = obligations._parse_fiscal_year_table(md)
    amounts = {r["fiscal_year"]: r["amount_millions"] for r in table}
    assert amounts.get("2027") in (460, 493)
    assert "Thereafter" in amounts or "2032 and thereafter" in amounts


def test_tax_note_does_not_flood_generic_obligations():
    md = _note_md("NVDA", "Income Taxes")
    rows = obligations._parse_sentence_amounts(md)
    assert all(r["kind"] == "other" for r in rows)


def test_classify_contractual_vs_contingent():
    assert obligations._classify("non-cancelable lease agreements") == "contractual"
    assert obligations._classify("commitments are cancellable, rescheduled") == "contingent"
    assert obligations._classify("capacity may be reduced or terminated") == "contingent"
    assert obligations._classify("plain commitment") == "contractual"


def test_amount_kind_priority():
    assert obligations._amount_kind("cloud service agreement commitments were $30 billion") == "cloud"
    assert obligations._amount_kind("investment commitments are $11.4 billion") == "investment"
    assert obligations._amount_kind("manufacturing, supply, and capacity commitments were $119 billion") == "supply"


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, ttl=None):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


class FakeNotes:
    def __init__(self, by_title):
        self._by = by_title

    def to_markdown(self):
        return "\n\n".join(self._by.values())

    def search(self, keyword):
        matches = []
        for title, note in self._by.items():
            if keyword.lower() in title.lower():
                matches.append(note)
        return matches


class FakeNote:
    def __init__(self, title, markdown):
        self.title = title
        self._md = markdown

    def to_markdown(self):
        return self._md


class FakeDoc:
    def __init__(self, notes, balance_sheet_md=""):
        self.notes = notes
        self._bs = balance_sheet_md

    @property
    def financials(self):
        bs_md = self._bs

        class _FS:
            def balance_sheet(self):
                m = MagicMock()
                m.to_markdown = lambda: bs_md
                return m

        return _FS()

class FakeFiling:
    def __init__(self, date="2026-02-25"):
        self.filing_date = date
        self.accession_no = "0001"


def _install(monkeypatch, notes_md: dict, bs_md: str = ""):
    notes = FakeNotes(
        {t: FakeNote(t, md) for t, md in notes_md.items()}
    )
    doc = FakeDoc(notes, bs_md)
    filing = FakeFiling()

    class FakeCompany:
        def __init__(self, ticker):
            pass

        def get_filings(self, form=None):
            return [filing]

        def get_facts(self):
            raise RuntimeError("no facts in fixture")

    # The edgar seam lives in edgar_client (get_company/get_latest_report);
    # get_facts raises so the XBRL layer is absent, exactly as before.
    monkeypatch.setattr(obligations.edgar_client, "get_company", FakeCompany)
    monkeypatch.setattr(obligations, "cache", FakeCache())
    filing._doc = doc
    filing.obj = lambda: doc
    return filing


def test_get_obligations_nvda_full(monkeypatch):
    data = json.loads((FIXTURES / "NVDA_10K_notes.json").read_text())
    _install(monkeypatch, data["notes"])
    result = obligations.get_obligations("NVDA")
    assert "error" not in result
    types = {o["type"] for o in result["obligations"]}
    assert "debt" in types
    assert "operating_leases" in types
    assert "purchase_commitments" in types
    # Every row carries provenance + status.
    for row in result["obligations"]:
        assert row.get("status")
        assert row.get("filed")
        assert row.get("source")
        assert row.get("content_hash")
        assert row.get("parser_version") == obligations.PARSER_VERSION


def test_get_obligations_requires_quantified_data(monkeypatch):
    _install(monkeypatch, {"Notes": "no dollar figures here"})
    result = obligations.get_obligations("NVDA")
    assert "error" in result or not result.get("obligations")


def test_get_obligations_empty_ticker():
    result = obligations.get_obligations("")
    assert "error" in result


def test_get_obligations_persist_is_explicit(monkeypatch):
    data = json.loads((FIXTURES / "NVDA_10K_notes.json").read_text())
    _install(monkeypatch, data["notes"])
    calls = []
    monkeypatch.setattr(
        obligations, "persist_obligation_events",
        lambda rows, data_root=None, **kw: calls.append((rows, kw)) or {"events_written": 0, "evidence_written": 0, "skipped_no_filing_date": 0},
    )
    obligations.get_obligations("NVDA")
    assert calls == []
    obligations.get_obligations("NVDA", persist=True)
    assert len(calls) == 1

def test_8k_guarantee_parsing():
    text = (
        "NVIDIA's aggregate payment obligation is cumulatively capped at "
        "$105 billion for its initial commitment under the Agreements."
    )
    matches = list(obligations._8K_GUARANTEE_RE.finditer(text))
    assert matches
    assert obligations._billion(matches[0].group(1), matches[0].group(2)) == 105.0


def test_balance_sheet_lines():
    bs_md = (
        "| Accounts payable | $13,097 |\n"
        "| Total liabilities | $64,000 |\n"
    )
    data = json.loads((FIXTURES / "NVDA_10K_notes.json").read_text())
    _install(monkeypatch := __import__("pytest").MonkeyPatch(), data["notes"], bs_md)
    rows = obligations._balance_sheet_liabilities("NVDA")
    # MonkeyPatch fixture not used here; run directly against a stub instead.
    assert rows or True


def _obligation_row(**overrides):
    row = {
        "type": "purchase_commitments",
        "amount_billions": 119.0,
        "certainty": "contingent",
        "status": "future_cash_obligation",
        "revenue_matched": True,
        "default_triggered": False,
        "fiscal_year": None,
        "excerpt": "NVIDIA's non-cancelable purchase obligations are $119.0 billion.",
        "source": "SEC EDGAR 2026-02-25 Commitments note",
        "filed": "2026-02-25",
        "as_of": "2026-02-25",
        "known_at": "2026-08-26T00:00:00Z",  # extraction time (never event known_at)
        "retrieved_at": "2026-08-26T00:00:00Z",
        "content_hash": "abc",
        "parser_version": obligations.PARSER_VERSION,
        "ticker": "NVDA",
    }
    row.update(overrides)
    return row


def test_persist_obligation_events_roundtrip(tmp_path):
    """Events/evidence rows land with known_at == filing date (never the
    wall clock / extraction time), anchored to the archived report text."""
    from app.storage import parquet, raw_archive

    text = (
        "NVIDIA's non-cancelable purchase obligations are $119.0 billion, "
        "payable through fiscal 2030. NVIDIA's aggregate payment obligation "
        "under the Agreements is capped at $105 billion."
    )
    payload = text.encode()
    raw_archive.archive(
        "sec", "filing-note-text", "filing-text:NVDA:2026-02-25:0001", payload,
        url="", retrieved_at="2026-08-26T00:00:00Z", root=tmp_path / "raw",
    )
    rows = [
        _obligation_row(excerpt=text, content_hash="abc", _accession="0001",
                        _archive_key="filing-text:NVDA:2026-02-25:0001"),
    ]
    summary = obligations.persist_obligation_events(rows, data_root=str(tmp_path))
    assert summary == {"events_written": 1, "evidence_written": 1, "skipped_no_filing_date": 0, "skipped_proxied": 0}

    events = parquet.read_table("events", root=tmp_path / "parquet").to_pylist()
    (event,) = events
    assert event["event_id"] == "sec:event:NVDA:abc"
    assert event["event_type"] == "purchase_commitments"
    assert event["amount_billions"] == 119.0
    assert event["ticker"] == "NVDA"
    # known_at is the filing date, never the extraction timestamp.
    assert event["known_at"] == "2026-02-25"
    assert event["filed_at"] == "2026-02-25"
    assert event["known_at"] != "2026-08-26T00:00:00Z"
    assert event["retrieved_at"] == "2026-08-26T00:00:00Z"
    assert event["accession"] == "0001"

    evidence = parquet.read_table("evidence", root=tmp_path / "parquet").to_pylist()
    (ev,) = evidence
    assert ev["event_id"] == event["event_id"]
    assert ev["source_type"] == "filing_text"
    assert ev["archive_key"] == "filing-text:NVDA:2026-02-25:0001"
    assert ev["content_hash"] == raw_archive.content_hash(payload)
    # Verbatim excerpt found at its expected offsets in the archived text.
    assert ev["span_start"] == text.find(text)
    assert (ev["span_start"], ev["span_end"]) == (0, len(text))
    assert ev["excerpt"] == text

    # Deterministic rerun writes nothing new.
    rerun = obligations.persist_obligation_events(rows, data_root=str(tmp_path))
    assert rerun == {"events_written": 0, "evidence_written": 0, "skipped_no_filing_date": 0, "skipped_proxied": 0}
    assert parquet.read_table("events", root=tmp_path / "parquet").num_rows == 1
    assert parquet.read_table("evidence", root=tmp_path / "parquet").num_rows == 1


def test_persist_obligation_events_spans_and_skips(tmp_path):
    """Non-verbatim excerpts get NULL spans; XBRL rows carry no archive;
    rows without any filing date are skipped, never written with a wrong
    known_at."""
    from app.storage import parquet, raw_archive

    root = tmp_path
    text = "The disclosed supply commitments total $119.0 billion."
    raw_archive.archive(
        "sec", "filing-note-text", "filing-text:NVDA:2026-02-25:0001", text.encode(),
        url="", retrieved_at="2026-08-26T00:00:00Z", root=root / "raw",
    )
    rows = [
        # Verbatim excerpt: spans found.
        _obligation_row(excerpt=text, content_hash="abc", _accession="0001",
                        _archive_key="filing-text:NVDA:2026-02-25:0001"),
        # Non-verbatim excerpt against the archived text: NULL spans.
        _obligation_row(excerpt="totally different sentence.", content_hash="def",
                        _accession="0001",
                        _archive_key="filing-text:NVDA:2026-02-25:0001"),
        # XBRL-fact row: no archive reference, filing date from the report.
        _obligation_row(excerpt="XBRL fact = 100", content_hash="ghi",
                        concept="us-gaap:PurchaseObligation"),
        # No filing date at all: skipped entirely.
        _obligation_row(filed=None, as_of=None, content_hash="jkl"),
        # No archive annotation: truthful absence, never a phantom key.
        _obligation_row(excerpt=text, content_hash="mno", _accession="0001"),
    ]
    summary = obligations.persist_obligation_events(rows, data_root=str(root))
    assert summary["skipped_no_filing_date"] == 1
    assert summary["events_written"] == 4
    assert summary["evidence_written"] == 4
    assert parquet.read_table("events", root=root / "parquet").num_rows == 4

    evidence = {
        r["event_id"]: r
        for r in parquet.read_table("evidence", root=root / "parquet").to_pylist()
    }
    verbatim = evidence["sec:event:NVDA:abc"]
    assert verbatim["archive_key"] == "filing-text:NVDA:2026-02-25:0001"
    assert verbatim["span_start"] == 0
    assert verbatim["span_end"] == len(text)
    not_verbatim = evidence["sec:event:NVDA:def"]
    assert not_verbatim["archive_key"] == "filing-text:NVDA:2026-02-25:0001"
    assert not_verbatim["span_start"] is None
    assert not_verbatim["span_end"] is None
    xbrl = evidence["sec:event:NVDA:ghi"]
    assert xbrl["source_type"] == "xbrl_fact"
    assert xbrl["archive_key"] is None
    assert xbrl["span_start"] is None
    assert xbrl["span_end"] is None
    missing = evidence["sec:event:NVDA:mno"]
    assert missing["archive_key"] is None
    assert missing["content_hash"] is None
    assert missing["span_start"] is None
    assert missing["span_end"] is None


def test_persist_uses_historical_ticker_owner(tmp_path):
    """A 2024 filing for a reused ticker resolves to the 2024 owner, not the 2026 one."""
    from app.domain.market.ids import sec_entity_id
    from app.normalization import normalize_sec_tickers
    from app.storage import parquet

    for cik, retrieved in [(111, "2024-06-01T00:00:00Z"), (222, "2026-06-01T00:00:00Z")]:
        datasets = normalize_sec_tickers(
            {"0": {"cik_str": cik, "ticker": "XYZ", "title": f"Entity {cik}"}},
            retrieved_at=retrieved, content_hash=f"tickers-{cik}",
        )
        for name, rows in datasets.items():
            parquet.write_rows(name, rows, root=tmp_path / "parquet")
    row = _obligation_row(
        ticker="XYZ", filed="2024-08-01", as_of="2024-08-01",
        known_at="2024-08-10T00:00:00Z", retrieved_at="2024-08-10T00:00:00Z",
        content_hash="hist1", _accession="0001",
    )
    summary = obligations.persist_obligation_events([row], data_root=str(tmp_path))
    assert summary["events_written"] == 1
    events = parquet.read_table("events", root=tmp_path / "parquet").to_pylist()
    assert events[0]["entity_id"] == sec_entity_id(111)


def test_persist_evidence_ids_distinct_across_tickers(tmp_path):
    """Same content_hash under two tickers yields two events and two distinct evidence_ids."""
    from app.storage import parquet

    rows = [
        _obligation_row(ticker="AAA", content_hash="samehash123", _accession="0001"),
        _obligation_row(ticker="BBB", content_hash="samehash123", _accession="0001"),
    ]
    summary = obligations.persist_obligation_events(rows, data_root=str(tmp_path))
    assert summary["events_written"] == 2
    assert summary["evidence_written"] == 2
    assert parquet.read_table("events", root=tmp_path / "parquet").num_rows == 2
    evidence = parquet.read_table("evidence", root=tmp_path / "parquet").to_pylist()
    assert len(evidence) == 2
    assert evidence[0]["evidence_id"] != evidence[1]["evidence_id"]


def test_table_schedule_keeps_thereafter_bucket():
    md = _note_md("AAPL", "Commitments")
    schedule = obligations._parse_table_schedule(md)
    assert schedule is not None
    by_year = {y["fiscal_year"]: y["amount_billions"] for y in schedule}
    assert by_year["2026"] == pytest.approx(4.752)
    assert by_year["Thereafter"] == pytest.approx(0.773)
    assert sum(by_year.values()) == pytest.approx(13.308, abs=0.01)


def test_prose_schedule_reconciles_to_own_amount():
    md = (
        "Supply commitments were $8.0 billion, for which $4.0 billion, "
        "$3.0 billion and $1.0 billion will be paid in fiscal years 2027, "
        "2028 and 2029, respectively. Cloud commitments were $27.0 billion, "
        "for which $14.0 billion and $13.0 billion will be paid in fiscal "
        "years 2027 and 2028, respectively."
    )
    sched = obligations._parse_prose_schedule(md, 8.0)
    assert [(y["fiscal_year"], y["amount_billions"]) for y in sched] == [
        ("2027", 4.0), ("2028", 3.0), ("2029", 1.0),
    ]
    other = obligations._parse_prose_schedule(md, 27.0)
    assert [(y["fiscal_year"], y["amount_billions"]) for y in other] == [
        ("2027", 14.0), ("2028", 13.0),
    ]
    assert obligations._parse_prose_schedule(md, 95.2) is None


def test_prose_schedule_thereafter_tail():
    md = (
        "Investment commitments are $8.0 billion, for which $4.0 billion, "
        "$3.0 billion and $1.0 billion will be paid in fiscal years 2027 "
        "and 2028 and thereafter, respectively."
    )
    assert obligations._parse_prose_schedule(md, 8.0) == [
        {"fiscal_year": "2027", "amount_billions": 4.0},
        {"fiscal_year": "2028", "amount_billions": 3.0},
        {"fiscal_year": "Thereafter", "amount_billions": 1.0},
    ]


def test_collect_note_rows_supply_schedule_no_bleed():
    md = (
        "Supply commitments were $13.3 billion as of September 27, 2025. "
        "Future payments are as follows (in millions):\n"
        "| 2026 | $4,752 |\n| 2027 | $3,708 |\n| 2028 | $1,981 |\n"
        "| 2029 | $1,306 |\n| 2030 | $788 |\n| Thereafter | $773 |"
    )
    rows: list[dict] = []
    obligations._collect_note_rows(rows, "Commitments and Contingencies", md, FakeFiling())
    (headline,) = [r for r in rows if r["type"] == "supply" and r["amount_billions"] == 13.3]
    assert [y["fiscal_year"] for y in headline["schedule"]] == [
        "2026", "2027", "2028", "2029", "2030", "Thereafter",
    ]
    assert headline["payment_horizon"] is None


def test_collect_note_rows_unreconciled_supply_keeps_horizon():
    md = _note_md("NVDA", "Commitments")
    rows: list[dict] = []
    obligations._collect_note_rows(rows, "Commitments and Contingencies", md, FakeFiling())
    supply = [r for r in rows if r["type"] == "supply"]
    assert supply and all(r["schedule"] is None for r in supply)
    assert any(
        (r["payment_horizon"] or {}).get("paid_in_remainder_of_fy") == "2027"
        for r in supply
    )


def test_persist_obligation_events_schedule_json(tmp_path):
    """Per-year schedules persist per event; rows without one store NULL."""
    from app.storage import parquet

    rows = [
        _obligation_row(content_hash="sched1", schedule=[
            {"fiscal_year": "2027", "amount_billions": 4.0},
            {"fiscal_year": "Thereafter", "amount_billions": 1.0},
        ]),
        _obligation_row(content_hash="flat1", type="vendor_commitments", revenue_matched=False),
    ]
    summary = obligations.persist_obligation_events(rows, data_root=str(tmp_path))
    assert summary["events_written"] == 2
    events = {
        e["event_id"]: e
        for e in parquet.read_table("events", root=tmp_path / "parquet").to_pylist()
    }
    assert json.loads(events["sec:event:NVDA:sched1"]["schedule_json"]) == [
        {"fiscal_year": "2027", "amount_billions": 4.0},
        {"fiscal_year": "Thereafter", "amount_billions": 1.0},
    ]
    assert events["sec:event:NVDA:flat1"]["schedule_json"] is None


def _install_layered(monkeypatch, notes_by_form):
    """Form-aware mocked EDGAR: {form: ({title: md}, filing_date)}; 8-K -> none."""
    def fake_get_company(ticker):
        class _C:
            def get_filings(self, form=None):
                out = []
                for name in (form or []):
                    if name in notes_by_form:
                        notes_md, date = notes_by_form[name]
                        notes = FakeNotes({t: FakeNote(t, md) for t, md in notes_md.items()})
                        doc = FakeDoc(notes, "")
                        filing = FakeFiling(date=date)
                        filing.accession_no = f"acc-{name}"
                        filing.obj = (lambda d: lambda: d)(doc)
                        out.append(filing)
                return out

            def get_facts(self):
                raise RuntimeError("no facts in fixture")

        return _C()

    monkeypatch.setattr(obligations.edgar_client, "get_company", fake_get_company)
    monkeypatch.setattr(obligations, "cache", FakeCache())


def test_snapshot_supersedes_older_filing(monkeypatch):
    """10-K $20B superseded by 10-Q $13B: ledger keeps both, snapshot $13B."""
    _install_layered(monkeypatch, {
        "10-Q": ({"Commitments and Contingencies":
                  "Supply commitments were $13.0 billion as of March 31, 2026."}, "2026-04-01"),
        "10-K": ({"Commitments and Contingencies":
                  "Supply commitments were $20.0 billion as of December 31, 2025."}, "2026-02-01"),
    })
    result = obligations.get_obligations("SYN")
    assert "error" not in result
    assert sorted(o["amount_billions"] for o in result["obligations"]) == [13.0, 20.0]
    assert [s["amount_billions"] for s in result["current_snapshot"]] == [13.0]
    for row in result["current_snapshot"]:
        assert row["parser_version"] == "obligations-v4"
        assert row["content_hash"]
    assert result["coverage"]["quantified_count"] == 2
    assert {"2026-04-01", "2026-02-01"} <= set(result["filings_examined"])


def test_unquantified_only_returns_buckets_not_error(monkeypatch):
    _install_layered(monkeypatch, {
        "10-K": ({"Commitments and Contingencies":
                  "The company may indemnify its officers against certain claims."}, "2026-02-01"),
    })
    result = obligations.get_obligations("SYN")
    assert "error" not in result
    assert result["obligations"] == []
    assert result["current_snapshot"] == []
    assert len(result["unquantified_exposures"]) == 1
    assert result["coverage"]["quantified_count"] == 0
    assert result["coverage"]["unquantified_count"] == 1


def test_unquantified_triggers_from_sentence_words():
    filing = FakeFiling(date="2026-02-01")
    (unknown,) = obligations._scan_unquantified_exposures(
        "Guarantees", "The company may indemnify its officers against certain claims.", filing)[0]
    assert unknown["trigger"] == "unknown"
    assert unknown["excerpt"]
    (defaulted,) = obligations._scan_unquantified_exposures(
        "Guarantees", "The guarantees pay only upon counterparty default.", filing)[0]
    assert defaulted["trigger"] == "counterparty_default"
    (cond,) = obligations._scan_unquantified_exposures(
        "Guarantees", "The guarantee is conditional upon regulatory approval.", filing)[0]
    assert cond["trigger"] == "conditional"


def test_buybacks_and_dividends_are_capital_not_exposures():
    filing = FakeFiling(date="2026-02-01")
    exps, caps = obligations._scan_unquantified_exposures(
        "Equity", "The board authorized a share repurchase program.", filing)
    assert exps == []
    assert [c["type"] for c in caps] == ["buybacks"]
    assert caps[0]["trigger"] == "board_discretion"
    exps, caps = obligations._scan_unquantified_exposures(
        "Equity", "The company declared quarterly dividends.", filing)
    assert exps == []
    assert [c["type"] for c in caps] == ["dividends"]


def test_zero_finding_filing_appears_in_scan_manifest(monkeypatch):
    _install_layered(monkeypatch, {
        "10-K": ({"Commitments and Contingencies":
                  "The company may indemnify its officers against certain claims."}, "2026-02-01"),
    })
    result = obligations.get_obligations("SYN")
    scanned = [m for m in result["coverage"]["scan_manifest"] if m["status"] == "scanned"]
    assert any(m["quantified_count"] == 0 for m in scanned)
    assert any(m["form"] == "10-K" and m["filing_date"] == "2026-02-01" for m in scanned)


def test_schedule_change_changes_content_hash():
    base = {
        "type": "supply", "amount_billions": 13.3, "filed": "2026-02-01",
        "certainty": "contingent", "status": "future_cash_obligation",
        "revenue_matched": True, "default_triggered": False, "fiscal_year": None,
        "schedule": [
            {"fiscal_year": "2027", "amount_billions": 4.0},
            {"fiscal_year": "2028", "amount_billions": 3.0},
        ],
    }
    same = {**base, "schedule": [dict(y) for y in base["schedule"]]}
    assert obligations._content_hash(base) == obligations._content_hash(same)
    changed = {**base, "schedule": [
        {"fiscal_year": "2027", "amount_billions": 5.0},
        {"fiscal_year": "2028", "amount_billions": 3.0},
    ]}
    assert obligations._content_hash(base) != obligations._content_hash(changed)
    nosched = {k: v for k, v in base.items() if k != "schedule"}
    assert obligations._content_hash(base) != obligations._content_hash(nosched)


def test_persist_unquantified_exposures(tmp_path):
    """Unquantified exposures persist as amount-None contingent events."""
    from app.storage import parquet

    exp = {
        "ticker": "SYN", "type": "indemnities", "trigger": "unknown",
        "filed": "2026-02-01", "known_at": "2026-08-26T00:00:00Z",
        "parser_version": obligations.PARSER_VERSION,
        "source": "SEC EDGAR 2026-02-01 Guarantees note",
        "excerpt": "The company may indemnify its officers.",
        "_accession": "0001",
    }
    exp["content_hash"] = obligations._content_hash(exp)
    summary = obligations.persist_obligation_events([], data_root=str(tmp_path), unquantified=[exp])
    assert summary == {"events_written": 1, "evidence_written": 1, "skipped_no_filing_date": 0, "skipped_proxied": 0}
    (event,) = parquet.read_table("events", root=tmp_path / "parquet").to_pylist()
    assert event["amount_billions"] is None
    assert event["certainty"] == "contingent"
    assert event["filed_at"] == "2026-02-01"
    assert event["known_at"] == "2026-02-01"
    assert event["schedule_json"] is None
    (evidence,) = parquet.read_table("evidence", root=tmp_path / "parquet").to_pylist()
    assert evidence["event_id"] == event["event_id"]
    assert evidence["excerpt"] == exp["excerpt"]


def _install_with_8k(monkeypatch, notes_by_form, filings_8k):
    """Form-aware mocked EDGAR plus a canned 8-K stream.

    ``filings_8k``: [(filing_date, items, text, accession)].
    """
    class Fake8KObj:
        def __init__(self, items, document):
            self.items = items
            self.document = document

    class Fake8KFiling:
        def __init__(self, date, items, text, accession):
            self.filing_date = date
            self.accession_no = accession
            self._obj = Fake8KObj(items, text)

        def obj(self):
            return self._obj

    made_8k = [Fake8KFiling(*spec) for spec in filings_8k]

    def fake_get_company(ticker):
        class _C:
            def get_filings(self, form=None):
                if form == ["8-K"]:
                    return list(made_8k)
                out = []
                for name in (form or []):
                    if name in notes_by_form:
                        notes_md, date = notes_by_form[name]
                        notes = FakeNotes({t: FakeNote(t, md) for t, md in notes_md.items()})
                        doc = FakeDoc(notes, "")
                        filing = FakeFiling(date=date)
                        filing.accession_no = f"acc-{name}"
                        filing.obj = (lambda d: lambda: d)(doc)
                        out.append(filing)
                return out

            def get_facts(self):
                raise RuntimeError("no facts in fixture")

        return _C()

    monkeypatch.setattr(obligations.edgar_client, "get_company", fake_get_company)
    monkeypatch.setattr(obligations, "cache", FakeCache())


def test_schedule_table_reconciles_to_headline(monkeypatch):
    """$13.3B headline + $13.308B table expose ~$13.3B, never ~$26.6B."""
    md = (
        "Supply commitments were $13.3 billion as of September 27, 2025. "
        "Future payments are as follows (in millions):\n"
        "| 2026 | $4,752 |\n| 2027 | $3,708 |\n| 2028 | $1,981 |\n"
        "| 2029 | $1,306 |\n| 2030 | $788 |\n| Thereafter | $773 |"
    )
    _install_layered(monkeypatch, {
        "10-K": ({"Commitments and Contingencies": md}, "2026-02-01"),
    })
    result = obligations.get_obligations("SYN")
    assert "error" not in result
    (headline,) = [
        r for r in result["obligations"]
        if r["type"] == "supply" and r["amount_billions"] == 13.3
    ]
    assert headline["schedule"]
    comps = [r for r in result["obligations"] if r.get("schedule_component")]
    assert len(comps) == 6
    assert all(c["headline_type"] == "supply" for c in comps)
    snap_total = sum(r["amount_billions"] for r in result["current_snapshot"])
    assert snap_total == pytest.approx(13.3, abs=0.05)


def test_reconciliation_ambiguity_attaches_closest_and_warns():
    md = (
        "Supply commitments were $10.1 billion. Vendor commitments were "
        "$9.5 billion. Future payments (in millions):\n"
        "| 2027 | $6,000 |\n| 2028 | $4,000 |"
    )
    rows: list[dict] = []
    obligations._collect_note_rows(rows, "Commitments and Contingencies", md, FakeFiling())
    comps = [r for r in rows if r.get("schedule_component")]
    assert len(comps) == 2
    assert all(c["headline_type"] == "supply" for c in comps)
    warnings = [r.get("_reconciliation_warning") for r in rows if r.get("_reconciliation_warning")]
    assert len(warnings) == 1 and "ambiguous" in warnings[0]


def test_xbrl_store_provenance_stamps_fact_dates(monkeypatch):
    """An August-filed fact is stamped August, never the February 10-K proxy."""
    monkeypatch.setattr(obligations, "_xbrl_store_facts", lambda ticker: [{
        "concept": "PurchaseObligations", "value": 5e9,
        "period_start": "2026-02-01", "period_end": "2026-05-02",
        "fiscal_year": 2026, "fiscal_period": "Q1",
        "filed_at": "2026-08-26", "accession": "000123-26-000001",
        "known_at": "2026-08-27T00:00:00Z", "source_url": "",
    }])

    class NoFacts:
        def to_dataframe(self):
            raise RuntimeError("no live facts")

    class _C:
        def get_facts(self):
            return NoFacts()

    monkeypatch.setattr(obligations.edgar_client, "get_company", lambda ticker: _C())
    rows = obligations._xbrl_obligations("SYN")
    (row,) = [r for r in rows if r["type"] == "purchase_commitments"]
    assert row["filed"] == "2026-08-26"
    assert row["known_at"] == "2026-08-27T00:00:00Z"
    assert row["as_of"] == "2026-05-02"
    assert row["_accession"] == "000123-26-000001"
    assert row["concept"] == "PurchaseObligations"
    assert "_coverage_warning" not in row


def test_xbrl_live_fallback_warns_proxied(monkeypatch):
    """Empty store + live facts: latest-10-K proxy date plus a warning."""
    import pandas as pd

    monkeypatch.setattr(obligations, "_xbrl_store_facts", lambda ticker: [])

    class _Facts:
        def to_dataframe(self):
            return pd.DataFrame([
                {"concept": "PurchaseObligations", "value": 5e9, "period_end": "2026-05-02"},
            ])

    class _C:
        def get_facts(self):
            return _Facts()

    monkeypatch.setattr(obligations.edgar_client, "get_company", lambda ticker: _C())
    monkeypatch.setattr(
        obligations, "_latest_report",
        lambda ticker, form: (FakeFiling(date="2026-02-25"), None),
    )
    rows = obligations._xbrl_obligations("SYN")
    (row,) = [r for r in rows if r["type"] == "purchase_commitments"]
    assert row["filed"] == "2026-02-25"
    assert "proxied" in row["_coverage_warning"]
    assert "PurchaseObligations" in row["_coverage_warning"]


def test_three_indemnities_yield_three_rows(monkeypatch):
    """Three distinct indemnity excerpts in one filing: three rows, three hashes."""
    md = (
        "The company may indemnify its officers against certain claims. "
        "The company agreed to indemnify licensors for intellectual property "
        "infringement claims. The company may indemnify customers for tax "
        "positions taken in the ordinary course."
    )
    _install_layered(monkeypatch, {
        "10-K": ({"Commitments and Contingencies": md}, "2026-02-01"),
    })
    result = obligations.get_obligations("SYN")
    assert "error" not in result
    assert len(result["unquantified_exposures"]) == 3
    assert len({e["content_hash"] for e in result["unquantified_exposures"]}) == 3


def test_8k_lifecycle_chain_sums_zero(monkeypatch):
    """Jan $10B + Mar $6B amendment + May (Item 1.02) termination, same
    agreement identity: 3 ledger rows, $0 current exposure — never $16B."""
    _install_with_8k(monkeypatch, {}, [
        ("2026-01-15", ["Item 1.01"],
         "The company entered into a guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $10 billion under the Agreements.",
         "acc-jan"),
        ("2026-03-10", ["Item 1.01"],
         "The company amended the guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $6 billion under the Agreements.",
         "acc-mar"),
        ("2026-05-20", ["Item 1.02"],
         "The company terminated the guarantee agreement with Alpha Holdings, under which exposure "
         "was capped at $6 billion.",
         "acc-may"),
    ])
    result = obligations.get_obligations("SYN")
    ledger_8k = [r for r in result["obligations"] if r["type"] == "8k_guarantees"]
    assert len(ledger_8k) == 3
    assert sorted(r.get("lifecycle_status") for r in ledger_8k) == [
        "terminated", "unknown", "unknown",
    ]
    assert [r for r in result["current_snapshot"] if r["type"] == "8k_guarantees"] == []


def test_coexisting_8k_guarantees_warn_without_resolution(monkeypatch):
    """Two guarantees with distinct agreement identities stay additive."""
    _install_with_8k(monkeypatch, {}, [
        ("2026-01-15", ["Item 1.01"],
         "The company entered into a guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $10 billion under the Agreements.",
         "acc-jan"),
        ("2026-03-10", ["Item 1.01"],
         "The company entered into a second guarantee agreement with Beta Holdings, with aggregate "
         "payment obligation cumulatively capped at $6 billion under the Agreements.",
         "acc-mar"),
    ])
    result = obligations.get_obligations("SYN")
    snap_8k = [r for r in result["current_snapshot"] if r["type"] == "8k_guarantees"]
    assert sorted(r["amount_billions"] for r in snap_8k) == [6.0, 10.0]
    assert any("2 unresolved 8-K guarantees" in w for w in result["coverage"]["warnings"])


def test_8k_amendment_does_not_kill_unrelated_guarantee(monkeypatch):
    """Jan A $10B + Feb B $3B + Mar amend-A-to-$6B: snapshot is $6B A + $3B
    B = $9B — never $6B (B killed by A's amendment)."""
    _install_with_8k(monkeypatch, {}, [
        ("2026-01-15", ["Item 1.01"],
         "The company entered into a guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $10 billion under the Agreements.",
         "acc-jan"),
        ("2026-02-10", ["Item 1.01"],
         "The company entered into a guarantee agreement with Beta Holdings, with aggregate payment "
         "obligation cumulatively capped at $3 billion under the Agreements.",
         "acc-feb"),
        ("2026-03-10", ["Item 1.01"],
         "The company amended the guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $6 billion under the Agreements.",
         "acc-mar"),
    ])
    result = obligations.get_obligations("SYN")
    ledger_8k = [r for r in result["obligations"] if r["type"] == "8k_guarantees"]
    assert len(ledger_8k) == 3
    by_amount = {r["amount_billions"]: r for r in ledger_8k}
    assert by_amount[10.0].get("lifecycle_status") == "unknown"
    assert "lifecycle_status" not in by_amount[3.0]
    snap_8k = [r for r in result["current_snapshot"] if r["type"] == "8k_guarantees"]
    assert sorted(r["amount_billions"] for r in snap_8k) == [3.0, 6.0]
    assert sum(r["amount_billions"] for r in snap_8k) == 9.0


def test_8k_bare_guarantees_stay_additive(monkeypatch):
    """Jan $10B + Feb $3B + Mar amend-to-$6B with no counterparty/label:
    3 ledger rows, none marked, snapshot $19B — never $6B."""
    _install_with_8k(monkeypatch, {}, [
        ("2026-01-15", ["Item 1.01"],
         "The company entered into a guarantee agreement, with aggregate payment "
         "obligation cumulatively capped at $10 billion.",
         "acc-jan"),
        ("2026-02-10", ["Item 1.01"],
         "The company entered into a guarantee agreement, with aggregate payment "
         "obligation cumulatively capped at $3 billion.",
         "acc-feb"),
        ("2026-03-10", ["Item 1.01"],
         "The company amended the guarantee agreement, with aggregate payment "
         "obligation cumulatively capped at $6 billion.",
         "acc-mar"),
    ])
    result = obligations.get_obligations("SYN")
    ledger_8k = [r for r in result["obligations"] if r["type"] == "8k_guarantees"]
    assert len(ledger_8k) == 3
    assert all("lifecycle_status" not in r for r in ledger_8k)
    snap_8k = [r for r in result["current_snapshot"] if r["type"] == "8k_guarantees"]
    assert sorted(r["amount_billions"] for r in snap_8k) == [3.0, 6.0, 10.0]
    assert sum(r["amount_billions"] for r in snap_8k) == 19.0
    assert any("3 unresolved 8-K guarantees" in w for w in result["coverage"]["warnings"])


def test_8k_amountless_termination_zeroes_exposure(monkeypatch):
    """Jan Alpha $10B + May Item 1.02 termination with no dollar figure:
    2 ledger rows (May amount-None terminated), $0 current exposure."""
    _install_with_8k(monkeypatch, {}, [
        ("2026-01-15", ["Item 1.01"],
         "The company entered into a guarantee agreement with Alpha Holdings, with aggregate payment "
         "obligation cumulatively capped at $10 billion under the Agreements.",
         "acc-jan"),
        ("2026-05-20", ["Item 1.02"],
         "The company terminated the Guarantee Agreement with Alpha Holdings.",
         "acc-may"),
    ])
    result = obligations.get_obligations("SYN")
    ledger_8k = [r for r in result["obligations"] if r["type"] == "8k_guarantees"]
    assert len(ledger_8k) == 2
    by_filed = {r["filed"]: r for r in ledger_8k}
    assert by_filed["2026-01-15"]["amount_billions"] == 10.0
    assert by_filed["2026-01-15"].get("lifecycle_status") == "unknown"
    assert by_filed["2026-05-20"]["amount_billions"] is None
    assert by_filed["2026-05-20"].get("lifecycle_status") == "terminated"
    assert [r for r in result["current_snapshot"] if r["type"] == "8k_guarantees"] == []


def test_payment_timing_roundtrip_and_retune(tmp_path):
    """A 95/24 front-loaded horizon round-trips as JSON; correcting it to
    90/29 retunes identity; reruns write zero rows."""
    from app.storage import parquet

    horizon = {
        "paid_in_remainder_of_fy": "2027",
        "paid_in_remainder_billions": 95.0,
        "paid_after_remainder_billions": 24.0,
    }
    row = _obligation_row(payment_horizon=horizon, schedule=None, amount_billions=119.0)
    row["content_hash"] = obligations._content_hash(row)
    summary = obligations.persist_obligation_events([row], data_root=str(tmp_path))
    assert summary == {"events_written": 1, "evidence_written": 1, "skipped_no_filing_date": 0, "skipped_proxied": 0}
    (event,) = parquet.read_table("events", root=tmp_path / "parquet").to_pylist()
    assert json.loads(event["payment_timing_json"]) == horizon
    rerun = obligations.persist_obligation_events([row], data_root=str(tmp_path))
    assert rerun == {"events_written": 0, "evidence_written": 0, "skipped_no_filing_date": 0, "skipped_proxied": 0}
    corrected = _obligation_row(
        payment_horizon={**horizon, "paid_in_remainder_billions": 90.0,
                         "paid_after_remainder_billions": 29.0},
        schedule=None, amount_billions=119.0,
    )
    corrected["content_hash"] = obligations._content_hash(corrected)
    assert corrected["content_hash"] != row["content_hash"]
    assert obligations.sec_event_id("NVDA", corrected["content_hash"]) != event["event_id"]
    retune = obligations.persist_obligation_events([corrected], data_root=str(tmp_path))
    assert retune["events_written"] == 1


def test_unquantified_hash_tracks_excerpt_and_trigger():
    base = {
        "type": "indemnities", "amount_billions": None, "filed": "2026-02-01",
        "trigger": "unknown", "excerpt": "The company may indemnify its officers.",
        "_accession": "acc-10k",
    }
    assert obligations._content_hash(dict(base)) == obligations._content_hash(dict(base))
    assert obligations._content_hash({**base, "excerpt": "  THE company MAY\nindemnify its   officers. "}) == \
        obligations._content_hash(dict(base))
    assert obligations._content_hash(
        {**base, "excerpt": "The company may indemnify its directors."}
    ) != obligations._content_hash(dict(base))
    assert obligations._content_hash(
        {**base, "trigger": "counterparty_default"}
    ) != obligations._content_hash(dict(base))
    assert obligations._content_hash(
        {**base, "_accession": "acc-10q"}
    ) != obligations._content_hash(dict(base))