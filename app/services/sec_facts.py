"""Store-first SEC fact read path behind get_fundamentals/get_xbrl_facts.

EPS and shares_outstanding read the normalized Parquet store first (gated
``known_at <= as_of``, restatements resolved by the true latest ``filed_at``),
with the live ``edgar_client`` path as fallback; balance_sheet, overview, and
xbrl facts stay always-live.  Every served result is wrapped in a truthfully
labeled envelope: ``data_source`` is ``'store'`` only when the value actually
came from the point-in-time store, ``'live'`` otherwise.

The live EPS payload carries a human label under the key ``source``; the
envelope needs ``source`` for the provider code, so the payload label key is
renamed ``source`` -> ``source_label`` in every envelope (store and live
alike).  All other payload keys stay byte-identical.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional, TypedDict

from .. import edgar_client
from ..domain.market.identity import resolve_ticker_aliases
from ..edgar_client import (
    _DERIVED_Q4_OFFSET_DAYS,
    _DIVIDEND_SOURCE,
    _FY_DAYS,
    _MISSING_QUARTER_GAP_DAYS,
    _QUARTER_DAYS,
    _YTD_DAYS,
    _dividend_annual_history,
    _dividend_growth,
    _dividend_valuation,
    _has_contiguous_quarters,
    _is_recent_dividend_period,
)
from ..storage import duckdb
from .dividend_analysis import analyze_dividends

DEFAULT_DATA_ROOT = duckdb.DEFAULT_DATA_ROOT

DILUTED_EPS_CONCEPT = "EarningsPerShareDiluted"
BASIC_EPS_CONCEPT = "EarningsPerShareBasic"
SHARES_OUTSTANDING_CONCEPT = "EntityCommonStockSharesOutstanding"
DIVIDEND_PER_SHARE_CONCEPT = "CommonStockDividendsPerShareDeclared"

_EPS_CONCEPTS = (DILUTED_EPS_CONCEPT, BASIC_EPS_CONCEPT)
_DIVIDEND_CONCEPTS = (DIVIDEND_PER_SHARE_CONCEPT,)
_METRICS = ("eps", "shares_outstanding", "balance_sheet", "overview", "dividends")


class FinancialFactRow(TypedDict):
    """One normalized ``financial_facts`` row (parquet DOUBLE reads as float)."""

    concept: str
    value: float
    period_end: str
    filed_at: str
    accession: str
    known_at: str
    period_start: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    source_url: str | None


class DividendEventRow(TypedDict):
    """One normalized ``dividend_events`` row; None amounts stay None."""

    dividend_event_id: str
    entity_id: str | None
    security_id: str | None
    ticker: str | None
    amount_per_share: float | None
    currency: str | None
    dividend_type: str | None
    declaration_date: str | None
    record_date: str | None
    payment_date: str | None
    ex_dividend_date: str | None
    ex_dividend_date_source: str | None
    status: str | None
    source_form: str | None
    accession: str | None
    filed_at: str | None
    known_at: str | None
    source_url: str | None
    source_concept: str | None
    source_type: str | None
    evidence_excerpt: str | None
    content_hash: str | None
    parser_version: str | None


class CanonicalDividendEventRow(DividendEventRow, total=False):
    """Amended-duplicate winner plus its merged ``source_types``."""

    source_types: list[str]


StoredFactKey = tuple[str, str, str]
RevisionKey = tuple[str, str, str]


def _today() -> _dt.date:
    return _dt.date.today()


def _validated_as_of(as_of: Optional[str]) -> Optional[_dt.date]:
    """None -> today; otherwise strict YYYY-MM-DD or an error marker."""
    if as_of is None:
        return _today()
    try:
        return _dt.datetime.strptime(as_of, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _resolve_entity(ticker: str, as_of: _dt.date, data_root: Optional[Path]) -> Optional[str]:
    """Resolve a ticker to its entity id through the alias store.

    The alias horizon is end-of-day UTC on the as-of date so an alias
    ingested on the as-of day itself is visible (day-granularity semantics,
    matching the store's ``known_at <= as_of`` facts gate).  Ambiguous or
    unresolved tickers resolve to None: no store path, never a guess.
    """
    horizon = _dt.datetime.combine(as_of, _dt.time.max, tzinfo=_dt.timezone.utc)
    aliases = duckdb.ticker_alias_candidates(ticker, horizon, data_root=data_root)
    if not aliases:
        return None
    resolution = resolve_ticker_aliases(ticker, aliases, as_of=horizon)
    if not resolution.resolved:
        return None
    return resolution.entity_id


def _validated_fact_row(row: Mapping[str, object]) -> FinancialFactRow | None:
    """Narrow one raw store row to a FinancialFactRow; None when unusable.

    The store is machine-written so every row validates; a row without a
    concept, period end, or numeric value cannot assemble and is skipped.
    """
    concept = row.get("concept")
    period_end = row.get("period_end")
    value = row.get("value")
    if not isinstance(concept, str) or not concept:
        return None
    if not isinstance(period_end, str) or not period_end:
        return None
    if not isinstance(value, (int, float)):
        return None
    filed_at = row.get("filed_at")
    accession = row.get("accession")
    known_at = row.get("known_at")
    fiscal_year = row.get("fiscal_year")
    period_start = row.get("period_start")
    fiscal_period = row.get("fiscal_period")
    source_url = row.get("source_url")
    return {
        "concept": concept,
        "value": float(value),
        "period_end": period_end,
        "filed_at": filed_at if isinstance(filed_at, str) else ("" if filed_at is None else str(filed_at)),
        "accession": accession if isinstance(accession, str) else ("" if accession is None else str(accession)),
        "known_at": known_at if isinstance(known_at, str) else ("" if known_at is None else str(known_at)),
        "period_start": period_start if isinstance(period_start, str) else None,
        "fiscal_year": fiscal_year if isinstance(fiscal_year, int) else None,
        "fiscal_period": fiscal_period if isinstance(fiscal_period, str) else None,
        "source_url": source_url if isinstance(source_url, str) else None,
    }


def _store_rows(entity_id: str, concepts: tuple[str, ...], as_of: _dt.date, data_root: Optional[Path]) -> list[FinancialFactRow]:
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    placeholders = ",".join("?" for _ in concepts)
    rows = duckdb.query(
        "SELECT concept, value, period_start, period_end, fiscal_year, "
        "fiscal_period, filed_at, accession, known_at, source_url "
        "FROM financial_facts "
        f"WHERE entity_id = ? AND concept IN ({placeholders}) AND {clause} "
        "ORDER BY period_end, filed_at, accession",
        params=[entity_id, *concepts, param],
        data_root=data_root,
    )
    validated: list[FinancialFactRow] = []
    for row in rows:
        fact = _validated_fact_row(row)
        if fact is not None:
            validated.append(fact)
    return validated


def _envelope(
    ticker: str,
    metric: str,
    payload: Mapping[str, object],
    *,
    data_source: str,
    as_of_date: str,
    requested_as_of: Optional[str] = None,
    row_count: Optional[int] = None,
    returned_count: Optional[int] = None,
    truncated: bool = False,
) -> dict[str, object]:
    """Wrap a payload in the exact public envelope shape."""
    env: dict[str, object] = {
        "source": "sec",
        "metric": metric,
        "data_source": data_source,
        "as_of_date": as_of_date,
        "row_count": row_count,
        "returned_count": returned_count if returned_count is not None else row_count,
        "truncated": truncated,
    }
    if requested_as_of is not None and requested_as_of != as_of_date:
        env["requested_as_of"] = requested_as_of
    for key, value in payload.items():
        env["source_label" if key == "source" else key] = value
    return env


# ---------------------------------------------------------------------------
# EPS: store assembly mirroring edgar_client semantics
# ---------------------------------------------------------------------------


def _duration_days(row: FinancialFactRow) -> Optional[int]:
    start, end = row.get("period_start"), row.get("period_end")
    if not start or not end:
        return None
    try:
        start_date = _dt.date.fromisoformat(start[:10])
        end_date = _dt.date.fromisoformat(end[:10])
    except ValueError:
        return None
    return (end_date - start_date).days


def _row_period_end(row: FinancialFactRow) -> str:
    """Sort key: fact period end as text (ISO dates order lexically)."""
    return row["period_end"]


def _row_payment_date(row: CanonicalDividendEventRow) -> str:
    """Sort key: dividend payment date as text."""
    return str(row["payment_date"])


def _row_known_at(row: Mapping[str, object]) -> str:
    """Sort key: revision recency as text (missing sorts first)."""
    return str(row.get("known_at") or "")


def _row_filed_accession(row: FinancialFactRow) -> tuple[str, str]:
    """Sort key: true latest filing first by filed_at, accession tie-break."""
    return ((row.get("filed_at") or ""), (row.get("accession") or ""))


def _row_stored_order(row: FinancialFactRow) -> tuple[str, str, str]:
    """Sort key: newest stored fact by period end, filed_at, accession."""
    return (row["period_end"], (row.get("filed_at") or ""), (row.get("accession") or ""))


def _row_revision_order(row: Mapping[str, object]) -> tuple[str, str, str]:
    """Sort key: latest revision by known_at, filed_at, accession."""
    return (str(row.get("known_at") or ""), str(row.get("filed_at") or ""), str(row.get("accession") or ""))


def _duration_rows(rows: Sequence[FinancialFactRow], concept: str, day_range: tuple[int, int]) -> list[FinancialFactRow]:
    """Facts of a given duration for a concept, restatements resolved
    by the true latest filed_at (accession DESC tie-break) — replacing
    edgar_client._dedup_latest's fiscal-year proxy — newest period first."""
    q: list[FinancialFactRow] = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if duration is not None and day_range[0] <= duration <= day_range[1]:
            q.append(row)
    by_end: dict[str, FinancialFactRow] = {}
    for row in q:
        key = row["period_end"]
        prev = by_end.get(key)
        if prev is None or ((row["filed_at"] or ""), (row["accession"] or "")) > (
            (prev["filed_at"] or ""), (prev["accession"] or "")
        ):
            by_end[key] = row
    return sorted(by_end.values(), key=_row_period_end)


def _derive_q4_from_facts(rows: Sequence[FinancialFactRow], concept: str, fy_end: _dt.date) -> Optional[FinancialFactRow]:
    """Q4 = FY_total - YTD_through_Q3 for the fiscal year ending fy_end."""
    fy = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if (
            duration is not None
            and _FY_DAYS[0] <= duration <= _FY_DAYS[1]
            and row["period_end"] == fy_end.isoformat()
        ):
            fy.append(row)
    if not fy:
        return None
    latest_fy = max(fy, key=_row_filed_accession)
    ytd = []
    for row in rows:
        if row.get("concept") != concept:
            continue
        duration = _duration_days(row)
        if duration is None or not (_YTD_DAYS[0] <= duration <= _YTD_DAYS[1]):
            continue
        try:
            row_end = _dt.date.fromisoformat(row["period_end"][:10])
        except ValueError:
            continue
        if fy_end - _dt.timedelta(days=_MISSING_QUARTER_GAP_DAYS) <= row_end < fy_end:
            ytd.append(row)
    if not ytd:
        return None
    ytd_q3 = sorted(ytd, key=_row_period_end)[-1]["value"]
    latest_total = latest_fy["value"]
    derived: FinancialFactRow = {
        **latest_fy,
        "value": latest_total - ytd_q3,
        "period_end": fy_end.isoformat(),
        "fiscal_period": "Q4",
    }
    return derived


def _quarters_with_derived_q4(quarter_rows: Sequence[FinancialFactRow], all_rows: Sequence[FinancialFactRow], concept: str) -> list[FinancialFactRow]:
    """Last 4 quarterly facts, deriving a missing final quarter (NVDA reports
    Q4 diluted EPS only as a full-year fact) — mirrors
    edgar_client._quarters_with_derived_q4."""
    quarter = sorted(quarter_rows, key=_row_period_end)
    if len(quarter) < 2:
        return quarter[-4:]
    ends = [_dt.date.fromisoformat(row["period_end"][:10]) for row in quarter]
    last_gap = (ends[-1] - ends[-2]).days
    if last_gap > _MISSING_QUARTER_GAP_DAYS:
        missing_end = ends[-1] - _dt.timedelta(days=_DERIVED_Q4_OFFSET_DAYS)
        derived = _derive_q4_from_facts(all_rows, concept, missing_end)
        if derived is not None:
            quarter = sorted([*quarter, derived], key=_row_period_end)
    return quarter[-4:]


def _assemble_eps_payload(ticker: str, rows: Sequence[FinancialFactRow]) -> Optional[dict[str, object]]:
    """Deterministic store assembly over feed rows (pure; no storage)."""
    recent_diluted = _quarters_with_derived_q4(
        _duration_rows(rows, DILUTED_EPS_CONCEPT, _QUARTER_DAYS), rows, DILUTED_EPS_CONCEPT
    )
    if not recent_diluted:
        return None
    recent_basic = None
    basic_quarters = _duration_rows(rows, BASIC_EPS_CONCEPT, _QUARTER_DAYS)
    if basic_quarters:
        recent_basic = _quarters_with_derived_q4(basic_quarters, rows, BASIC_EPS_CONCEPT)

    quarterly_eps: list[dict[str, object]] = []
    for r in recent_diluted:
        entry: dict[str, object] = {
            "fiscal_year": str(r["fiscal_year"]) if r.get("fiscal_year") is not None else "",
            "fiscal_period": (r.get("fiscal_period") or ""),
            "eps_diluted": round(r["value"], 2),
            "period_end": r["period_end"],
        }
        if recent_basic:
            matching = next(
                (b for b in recent_basic if b["period_end"] == entry["period_end"]), None
            )
            if matching is not None:
                entry["eps_basic"] = round(matching["value"], 2)
        quarterly_eps.append(entry)

    result: dict[str, object] = {
        "ticker": ticker,
        "quarterly_eps": quarterly_eps,
        "source": "SEC EDGAR company facts (Basic & Diluted EPS)",
    }
    if len(recent_diluted) == 4:
        result["ttm_eps_diluted"] = round(sum(r["value"] for r in recent_diluted), 2)
    if recent_basic is not None and len(recent_basic) == 4:
        result["ttm_eps_basic"] = round(sum(r["value"] for r in recent_basic), 2)
    return result


def _assemble_dividend_payload(ticker: str, rows: Sequence[FinancialFactRow], as_of: _dt.date) -> Optional[dict[str, object]]:
    """Deterministic store assembly over feed rows (pure; no storage)."""
    if not any(r.get("concept") == DIVIDEND_PER_SHARE_CONCEPT for r in rows):
        return None
    quarters = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, _QUARTER_DAYS)
    recent = _quarters_with_derived_q4(quarters, rows, DIVIDEND_PER_SHARE_CONCEPT)
    if len(recent) == 4 and _has_contiguous_quarters([r["period_end"] for r in recent]) and _is_recent_dividend_period(recent[-1]["period_end"], as_of):
        ttm = round(sum(r["value"] for r in recent), 4)
    else:
        ttm = None
    fy_rows = _duration_rows(rows, DIVIDEND_PER_SHARE_CONCEPT, _FY_DAYS)
    history, annual = _dividend_annual_history(fy_rows)
    return {
        "ticker": ticker,
        "dividend_status": "paying" if ttm is not None else "unknown",
        "ttm_dividend_per_share": ttm,
        **_dividend_growth(annual),
        "annual_history": history,
        "source": _DIVIDEND_SOURCE,
    }


# ---------------------------------------------------------------------------
# Dividend events: past / present / future-declared (no projections)
# ---------------------------------------------------------------------------


def _validated_dividend_event(row: Mapping[str, object]) -> DividendEventRow | None:
    """Narrow one raw ``dividend_events`` row; None amounts stay None, never zero."""
    event_id = row.get("dividend_event_id")
    if not isinstance(event_id, str) or not event_id:
        return None

    def _text(key: str) -> str | None:
        value = row.get(key)
        if isinstance(value, str):
            return value
        return None if value is None else str(value)

    amount = row.get("amount_per_share")
    return {
        "dividend_event_id": event_id,
        "entity_id": _text("entity_id"),
        "security_id": _text("security_id"),
        "ticker": _text("ticker"),
        "amount_per_share": float(amount) if isinstance(amount, (int, float)) else None,
        "currency": _text("currency"),
        "dividend_type": _text("dividend_type"),
        "declaration_date": _text("declaration_date"),
        "record_date": _text("record_date"),
        "payment_date": _text("payment_date"),
        "ex_dividend_date": _text("ex_dividend_date"),
        "ex_dividend_date_source": _text("ex_dividend_date_source"),
        "status": _text("status"),
        "source_form": _text("source_form"),
        "accession": _text("accession"),
        "filed_at": _text("filed_at"),
        "known_at": _text("known_at"),
        "source_url": _text("source_url"),
        "source_concept": _text("source_concept"),
        "source_type": _text("source_type"),
        "evidence_excerpt": _text("evidence_excerpt"),
        "content_hash": _text("content_hash"),
        "parser_version": _text("parser_version"),
    }


def _store_dividend_events(entity_id: str, as_of: _dt.date, data_root: Optional[Path]) -> list[DividendEventRow]:
    """PIT-gated dividend events, deduped by id keeping max known_at.

    Amended filings produce different ids (amount is part of the id) so both
    revisions stay visible; duplicate 8-K/10-Q disclosures share an id and
    collapse here.
    """
    clause, param = duckdb.as_of_clause(as_of.isoformat())
    rows = duckdb.query(
        "SELECT dividend_event_id, entity_id, security_id, ticker, amount_per_share, "
        "currency, dividend_type, declaration_date, record_date, payment_date, "
        "ex_dividend_date, ex_dividend_date_source, status, source_form, accession, "
        "filed_at, known_at, source_url, source_concept, source_type, evidence_excerpt, "
        "content_hash, parser_version "
        "FROM dividend_events "
        f"WHERE entity_id = ? AND {clause} "
        "ORDER BY known_at, dividend_event_id",
        params=[entity_id, param],
        data_root=data_root,
    )
    by_id: dict[str, DividendEventRow] = {}
    for row in rows:
        event = _validated_dividend_event(row)
        if event is None:
            continue
        key = event["dividend_event_id"]
        prev = by_id.get(key)
        if prev is None or (event.get("known_at") or "") >= (prev.get("known_at") or ""):
            by_id[key] = event
    return list(by_id.values())


def _classify_dividend_event(row: CanonicalDividendEventRow, as_of: _dt.date) -> str:
    """payment_date < as_of -> paid; >= as_of -> upcoming; null -> unknown."""
    pay = row.get("payment_date")
    if not pay:
        return "unknown"
    return "paid" if pay[:10] < as_of.isoformat() else "upcoming"


def _extreme_event(cands: Sequence[CanonicalDividendEventRow], *, earliest: bool) -> Optional[CanonicalDividendEventRow]:
    """Edge payment date wins; same-date revisions prefer latest known_at."""
    edge = (min if earliest else max)(str(r["payment_date"]) for r in cands)
    tied = [r for r in cands if str(r["payment_date"]) == edge]
    return max(tied, key=_row_known_at)


def _canonical_dividend_events(events: Sequence[DividendEventRow]) -> list[CanonicalDividendEventRow]:
    """Collapse amended duplicates; undated rows never merge.

    Buckets share (record_date, payment_date). Concrete types cluster exactly
    (regular never merges into special). An unknown row joins a typed cluster
    only on a unique amount match — or when the bucket holds a single typed
    identity (amendment); otherwise it stays unresolved instead of attaching
    to the first compatible bucket.
    """
    undated = [r for r in events if not r.get("payment_date")]
    groups: dict[tuple[str, str], list[DividendEventRow]] = {}
    for row in events:
        if not row.get("payment_date"):
            continue
        groups.setdefault(
            ((row.get("record_date") or ""), row.get("payment_date")), []).append(row)
    canonical: list[CanonicalDividendEventRow] = []
    for group in groups.values():
        clusters: list[list[DividendEventRow]] = []
        for row in group:
            if (row.get("dividend_type") or "") in ("", "unknown"):
                continue
            for cluster in clusters:
                if all((m.get("dividend_type") or "") == (row.get("dividend_type") or "")
                       for m in cluster):
                    cluster.append(row)
                    break
            else:
                clusters.append([row])
        stray: list[DividendEventRow] = []
        for row in group:
            if (row.get("dividend_type") or "") not in ("", "unknown"):
                continue
            amount = row.get("amount_per_share")
            matched = [c for c in clusters
                       if amount is not None and any(m.get("amount_per_share") == amount for m in c)]
            if len(matched) == 1:
                matched[0].append(row)
            elif not matched and len(clusters) <= 1:
                if clusters:
                    clusters[0].append(row)
                else:
                    stray.append(row)
            else:
                stray.append(row)
        if stray:
            clusters.append(stray)
        for bucket in clusters:
            winner = max(bucket, key=_row_revision_order)
            out: CanonicalDividendEventRow = {
                **winner,
                "source_types": sorted({m.get("source_type") for m in bucket if m.get("source_type")}),
            }
            for mate in bucket:
                if mate is winner:
                    continue
                mate_amount = mate.get("amount_per_share")
                if out.get("amount_per_share") is None and isinstance(mate_amount, (int, float)):
                    out["amount_per_share"] = float(mate_amount)
                mate_type = mate.get("dividend_type")
                if (out.get("dividend_type") or "") in ("", "unknown") and isinstance(mate_type, str) and mate_type not in ("", "unknown"):
                    out["dividend_type"] = mate_type
                mate_excerpt = mate.get("evidence_excerpt")
                if not out.get("evidence_excerpt") and isinstance(mate_excerpt, str) and mate_excerpt:
                    out["evidence_excerpt"] = mate_excerpt
                mate_concept = mate.get("source_concept")
                if not out.get("source_concept") and isinstance(mate_concept, str) and mate_concept:
                    out["source_concept"] = mate_concept
            canonical.append(out)
    undated_canonical: list[CanonicalDividendEventRow] = [
        {**r, "source_types": [r["source_type"]] if r["source_type"] else []}
        for r in undated
    ]
    return undated_canonical + canonical


def _dividend_event_source_types(row: CanonicalDividendEventRow) -> list[str]:
    sts = row.get("source_types")
    if isinstance(sts, (list, tuple, set)) and sts:
        return [s for s in sts if s]
    st = row.get("source_type")
    return [st] if st else []


def _dividend_event_payload(events: Sequence[DividendEventRow], as_of: _dt.date, *, growth: Mapping[str, object] | None = None, ttm_dps: float | None = None, annual_history: Sequence[Mapping[str, object]] | None = None) -> dict[str, object]:
    """last/next/past/coverage over classified events (pure; no storage)."""
    canonical = _canonical_dividend_events(events)
    upcoming: list[CanonicalDividendEventRow] = [
        r for r in canonical
        if _classify_dividend_event(r, as_of) == "upcoming"
        and r.get("amount_per_share") is not None and r.get("payment_date")
    ]
    paid: list[CanonicalDividendEventRow] = [
        r for r in canonical
        if _classify_dividend_event(r, as_of) == "paid"
        and r.get("amount_per_share") is not None and r.get("payment_date")
    ]
    nxt = _extreme_event(upcoming, earliest=True) if upcoming else None
    last = _extreme_event(paid, earliest=False) if paid else None
    past: list[dict[str, object]] = [
        {
            "amount_per_share": r.get("amount_per_share"),
            "payment_date": r.get("payment_date"),
            "declaration_date": r.get("declaration_date"),
            "record_date": r.get("record_date"),
            "dividend_type": r.get("dividend_type"),
            "status": _classify_dividend_event(r, as_of),
            "accession": r.get("accession"),
            "source_url": r.get("source_url"),
        }
        for r in sorted(paid, key=_row_payment_date, reverse=True)[:12]
    ]
    has_xbrl = any("structured_xbrl" in _dividend_event_source_types(r) for r in canonical)
    has_text = any("filing_text" in _dividend_event_source_types(r) for r in canonical)
    coverage = (
        "structured_and_text" if has_xbrl and has_text
        else "structured_only" if has_xbrl
        else "text_only" if has_text
        else "no_structured_events"
    )
    analysis: dict[str, object] = analyze_dividends(paid_events=paid, as_of=as_of, ttm_dps=ttm_dps, growth=growth, annual_history=annual_history)
    return {
        "last_dividend": (
            {"amount_per_share": last["amount_per_share"],
             "payment_date": last["payment_date"], "type": last.get("dividend_type")}
            if last is not None else None
        ),
        "next_declared_dividend": (
            {
                "amount_per_share": nxt["amount_per_share"],
                "declaration_date": nxt.get("declaration_date"),
                "record_date": nxt.get("record_date"),
                "payment_date": nxt["payment_date"],
                "status": "upcoming",
                "source_url": nxt.get("source_url"),
                "accession": nxt.get("accession"),
            }
            if nxt is not None else None
        ),
        "past_events": past,
        "events_coverage": coverage,
        **analysis,
    }


# ---------------------------------------------------------------------------
# Dividend safety: FCF-based coverage on SEC inputs (no new providers)
# ---------------------------------------------------------------------------

_SAFETY_METHODOLOGY = "common-stock EPS/FCF basis; not AFFO/FFO"

_OCF_CONCEPT = "OperatingCashFlow"
_CAPEX_CONCEPT = "CapEx"
_DIV_PAID_CONCEPT = "DividendsPaid"
_CASH_CONCEPT = "CashAndCashEquivalents"
_DEBT_CONCEPT = "LongTermDebt"
_NET_INCOME_CONCEPT = "NetIncomeLoss"

_SAFETY_CONCEPTS = (
    _OCF_CONCEPT, _CAPEX_CONCEPT, _DIV_PAID_CONCEPT, _CASH_CONCEPT,
    _DEBT_CONCEPT, _NET_INCOME_CONCEPT,
    DILUTED_EPS_CONCEPT, BASIC_EPS_CONCEPT,
)


def _ttm_cash_total(rows: Sequence[FinancialFactRow], concept: str, *, outflow: bool = False) -> Optional[float]:
    """Trailing-4-quarter total via the shared quarterly + derived-Q4 machinery.

    Outflow concepts (CapEx, dividends paid) are cash-flow debits, usually
    filed as negative values; coverage math needs their magnitude.
    """
    quarters = _quarters_with_derived_q4(
        _duration_rows(rows, concept, _QUARTER_DAYS), rows, concept
    )
    if len(quarters) != 4 or not _has_contiguous_quarters([r["period_end"] for r in quarters]):
        return None
    values = [abs(r["value"]) if outflow else r["value"] for r in quarters]
    return round(sum(values), 2)


def _fy_annual_totals(rows: Sequence[FinancialFactRow], concept: str, *, outflow: bool = False) -> dict[int, float]:
    """FY-duration facts keyed by calendar year of period_end."""
    annual: dict[int, float] = {}
    for r in _duration_rows(rows, concept, _FY_DAYS):
        try:
            year = _dt.date.fromisoformat(r["period_end"][:10]).year
            val = abs(r["value"]) if outflow else r["value"]
        except (TypeError, ValueError):
            continue
        annual[year] = round(val, 2)
    return annual


def _latest_concept_value(rows: Sequence[FinancialFactRow], concept: str) -> Optional[FinancialFactRow]:
    """Latest row for a concept by (period_end, filed_at, accession)."""
    cands = [r for r in rows if r.get("concept") == concept and r.get("period_end")]
    if not cands:
        return None
    return max(cands, key=_row_stored_order)


def _debt_up_yoy(rows: Sequence[FinancialFactRow]) -> Optional[bool]:
    """Latest LongTermDebt vs the most recent row at least ~10 months older."""
    latest = _latest_concept_value(rows, _DEBT_CONCEPT)
    if latest is None:
        return None
    try:
        end = _dt.date.fromisoformat(latest["period_end"][:10])
        now_val = latest["value"]
    except (TypeError, ValueError):
        return None
    cutoff = (end - _dt.timedelta(days=300)).isoformat()
    older = [r for r in rows
             if r.get("concept") == _DEBT_CONCEPT and (r.get("period_end") or "") <= cutoff]
    if not older:
        return None
    base = _latest_concept_value(older, _DEBT_CONCEPT)
    if base is None:
        return None
    try:
        then_val = base["value"]
    except (TypeError, ValueError):
        return None
    return now_val > then_val


def _assemble_dividend_safety(
    rows: Sequence[FinancialFactRow],
    dividend: Mapping[str, object],
    *,
    ttm_eps_diluted: Optional[float] = None,
    ttm_yield: Optional[float] = None,
) -> dict[str, object]:
    """FCF/EPS safety on SEC inputs only (pure; no storage).

    Absent concepts yield nulls with reasons, never zero-filled or borrowed.
    """
    ttm_dps_raw = dividend.get("ttm_dividend_per_share")
    ttm_dps = float(ttm_dps_raw) if isinstance(ttm_dps_raw, (int, float)) else None
    ttm_ocf = _ttm_cash_total(rows, _OCF_CONCEPT)
    ttm_capx = _ttm_cash_total(rows, _CAPEX_CONCEPT, outflow=True)
    ttm_div_paid = _ttm_cash_total(rows, _DIV_PAID_CONCEPT, outflow=True)
    ttm_fcf = round(ttm_ocf - ttm_capx, 2) if ttm_ocf is not None and ttm_capx is not None else None

    safety: dict[str, object] = {
        "ttm_fcf": ttm_fcf,
        "ttm_dividends_paid": ttm_div_paid,
        "methodology": _SAFETY_METHODOLOGY,
    }
    def _set(key: str, value: float | None, reason: Optional[str] = None) -> None:
        safety[key] = value
        if value is None and reason:
            safety[f"{key}_reason"] = reason
    flags: list[dict[str, object]] = []

    def _flag(flag: str, status: Optional[bool], basis: str) -> None:
        flags.append({"flag": flag, "status": status, "basis": basis})

    if ttm_eps_diluted is not None and ttm_eps_diluted <= 0:
        _set("earnings_payout_ratio", None, f"ttm_eps_diluted {ttm_eps_diluted} <= 0")
        _flag("negative_eps", True, f"ttm_eps_diluted {ttm_eps_diluted} <= 0; payout meaningless")
    elif ttm_dps is None or ttm_eps_diluted is None:
        _set("earnings_payout_ratio", None, "missing ttm dps or diluted eps")
    else:
        _set("earnings_payout_ratio", round(ttm_dps / ttm_eps_diluted, 4))

    fcf_nonpositive = ttm_fcf is not None and ttm_fcf <= 0
    if fcf_nonpositive:
        _set("fcf_payout_ratio", None, f"ttm_fcf {ttm_fcf} <= 0")
        _set("fcf_coverage", None, f"ttm_fcf {ttm_fcf} <= 0")
        _flag("negative_or_zero_fcf", True, f"ttm_fcf {ttm_fcf} <= 0; payout/coverage meaningless")
    elif ttm_div_paid is None or ttm_fcf is None:
        _set("fcf_payout_ratio", None, "missing ttm dividends-paid or fcf")
        _set("fcf_coverage", None, "missing ttm dividends-paid or fcf")
    elif ttm_div_paid == 0:
        _set("fcf_payout_ratio", 0.0)
        _set("fcf_coverage", None, "zero ttm dividends-paid")
        _flag("zero_dividend", True, "ttm_dividends_paid is zero; coverage undefined")
    else:
        _set("fcf_payout_ratio", round(ttm_div_paid / ttm_fcf, 4))
        _set("fcf_coverage", round(ttm_fcf / ttm_div_paid, 4))

    cash_row = _latest_concept_value(rows, _CASH_CONCEPT)
    try:
        cash = cash_row["value"] if cash_row is not None else None
    except (TypeError, ValueError):
        cash = None
    if cash is None or ttm_div_paid is None:
        _set("cash_to_annual_dividend", None, "missing cash balance or ttm dividends-paid")
    elif ttm_div_paid == 0:
        _set("cash_to_annual_dividend", None, "zero ttm dividends-paid")
    else:
        _set("cash_to_annual_dividend", round(cash / ttm_div_paid, 4))

    _set("interest_coverage", None, "operating-income/interest-expense concepts not in store")

    debt_up = _debt_up_yoy(rows)
    safety["debt_up_yoy"] = debt_up
    if debt_up is None:
        safety["debt_up_yoy_reason"] = "missing current or year-ago long-term debt"

    ocf_ann = _fy_annual_totals(rows, _OCF_CONCEPT)
    capx_ann = _fy_annual_totals(rows, _CAPEX_CONCEPT, outflow=True)
    fcf_ann = {y: round(ocf_ann[y] - capx_ann[y], 2) for y in ocf_ann if y in capx_ann}
    paid_ann = _fy_annual_totals(rows, _DIV_PAID_CONCEPT, outflow=True)
    div_cagr_raw = dividend.get("growth_5y_cagr")
    div_cagr = float(div_cagr_raw) if isinstance(div_cagr_raw, (int, float)) else None
    fcf_cagr = _dividend_growth(fcf_ann)["growth_5y_cagr"]
    if div_cagr is None or fcf_cagr is None:
        verdict = "insufficient_data"
    elif div_cagr - fcf_cagr > 0.02:
        verdict = "payout_expanding"
    else:
        verdict = "runway_supported"
    safety["dividend_vs_fcf_growth_5y"] = {
        "dividend_cagr": div_cagr, "fcf_cagr": fcf_cagr, "verdict": verdict,
    }

    if ttm_yield is None:
        _flag("high_absolute_yield", None, "no current ttm yield (historical as_of or missing price)")
    else:
        _flag("high_absolute_yield", ttm_yield >= 0.06, f"ttm_dividend_yield {ttm_yield}")
    if fcf_ann and max(fcf_ann) - 1 in fcf_ann:
        latest_y = max(fcf_ann)
        _flag("fcf_declined_yoy", fcf_ann[latest_y] < fcf_ann[latest_y - 1],
              f"annual fcf {latest_y - 1} {fcf_ann[latest_y - 1]} -> {latest_y} {fcf_ann[latest_y]}")
    else:
        _flag("fcf_declined_yoy", None, "missing consecutive annual fcf totals")
    if fcf_ann and paid_ann and max(fcf_ann) - 1 in fcf_ann and max(fcf_ann) - 1 in paid_ann:
        latest_y = max(fcf_ann)
        prior_y = latest_y - 1
        if fcf_ann[latest_y] > 0 and fcf_ann[prior_y] > 0:
            cur, prev = paid_ann[latest_y] / fcf_ann[latest_y], paid_ann[prior_y] / fcf_ann[prior_y]
            _flag("fcf_payout_expanded", cur - prev > 0.10,
                  f"annual fcf payout {prev:.4f} ({prior_y}) -> {cur:.4f} ({latest_y})")
        else:
            _flag("fcf_payout_expanded", None, "non-positive annual fcf base")
    else:
        _flag("fcf_payout_expanded", None, "missing consecutive annual payout bases")
    eps_ann = _fy_annual_totals(rows, DILUTED_EPS_CONCEPT)
    eps_basis_name = "diluted eps"
    if not eps_ann:
        eps_ann = _fy_annual_totals(rows, _NET_INCOME_CONCEPT)
        eps_basis_name = "net income"
    if eps_ann and max(eps_ann) - 1 in eps_ann:
        latest_y = max(eps_ann)
        _flag("eps_declined_yoy", eps_ann[latest_y] < eps_ann[latest_y - 1],
              f"annual {eps_basis_name} {latest_y - 1} {eps_ann[latest_y - 1]} -> "
              f"{latest_y} {eps_ann[latest_y]}")
    else:
        _flag("eps_declined_yoy", None, "missing consecutive annual earnings totals")
    if debt_up is None:
        _flag("leverage_rising", None, "missing current or year-ago long-term debt")
    else:
        _flag("leverage_rising", debt_up, f"long-term debt {'up' if debt_up else 'not up'} year-over-year")
    g1_raw, g5_raw = dividend.get("growth_1y"), dividend.get("growth_5y_cagr")
    g1 = float(g1_raw) if isinstance(g1_raw, (int, float)) else None
    g5 = float(g5_raw) if isinstance(g5_raw, (int, float)) else None
    if g1 is None or g5 is None:
        _flag("growth_decelerating", None, "missing growth_1y or growth_5y_cagr")
    else:
        _flag("growth_decelerating", g1 < g5, f"growth_1y {g1} vs growth_5y_cagr {g5}")

    safety["risk_flags"] = flags
    return safety


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def get_fundamentals(ticker: str, metric: str, as_of: Optional[str] = None) -> dict[str, object]:
    """Store-first fundamentals with a truthful data_source envelope."""
    explicit_as_of = as_of is not None
    requested = _validated_as_of(as_of)
    if requested is None:
        return {
            "error": "as_of must be a date in YYYY-MM-DD format",
            "error_type": "invalid_tool_arguments",
        }
    if metric == "shares_float":
        metric = "shares_outstanding"
    if metric == "eps":
        return _eps_fundamental(ticker, requested, explicit_as_of)
    if metric == "dividends":
        return _dividend_fundamental(ticker, requested, explicit_as_of)
    if metric == "shares_outstanding":
        return _shares_outstanding_fundamental(ticker, requested, explicit_as_of)
    if metric in ("balance_sheet", "overview"):
        return _live_only_fundamental(ticker, metric, requested, explicit_as_of)
    return {"error": f"Unknown metric '{metric}'", "error_type": "invalid_tool_arguments"}


def _pit_unavailable(ticker: str, metric: str, requested: _dt.date) -> dict[str, object]:
    return {
        "error": f"No {metric} data knowable as of {requested.isoformat()} for {ticker}",
        "error_type": "pit_data_unavailable",
    }


def _dividend_fundamental(ticker: str, requested: _dt.date, explicit_as_of: bool = False) -> dict[str, object]:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    store_rows: list[FinancialFactRow] = _store_rows(entity_id, _DIVIDEND_CONCEPTS + _SAFETY_CONCEPTS, requested, data_root) if entity_id else []
    payload: dict[str, object] | None = _assemble_dividend_payload(ticker, store_rows, requested) if store_rows else None
    events: list[DividendEventRow] = _store_dividend_events(entity_id, requested, data_root) if entity_id else []
    current = (requested == _today()) and not explicit_as_of
    if payload is None and events:
        payload = {"ticker": ticker, "dividend_status": "unknown",
                   "ttm_dividend_per_share": None, **_dividend_growth({}),
                   "annual_history": [], "source": _DIVIDEND_SOURCE}
    if payload is not None:
        valuation = _dividend_valuation(ticker, payload.get("ttm_dividend_per_share"), include_price=current)
        eps_payload = _assemble_eps_payload(ticker, store_rows)
        eps_ttm_raw = eps_payload.get("ttm_eps_diluted") if eps_payload else None
        eps_ttm = eps_ttm_raw if isinstance(eps_ttm_raw, (int, float)) else None
        safety = _assemble_dividend_safety(
            store_rows, payload,
            ttm_eps_diluted=eps_ttm,
        )
        ttm_raw = payload.get("ttm_dividend_per_share")
        ttm_dps = float(ttm_raw) if isinstance(ttm_raw, (int, float)) else None
        history_raw = payload.get("annual_history")
        annual_history = (
            [h for h in history_raw if isinstance(h, Mapping)]
            if isinstance(history_raw, list) else None
        )
        payload = {**payload, **_dividend_event_payload(
            events, requested,
            growth={"growth_1y": payload.get("growth_1y"), "growth_5y_cagr": payload.get("growth_5y_cagr")},
            ttm_dps=ttm_dps,
            annual_history=annual_history),
                   **valuation, "safety": safety}
        history_count = payload.get("annual_history")
        return _envelope(
            ticker, "dividends", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=len(history_count) if isinstance(history_count, list) else 0,
        )
    if explicit_as_of:
        return _pit_unavailable(ticker, "dividends", requested)
    payload = edgar_client.get_fundamentals(ticker, "dividends", include_dividend_price=current)
    if "error" in payload:
        return payload
    payload = {**payload, **_dividend_event_payload([], requested), "safety": None}
    live_history = payload.get("annual_history")
    return _envelope(
        ticker, "dividends", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=len(live_history) if isinstance(live_history, list) else 0,
    )


def _eps_fundamental(ticker: str, requested: _dt.date, explicit_as_of: bool = False) -> dict[str, object]:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    store_rows: list[FinancialFactRow] = _store_rows(entity_id, _EPS_CONCEPTS, requested, data_root) if entity_id else []
    payload: dict[str, object] | None = _assemble_eps_payload(ticker, store_rows) if store_rows else None
    if payload is not None:
        quarters_count = payload.get("quarterly_eps")
        return _envelope(
            ticker, "eps", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=len(quarters_count) if isinstance(quarters_count, list) else 0,
        )
    if explicit_as_of:
        return _pit_unavailable(ticker, "eps", requested)
    payload = edgar_client.get_fundamentals(ticker, "eps")
    if "error" in payload:
        return payload
    quarters_raw = payload.get("quarterly_eps")
    quarters: list[object] = quarters_raw if isinstance(quarters_raw, list) else []
    return _envelope(
        ticker, "eps", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=len(quarters),
    )


def _shares_outstanding_fundamental(ticker: str, requested: _dt.date, explicit_as_of: bool = False) -> dict[str, object]:
    data_root = DEFAULT_DATA_ROOT
    entity_id = _resolve_entity(ticker, requested, data_root)
    row: Optional[dict[str, object]] = None
    shares_value: float | None = None
    if entity_id:
        clause, param = duckdb.as_of_clause(requested.isoformat())
        rows = duckdb.query(
            "SELECT value, period_end, filed_at, accession, known_at, source_url "
            "FROM financial_facts "
            f"WHERE entity_id = ? AND concept = ? AND {clause} "
            "ORDER BY period_end DESC, filed_at DESC, accession DESC LIMIT 1",
            params=[entity_id, SHARES_OUTSTANDING_CONCEPT, param],
            data_root=data_root,
        )
        if rows:
            candidate_value = rows[0].get("value")
            if isinstance(candidate_value, (int, float)):
                row = rows[0]
                shares_value = float(candidate_value)
    if row is not None:
        payload = {
            "ticker": ticker,
            "shares_outstanding": shares_value,
            "as_of": str(row["period_end"]),
            "source": "SEC EDGAR company facts",
            "note": "SEC-reported shares outstanding, not public float",
            "filed_at": str(row["filed_at"] or ""),
            "accession": row.get("accession"),
            "source_url": row.get("source_url"),
            "known_at": str(row["known_at"] or ""),
        }
        return _envelope(
            ticker, "shares_outstanding", payload,
            data_source="store", as_of_date=requested.isoformat(),
            row_count=1,
        )
    if explicit_as_of:
        return _pit_unavailable(ticker, "shares_outstanding", requested)
    payload = edgar_client.get_fundamentals(ticker, "shares_outstanding")
    if "error" in payload:
        return payload
    return _envelope(
        ticker, "shares_outstanding", payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=1,
    )


def _live_only_fundamental(ticker: str, metric: str, requested: _dt.date, explicit_as_of: bool = False) -> dict[str, object]:
    """balance_sheet/overview are live-only: explicit as_of never gets current data."""
    if explicit_as_of:
        return _pit_unavailable(ticker, metric, requested)
    payload = edgar_client.get_fundamentals(ticker, metric)
    if "error" in payload:
        return payload
    row_count = 1
    if metric == "balance_sheet":
        sheet = payload.get("balance_sheet")
        if isinstance(sheet, dict):
            row_count = len(sheet)
    return _envelope(
        ticker, metric, payload,
        data_source="live", as_of_date=_today().isoformat(),
        requested_as_of=requested.isoformat(),
        row_count=row_count,
    )


def get_xbrl_facts(ticker: str, concept: str) -> dict[str, object]:
    """Always-live XBRL fact search, enveloped (label key source_label)."""
    payload = edgar_client.get_xbrl_facts(ticker, concept)
    if "error" in payload:
        return payload
    matching_raw = payload.get("matching_concepts")
    matching: list[object] = matching_raw if isinstance(matching_raw, list) else []
    count_raw = payload.get("count")
    count: int = count_raw if isinstance(count_raw, int) and count_raw else len(matching)
    return _envelope(
        ticker, "concept", payload,
        data_source="live", as_of_date=_today().isoformat(),
        row_count=count, returned_count=len(matching),
        truncated=count > len(matching),
    )
