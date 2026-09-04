"""Trust/taint regressions for the Exa ontology layer."""
import inspect

from app.domain.evidence.models import ResolutionStatus
from app.domain.evidence.source_quality import classify_source
from app.domain.market.securities import SecurityResolution
from app.security.context import Integrity
from app.domain.evidence.models import SourceTier
from app.services.evidence_claims import build_evidence_claims, claim_to_enriched_dict
from app.storage import parquet
from app.tools import plan_public_search_queries, suggest_public_search_queries


def test_high_trust_hosts_elevate():
    c = classify_source("https://www.reuters.com/x")
    assert c.integrity == Integrity.HIGH_TRUST_REPORTED
    assert c.source_tier == SourceTier.HIGH_TRUST_NEWS
    assert c.publisher == "Reuters"


def test_spoof_ir_and_gov_never_elevate():
    for url in (
        "https://reuters.com.evil.com/x",
        "https://evil-reuters.com/x",
        "https://ir.evil.com/x",
        "https://investor.evil.com/x",
        "https://www.sec.gov/x",
        "https://ir.nvidia.com/x",
    ):
        c = classify_source(url)
        assert c.integrity == Integrity.EXTERNAL, url
        assert c.source_tier == SourceTier.UNKNOWN, url


def test_sec_gov_not_canonical_or_primary():
    c = classify_source("https://www.sec.gov/x")
    assert c.source_tier != SourceTier.CANONICAL
    assert c.integrity not in (Integrity.CANONICAL, Integrity.PRIMARY_EXTERNAL)


def _amb(ticker=None, name=None, as_of=None):
    return SecurityResolution(None, None, ticker or name, False, "ambiguous")


def _unr(ticker=None, name=None, as_of=None):
    return SecurityResolution(None, None, ticker or name, False, "unresolved")


def _item(ticker="ABC"):
    return {
        "subject_ticker": ticker,
        "subject_name": "Abc",
        "claim": "x",
        "claim_type": "other",
        "source_url": "https://example.com",
        "retrieved_at": "2026-01-01T00:00:00+00:00",
    }


def test_ambiguous_ticker_strict_identity():
    (c,) = build_evidence_claims(
        reader_items=[_item("ABC")], resolve=_amb, retrieved_fallback="2026-01-01T00:00:00+00:00"
    )
    assert c.entity_id is None and c.security_id is None and c.ticker is None
    assert c.subject_resolution == ResolutionStatus.AMBIGUOUS
    assert c.reported_ticker == "ABC"
    d = claim_to_enriched_dict(c)
    assert d["ticker"] is None and d["reported_ticker"] == "ABC"
    assert d["subject_resolution"] == "ambiguous"


def test_unknown_ticker_unresolved():
    (c,) = build_evidence_claims(
        reader_items=[_item("ZZZ")], resolve=_unr, retrieved_fallback="2026-01-01T00:00:00+00:00"
    )
    assert c.ticker is None
    assert c.subject_resolution == ResolutionStatus.UNRESOLVED


def test_claim_persist_writes_only_evidence_claims(tmp_path):
    root = tmp_path / "parquet"
    (c,) = build_evidence_claims(
        reader_items=[_item("ABC")], resolve=_amb, retrieved_fallback="2026-01-01T00:00:00+00:00"
    )
    rows = [claim_to_enriched_dict(c)]
    assert parquet.write_rows("evidence_claims", rows, root=root) == 1
    assert parquet.write_rows("evidence_claims", rows, root=root) == 0
    assert parquet.count_rows("evidence_claims", root=root) == 1
    for other in ("financial_facts", "entities", "entity_aliases", "portfolio_snapshots"):
        assert parquet.count_rows(other, root=root) == 0


def test_planner_exposes_no_portfolio_or_snapshot():
    for fn in (plan_public_search_queries, suggest_public_search_queries):
        params = set(inspect.signature(fn).parameters)
        assert not {p for p in params if "portfolio" in p or "snapshot" in p or p == "data_root"}
        assert "include_portfolio" not in params
    queries = plan_public_search_queries(primary_name="Acme", primary_ticker="ACME", related_names=["Beta"])
    assert queries and all("TSLA" not in q["query"] for q in queries)
    queries2 = suggest_public_search_queries("e1", "Acme", "ACME", relationships=(), names_by_entity={})
    assert all("TSLA" not in q["query"] for q in queries2)
