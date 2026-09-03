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
from .domain.events import CorporateEvent, Evidence, sec_evidence_id, sec_event_id

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 86400

PARSER_VERSION = "obligations-v2"
_ARCHIVE_KIND = "filing-note-text"

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

DEFAULT_TRIGGERED_TYPES = ("8k_guarantees", "facility_lease_guarantees", "guarantees")

REVENUE_MATCHED_KINDS = ("supply",)

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


def _parse_table_schedule(note_text: str) -> list[dict] | None:
    """Per-year schedule from an explicit fiscal-year table, else None."""
    table = _parse_fiscal_year_table(note_text)
    if len(table) < 2:
        return None
    schedule = []
    for r in table:
        m = re.search(r"20\d\d", str(r["fiscal_year"]))
        if not m:
            continue
        schedule.append({"fiscal_year": m.group(0), "amount_billions": round(r["amount_millions"] / 1000.0, 3)})
    return schedule if len(schedule) >= 2 else None


def _parse_prose_schedule(note_text: str) -> list[dict] | None:
    """Per-year schedule from prose like '$7B, $6B ... paid in FY 2027, 2028 ...'."""
    for sentence in re.split(r"\.\s+", note_text):
        if "will be paid in fiscal year" not in sentence.lower():
            continue
        amounts_part, _, years_part = sentence.partition("will be paid")
        if "for which" in amounts_part:
            amounts_part = amounts_part.split("for which", 1)[1]
        amounts = [round(_billion(a, u), 3) for a, u in _AMOUNT_RE.findall(amounts_part)]
        years = re.findall(r"20\d\d", years_part)
        if len(amounts) >= 2 and len(amounts) == len(years):
            return [{"fiscal_year": y, "amount_billions": a} for y, a in zip(years, amounts)]
    return None


def _parse_front_horizon(note_text: str, amount_billions: float) -> dict | None:
    """Front-loaded horizon when text says substantially all/majority paid through FY."""
    m = re.search(
        r"(substantially all|majority)[^.]{0,120}?paid through fiscal year\s*(20\d\d)",
        note_text, re.I,
    )
    if not m:
        return None
    return {
        "paid_in_remainder_of_fy": m.group(2),
        "paid_in_remainder_billions": round(amount_billions, 3),
        "paid_after_remainder_billions": 0.0,
    }




def _latest_report(ticker: str, form: str):
    """Latest filing object + metadata for one form (10-Q or 10-K)."""
    return edgar_client.get_latest_report(ticker, form)


def _xbrl_obligations(ticker: str) -> list[dict]:
    """Layer 1: standardized us-gaap obligation concepts."""
    try:
        facts = edgar_client.get_company(ticker).get_facts().to_dataframe()
    except Exception as e:
        logger.warning("xbrl obligations failed for %s: %s", ticker, e)
        return []
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # Company facts aggregate every filing; the facts frame exposes no
    # per-fact filing date, so the latest 10-K/10-Q filing date stands in
    # as the row's filed date (never period_end — see persist known_at).
    filing_date: Optional[str] = None
    for form in ("10-K", "10-Q"):
        found = _latest_report(ticker, form)
        if found is not None:
            filing_date = str(found[0].filing_date)
            break
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
                    "filed": filing_date,
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


def _note_obligations(ticker: str, *, archive: bool = False) -> list[dict]:
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
        start = len(rows)
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
        if notes_md:
            _annotate_archive(rows[start:], ticker, filing, "\n\n".join(notes_md.values()), archive=archive)
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
            payment_horizon = None
            schedule = None
            if kind == "cloud":
                prose = _parse_prose_schedule(md)
                if prose is not None:
                    payment_horizon = {"schedule": prose}
            elif kind in ("supply", "investment"):
                payment_horizon = _parse_front_horizon(md, s["amount_billions"])
            elif kind == "operating_leases":
                table_sched = _parse_table_schedule(md)
                if table_sched is not None:
                    total = sum(y["amount_billions"] for y in table_sched)
                    if total and abs(total - s["amount_billions"]) / total < 0.1:
                        schedule = table_sched
            rows.append(
                {
                    "type": kind,
                    "amount_billions": s["amount_billions"],
                    "certainty": certainty,
                    "status": status,
                    "revenue_matched": kind in REVENUE_MATCHED_KINDS,
                    "default_triggered": kind in DEFAULT_TRIGGERED_TYPES,
                    "payment_horizon": payment_horizon,
                    "schedule": schedule,
                    "source": f"SEC EDGAR {filing.filing_date} {title} note",
                    "filed": str(filing.filing_date),
                    "as_of": str(filing.filing_date),
                    "excerpt": s["excerpt"],
                }
            )


def _balance_sheet_liabilities(ticker: str, *, archive: bool = False) -> list[dict]:
    """Layer 3: balance-sheet liabilities with on-balance-sheet status."""
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
        start = len(rows)
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
        _annotate_archive(rows[start:], ticker, filing, md, archive=archive)
    except Exception as e:
        logger.warning("balance sheet obligations failed for %s: %s", ticker, e)
    return rows


def _scan_8k_obligations(ticker: str, *, archive: bool = False) -> list[dict]:
    """Recent 8-K material agreements with quantified guarantees."""
    rows: list[dict] = []
    try:
        company = edgar_client.get_company(ticker)
        filings = company.get_filings(form=["8-K"])
    except Exception as e:
        logger.warning("8-K scan failed for %s: %s", ticker, e)
        return rows
    for filing in filings[:6]:
        start = len(rows)
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
            if len(rows) > start:
                _annotate_archive(rows[start:], ticker, filing, text, archive=archive)
        except Exception as e:
            logger.warning("8-K %s scan error: %s", filing.accession_no, e)
    return rows


def _known_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _archive_filing_text(ticker: str, filing, text: str, *, archive: bool = False) -> Optional[tuple[str, str]]:
    """Archive one distinct report text write-once; returns (key, sha256)."""
    from .storage import raw_archive

    if not archive or not text:
        return None
    payload = text.encode("utf-8")
    sha = raw_archive.content_hash(payload)
    accession = str(getattr(filing, "accession_no", None) or "")
    key = (
        f"filing-text:{ticker}:{getattr(filing, 'filing_date', '')}:"
        f"{accession or sha[:8]}"
    )
    try:
        record = raw_archive.archive(
            "sec", _ARCHIVE_KIND, key, payload,
            url="", retrieved_at=_known_at(),
        )
    except OSError:
        return None
    return record.key, sha


def _annotate_archive(rows: list[dict], ticker: str, filing, text: str, *, archive: bool = False) -> None:
    """Attach the archived report reference to rows produced from a filing."""
    archived = _archive_filing_text(ticker, filing, text, archive=archive)
    if archived is None:
        return
    key, sha = archived
    for row in rows:
        row["_archive_key"] = key
        row["_archive_sha"] = sha
        row["_accession"] = str(getattr(filing, "accession_no", None) or "")


def _content_hash(row: dict) -> str:
    payload = json.dumps(
        {k: row.get(k) for k in (
            "type", "amount_billions", "filed", "certainty", "status",
            "revenue_matched", "default_triggered", "fiscal_year",
        )},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def get_obligations(ticker: str, *, persist: bool = False) -> dict:
    """Return the full obligations picture for ANY ticker (cached 24h)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    key = f"obligations:{ticker}"
    hit = cache.get(key, ttl=CACHE_TTL_SECONDS)
    if hit is not None and not persist:
        return hit
    try:
        rows: list[dict] = []
        rows.extend(_xbrl_obligations(ticker))
        rows.extend(_note_obligations(ticker, archive=persist))
        rows.extend(_balance_sheet_liabilities(ticker, archive=persist))
        rows.extend(_scan_8k_obligations(ticker, archive=persist))
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
    if persist:
        try:
            summary = persist_obligation_events(rows)
            if summary["events_written"]:
                logger.info(
                    "persisted %d obligation events for %s", summary["events_written"], ticker
                )
            if summary["skipped_no_filing_date"]:
                logger.warning(
                    "skipped %d obligation rows without a filing date for %s",
                    summary["skipped_no_filing_date"], ticker,
                )
        except Exception as e:
            logger.warning("obligation persistence failed for %s: %s", ticker, e)
    for row in rows:  # archive annotations are persist-internal, not public
        row.pop("_archive_key", None)
        row.pop("_archive_sha", None)
        row.pop("_accession", None)
    return value


def persist_obligation_events(rows: list[dict], data_root: Optional[str] = None) -> dict:
    """Write obligations rows as CorporateEvent + Evidence rows.

    One source row -> one CorporateEvent plus one Evidence row.  Event
    ``known_at`` is the source filing's ``filed`` date — NEVER the wall clock
    or a period end — so rows without a filing date are skipped and counted
    in ``skipped_no_filing_date``.  Filing-text evidence is anchored to the
    report text archived at fetch time (``raw_archive`` under
    ``filing-text:{ticker}:{filed}:{accession-or-hash}``); XBRL-fact rows
    carry no archive.  ``data_root`` is a research data root (parquet/ +
    raw/ subdirectories; default: the repo data root).

    Returns ``{events_written, evidence_written, skipped_no_filing_date}``;
    a deterministic rerun writes 0 rows (dedup by event/evidence id).
    """
    from dataclasses import asdict
    from datetime import date
    from pathlib import Path

    from .domain.market.ids import sec_entity_id
    from .services.sec_facts import _resolve_entity
    from .storage import duckdb, parquet, raw_archive

    data_root = Path(data_root) if data_root is not None else Path(duckdb.DEFAULT_DATA_ROOT)
    event_rows: list[dict] = []
    evidence_rows: list[dict] = []
    skipped = 0
    for row in rows:
        ticker = str(row.get("ticker") or "").strip().upper()
        filed = str(row.get("filed") or "").strip() or None
        if not filed:
            skipped += 1
            continue
        content_hash = str(row.get("content_hash") or "")
        event_id = sec_event_id(ticker, content_hash)
        entity_id = None
        if ticker:
            accession = str(row.get("_accession") or "")
            m = re.match(r"^(\d{10})-", accession)
            if m:
                entity_id = sec_entity_id(m.group(1))
            else:
                entity_id = _resolve_entity(ticker, date.fromisoformat(filed[:10]), data_root)
        event = CorporateEvent(
            event_id=event_id,
            entity_id=entity_id,
            security_id=None,
            ticker=ticker,
            event_type=row.get("type", "other"),
            amount_billions=row.get("amount_billions"),
            certainty=row.get("certainty"),
            status=row.get("status"),
            revenue_matched=bool(row.get("revenue_matched")),
            default_triggered=bool(row.get("default_triggered")),
            fiscal_year=str(row.get("fiscal_year")) if row.get("fiscal_year") is not None else None,
            filed_at=filed,
            known_at=filed,
            retrieved_at=str(row.get("known_at") or ""),
            accession=str(row.get("_accession") or "") or None,
            source=row.get("source"),
            source_url=None,
            content_hash=content_hash,
            parser_version=row.get("parser_version"),
        )
        event_rows.append(asdict(event))

        is_xbrl_fact = "concept" in row
        archive_key = str(row.get("_archive_key") or "") or None
        archived_sha: Optional[str] = None
        span_start: Optional[int] = None
        span_end: Optional[int] = None
        if archive_key is not None and not is_xbrl_fact:
            record = raw_archive.find(
                "sec", _ARCHIVE_KIND, archive_key, root=data_root / "raw"
            )
            if record is not None:
                archived_sha = record.sha256
                text = record.payload_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                excerpt = str(row.get("excerpt") or "")
                if excerpt:
                    start = text.find(excerpt)
                    if start >= 0:
                        span_start, span_end = start, start + len(excerpt)
        evidence = Evidence(
            evidence_id=sec_evidence_id(event_id, content_hash),
            event_id=event_id,
            source_type="xbrl_fact" if is_xbrl_fact else "filing_text",
            archive_key=archive_key if not is_xbrl_fact else None,
            content_hash=archived_sha,
            excerpt=row.get("excerpt"),
            span_start=span_start,
            span_end=span_end,
            retrieved_at=str(row.get("known_at") or ""),
            parser_version=row.get("parser_version"),
        )
        evidence_rows.append(asdict(evidence))
    return {
        "events_written": parquet.write_rows("events", event_rows, root=data_root / "parquet"),
        "evidence_written": parquet.write_rows("evidence", evidence_rows, root=data_root / "parquet"),
        "skipped_no_filing_date": skipped,
    }


__all__ = ["get_obligations", "persist_obligation_events", "DEFAULT_TRIGGERED_TYPES", "REVENUE_MATCHED_KINDS"]