"""Offline tests for app/sec/insider.py (no network)."""

from types import SimpleNamespace

import app.sec.insider as insider
from app.sec.models import InsiderTransaction, ProposedInsiderSale


class _Activity:
    def __init__(self, code=None, shares=None, price=None, date=None,
                 title=None, ad=None, holdings=None):
        self.transaction_code = code
        self.shares = shares
        self.price = price
        self.transaction_date = date
        self.security_title = title
        self.acquired_disposed = ad
        self.holdings_after = holdings


class _Obj:
    def __init__(self, rows, name="Jane Doe", cik="999"):
        self.insider_name = name
        self.insider_cik = cik
        self._rows = rows

    def get_transaction_activities(self):
        return self._rows


def test_code_kinds_and_missing_fields():
    rows = [
        _Activity(code="P", shares="1,000", price="10.5", date="2024-01-10",
                  title="Common", ad="A", holdings="5000"),
        _Activity(code="S", shares=200, price=11.0, date="2024-02-10",
                  title="Common", ad="D", holdings=4800),
        _Activity(code="M", shares=50, price="0", date="2024-03-10",
                  title="Option", ad="A", holdings=4850),
        _Activity(code="Z", shares=7, price=1.0, date="2024-04-10",
                  title="Common", ad="A", holdings=4857),
        _Activity(),  # everything missing
    ]
    txns = insider.normalize_ownership_filing(
        _Obj(rows), issuer="ACME", form="4", filed_at="2024-05-01",
        accession_no="x1")
    assert [t.transaction_kind for t in txns] == [
        "open_market_purchase", "open_market_sale", "exercise", "other",
        "other"]
    # never default unknown disposals to bearish selling
    assert txns[3].transaction_kind == "other"
    assert txns[3].transaction_kind != "open_market_sale"
    blank = txns[4]
    assert blank.shares is None and blank.price is None
    assert blank.transaction_date is None and blank.security is None
    assert blank.transaction_code is None and blank.holdings_after is None
    assert txns[0].shares == 1000 and txns[0].price == 10.5


class _FakeDF:
    columns = ["Shares to be sold"]

    def __getitem__(self, key):
        assert key == "Shares to be sold"
        return ["1,000", "500", None]


def test_normalize_144_sums_shares_column():
    form144 = SimpleNamespace(person_selling="John Smith",
                              seller_cik="123",
                              securities_to_be_sold=_FakeDF())
    sale = insider.normalize_144(form144, issuer="ACME",
                                 filed_at="2024-05-01", accession_no="t1")
    assert sale.seller_name == "John Smith"
    assert sale.shares_proposed == 1500


def test_normalize_144_never_raises():
    sale = insider.normalize_144(object(), issuer="ACME", filed_at=None,
                                 accession_no="t9")
    assert sale.shares_proposed is None


def test_compare_144_to_form4_date_filter():
    proposed = ProposedInsiderSale("John Smith", "123", "ACME", "2024-05-01",
                                   "t1", shares_proposed=1500)

    def txn(name, date, shares, kind="open_market_sale"):
        return InsiderTransaction(name, "123", "ACME", "4", "2024-06-01",
                                  "f1", date, "Common", "S", kind, shares,
                                  10.0, "D", 1000)

    txns = [
        txn("john smith", "2024-06-01", 400),   # later sale, case-insensitive
        txn("John Smith", "2024-04-01", 9999),  # earlier sale: ignored
        txn("John Smith", "2024-06-02", 100, kind="open_market_purchase"),
        txn("Someone Else", "2024-06-03", 500),
    ]
    result = insider.compare_144_to_form4(proposed, txns)
    assert result["executed_sale_shares"] == 400
    assert result["matched"] is True
    assert result["proposed_shares"] == 1500
    assert result["seller_name"] == "John Smith"


def test_compare_144_unmatched():
    proposed = ProposedInsiderSale("Jane Doe", None, "ACME", "2024-05-01",
                                   "t2", shares_proposed=100)
    result = insider.compare_144_to_form4(proposed, [])
    assert result["matched"] is False
    assert result["executed_sale_shares"] == 0


def test_get_insider_activity_skips_failed_loads(monkeypatch):
    filings = [
        SimpleNamespace(accession_no="g1", form="4", filed_at="2024-01-15",
                        company="ACME"),
        SimpleNamespace(accession_no="bad", form="4", filed_at="2024-02-15",
                        company="ACME"),
    ]
    monkeypatch.setattr(insider, "list_sec_filings", lambda *a, **k: filings)

    def fake_load(accession):
        if accession == "bad":
            raise RuntimeError("boom")
        return _Obj([_Activity(code="P", shares=10)])

    monkeypatch.setattr(insider, "load_ownership", fake_load)
    txns = insider.get_insider_activity("ACME")
    assert len(txns) == 1
    assert txns[0].transaction_kind == "open_market_purchase"


def test_store_queries_insider_both_directions(tmp_path):
    from app.sec.store import query_insider_transactions, store_insider_transaction

    assert store_insider_transaction({
        "accession": "0000000000-25-000015", "form": "4",
        "issuer_cik": 320193, "issuer_name": "Issuer Inc",
        "owner_cik": 1206472, "owner_name": "Jane Doe",
        "is_director": True, "transaction_code": "P",
        "shares": 100, "known_at": "2024-04-01",
    }, root=tmp_path) == 1
    by_issuer = query_insider_transactions(issuer_cik=320193, root=tmp_path)
    assert by_issuer[0]["owner_name"] == "Jane Doe"
    assert by_issuer[0]["is_director"] is True
    by_owner = query_insider_transactions(owner_cik=1206472, root=tmp_path)
    assert [r["issuer_cik"] for r in by_owner] == ["320193"]
    # Roles stay on their own side: issuer is never the owner.
    assert by_owner[0]["issuer_name"] != by_owner[0]["owner_name"]
