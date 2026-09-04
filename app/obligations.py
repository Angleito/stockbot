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

PARSER_VERSION = "obligations-v4"
_ARCHIVE_KIND = "filing-note-text"
_PICTURE_SOURCE = "SEC EDGAR XBRL facts + 10-Q/10-K notes + balance sheet + 8-K material agreements"
_PICTURE_NOTE = (
    "Status labels: 'on_balance_sheet' items are already accrued or "
    "expensed (informational; never double-counted in EPS). "
    "'future_cash_obligation' items are disclosed commitments not "
    "yet on the balance sheet. 'off_balance_sheet' items are "
    "disclosed outside the balance sheet (e.g. not-yet-commenced "
    "leases). 'contingent' items depend on counterparty default or "
    "other conditions. Certainty reflects the filing's own "
    "language. No figures are estimated or borrowed across companies. "
    "'unquantified_exposures' are disclosed without a dollar amount "
    "and are excluded from the quantified obligations above. "
    "'capital_allocation' (buybacks, dividends) is discretionary, "
    "not an obligation. 'current_snapshot' holds the latest filing "
    "per obligation type; 'obligations' retains the full history."
)


def _publish_lifecycle(rows: list[dict], bucket: list[dict], capital: list[dict]) -> None:
    """Strip persist-internal keys; publish underscore lifecycle as public."""
    for row in rows + bucket + capital:  # archive annotations are persist-internal, not public
        row.pop("_archive_key", None)
        row.pop("_archive_sha", None)
        row.pop("_accession", None)
        if _snapshot_layer(row) == "8k":
            row["lifecycle_event"] = row.pop("_lifecycle_event", None)
            row.setdefault("agreement_key", None)
        else:
            row.pop("_lifecycle_event", None)
            row["agreement_key"] = None
            row["lifecycle_event"] = None
            row["lifecycle_status"] = None

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

# Disclosed-but-unquantified exposures: sentences invoking obligation
# language with no dollar amount attached. Structured (never estimated)
# so the agent sees what the quantified totals exclude.
_UNQUANTIFIED_RE = re.compile(
    r"([^.\n]{0,160}(indemnif\w*|guarant\w*|share\s+repurchase|buyback|dividend)[^.:\n]{0,160})",
    re.I,
)
_UNQUANTIFIED_KIND = (
    ("indemnif", "indemnities"),
    ("guarant", "guarantees"),
    ("repurchase", "buybacks"),
    ("buyback", "buybacks"),
    ("dividend", "dividends"),
)


def _row_trigger(row: dict) -> Optional[str]:
    """Trigger/condition from the filing's own classifier flags, never inferred."""
    if bool(row.get("default_triggered")) or row.get("type") in DEFAULT_TRIGGERED_TYPES:
        return "counterparty_default"
    if row.get("certainty") == "contingent" or row.get("status") == "contingent":
        return "conditional"
    return None


def _scan_unquantified_exposures(title: str, md: str, filing, limit: int = 3) -> tuple[list[dict], list[dict]]:
    """Sentences disclosing an exposure with no dollar amount.

    Returns ``(exposures, capital_allocation)``: buyback/dividend sentences
    are discretionary capital returns, never obligations. Triggers come from
    the sentence's own words, never inferred (``unknown`` when unstated).

    ponytail: first-wins capped scan; quantified ($/million/billion)
    sentences belong to the numeric rows, never here.
    """
    exposures: list[dict] = []
    capital: list[dict] = []
    for m in _UNQUANTIFIED_RE.finditer(md):
        sentence = re.sub(r"\s+", " ", m.group(1)).strip()
        if not sentence:
            continue
        low = sentence.lower()
        if "$" in sentence or "million" in low or "billion" in low:
            continue
        kind = "other_commitments"
        for needle, name in _UNQUANTIFIED_KIND:
            if needle in low:
                kind = name
                break
        entry = {
            "type": kind,
            "source": f"SEC EDGAR {filing.filing_date} {title} note",
            "filed": str(filing.filing_date),
            "excerpt": sentence,
        }
        if kind in ("buybacks", "dividends"):
            entry["trigger"] = "board_discretion"
            entry["reason"] = "discretionary capital return; not an obligation"
            capital.append(entry)
        else:
            if any(k in low for k in ("default", "insolven", "fail to pay", "counterparty")):
                entry["trigger"] = "counterparty_default"
            elif any(k in low for k in ("condition", "contingen", "subject to")):
                entry["trigger"] = "conditional"
            else:
                entry["trigger"] = "unknown"
            entry["reason"] = "disclosed without a dollar amount; excluded from quantified obligations"
            exposures.append(entry)
        if len(exposures) + len(capital) >= limit:
            break
    return exposures, capital

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

# 8-K lifecycle language: termination/amendment near agreement/guarantee
# words marks the row's lifecycle event; _resolve_8k_lifecycle stamps status.
_8K_TERMINATION_RE = re.compile(r"terminat\w*", re.I)
_8K_AMENDMENT_RE = re.compile(r"amend\w*", re.I)
_8K_AGREEMENT_RE = re.compile(r"agreement|guarant\w*", re.I)
# Agreement identity: normalized counterparty phrase + agreement-type token.
# Counterparty is the capitalized entity after with/for/in-favor-of (or the
# "Agreement X" label); agreement type reuses the _8K_AGREEMENT_RE vocabulary.
# ponytail: heuristic text match, fail-open (None = unlinkable, never marked).
_8K_COUNTERPARTY_RE = re.compile(
    r"(?:with|for|in\s+favor\s+of|issued\s+to|between|by|counterparty)\s+"
    r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})"
)
_8K_AGREEMENT_LABEL_RE = re.compile(r"[Aa]greement\s+([A-Z0-9][\w-]*)")
_8K_AGREEMENT_TYPE_RE = re.compile(r"guarant\w*\s+agreement|guarant\w*|agreement", re.I)

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

def _manifest_entry(form, filing, sections, q_count, u_count, status, warning=None) -> dict:
    """One per-filing scan record: what was examined, what it yielded."""
    return {
        "form": form,
        "filing_date": str(getattr(filing, "filing_date", "") or "") or None,
        "accession": str(getattr(filing, "accession_no", "") or "") or None,
        "status": status,
        "sections_examined": list(sections),
        "quantified_count": q_count,
        "unquantified_count": u_count,
        "warning": warning,
    }


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
        if m:
            schedule.append({"fiscal_year": m.group(0), "amount_billions": round(r["amount_millions"] / 1000.0, 3)})
        elif str(r["fiscal_year"]).strip().lower() == "thereafter":
            schedule.append({"fiscal_year": "Thereafter", "amount_billions": round(r["amount_millions"] / 1000.0, 3)})
    return schedule if len(schedule) >= 2 else None


def _parse_prose_schedule(note_text: str, amount_billions: float | None = None) -> list[dict] | None:
    """Per-year schedule from prose like '$7B, $6B ... paid in FY 2027, 2028 ...'."""
    for sentence in re.split(r"\.\s+", note_text):
        if "will be paid in fiscal year" not in sentence.lower():
            continue
        amounts_part, _, years_part = sentence.partition("will be paid")
        if "for which" in amounts_part:
            amounts_part = amounts_part.split("for which", 1)[1]
        amounts = [round(_billion(a, u), 3) for a, u in _AMOUNT_RE.findall(amounts_part)]
        years = re.findall(r"20\d\d", years_part)
        sched = None
        if len(amounts) >= 2 and len(amounts) == len(years):
            sched = [{"fiscal_year": y, "amount_billions": a} for y, a in zip(years, amounts)]
        elif len(amounts) >= 2 and len(amounts) == len(years) + 1 and "thereafter" in years_part.lower():
            sched = [{"fiscal_year": y, "amount_billions": a} for y, a in zip(years, amounts)]
            sched.append({"fiscal_year": "Thereafter", "amount_billions": amounts[-1]})
        if sched is None:
            continue
        if amount_billions is None:
            return sched
        total = sum(y["amount_billions"] for y in sched)
        if total and abs(total - amount_billions) / total < 0.1:
            return sched
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


def _xbrl_store_facts(ticker: str) -> list[dict]:
    """Normalized-store XBRL facts for obligation concepts (PIT provenance).

    Returns raw ``financial_facts`` rows (concept/value/period/filed_at/
    accession/known_at); empty when the store has nothing (ingestion gap).
    Separated for testability; raises only on unexpected store errors.
    """
    from datetime import date

    from .services import sec_facts
    from .storage import duckdb

    today = date.today()
    entity_id = sec_facts._resolve_entity(ticker, today, sec_facts.DEFAULT_DATA_ROOT)
    if not entity_id:
        return []
    needles = tuple(_XBRL_OBLIGATION_CONCEPTS)
    like = " OR ".join(["concept LIKE ?"] * len(needles))
    clause, param = duckdb.as_of_clause(today.isoformat())
    return duckdb.query(
        "SELECT concept, value, period_start, period_end, fiscal_year, "
        "fiscal_period, filed_at, accession, known_at, source_url "
        "FROM financial_facts "
        f"WHERE entity_id = ? AND ({like}) AND {clause} "
        "ORDER BY period_end, filed_at, accession",
        params=[entity_id, *[f"%{n}%" for n in needles], param],
        data_root=sec_facts.DEFAULT_DATA_ROOT,
    )


def _xbrl_obligations(ticker: str, *, manifest: list | None = None) -> list[dict]:
    """Layer 1: standardized us-gaap obligation concepts.

    Store-first: each concept resolves through the normalized
    ``financial_facts`` store so rows carry their own fact provenance
    (``filed=filed_at``, ``known_at``, ``accession``, ``as_of=period_end``).
    Restatements resolve by latest ``(filed_at, accession)``. The live
    Company Facts read is fallback only for concepts the store lacks, and
    those rows stash a proxied-provenance warning for coverage.
    """
    try:
        store_facts = _xbrl_store_facts(ticker)
    except Exception as e:
        logger.warning("xbrl store read failed for %s: %s", ticker, e)
        store_facts = []
    store_by_kind: dict[str, dict] = {}
    for needle, kind in _XBRL_OBLIGATION_CONCEPTS.items():
        cands = [f for f in store_facts if needle in str(f.get("concept") or "")]
        if not cands:
            continue
        pick = max(
            cands,
            key=lambda f: (
                str(f.get("period_end") or ""),
                str(f.get("filed_at") or ""),
                str(f.get("accession") or ""),
            ),
        )
        if float(pick.get("value") or 0) == 0:
            continue
        prev = store_by_kind.get(kind)
        if prev is None or (
            str(pick.get("period_end") or ""),
            str(pick.get("filed_at") or ""),
            str(pick.get("accession") or ""),
        ) > (
            str(prev.get("period_end") or ""),
            str(prev.get("filed_at") or ""),
            str(prev.get("accession") or ""),
        ):
            store_by_kind[kind] = pick
    try:
        facts = edgar_client.get_company(ticker).get_facts().to_dataframe()
    except Exception as e:
        logger.warning("xbrl obligations failed for %s: %s", ticker, e)
        facts = None
        if not store_by_kind and manifest is not None:
            manifest.append(_manifest_entry("XBRL", None, ["xbrl_facts"], 0, 0, "failed", str(e)))
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, fact in store_by_kind.items():
        concept = str(fact.get("concept"))
        value = float(fact.get("value"))
        period_end = str(fact.get("period_end"))
        key = (kind, period_end)
        if key in seen:
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
                "filed": str(fact.get("filed_at")),
                "known_at": str(fact.get("known_at")),
                "as_of": period_end,
                "excerpt": f"XBRL fact {concept} = {value:,.0f} as of {period_end}",
                "concept": concept,
                "_accession": str(fact.get("accession") or ""),
            }
        )
    if facts is not None:
        # Company facts aggregate every filing; the facts frame exposes no
        # per-fact filing date, so the latest 10-K/10-Q filing date stands in
        # as the row's filed date (never period_end — see persist known_at).
        filing_date: Optional[str] = None
        proxy_form = "XBRL"
        proxy_filing = None
        for form in ("10-K", "10-Q"):
            found = _latest_report(ticker, form)
            if found is not None:
                proxy_filing = found[0]
                filing_date = str(proxy_filing.filing_date)
                proxy_form = form
                break
        for concept in facts["concept"].unique():
            for needle, kind in _XBRL_OBLIGATION_CONCEPTS.items():
                if needle not in concept:
                    continue
                if kind in store_by_kind:
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
                        "provenance": "proxied",
                        "_coverage_warning": (
                            f"XBRL provenance is proxied for {concept}: store has no "
                            f"rows, filed date is the latest {proxy_form} proxy"
                        ),
                    }
                )
        if manifest is not None:
            manifest.append(_manifest_entry(proxy_form, proxy_filing, ["xbrl_facts"], len(rows), 0, "scanned"))
    elif store_by_kind and manifest is not None:
        manifest.append(_manifest_entry("XBRL", None, ["xbrl_facts"], len(rows), 0, "scanned"))
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


def _note_obligations(ticker: str, *, archive: bool = False, manifest: list | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Layer 2: note-text extraction from latest 10-Q and 10-K."""
    rows: list[dict] = []
    unquantified: list[dict] = []
    capital: list[dict] = []
    for form in ("10-Q", "10-K"):
        found = _latest_report(ticker, form)
        if found is None:
            continue
        filing, doc = found
        notes = getattr(doc, "notes", None)
        if notes is None:
            if manifest is not None:
                manifest.append(_manifest_entry(form, filing, [], 0, 0, "parser_warning", "filing notes unavailable"))
            continue
        start, u_start, c_start = len(rows), len(unquantified), len(capital)
        notes_md = {}
        for kw, _needles in _NOTE_KEYWORDS.items():
            for note in notes.search(kw)[:2]:
                title = getattr(note, "title", "?")
                if title not in notes_md:
                    notes_md[title] = note.to_markdown()
        for title, md in notes_md.items():
            _collect_note_rows(rows, title, md, filing)
            exps, caps = _scan_unquantified_exposures(title, md, filing)
            unquantified.extend(exps)
            capital.extend(caps)
        present_kinds = {r["type"] for r in rows if r.get("filed") == str(filing.filing_date)}
        for title, md in notes_md.items():
            rows.extend(
                _targeted_balance_rows(title, md, filing, present_kinds)
            )
        joined = "\n\n".join(notes_md.values())
        if notes_md:
            _annotate_archive(rows[start:], ticker, filing, joined, archive=archive)
        _annotate_archive(unquantified[u_start:], ticker, filing, joined, archive=archive)
        _annotate_archive(capital[c_start:], ticker, filing, joined, archive=archive)
        if manifest is not None:
            manifest.append(_manifest_entry(
                form, filing, sorted(notes_md),
                len(rows) - start, len(unquantified) - u_start, "scanned",
            ))
    return rows, unquantified, capital

def _collect_note_rows(rows: list[dict], title: str, md: str, filing) -> None:
    start = len(rows)
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
                # ponytail: prose/table schedule match is heuristic; attach only on
                # 10%-total reconciliation, else keep the front-loaded horizon.
                sched = _parse_prose_schedule(md, s["amount_billions"])
                if sched is None:
                    table_sched = _parse_table_schedule(md)
                    if table_sched is not None:
                        total = sum(y["amount_billions"] for y in table_sched)
                        if total and abs(total - s["amount_billions"]) / total < 0.1:
                            sched = table_sched
                if sched is not None:
                    schedule = sched
                else:
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
    _reconcile_schedule_components(rows, start)


def _is_fiscal_component_row(row: dict) -> bool:
    """Fiscal-year table rows (never debt per-issue rows like 'Notes Due 2026')."""
    if not str(row.get("source") or "").endswith("note table"):
        return False
    fy = str(row.get("fiscal_year") or "")
    return "Due" not in fy and ("20" in fy or "hereafter" in fy.lower())


def _reconcile_schedule_components(rows: list[dict], start: int) -> None:
    """Flag fiscal-year table rows that break down a headline sentence amount.

    Per filing-note window: when the table total (incl. Thereafter) is within
    the 10% reconciliation tolerance of a headline amount, the headline keeps
    the amount (plus the schedule) and each table row becomes a
    ``schedule_component`` of that headline kind. Closest headline wins; a
    multi-headline match stashes an ambiguity warning for coverage.
    """
    new = rows[start:]
    table_rows = [r for r in new if _is_fiscal_component_row(r)]
    headlines = [
        r for r in new
        if str(r.get("source") or "").endswith(" note") and (r.get("amount_billions") or 0) > 0
    ]
    if not table_rows or not headlines:
        return
    total = sum(r.get("amount_billions") or 0 for r in table_rows)
    if not total:
        return
    matches = [
        h for h in headlines
        if abs(total - (h.get("amount_billions") or 0)) / total < 0.1
    ]
    if not matches:
        return
    best = min(matches, key=lambda h: abs(total - (h.get("amount_billions") or 0)))
    if best.get("schedule") is None:
        best["schedule"] = [
            {"fiscal_year": r.get("fiscal_year"), "amount_billions": r.get("amount_billions")}
            for r in table_rows
        ]
    for r in table_rows:
        r["schedule_component"] = True
        r["headline_type"] = best.get("type")
    if len(matches) > 1:
        best["_reconciliation_warning"] = (
            f"ambiguous schedule reconciliation: table total {round(total, 3)}B matches "
            f"{len(matches)} headlines; attached to closest "
            f"({best.get('type')} {best.get('amount_billions')}B)"
        )


def _balance_sheet_liabilities(ticker: str, *, archive: bool = False, manifest: list | None = None) -> list[dict]:
    """Layer 3: balance-sheet liabilities with on-balance-sheet status."""
    rows: list[dict] = []
    form: str | None = None
    filing = None
    try:
        found = _latest_report(ticker, "10-Q")
        form = "10-Q"
        if found is None:
            found = _latest_report(ticker, "10-K")
            form = "10-K"
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
        if manifest is not None:
            manifest.append(_manifest_entry(form, filing, ["balance_sheet"], len(rows) - start, 0, "scanned"))
    except Exception as e:
        logger.warning("balance sheet obligations failed for %s: %s", ticker, e)
        if manifest is not None:
            manifest.append(_manifest_entry(form or "10-Q/10-K", filing, ["balance_sheet"], 0, 0, "failed", str(e)))
    return rows


def _agreement_key(window: str) -> str | None:
    """Agreement identity for one 8-K guarantee window (fail-open).

    Normalized counterparty phrase + agreement-type token; ``None`` when no
    identity is extractable. A label or usable counterparty is required —
    a bare agreement-type phrase never links. Unlinkable rows are never
    marked by others.
    """
    type_m = _8K_AGREEMENT_TYPE_RE.search(window)
    if not type_m:
        return None
    type_norm = re.sub(r"\s+", " ", type_m.group(0)).strip().casefold()
    label_m = _8K_AGREEMENT_LABEL_RE.search(window)
    cp_m = _8K_COUNTERPARTY_RE.search(window)
    if label_m:
        cp_norm = re.sub(r"\s+", " ", label_m.group(1)).strip().casefold()
        return f"{cp_norm}||{type_norm}"
    if cp_m:
        cp_norm = re.sub(r"\s+", " ", cp_m.group(1)).strip().casefold().rstrip(".,;:")
        # "the Agreements" / bare plurals are not counterparties.
        if cp_norm not in ("agreement", "agreements", "company", "item"):
            return f"{cp_norm}||{type_norm}"
    return None


def _scan_8k_obligations(ticker: str, *, archive: bool = False, manifest: list | None = None) -> list[dict]:
    """Recent 8-K material agreements with quantified guarantees."""
    rows: list[dict] = []
    try:
        company = edgar_client.get_company(ticker)
        filings = company.get_filings(form=["8-K"])
    except Exception as e:
        logger.warning("8-K scan failed for %s: %s", ticker, e)
        if manifest is not None:
            manifest.append(_manifest_entry("8-K", None, [], 0, 0, "failed", str(e)))
        return rows
    for filing in filings[:6]:
        start = len(rows)
        sections: list = []
        try:
            obj = filing.obj()
            items = getattr(obj, "items", []) or []
            sections = [str(i) for i in items]
            if not any(i in items for i in ("Item 1.01", "Item 1.02", "Item 2.03", "Item 7.01")):
                if manifest is not None:
                    manifest.append(_manifest_entry("8-K", filing, sections, 0, 0, "scanned"))
                continue
            text = str(getattr(obj, "document", ""))
            if not any(kw in text.lower() for kw in _8K_OBLIGATION_KEYWORDS):
                if manifest is not None:
                    manifest.append(_manifest_entry("8-K", filing, sections, 0, 0, "scanned"))
                continue
            quantified_windows: list[tuple[int, int]] = []
            for m in _8K_GUARANTEE_RE.finditer(text):
                amount_b = _billion(m.group(1), m.group(2))
                if amount_b < 0.1:
                    continue
                window = text[max(0, m.start() - 500):m.end() + 500]
                lifecycle_event = None
                if _8K_TERMINATION_RE.search(window) and _8K_AGREEMENT_RE.search(window):
                    lifecycle_event = "termination"
                elif _8K_AMENDMENT_RE.search(window) and _8K_AGREEMENT_RE.search(window):
                    lifecycle_event = "amendment"
                quantified_windows.append((m.start() - 500, m.end() + 500))
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
                        "_lifecycle_event": lifecycle_event,
                        "agreement_key": _agreement_key(window),
                    }
                )
            # Lifecycle-only mentions (e.g. amount-less Item 1.02 termination):
            # one row per trigger site outside any quantified window.
            lifecycle_windows: list[tuple[int, int]] = []
            for trig, event in (
                [(t, "termination") for t in _8K_TERMINATION_RE.finditer(text)]
                + [(a, "amendment") for a in _8K_AMENDMENT_RE.finditer(text)]
            ):
                pos = trig.start()
                if any(lo <= pos <= hi for lo, hi in quantified_windows):
                    continue
                if any(lo <= pos <= hi for lo, hi in lifecycle_windows):
                    continue
                window = text[max(0, trig.start() - 500):trig.end() + 500]
                if not _8K_AGREEMENT_RE.search(window):
                    continue
                lifecycle_windows.append((trig.start() - 500, trig.end() + 500))
                rows.append(
                    {
                        "type": "8k_guarantees",
                        "amount_billions": None,
                        "certainty": "contingent",
                        "status": "contingent",
                        "revenue_matched": False,
                        "default_triggered": True,
                        "source": f"SEC EDGAR 8-K {filing.filing_date} material agreement",
                        "filed": str(filing.filing_date),
                        "as_of": str(filing.filing_date),
                        "excerpt": _excerpt(text, trig.start(), trig.end()),
                        "_lifecycle_event": event,
                        "agreement_key": _agreement_key(window),
                    }
                )
            if len(rows) > start:
                _annotate_archive(rows[start:], ticker, filing, text, archive=archive)
            if manifest is not None:
                manifest.append(_manifest_entry("8-K", filing, sections, len(rows) - start, 0, "scanned"))
        except Exception as e:
            logger.warning("8-K %s scan error: %s", filing.accession_no, e)
            if manifest is not None:
                manifest.append(_manifest_entry("8-K", filing, sections, len(rows) - start, 0, "failed", str(e)))
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
    # ponytail: accession is filing metadata, set on every path (not only persist)
    for row in rows:
        row["_accession"] = str(getattr(filing, "accession_no", None) or "")
    archived = _archive_filing_text(ticker, filing, text, archive=archive)
    if archived is None:
        return
    key, sha = archived
    for row in rows:
        row["_archive_key"] = key
        row["_archive_sha"] = sha


def _normalize_excerpt(text) -> str:
    """Whitespace-collapsed, case-folded excerpt for identity (never display)."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _content_hash(row: dict) -> str:
    sched = row.get("schedule") or (row.get("payment_horizon") or {}).get("schedule") or []
    try:
        norm = sorted(
            [[str(y.get("fiscal_year")), float(y.get("amount_billions") or 0)] for y in sched],
            key=lambda p: (p[0], p[1]),
        )
    except Exception:
        norm = []
    payload: dict = {
        **{k: row.get(k) for k in (
            "type", "amount_billions", "filed", "certainty", "status",
            "revenue_matched", "default_triggered", "fiscal_year",
        )},
        "schedule": norm,
    }
    horizon = row.get("payment_horizon")
    if isinstance(horizon, dict) and any(
        horizon.get(k) is not None
        for k in ("schedule", "paid_in_remainder_of_fy",
                  "paid_in_remainder_billions", "paid_after_remainder_billions")
    ):
        # ponytail: conditional key — rows without timing keep byte-identical
        # payloads (no quantified id churn); a 95/24 correction retunes identity.
        try:
            tnorm = sorted(
                [[str(y.get("fiscal_year")), float(y.get("amount_billions") or 0)]
                 for y in (horizon.get("schedule") or [])],
                key=lambda p: (p[0], p[1]),
            )
        except Exception:
            tnorm = []
        payload["timing"] = {
            "schedule": tnorm,
            "remainder_fy": horizon.get("paid_in_remainder_of_fy"),
            "remainder_b": horizon.get("paid_in_remainder_billions"),
            "after_b": horizon.get("paid_after_remainder_billions"),
        }
    if row.get("amount_billions") is None:
        # Unquantified identity is evidence identity: distinct excerpts in one
        # filing yield distinct rows; byte-identical repeats still collapse.
        payload["accession"] = str(row.get("accession") or row.get("_accession") or "")
        payload["trigger"] = row.get("trigger")
        payload["excerpt"] = _normalize_excerpt(row.get("excerpt"))
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _snapshot_layer(row: dict) -> str:
    if "concept" in row:
        return "xbrl"
    kind = str(row.get("type") or "")
    source = str(row.get("source") or "")
    if kind.startswith("8k_") or "8-K" in source:
        return "8k"
    if "balance sheet" in source.lower():
        return "balance"
    return "note"


def _resolve_8k_lifecycle(rows: list[dict], filings=None) -> list[str]:
    """Stamp 8-K guarantee lifecycle status; the ledger keeps every event.

    Amendment/termination marks apply only within the same ``agreement_key``
    group. A termination zeroes every earlier same-key row (``unknown``,
    own row ``terminated``). A quantified amendment supersedes earlier
    quantified rows (``unknown``). An amount-less amendment retains the last
    quantified exposure (earlier quantified rows become ``amended`` — still
    summed in the snapshot) until a new amount or termination supersedes it.
    Rows with no extractable key (``None``) are unlinkable — their marks
    affect nothing and they are never marked by others — so they stay
    additive with a coverage warning. Fail-open, never silent summation.
    """
    guarantees = [
        r for r in rows
        if _snapshot_layer(r) == "8k" and (
            (r.get("amount_billions") or 0) > 0
            or (r.get("amount_billions") is None and r.get("_lifecycle_event") in ("amendment", "termination"))
        )
    ]
    if not guarantees:
        return []
    groups: dict = {}
    for r in guarantees:
        groups.setdefault(r.get("agreement_key"), []).append(r)
    for key, members in groups.items():
        if key is None:
            for r in members:
                if r.get("_lifecycle_event") == "termination":
                    r["lifecycle_status"] = "terminated"
                else:
                    r.pop("lifecycle_status", None)
            continue
        term_marks = sorted(
            str(r.get("filed") or "")
            for r in members if r.get("_lifecycle_event") == "termination"
        )
        quant_amend_marks = sorted(
            str(r.get("filed") or "")
            for r in members
            if r.get("_lifecycle_event") == "amendment" and (r.get("amount_billions") or 0) > 0
        )
        amountless_amend_marks = sorted(
            str(r.get("filed") or "")
            for r in members
            if r.get("_lifecycle_event") == "amendment" and r.get("amount_billions") is None
        )
        for r in members:
            if r.get("_lifecycle_event") == "termination":
                r["lifecycle_status"] = "terminated"
            elif r.get("amount_billions") is None:
                r.pop("lifecycle_status", None)
            else:
                filed = str(r.get("filed") or "")
                if any(m > filed for m in term_marks):
                    r["lifecycle_status"] = "unknown"
                elif any(m > filed for m in quant_amend_marks):
                    r["lifecycle_status"] = "unknown"
                elif any(m > filed for m in amountless_amend_marks):
                    r["lifecycle_status"] = "amended"
                else:
                    r.pop("lifecycle_status", None)
    warnings: list[str] = []
    unresolved = [r for r in guarantees if "lifecycle_status" not in r and (r.get("amount_billions") or 0) > 0]
    if len(unresolved) > 1:
        warnings.append(
            f"{len(unresolved)} unresolved 8-K guarantees are summed without "
            "lifecycle resolution"
        )
    seen_retained: set[tuple] = set()
    for key, members in groups.items():
        if key is None:
            continue
        if not any((m.get("amount_billions") or 0) > 0 for m in members):
            continue
        for m in members:
            if m.get("amount_billions") is None and m.get("_lifecycle_event") == "amendment":
                rk = (m.get("agreement_key"), m.get("filed"))
                if rk in seen_retained:
                    continue
                seen_retained.add(rk)
                warnings.append(
                    "A later amendment was found but did not disclose a replacement amount. "
                    "The last quantified exposure is retained for downside analysis until "
                    "superseded by a new amount or termination."
                )
    seen_stale_term: set[tuple] = set()
    for key, members in groups.items():
        if key is None:
            continue
        terms = [m for m in members if m.get("_lifecycle_event") == "termination"]
        if not terms:
            continue
        for t in terms:
            t_filed = str(t.get("filed") or "")
            prior_quant = [
                str(m.get("filed") or "") for m in members
                if (m.get("amount_billions") or 0) > 0 and str(m.get("filed") or "") < t_filed
            ]
            if not prior_quant:
                continue
            last_quant = max(prior_quant)
            intervening = [
                m for m in members
                if last_quant < str(m.get("filed") or "") < t_filed
                and m.get("_lifecycle_event") in ("amendment", "termination")
            ]
            if not intervening:
                continue
            latest = max(intervening, key=lambda m: str(m.get("filed") or ""))
            if latest.get("_lifecycle_event") == "amendment" and latest.get("amount_billions") is None:
                sk = (key, t_filed)
                if sk in seen_stale_term:
                    continue
                seen_stale_term.add(sk)
                warnings.append(
                    "Agreement terminated after an amendment that disclosed no replacement amount; "
                    "canceled amount unknown, defaulting to zero until further news."
                )
    seen_dangling: set[tuple] = set()
    for key, members in groups.items():
        if any((m.get("amount_billions") or 0) > 0 for m in members):
            continue
        for m in members:
            if m.get("amount_billions") is None and m.get("_lifecycle_event") in ("amendment", "termination"):
                dk = (m.get("agreement_key"), m.get("_lifecycle_event"), m.get("filed"))
                if dk in seen_dangling:
                    continue
                seen_dangling.add(dk)
                warnings.append(
                    f"8-K {m.get('_lifecycle_event')} on {m.get('filed')} matches no known "
                    "agreement and was recorded without effect"
                )
    return warnings


def _current_snapshot(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Latest filing per (type, layer); 8-K events are additive, never supersede.

    Rows sharing (type, layer, filed) are all kept (same-filing schedule
    siblings); only strictly older filings are superseded. Rows without a
    filing date stay in the ledger but are excluded here, with a warning.
    Reconciled ``schedule_component`` rows and lifecycle-excluded
    (terminated/unknown) 8-K rows never enter the snapshot.
    Snapshot rows are references to the already-stamped ledger rows.
    Returns (snapshot, warnings).
    """
    best: dict[tuple, str] = {}
    for row in rows:
        if _snapshot_layer(row) == "8k":
            continue
        filed = str(row.get("filed") or "").strip()
        if not filed:
            continue
        key = (row.get("type"), _snapshot_layer(row))
        if key not in best or filed > best[key]:
            best[key] = filed
    snapshot: list[dict] = []
    warnings: list[str] = _resolve_8k_lifecycle(rows)
    warned: set = set()
    for row in rows:
        if row.get("schedule_component"):
            continue
        layer = _snapshot_layer(row)
        if layer == "8k":
            if row.get("amount_billions") is None:
                continue
            if row.get("lifecycle_status") in ("terminated", "unknown"):
                continue
            snapshot.append(row)
            continue
        filed = str(row.get("filed") or "").strip()
        if not filed:
            if row.get("type") not in warned:
                warned.add(row.get("type"))
                warnings.append(
                    f"excluded from current snapshot (no filing date): "
                    f"{row.get('type')} {row.get('amount_billions')}B"
                )
            continue
        if filed == best.get((row.get("type"), layer)):
            snapshot.append(row)
    return snapshot, warnings


def get_obligations(ticker: str, *, persist: bool = False) -> dict:
    """Return the full obligations picture for ANY ticker (cached 24h)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    key = f"obligations:{ticker}"
    hit = cache.get(key, ttl=CACHE_TTL_SECONDS)
    if hit is not None and not persist:
        return hit
    manifest: list[dict] = []
    try:
        rows: list[dict] = []
        rows.extend(_xbrl_obligations(ticker, manifest=manifest))
        note_rows, unquantified, capital_raw = _note_obligations(ticker, archive=persist, manifest=manifest)
        rows.extend(note_rows)
        rows.extend(_balance_sheet_liabilities(ticker, archive=persist, manifest=manifest))
        rows.extend(_scan_8k_obligations(ticker, archive=persist, manifest=manifest))
    except Exception as e:
        logger.warning("obligations failed for %s: %s", ticker, e)
        return {"error": f"Obligations unavailable for {ticker}: {e}"}

    # Dedup: identical (type, amount, filed) rows appear from both the
    # 10-Q and 10-K or from table + sentence paths; drop negatives.
    seen: set[tuple] = set()
    cleaned: list[dict] = []
    for row in rows:
        amount = row.get("amount_billions")
        if amount is None and row.get("_lifecycle_event") in ("amendment", "termination"):
            dedup_key = (
                row.get("type"),
                row.get("filed"),
                row.get("agreement_key"),
                row.get("_lifecycle_event"),
            )
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            cleaned.append(row)
            continue
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
        row["known_at"] = row.get("known_at") or known_at
        row["parser_version"] = PARSER_VERSION
        row["accession"] = str(row.get("_accession") or "") or None
        row["trigger"] = _row_trigger(row)
    # Unquantified identity is evidence identity: dedup on the content hash
    # (accession/trigger/normalized excerpt), never on (type, filed).
    seen_u: set[str] = set()
    bucket: list[dict] = []
    for exp in unquantified:
        digest = _content_hash(exp)
        if digest in seen_u:
            continue
        seen_u.add(digest)
        exp["content_hash"] = digest
        bucket.append(exp)
    seen_c: set[str] = set()
    capital: list[dict] = []
    for entry in capital_raw:
        digest = _content_hash(entry)
        if digest in seen_c:
            continue
        seen_c.add(digest)
        entry["content_hash"] = digest
        capital.append(entry)
    for exp in bucket + capital:
        exp["ticker"] = ticker
        exp["content_hash"] = _content_hash(exp)
        exp["known_at"] = known_at
        exp["parser_version"] = PARSER_VERSION
        exp["accession"] = str(exp.get("_accession") or "") or None
    snapshot, snap_warnings = _current_snapshot(rows)
    stashed: list[str] = []
    for row in rows + bucket + capital:
        for stash_key in ("_reconciliation_warning", "_coverage_warning"):
            warning = row.pop(stash_key, None)
            if warning:
                stashed.append(str(warning))
    coverage = {
        "scan_manifest": manifest,
        "quantified_count": len(rows),
        "unquantified_count": len(bucket),
        "warnings": list(snap_warnings) + stashed,
    }

    if not rows and not bucket and not capital:
        return _no_data(ticker, "no quantified obligations found in filings")

    value = {
        "ticker": ticker,
        "as_of": known_at,
        "source": _PICTURE_SOURCE,
        "obligations": rows,
        "current_snapshot": snapshot,
        "unquantified_exposures": bucket,
        "capital_allocation": capital,
        "coverage": coverage,
        "filings_examined": sorted({str(m.get("filing_date")) for m in manifest if m.get("filing_date")})
        or sorted({str(r.get("filed")) for r in rows if r.get("filed")}),
        "sections_examined": sorted({str(s) for m in manifest for s in (m.get("sections_examined") or []) if s})
        or sorted({str(r.get("source")) for r in rows if r.get("source")}),
        "note": _PICTURE_NOTE,
    }
    cache.set(key, value)
    if persist:
        try:
            summary = persist_obligation_events(rows, unquantified=bucket, capital=capital)
            if summary["events_written"]:
                logger.info(
                    "persisted %d obligation events for %s", summary["events_written"], ticker
                )
            if summary["skipped_no_filing_date"]:
                logger.warning(
                    "skipped %d obligation rows without a filing date for %s",
                    summary["skipped_no_filing_date"], ticker,
                )
            if summary.get("skipped_proxied"):
                logger.warning(
                    "skipped %d proxied XBRL rows (live-only) for %s",
                    summary["skipped_proxied"], ticker,
                )
        except Exception as e:
            logger.warning("obligation persistence failed for %s: %s", ticker, e)
    _publish_lifecycle(rows, bucket, capital)
    return value


def persist_obligation_events(rows: list[dict], data_root: Optional[str] = None, *, unquantified: list[dict] | None = None, capital: list[dict] | None = None) -> dict:
    """Write obligations rows as CorporateEvent + Evidence rows.

    One source row -> one CorporateEvent plus one Evidence row.  Event
    ``known_at`` is the source filing's ``filed`` date — NEVER the wall clock
    or a period end — so rows without a filing date are skipped and counted
    in ``skipped_no_filing_date``.  Filing-text evidence is anchored to the
    report text archived at fetch time (``raw_archive`` under
    ``filing-text:{ticker}:{filed}:{accession-or-hash}``); XBRL-fact rows
    carry no archive.  ``data_root`` is a research data root (parquet/ +
    raw/ subdirectories; default: the repo data root).

    ``unquantified`` exposures persist as amount-None contingent events with
    evidence (excerpt/archive span) via the same path. No schema change: the
    trigger rides on the existing ``default_triggered`` flag (True only for
    ``counterparty_default``).

    Returns ``{events_written, evidence_written, skipped_no_filing_date, skipped_proxied}``;
    a deterministic rerun writes 0 rows (dedup by event/evidence id). Proxied
    XBRL rows (``provenance == "proxied"``) are live-only evidence, never persisted.
    Status is derived by ``_resolve_8k_lifecycle`` at read time and is never stored.
    """
    from dataclasses import asdict
    from datetime import date
    from pathlib import Path

    from .domain.market.ids import sec_entity_id
    from .services.sec_facts import _resolve_entity
    from .storage import duckdb, parquet, raw_archive

    data_root = Path(data_root) if data_root is not None else Path(duckdb.DEFAULT_DATA_ROOT)
    event_rows: list[dict] = []
    capital_rows: list[dict] = []
    evidence_rows: list[dict] = []
    skipped = 0
    skipped_proxied = 0
    work = list(rows or [])
    for exp in unquantified or []:
        filed = str(exp.get("filed") or "").strip() or None
        norm = {
            "ticker": exp.get("ticker"),
            "type": exp.get("type", "other"),
            "amount_billions": None,
            "certainty": "contingent",
            "status": "contingent",
            "revenue_matched": False,
            "default_triggered": exp.get("trigger") == "counterparty_default",
            "fiscal_year": None,
            "schedule": None,
            "payment_horizon": None,
            "filed": filed,
            "known_at": exp.get("known_at"),
            "parser_version": exp.get("parser_version"),
            "trigger": exp.get("trigger"),
            "_accession": exp.get("_accession") or exp.get("accession"),
            "_archive_key": exp.get("_archive_key"),
            "excerpt": exp.get("excerpt"),
            "source": exp.get("source"),
        }
        norm["content_hash"] = exp.get("content_hash") or _content_hash(norm)
        work.append(norm)
    capital_work: list[dict] = []
    for entry in capital or []:
        if not entry.get("content_hash"):
            entry = {**entry, "content_hash": _content_hash(entry)}
        capital_work.append(entry)
    def _build(row: dict, event_target: list[dict]) -> None:
        nonlocal skipped, skipped_proxied
        if row.get("provenance") == "proxied":
            skipped_proxied += 1
            return
        ticker = str(row.get("ticker") or "").strip().upper()
        filed = str(row.get("filed") or "").strip() or None
        if not filed:
            skipped += 1
            return
        content_hash = str(row.get("content_hash") or "")
        horizon = row.get("payment_horizon") or {}
        sched = row.get("schedule") or horizon.get("schedule") or []
        schedule_json = json.dumps(sched) if sched else None
        if sched:
            payment_timing_json = schedule_json
        elif any(horizon.get(k) is not None for k in (
            "paid_in_remainder_of_fy", "paid_in_remainder_billions",
            "paid_after_remainder_billions",
        )):
            payment_timing_json = json.dumps({
                "paid_in_remainder_of_fy": horizon.get("paid_in_remainder_of_fy"),
                "paid_in_remainder_billions": horizon.get("paid_in_remainder_billions"),
                "paid_after_remainder_billions": horizon.get("paid_after_remainder_billions"),
            })
        else:
            payment_timing_json = None
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
            schedule_json=schedule_json,
            payment_timing_json=payment_timing_json,
            filed_at=filed,
            known_at=filed,
            retrieved_at=str(row.get("known_at") or ""),
            accession=str(row.get("_accession") or "") or None,
            source=row.get("source"),
            source_url=None,
            content_hash=content_hash,
            parser_version=row.get("parser_version"),
            agreement_key=row.get("agreement_key"),
            lifecycle_event=row.get("_lifecycle_event", row.get("lifecycle_event")),
        )
        event_target.append(asdict(event))
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
    for row in work:
        _build(row, event_rows)
    for row in capital_work:
        _build(row, capital_rows)
    return {
        "events_written": parquet.write_rows("events", event_rows, root=data_root / "parquet"),
        "capital_events_written": parquet.write_rows("capital_events", capital_rows, root=data_root / "parquet"),
        "evidence_written": parquet.write_rows("evidence", evidence_rows, root=data_root / "parquet"),
        "skipped_no_filing_date": skipped,
        "skipped_proxied": skipped_proxied,
    }


def get_obligations_as_of(ticker: str, as_of: str, data_root: Optional[str] = None) -> dict:
    """Replay the full obligations picture from stored events as of a date."""
    from pathlib import Path

    from .storage import duckdb, parquet

    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    data_root = Path(data_root) if data_root is not None else Path(duckdb.DEFAULT_DATA_ROOT)
    parquet_root = data_root / "parquet"
    stored = parquet.read_table("events", root=parquet_root).to_pylist()
    try:
        stored_capital = parquet.read_table("capital_events", root=parquet_root).to_pylist()
    except Exception:
        stored_capital = []
    stored_evidence = parquet.read_table("evidence", root=parquet_root).to_pylist()
    evidence_by_event: dict[str, dict] = {}
    for ev in stored_evidence:
        eid = str(ev.get("event_id") or "")
        if eid and eid not in evidence_by_event:
            evidence_by_event[eid] = ev
    kept: list[dict] = []
    for e in stored:
        if str(e.get("ticker") or "").strip().upper() != ticker:
            continue
        if str(e.get("filed_at") or "")[:10] > as_of:
            continue
        kept.append(e)
    kept_capital: list[dict] = []
    for e in stored_capital:
        if str(e.get("ticker") or "").strip().upper() != ticker:
            continue
        if str(e.get("filed_at") or "")[:10] > as_of:
            continue
        kept_capital.append(e)
    event_ids = {str(e.get("event_id") or "") for e in kept + kept_capital}
    event_ids |= {sec_event_id(ticker, str(e.get("content_hash") or "")) for e in kept + kept_capital}
    def _rebuild(e: dict) -> dict:
        content_hash = str(e.get("content_hash") or "")
        eid = str(e.get("event_id") or "") or sec_event_id(ticker, content_hash)
        ev = evidence_by_event.get(eid) or evidence_by_event.get(sec_event_id(ticker, content_hash))
        try:
            schedule = json.loads(e.get("schedule_json")) if e.get("schedule_json") else None
        except Exception:
            schedule = None
        payment_horizon = None
        if e.get("payment_timing_json"):
            try:
                parsed = json.loads(e.get("payment_timing_json"))
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                payment_horizon = {"schedule": parsed}
            elif isinstance(parsed, dict):
                payment_horizon = parsed
        row: dict = {
            "type": e.get("event_type"),
            "amount_billions": e.get("amount_billions"),
            "filed": e.get("filed_at"),
            "known_at": e.get("known_at"),
            "certainty": e.get("certainty"),
            "status": e.get("status"),
            "revenue_matched": e.get("revenue_matched"),
            "default_triggered": e.get("default_triggered"),
            "fiscal_year": e.get("fiscal_year"),
            "schedule": schedule,
            "payment_horizon": payment_horizon,
            "agreement_key": e.get("agreement_key"),
            "_lifecycle_event": e.get("lifecycle_event"),
            "source": e.get("source"),
            "accession": e.get("accession"),
            "_accession": e.get("accession"),
            "excerpt": (ev or {}).get("excerpt"),
            "ticker": ticker,
            "content_hash": content_hash,
            "parser_version": e.get("parser_version"),
        }
        if (ev or {}).get("source_type") == "xbrl_fact":
            row["concept"] = True
        return row
    rows: list[dict] = []
    bucket: list[dict] = []
    for e in kept:
        row = _rebuild(e)
        if row.get("amount_billions") is None and row.get("_lifecycle_event") not in ("amendment", "termination"):
            row["trigger"] = _row_trigger(row)
            bucket.append(row)
        else:
            row["trigger"] = _row_trigger(row)
            rows.append(row)
    capital: list[dict] = []
    for e in kept_capital:
        row = _rebuild(e)
        row["trigger"] = "board_discretion"
        capital.append(row)
    # Ignore evidence without a matching event (never joined above).
    _ = {ev.get("event_id") for ev in stored_evidence if str(ev.get("event_id") or "") not in event_ids}
    if not rows and not bucket and not capital:
        return _no_data(ticker, f"no stored obligation events as of {as_of}")
    snapshot, snap_warnings = _current_snapshot(rows)
    _publish_lifecycle(rows, bucket, capital)
    coverage = {
        "scan_manifest": [],
        "quantified_count": len(rows),
        "unquantified_count": len(bucket),
        "warnings": list(snap_warnings),
    }
    return {
        "ticker": ticker,
        "as_of": as_of,
        "source": f"{_PICTURE_SOURCE} (replayed from stored events as of {as_of})",
        "obligations": rows,
        "current_snapshot": snapshot,
        "unquantified_exposures": bucket,
        "capital_allocation": capital,
        "coverage": coverage,
        "filings_examined": sorted({str(r.get("filed")) for r in rows + bucket + capital if r.get("filed")}),
        "sections_examined": sorted({str(r.get("source")) for r in rows + bucket + capital if r.get("source")}),
        "note": _PICTURE_NOTE,
    }


__all__ = ["get_obligations", "get_obligations_as_of", "persist_obligation_events", "DEFAULT_TRIGGERED_TYPES", "REVENUE_MATCHED_KINDS"]