"""Offline tests for 8-K events, exhibits, and filing diffs (no network)."""

from types import SimpleNamespace

import pytest

from app.sec import diffs, documents, events8k

ACC = "0000000000-26-000001"


def test_parse_multi_item():
    items = {
        "Item 1.03": "bankruptcy filing text",
        "1.02": "",  # empty -> skipped
        "item_202": "earnings beat, see EX-99.1 attached",
        "9.01": "exhibits list",
        "Item 11.11": "unknown passes through untouched",
    }
    events = events8k.parse_8k_events(ACC, items, event_date="2026-01-01")
    assert [(e.item_number, e.item_name) for e in events] == [
        ("1.03", events8k.KNOWN_8K_ITEMS["1.03"]),
        ("2.02", events8k.KNOWN_8K_ITEMS["2.02"]),
        ("9.01", events8k.KNOWN_8K_ITEMS["9.01"]),
    ]
    assert events[1].exhibit_refs == ("EX-99.1",)
    assert all(e.accession_no == ACC and e.event_date == "2026-01-01" for e in events)
    assert events[0].to_dict()["item_number"] == "1.03"


def test_extract_wrapper_skips_falsy():
    texts = {"Item 1.03": "bankruptcy text", "Item 2.02": ""}

    class R:
        items = ["Item 1.03", "Item 2.02"]

        def __getitem__(self, k):
            return texts[k]

    events = events8k.extract_8k_events(R(), ACC)
    assert [e.item_number for e in events] == ["1.03"]


def test_exhibits(monkeypatch):
    atts = [
        SimpleNamespace(document_type="EX-99.1", description="Press release",
                        document="ex991.htm", url="https://x/ex991.htm"),
        SimpleNamespace(document_type="EX-10.1", description="Agreement",
                        document="ex101.htm", url="https://x/ex101.htm"),
    ]
    monkeypatch.setattr(documents, "get_by_accession_number",
                        lambda a: SimpleNamespace(exhibits=atts))
    rows = documents.get_filing_exhibits(ACC)
    assert rows[0] == {"accession_no": ACC, "exhibit": "EX-99.1",
                       "description": "Press release", "document": "ex991.htm",
                       "url": "https://x/ex991.htm"}
    assert documents.get_filing_exhibit(ACC, "ex-99.1")["document"] == "ex991.htm"
    with pytest.raises(ValueError):
        documents.get_filing_exhibit(ACC, "EX-99.9")


def test_diff_specialization_and_counts(monkeypatch):
    from app.sec import filings

    monkeypatch.setattr(documents, "get_sec_filing_text",
                        lambda a, d=None: "a\nb\nc\n" if a == "old" else "a\nB\nc\nd\n")
    monkeypatch.setattr(filings, "get_sec_filing",
                        lambda a: SimpleNamespace(form="10-K"))
    out = diffs.diff_filings("new", "old")
    assert out["specialization"] == "10-K/10-Q"
    assert out["added"] == 2 and out["removed"] == 1
    assert out["truncated"] is False


def test_diff_truncated_and_error(monkeypatch):
    from app.sec import filings
    texts = {"n": "\n".join(str(i) for i in range(1000, 2000)),
             "o": "\n".join(str(i) for i in range(1000))}
    monkeypatch.setattr(documents, "get_sec_filing_text",
                        lambda a, d=None: texts[a])

    monkeypatch.setattr(filings, "get_sec_filing",
                        lambda a: SimpleNamespace(form="8-K"))
    out = diffs.diff_filings("n", "o")
    assert out["truncated"] is True and len(out["diff_lines"]) == 500

    def boom(a, d=None):
        raise ValueError("bad accession")

    monkeypatch.setattr(documents, "get_sec_filing_text", boom)
    assert "error" in diffs.diff_filings("n", "o")
