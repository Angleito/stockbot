"""Deterministic extraction of obligations from SEC filings for ANY company.

Three generic layers, none company-specific:

* Layer 1 — standardized XBRL concepts (us-gaap tags every US filer uses):
  purchase obligations, lease liabilities, debt, deferred revenue,
  unrecognized tax benefits. This is the universal baseline.
* Layer 2 — generic note-text extractors over the latest 10-Q + 10-K +
  recent 8-K notes: fiscal-year obligation tables (``| 2026 | $4,752 |``),
  sentence amounts near obligation keywords, off-balance-sheet language
  ("not yet commenced", "unconditional purchase obligations").
* Layer 3 — balance-sheet liabilities with status labeling:
  ``on_balance_sheet`` (already accrued/expensed — informational, never
  double-counted in EPS) vs ``future_cash_obligation`` vs ``contingent``.

Every item carries filing + as-of provenance. Categories with no disclosed
amount are reported as absent — never estimated, never borrowed from another
company.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from . import cache
from . import edgar_client

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86400

PARSER_VERSION = "obligations-v2"

# ---------------------------------------------------------------------------
# Layer 1: standardized XBRL obligation concepts
# ---------------------------------------------------------------------------

# concept substring -> normalized obligation kind. Concept names are stable
# us-gaap tags across filers.
_XBRL_OBLIGATION_CONCEPTS: dict[str, str] = {
    "PurchaseObligation": "purchase_commitments",
    "ContractWithCustomerLiability": "deferred_revenue",
    "DeferredRevenue": "deferred_revenue",
    "OperatingLeaseLiability": "operating_leases",
    "FinanceLeaseLiability": "finance_leases",
    "LongTermDebt": "debt",
    "UnrecognizedTaxBenefits": "unrecognized_tax_benefits",
    "LesseeOperatingLeaseLiabilityPaymentsDue": "operating_leases",
    "DebtInstrument": "debt",
}

# Balance-sheet line items to pull from Layer 3.
_BS_LINE_ITEMS = (
    ("accounts_payable", "Accounts payable"),
    ("accrued_liabilities", "Accrued"),
    ("short_term_debt", "Short-term debt"),
    ("long_term_debt", "Long-term debt"),
    ("operating_lease_liabilities", "Operating lease liabilities"),
    ("other_long_term_liabilities", "Other long-term liabilities"),
    ("total_liabilities", "Total liabilities"),
)

# ---------------------------------------------------------------------------
# Layer 2: generic note-text patterns
# ---------------------------------------------------------------------------

_FISCAL_YEAR_TABLE_RE = re.compile(
    r"\| (20\d\d(?:[^|]*)?|Thereafter) \| \$?([\d,]+)(?:\.\d+)? \|",
)

# Debt notes present per-issue rows: | 3.20% Notes Due 2026 | 0.6 | 3.31% |
# 1,000 | 1,000 |. Capture the issue and its carrying amount.
_DEBT_ISSUE_RE = re.compile(
    r"\| ([^|]{0,60}?Due \d{4})[^|]*\|[^|]*\|[^|]*\|\s*\$?([\d,]+)",
)

_AMOUNT_RE = re.compile(
    r"\$([\d.,]+)\s*(billion|million)",
    re.I,
)

# Sentence amounts that are NOT obligations: program capacity ("commercial
# paper program ... $25.0 billion"), fair values, compensation plan limits.
_NON_OBLIGATION_CONTEXT = (
    "program had",
    "program capacity",
    "commercial paper program",
    "fair value",
    "authorized",
    "aggregate amounts authorized",
)

_KIND_KEYWORDS = (
    ("supply", ("manufacturing", "supply", "inventory purchase")),
    ("cloud", ("cloud",)),
    ("vendor", ("vendor",)),
    ("investment", ("investment commitment", "investments to be made")),
    ("facility", ("facility", "data center", "datacenter")),
)

_OFF_BALANCE_SHEET_LANGUAGE = (
    "not yet commenced",
    "not yet on the balance sheet",
    "future lease commencements",
    "off-balance sheet",
    "off–balance sheet",
    "unconditional purchase obligations",
    "undiscounted",
)

_CANCEL_LANGUAGE = (
    "cancellable",
    "cancelable",
    "rescheduled",
    "adjustable",
    "may be reduced",
    "may be terminated",
    "can be terminated",
    "reduced or terminated",
    "in the event of their default",
    "in the event of default",
)

_DEFAULT_TRIGGERED_TYPES = ("8k_guarantees", "facility_lease_guarantees", "guarantees")

_REVENUE_MATCHED_KINDS = ("supply",)

# 8-K material-agreement guarantee language.
_8K_GUARANTEE_RE = re.compile(
    r"(?:cumulatively\s+capped|capped|guarante(?:e|d|es|ing|y))\s*"
    r"(?:at|to|under)?[\s\S]{0,120}?\$([\d.,]+)\s*(billion|million)",
    re.I,
)

_8K_OBLIGATION_KEYWORDS = (
    "residual value",
    "guarant",
    "credit support",
    "payment obligation",
    "direct financial obligation",
    "off-balance sheet",
    "lease",
    "commitment",
)

_NOTE_KEYWORDS = {
    "debt": ("debt",),
    "lease": ("lease",),
    "commitment": ("commitment", "contingenc"),
    "tax": ("tax",),
    "stock": ("stock", "share"),
    "intangible": ("intangible",),
}

# Targeted one-line disclosures: unearned SBC balance and unrecognized tax
# benefits balance. XBRL (Layer 1) also covers both; these patterns confirm
# the balance without dragging in multi-year comparison tables.
_UNEARNED_SBC_RE = re.compile(
    r"unearned stock-based compensation expense was \$([\d.,]+)\s*(billion|million)",
    re.I,
)
_TAX_BENEFITS_RE = re.compile(
    r"unrecognized tax benefits(?: and other income tax positions)?"
    r"(?: related to uncertain tax positions)?"
    r"(?: as of [^,]{0,40}?)?"
    r"(?: were| was| is| are)\s*\$([\d.,]+)\s*(billion|million)",
    re.I,
)

def _no_data(ticker: str, what: str) -> dict:
    return {"error": f"No obligations data for {ticker}: {what}"}


def _billion(value: str, unit: str) -> float:
    amount = float(value.replace(",", ""))
    return amount / 1000.0 if unit.startswith("million") else amount


def _classify(text: str) -> str:
    """Certainty from the filing's own language."""
    lowered = text.lower()
    if "non-cancelable" in lowered:
        return "contractual"
    if any(phrase in lowered for phrase in _CANCEL_LANGUAGE):
        return "contingent"
    return "contractual"


def _excerpt(text: str, start: int, end: int, span: int = 350) -> str:
    return re.sub(
        r"\s+", " ",
        text[max(0, start - span // 2) : min(len(text), end + span)],
    ).strip()


def _amount_kind(context: str) -> str:
    """Kind from the phrase nearest before the amount, anchored on the
    nearest 'commitment' word when present (e.g. 'cloud service agreement
    commitments ... were $30 billion'). Priority: cloud > supply > investment
    > vendor > facility > other."""
    lowered = context.lower()
    # "Investment commitments" contains the anchor itself; give it priority.
    if "investment commitment" in lowered:
        return "investment"
    anchor = lowered.rfind("commitment")
    window = lowered[max(0, anchor - 250) :] if anchor >= 0 else lowered
    for candidate in ("cloud", "supply", "investment", "vendor", "facility"):
        keywords = dict(_KIND_KEYWORDS)[candidate]
        if any(kw in window for kw in keywords):
            return candidate
    for candidate, keywords in _KIND_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return candidate
    return "other"


def _parse_fiscal_year_table(note_text: str, max_rows: int = 12) -> list[dict]:
    """Rows of fiscal-year obligation tables (e.g. lease/purchase schedules)."""
    rows: list[dict] = []
    for match in _FISCAL_YEAR_TABLE_RE.finditer(note_text):
        rows.append(
            {
                "fiscal_year": match.group(1).strip(),
                "amount_millions": float(match.group(2).replace(",", "")),
            }
        )
        if len(rows) >= max_rows:
            break
    return rows


def _parse_sentence_amounts(note_text: str) -> list[dict]:
    """Sentence-level ``$X billion`` disclosures with kind + certainty."""
    rows: list[dict] = []
    for match in _AMOUNT_RE.finditer(note_text):
        context_start = max(0, match.start() - 400)
        context = note_text[context_start : match.end() + 300]
        if any(phrase in context.lower() for phrase in _NON_OBLIGATION_CONTEXT):
            continue
        kind = _amount_kind(context[: match.start() - context_start + 1])
        rows.append(
            {
                "kind": kind,
                "amount_billions": round(_billion(match.group(1), match.group(2)), 3),
                "certainty": _classify(context),
                "off_balance_sheet": any(
                    phrase in context.lower() for phrase in _OFF_BALANCE_SHEET_LANGUAGE
                ),
                "excerpt": _excerpt(note_text, match.start(), match.end()),
            }
        )
    return rows


def _latest_report(ticker: str, form: str):
    """Latest filing object + metadata for one form (10-Q or 10-K)."""
    from edgar import Company

    edgar_client._ensure_init()
    company = Company(ticker)
    filings = company.get_filings(form=[form])
    if not filings:
        return None
    filing = filings[0]
    return filing, filing.obj()


def _xbrl_obligations(ticker: str) -> list[dict]:
    """Layer 1: standardized us-gaap obligation concepts."""
    from edgar import Company

    edgar_client._ensure_init()
    try:
        facts = Company(ticker).get_facts().to_dataframe()
    except Exception as e:
        logger.warning("xbrl obligations failed for %s: %s", ticker, e)
        return []
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for concept in facts["concept"].unique():
        for needle, kind in _XBRL_OBLIGATION_CONCEPTS.items():
            if needle not in concept:
                continue
            sub = facts[facts["concept"] == concept]
            latest = sub.sort_values("period_end").iloc[-1]
            value = float(latest["value"])
            period_end = str(latest["period_end"])
            key = (kind, period_end)
            if key in seen or value == 0:
                continue
            seen.add(key)
            rows.append(
                {
                    "type": kind,
                    "amount_billions": round(value / 1e9, 3),
                    "certainty": "contractual",
                    "status": "on_balance_sheet" if kind in (
                        "debt", "deferred_revenue", "operating_leases",
                        "finance_leases", "unrecognized_tax_benefits",
                    ) else "future_cash_obligation",
                    "revenue_matched": False,
                    "default_triggered": False,
                    "source": f"SEC EDGAR XBRL {concept}",
                    "filed": period_end,
                    "as_of": period_end,
                    "excerpt": f"XBRL fact {concept} = {value:,.0f} as of {period_end}",
                    "concept": concept,
                }
            )
    return rows


def _targeted_balance_rows(title: str, md: str, filing, present_kinds: set) -> list[dict]:
    """One-line balances (unearned SBC, unrecognized tax benefits) that XBRL
    may not tag; first match only, never multi-year comparison lists."""
    lower = title.lower()
    rows: list[dict] = []
    if "stock" in lower or "share" in lower:
        if "unearned_sbc" not in present_kinds:
            m = _UNEARNED_SBC_RE.search(md)
            if m:
                rows.append(
                    {
                        "type": "unearned_sbc",
                        "amount_billions": round(
                            _billion(m.group(1), m.group(2)), 3
                        ),
                        "certainty": "contractual",
                        "status": "on_balance_sheet",
                        "revenue_matched": False,
                        "default_triggered": False,
                        "source": f"SEC EDGAR {filing.filing_date} {title} note",
                        "filed": str(filing.filing_date),
                        "as_of": str(filing.filing_date),
                        "excerpt": _excerpt(md, m.start(), m.end()),
                    }
                )
    if "tax" in lower:
        if "unrecognized_tax_benefits" not in present_kinds:
            m = _TAX_BENEFITS_RE.search(md)
            if m:
                rows.append(
                    {
                        "type": "unrecognized_tax_benefits",
                        "amount_billions": round(
                            _billion(m.group(1), m.group(2)), 3
                        ),
                        "certainty": "contractual",
                        "status": "on_balance_sheet",
                        "revenue_matched": False,
                        "default_triggered": False,
                        "source": f"SEC EDGAR {filing.filing_date} {title} note",
                        "filed": str(filing.filing_date),
                        "as_of": str(filing.filing_date),
                        "excerpt": _excerpt(md, m.start(), m.end()),
                    }
                )
    return rows


def _note_obligations(ticker: str) -> list[dict]:
    """Layer 2: note-text extraction from latest 10-Q and 10-K."""
    rows: list[dict] = []
    for form in ("10-Q", "10-K"):
        found = _latest_report(ticker, form)
        if found is None:
            continue
        filing, doc = found
        notes = getattr(doc, "notes", None)
        if notes is None:
            continue
        notes_md = {}
        for kw, _needles in _NOTE_KEYWORDS.items():
            for note in notes.search(kw)[:2]:
                title = getattr(note, "title", "?")
                if title not in notes_md:
                    notes_md[title] = note.to_markdown()
        for title, md in notes_md.items():
            _collect_note_rows(rows, title, md, filing)
        present_kinds = {r["type"] for r in rows if r.get("filed") == str(filing.filing_date)}
        for title, md in notes_md.items():
            rows.extend(
                _targeted_balance_rows(title, md, filing, present_kinds)
            )
    return rows


def _collect_note_rows(rows: list[dict], title: str, md: str, filing) -> None:
    lower_title = title.lower()
    table = _parse_fiscal_year_table(md)
    sentences = _parse_sentence_amounts(md)
    is_lease = "lease" in lower_title
    is_debt = "debt" in lower_title
    is_commitment = "commitment" in lower_title or "contingenc" in lower_title
    is_tax = "tax" in lower_title
    is_sbc = "stock" in lower_title or "share" in lower_title
    is_intangible = "intangible" in lower_title

    # Debt notes: per-issue table rows (| 3.20% Notes Due 2026 | ... | 1,000 |).
    if is_debt:
        for m in _DEBT_ISSUE_RE.finditer(md):
            amount_m = float(m.group(2).replace(",", ""))
            if amount_m <= 0:
                continue
            rows.append(
                {
                    "type": "debt",
                    "amount_billions": round(amount_m / 1000.0, 3),
                    "fiscal_year": m.group(1),
                    "certainty": "contractual",
                    "status": "on_balance_sheet",
                    "revenue_matched": False,
                    "default_triggered": False,
                    "source": f"SEC EDGAR {filing.filing_date} {title} note table",
                    "filed": str(filing.filing_date),
                    "as_of": str(filing.filing_date),
                    "excerpt": _excerpt(md, m.start(), m.end()),
                }
            )

    if table:
        for row in table:
            if row["amount_millions"] <= 0:
                continue
            if is_lease:
                kind, status = "operating_leases", "on_balance_sheet"
            elif is_debt:
                kind, status = "debt", "on_balance_sheet"
            elif is_commitment:
                kind, status = "purchase_commitments", "future_cash_obligation"
            elif is_intangible:
                kind, status = "intangible_amortization", "on_balance_sheet"
            else:
                continue
            rows.append(
                {
                    "type": kind,
                    "amount_billions": round(row["amount_millions"] / 1000.0, 3),
                    "fiscal_year": row["fiscal_year"],
                    "certainty": _classify(md),
                    "status": status,
                    "revenue_matched": False,
                    "default_triggered": False,
                    "source": f"SEC EDGAR {filing.filing_date} {title} note table",
                    "filed": str(filing.filing_date),
                    "as_of": str(filing.filing_date),
                    "excerpt": _excerpt(md, 0, min(len(md), 120)),
                }
            )
    if sentences:
        for s in sentences:
            if s["amount_billions"] < 0.001:
                continue
            if is_tax or is_sbc or is_intangible:
                # Tax/SBC/intangible balances come from Layer 1 XBRL facts,
                # which are authoritative single values. Note prose carries
                # multi-year comparison tables that create duplicate noise.
                continue
            if is_lease or is_debt or is_commitment:
                kind = s["kind"]
                if kind == "other" or (is_lease and kind == "facility"):
                    if is_lease:
                        kind = "operating_leases"
                    elif is_debt:
                        kind = "debt"
                    elif is_commitment:
                        kind = "purchase_commitments"
                status = (
                    "off_balance_sheet"
                    if s["off_balance_sheet"]
                    else "future_cash_obligation"
                )
                if is_lease and not s["off_balance_sheet"]:
                    status = "on_balance_sheet"
                # Not-yet-commenced leases are conditional (subject to
                # conditions being met / commencement), never "contractual".
                if s["off_balance_sheet"] and is_lease:
                    certainty = "contingent"
                else:
                    certainty = s["certainty"]
            else:
                continue
            rows.append(
                {
                    "type": kind,
                    "amount_billions": s["amount_billions"],
                    "certainty": certainty,
                    "status": status,
                    "revenue_matched": kind in _REVENUE_MATCHED_KINDS,
                    "default_triggered": kind in _DEFAULT_TRIGGERED_TYPES,
                    "source": f"SEC EDGAR {filing.filing_date} {title} note",
                    "filed": str(filing.filing_date),
                    "as_of": str(filing.filing_date),
                    "excerpt": s["excerpt"],
                }
            )


def _balance_sheet_liabilities(ticker: str) -> list[dict]:
    """Layer 3: balance-sheet liabilities with on-balance-sheet status."""
    from edgar import Company

    rows: list[dict] = []
    try:
        found = _latest_report(ticker, "10-Q")
        if found is None:
            found = _latest_report(ticker, "10-K")
        if found is None:
            return rows
        filing, doc = found
        bs = doc.financials.balance_sheet()
        md = bs.to_markdown() if hasattr(bs, "to_markdown") else str(bs)
        for field, label in _BS_LINE_ITEMS:
            match = re.search(
                rf"{re.escape(label)}[^|]*\|\s*\$?([\d,]+)", md
            )
            if not match:
                continue
            rows.append(
                {
                    "type": f"bs_{field}",
                    "amount_billions": round(
                        float(match.group(1).replace(",", "")) / 1000.0, 3
                    ),
                    "certainty": "contractual",
                    "status": "on_balance_sheet",
                    "revenue_matched": False,
                    "default_triggered": False,
                    "source": f"SEC EDGAR {filing.filing_date} balance sheet",
                    "filed": str(filing.filing_date),
                    "as_of": str(filing.filing_date),
                    "excerpt": f"Balance sheet line item: {label}",
                }
            )
    except Exception as e:
        logger.warning("balance sheet obligations failed for %s: %s", ticker, e)
    return rows


def _scan_8k_obligations(ticker: str) -> list[dict]:
    """Recent 8-K material agreements with quantified guarantees."""
    from edgar import Company

    rows: list[dict] = []
    try:
        company = Company(ticker)
        filings = company.get_filings(form=["8-K"])
    except Exception as e:
        logger.warning("8-K scan failed for %s: %s", ticker, e)
        return rows
    for filing in filings[:6]:
        try:
            obj = filing.obj()
            items = getattr(obj, "items", []) or []
            if not any(i in items for i in ("Item 1.01", "Item 2.03", "Item 7.01")):
                continue
            text = str(getattr(obj, "document", ""))
            if not any(kw in text.lower() for kw in _8K_OBLIGATION_KEYWORDS):
                continue
            for m in _8K_GUARANTEE_RE.finditer(text):
                amount_b = _billion(m.group(1), m.group(2))
                if amount_b < 0.1:
                    continue
                rows.append(
                    {
                        "type": "8k_guarantees",
                        "amount_billions": round(amount_b, 3),
                        "certainty": "contingent",
                        "status": "contingent",
                        "revenue_matched": False,
                        "default_triggered": True,
                        "source": f"SEC EDGAR 8-K {filing.filing_date} material agreement",
                        "filed": str(filing.filing_date),
                        "as_of": str(filing.filing_date),
                        "excerpt": _excerpt(text, m.start(), m.end()),
                    }
                )
        except Exception as e:
            logger.warning("8-K %s scan error: %s", filing.accession_no, e)
    return rows


def _known_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _content_hash(row: dict) -> str:
    payload = json.dumps(
        {k: row.get(k) for k in (
            "type", "amount_billions", "filed", "certainty", "status",
            "revenue_matched", "default_triggered", "fiscal_year",
        )},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def get_obligations(ticker: str) -> dict:
    """Return the full obligations picture for ANY ticker (cached 24h)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    key = f"obligations:{ticker}"
    hit = cache.get(key, ttl=CACHE_TTL_SECONDS)
    if hit is not None:
        return hit
    try:
        rows: list[dict] = []
        rows.extend(_xbrl_obligations(ticker))
        rows.extend(_note_obligations(ticker))
        rows.extend(_balance_sheet_liabilities(ticker))
        rows.extend(_scan_8k_obligations(ticker))
    except Exception as e:
        logger.warning("obligations failed for %s: %s", ticker, e)
        return {"error": f"Obligations unavailable for {ticker}: {e}"}

    if not rows:
        return _no_data(ticker, "no quantified obligations found in filings")

    # Dedup: identical (type, amount, filed) rows appear from both the
    # 10-Q and 10-K or from table + sentence paths; drop negatives.
    seen: set[tuple] = set()
    cleaned: list[dict] = []
    for row in rows:
        amount = row.get("amount_billions")
        if amount is None or amount <= 0:
            continue
        dedup_key = (
            row.get("type"),
            round(amount, 2),
            row.get("filed"),
            row.get("fiscal_year"),
        )
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        cleaned.append(row)
    rows = cleaned

    known_at = _known_at()
    for row in rows:
        row["ticker"] = ticker
        row["content_hash"] = _content_hash(row)
        row["known_at"] = known_at
        row["parser_version"] = PARSER_VERSION

    value = {
        "ticker": ticker,
        "as_of": known_at,
        "source": "SEC EDGAR XBRL facts + 10-Q/10-K notes + balance sheet + 8-K material agreements",
        "obligations": rows,
        "note": (
            "Status labels: 'on_balance_sheet' items are already accrued or "
            "expensed (informational; never double-counted in EPS). "
            "'future_cash_obligation' items are disclosed commitments not "
            "yet on the balance sheet. 'off_balance_sheet' items are "
            "disclosed outside the balance sheet (e.g. not-yet-commenced "
            "leases). 'contingent' items depend on counterparty default or "
            "other conditions. Certainty reflects the filing's own "
            "language. No figures are estimated or borrowed across companies."
        ),
    }
    cache.set(key, value)
    _maybe_persist(ticker, rows)
    return value


def _maybe_persist(ticker: str, rows: list[dict]) -> None:
    """Persist fresh obligations into the point-in-time warehouse dataset.

    Runs only when the real cache is in use (tests swap in a FakeCache, in
    which case nothing is written). Idempotent by obligation_id.
    """
    from . import cache as real_cache

    if cache is not real_cache:
        return
    try:
        written = persist_obligations(ticker, rows)
        if written:
            logger.info("persisted %d new obligation rows for %s", written, ticker)
    except Exception as e:
        logger.warning("obligation persistence failed for %s: %s", ticker, e)


def persist_obligations(
    ticker: str, obligations_rows: list[dict], data_root: Optional[str] = None
) -> int:
    """Write obligations into the point-in-time warehouse dataset.

    Returns the number of rows written (0 on deterministic rerun). Each row
    is keyed by (ticker, type, filed, amount, status) so re-runs are idempotent.
    """
    from pathlib import Path

    from .storage import parquet

    root = Path(data_root) if data_root else None

    rows = []
    for row in obligations_rows:
        rows.append(
            {
                "obligation_id": (
                    f"sec:obligation:{ticker}:{row.get('type', '?')}"
                    f":{row.get('filed', '')}:{row.get('amount_billions', '')}"
                    f":{row.get('status', '')}"
                ),
                "ticker": ticker,
                "obligation_type": row.get("type", "other"),
                "amount_billions": row.get("amount_billions"),
                "certainty": row.get("certainty"),
                "status": row.get("status"),
                "revenue_matched": bool(row.get("revenue_matched")),
                "default_triggered": bool(row.get("default_triggered")),
                "fiscal_year": row.get("fiscal_year"),
                "excerpt": row.get("excerpt", ""),
                "source": row.get("source", ""),
                "filed_at": row.get("filed"),
                "as_of": row.get("as_of"),
                "known_at": row.get("known_at"),
                "retrieved_at": row.get("known_at"),
                "content_hash": row.get("content_hash"),
            "parser_version": row.get("parser_version"),
        }
    )
    return parquet.write_rows("company_obligations", rows, root=root)


__all__ = ["get_obligations", "persist_obligations", "_DEFAULT_TRIGGERED_TYPES", "_REVENUE_MATCHED_KINDS"]