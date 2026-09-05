"""Offline tests for M&A transaction parsing (no network)."""

from types import SimpleNamespace

from app.sec import transactions
from app.sec.models import Transaction
from app.sec.transactions import (
    diff_transaction,
    normalize_transaction,
    update_transaction,
)


def test_tender_offer_announced_with_per_share():
    text = "Buyer offers $54.20 per share in cash. Expiration is June 1."
    txn = normalize_transaction("acc-1", "SC TO-T", target="tgt",
                                filed_at="2024-01-01", text=text)
    assert txn.deal_type == "tender_offer"
    assert txn.status == "unknown"
    assert txn.consideration == "$54.20 per share"
    assert txn.event_id == "TGT:tender_offer:acc-1"
    assert txn.source_accessions == ("acc-1",)
    assert txn.tender_expiry is not None


def test_amendment_is_pending():
    txn = normalize_transaction("acc-2", "S-4/A", target="tgt", text=None)
    assert txn.deal_type == "merger"
    assert txn.status == "unknown"
    assert txn.consideration is None


def test_update_unions_accessions_and_status_wins():
    prev = normalize_transaction("acc-1", "SC TO-T", target="TGT",
                                 filed_at="2024-01-01",
                                 text="Offers $10 per share.")
    curr = normalize_transaction("acc-2", "SC TO-T/A", target="TGT",
                                 filed_at="2024-02-01",
                                 text="Offers $12 per share.")
    merged = update_transaction(prev, curr)
    assert merged.event_id == prev.event_id
    assert merged.source_accessions == ("acc-1", "acc-2")
    assert merged.status == "unknown"
    assert merged.consideration == "$12 per share"


def test_diff_names_only_changed_fields():
    prev = Transaction(event_id="e", target="T", accession_no="a1",
                       status="unknown", consideration="$10 per share",
                       source_accessions=("a1",))
    curr = Transaction(event_id="e", target="T", accession_no="a2",
                       status="unknown", consideration="$12 per share",
                       source_accessions=("a1", "a2"))
    diff = diff_transaction(prev, curr)
    assert set(diff) == {"consideration", "accession_no", "source_accessions"}
    assert diff["consideration"] == ["$10 per share", "$12 per share"]


def test_get_transaction_status_newest_first(monkeypatch):
    filings = [SimpleNamespace(accession_no="new", form="SC TO-T/A",
                               filed_at="2024-02-01", company="tgt"),
               SimpleNamespace(accession_no="old", form="SC TO-T",
                               filed_at="2024-01-01", company="tgt")]

    def fake_list(ticker_or_cik, **kwargs):
        return filings

    monkeypatch.setattr(transactions, "list_sec_filings", fake_list)
    monkeypatch.setattr(transactions, "load_transaction_text",
                        lambda acc: (_ for _ in ()).throw(
                            RuntimeError("offline")))
    out = transactions.get_transaction_status("TGT")
    assert [t.accession_no for t in out] == ["new", "old"]
    assert [t.status for t in out] == ["unknown", "unknown"]
    assert all(t.target == "TGT" for t in out)


def test_store_transaction_unknown_status_both_directions(tmp_path):
    from app.sec.store import query_transactions, store_transaction

    assert store_transaction({
        "accession": "0000000000-25-000016", "form": "S-4",
        "filer_cik": 111111, "filer_name": "Acquirer Inc",
        "subject_cik": 222222, "subject_name": "Target Co",
        "target_cik": 222222, "target_name": "Target Co",
        "known_at": "2024-05-01",
    }, root=tmp_path) == 1
    by_filer = query_transactions(filer_cik=111111, root=tmp_path)
    assert by_filer[0]["status"] == "unknown"
    assert by_filer[0]["target_name"] == "Target Co"
    by_subject = query_transactions(subject_cik=222222, root=tmp_path)
    # Registration without closing evidence keeps status unknown.
    assert [r["status"] for r in by_subject] == ["unknown"]
    assert by_subject[0]["filer_name"] == "Acquirer Inc"
