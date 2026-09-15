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
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, runtime_checkable

from edgar import Filing

from . import cache, edgar_client
from .domain.events import sec_event_id
from .services.sec_facts import FinancialFactRow, StoredFactKey

if TYPE_CHECKING:
    import pandas as pd

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


@runtime_checkable
class _Filing(Protocol):
    """Structural filing seam: edgartools filing or test double.

    Helpers only read filing_date/accession_no (never construct filings),
    so both the real EntityFiling and test fakes satisfy this contract.
    """

    filing_date: str
    accession_no: str
    def obj(self) -> object: ...


class ObligationScheduleYear(TypedDict, total=False):
    """One per-year slice of a disclosed payment schedule."""

    fiscal_year: str
    amount_billions: float


class QuantifiedObligationRow(TypedDict, total=False):
    """Quantified obligation: every key optional, amounts in $B (never estimated)."""

    type: str
    amount_billions: float | None
    certainty: str
    status: str
    source: str
    filed: str
    fiscal_year: str
    concept: str
    schedule: list[ObligationScheduleYear]
    schedule_component: bool
    headline_type: str
    lifecycle_status: str
    agreement_key: str | None
    trigger: str | None
    excerpt: str
    provenance: str
    content_hash: str
    known_at: str
    accession: str | None


class UnquantifiedExposureRow(TypedDict, total=False):
    """Disclosed-but-unquantified exposure: no dollar amount attached."""

    type: str
    source: str
    filed: str
    excerpt: str
    trigger: str
    content_hash: str
    known_at: str
    accession: str | None


class _FiscalTableRow(TypedDict):
    """One fiscal-year table row: year label plus millions amount."""

    fiscal_year: str
    amount_millions: float


class _SentenceAmount(TypedDict):
    """One sentence-level ``$X billion`` disclosure with kind + certainty."""

    kind: str
    amount_billions: float
    certainty: str
    off_balance_sheet: bool
    excerpt: str


ObligationDedupKey = tuple[str, float, str, str]
SnapshotBestKey = tuple[str | None, str]


def _opt_str(value: object) -> str | None:
    """str value or None (non-string dedup keys never collide with real ones)."""
    return value if isinstance(value, str) else None


def _opt_bool(value: object) -> bool | None:
    """bool value or None (missing flags stay missing, never defaulted)."""
    return value if isinstance(value, bool) else None


def _positive_amount(row: Mapping[str, object]) -> float | None:
    """Positive numeric ``amount_billions`` or None (missing/zero/non-numeric)."""
    amount = row.get("amount_billions")
    if isinstance(amount, (int, float)) and amount > 0:
        return float(amount)
    return None

def _publish_lifecycle(rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]]) -> None:
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
    re.IGNORECASE,
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
    re.IGNORECASE,
)
_UNQUANTIFIED_KIND = (
    ("indemnif", "indemnities"),
    ("guarant", "guarantees"),
    ("repurchase", "buybacks"),
    ("buyback", "buybacks"),
    ("dividend", "dividends"),
)


def _row_trigger(row: Mapping[str, object]) -> str | None:
    """Trigger/condition from the filing's own classifier flags, never inferred."""
    if bool(row.get("default_triggered")) or row.get("type") in DEFAULT_TRIGGERED_TYPES:
        return "counterparty_default"
    if row.get("certainty") == "contingent" or row.get("status") == "contingent":
        return "conditional"
    return None


def _unquantified_kind(sentence: str) -> str:
    """Exposure kind from the sentence's own keywords (other fallback)."""
    low = sentence.lower()
    for needle, name in _UNQUANTIFIED_KIND:
        if needle in low:
            return name
    return "other_commitments"


def _unquantified_trigger(sentence: str) -> str:
    """Trigger from the sentence's own words (unknown when unstated)."""
    low = sentence.lower()
    if any(k in low for k in ("default", "insolven", "fail to pay", "counterparty")):
        return "counterparty_default"
    if any(k in low for k in ("condition", "contingen", "subject to")):
        return "conditional"
    return "unknown"


def _unquantified_sentence(match: re.Match[str]) -> str | None:
    """Whitespace-collapsed sentence, None when empty or quantified."""
    sentence = re.sub(r"\s+", " ", match.group(1)).strip()
    if not sentence:
        return None
    low = sentence.lower()
    if "$" in sentence or "million" in low or "billion" in low:
        return None
    return sentence


def _unquantified_entry(kind: str, title: str, sentence: str, filing: _Filing) -> dict[str, object]:
    """One exposure/capital entry with filing provenance attached."""
    return {
        "type": kind,
        "source": f"SEC EDGAR {filing.filing_date} {title} note",
        # edgar filing_date is annotated str but arrives as an Arrow date at runtime;
        # getattr keeps this str() boundary conversion honest.
        "filed": str(filing.filing_date),
        "excerpt": sentence,
    }


def _unquantified_route(entry: dict[str, object], kind: str, sentence: str, exposures: list[dict[str, object]], capital: list[dict[str, object]]) -> None:
    """Route one entry to capital (board discretion) or exposures bucket."""
    if kind in ("buybacks", "dividends"):
        entry["trigger"] = "board_discretion"
        entry["reason"] = "discretionary capital return; not an obligation"
        capital.append(entry)
        return
    entry["trigger"] = _unquantified_trigger(sentence)
    entry["reason"] = "disclosed without a dollar amount; excluded from quantified obligations"
    exposures.append(entry)


def _scan_unquantified_exposures(title: str, md: str, filing: _Filing, limit: int = 3) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Sentences disclosing an exposure with no dollar amount.

    Returns ``(exposures, capital_allocation)``: buyback/dividend sentences
    are discretionary capital returns, never obligations. Triggers come from
    the sentence's own words, never inferred (``unknown`` when unstated).

    ponytail: first-wins capped scan; quantified ($/million/billion)
    sentences belong to the numeric rows, never here.
    """
    exposures: list[dict[str, object]] = []
    capital: list[dict[str, object]] = []
    for m in _UNQUANTIFIED_RE.finditer(md):
        sentence = _unquantified_sentence(m)
        if sentence is None:
            continue
        kind = _unquantified_kind(sentence)
        _unquantified_route(_unquantified_entry(kind, title, sentence, filing), kind, sentence, exposures, capital)
        if len(exposures) + len(capital) >= limit:
            break
    return exposures, capital

# 8-K material-agreement guarantee language.
_8K_GUARANTEE_RE = re.compile(
    r"(?:cumulatively\s+capped|capped|guarante(?:e|d|es|ing|y))\s*"
    r"(?:at|to|under)?[\s\S]{0,120}?\$([\d.,]+)\s*(billion|million)",
    re.IGNORECASE,
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
_8K_TERMINATION_RE = re.compile(r"terminat\w*", re.IGNORECASE)
_8K_AMENDMENT_RE = re.compile(r"amend\w*", re.IGNORECASE)
_8K_AGREEMENT_RE = re.compile(r"agreement|guarant\w*", re.IGNORECASE)
# Agreement identity: normalized counterparty phrase + agreement-type token.
# Counterparty is the capitalized entity after with/for/in-favor-of (or the
# "Agreement X" label); agreement type reuses the _8K_AGREEMENT_RE vocabulary.
# ponytail: heuristic text match, fail-open (None = unlinkable, never marked).
_8K_COUNTERPARTY_RE = re.compile(
    r"(?:with|for|in\s+favor\s+of|issued\s+to|between|by|counterparty)\s+"
    r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})"
)
_8K_AGREEMENT_LABEL_RE = re.compile(r"[Aa]greement\s+([A-Z0-9][\w-]*)")
_8K_AGREEMENT_TYPE_RE = re.compile(r"guarant\w*\s+agreement|guarant\w*|agreement", re.IGNORECASE)

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
    re.IGNORECASE,
)
_TAX_BENEFITS_RE = re.compile(
    r"unrecognized tax benefits(?: and other income tax positions)?"
    r"(?: related to uncertain tax positions)?"
    r"(?: as of [^,]{0,40}?)?"
    r"(?: were| was| is| are)\s*\$([\d.,]+)\s*(billion|million)",
    re.IGNORECASE,
)

def _no_data(ticker: str, what: str) -> dict[str, object]:
    return {"error": f"No obligations data for {ticker}: {what}"}

def _manifest_entry(form: str | None, filing: _Filing | None, sections: list[str], q_count: int, u_count: int, status: str, warning: str | None = None) -> dict[str, object]:
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


def _amount_kind_window(context: str) -> str:
    """250-char window anchored on the nearest commitment word."""
    lowered = context.lower()
    anchor = lowered.rfind("commitment")
    if anchor < 0:
        return lowered
    return lowered[max(0, anchor - 250):]


def _amount_kind_in(text: str) -> str | None:
    """First priority kind whose keywords appear in the text, else None."""
    for candidate in ("cloud", "supply", "investment", "vendor", "facility"):
        keywords = dict(_KIND_KEYWORDS)[candidate]
        if any(kw in text for kw in keywords):
            return candidate
    return None


def _amount_kind(context: str) -> str:
    """Kind from the phrase nearest before the amount, anchored on the
    nearest 'commitment' word when present (e.g. 'cloud service agreement
    commitments ... were $30 billion'). Priority: cloud > supply > investment
    > vendor > facility > other."""
    lowered = context.lower()
    # "Investment commitments" contains the anchor itself; give it priority.
    if "investment commitment" in lowered:
        return "investment"
    windowed = _amount_kind_in(_amount_kind_window(context))
    if windowed is not None:
        return windowed
    for _candidate, keywords in _KIND_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return _candidate
    return "other"


def _parse_fiscal_year_table(note_text: str, max_rows: int = 12) -> list[_FiscalTableRow]:
    """Rows of fiscal-year obligation tables (e.g. lease/purchase schedules)."""
    rows: list[_FiscalTableRow] = []
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


def _parse_sentence_amounts(note_text: str) -> list[_SentenceAmount]:
    """Sentence-level ``$X billion`` disclosures with kind + certainty."""
    rows: list[_SentenceAmount] = []
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


def _parse_table_schedule(note_text: str) -> list[ObligationScheduleYear] | None:
    """Per-year schedule from an explicit fiscal-year table, else None."""
    table = _parse_fiscal_year_table(note_text)
    if len(table) < 2:
        return None
    schedule: list[ObligationScheduleYear] = []
    for r in table:
        m = re.search(r"20\d\d", r["fiscal_year"])
        if m:
            schedule.append({"fiscal_year": m.group(0), "amount_billions": round(r["amount_millions"] / 1000.0, 3)})
        elif r["fiscal_year"].strip().lower() == "thereafter":
            schedule.append({"fiscal_year": "Thereafter", "amount_billions": round(r["amount_millions"] / 1000.0, 3)})
    return schedule if len(schedule) >= 2 else None


def _prose_schedule_amounts(amounts_part: str) -> tuple[list[float], str]:
    """Amounts plus years-part split from a paid-in-FY sentence."""
    text = amounts_part.split("for which", 1)[1] if "for which" in amounts_part else amounts_part
    return [round(_billion(a, u), 3) for a, u in _AMOUNT_RE.findall(text)], text


def _prose_year(fiscal_year: str, amount_billions: float) -> ObligationScheduleYear:
    """One per-year schedule slice with explicit TypedDict construction."""
    return ObligationScheduleYear(fiscal_year=fiscal_year, amount_billions=amount_billions)


def _prose_schedule_build(amounts: list[float], years: list[str], years_part: str) -> list[ObligationScheduleYear] | None:
    """Pair amounts with years (plus Thereafter tail), None when unpaired."""
    if len(amounts) < 2:
        return None
    if len(amounts) == len(years):
        return [_prose_year(y, a) for y, a in zip(years, amounts)]
    if len(amounts) == len(years) + 1 and "thereafter" in years_part.lower():
        sched = [_prose_year(y, a) for y, a in zip(years, amounts)]
        sched.append(_prose_year("Thereafter", amounts[-1]))
        return sched
    return None

def _prose_schedule_accept(sched: list[ObligationScheduleYear], amount_billions: float | None) -> list[ObligationScheduleYear] | None:
    """Schedule kept as-is (no total) or only on 10% total reconciliation."""
    if amount_billions is None:
        return sched
    total = sum(y["amount_billions"] for y in sched)
    if total and abs(total - amount_billions) / total < 0.1:
        return sched
    return None


def _prose_sentence_schedule(sentence: str, amount_billions: float | None) -> list[ObligationScheduleYear] | None:
    """Schedule from one paid-in-FY sentence, None when absent/unreconciled."""
    if "will be paid in fiscal year" not in sentence.lower():
        return None
    amounts_part, _, years_part = sentence.partition("will be paid")
    amounts, _ = _prose_schedule_amounts(amounts_part)
    sched = _prose_schedule_build(amounts, re.findall(r"20\d\d", years_part), years_part)
    if sched is None:
        return None
    return _prose_schedule_accept(sched, amount_billions)


def _parse_prose_schedule(note_text: str, amount_billions: float | None = None) -> list[ObligationScheduleYear] | None:
    """Per-year schedule from prose like '$7B, $6B ... paid in FY 2027, 2028 ...'."""
    for sentence in re.split(r"\.\s+", note_text):
        sched = _prose_sentence_schedule(sentence, amount_billions)
        if sched is not None:
            return sched
    return None


def _parse_front_horizon(note_text: str, amount_billions: float) -> dict[str, object] | None:
    """Front-loaded horizon when text says substantially all/majority paid through FY."""
    m = re.search(
        r"(substantially all|majority)[^.]{0,120}?paid through fiscal year\s*(20\d\d)",
        note_text, re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "paid_in_remainder_of_fy": m.group(2),
        "paid_in_remainder_billions": round(amount_billions, 3),
        "paid_after_remainder_billions": 0.0,
    }


def _latest_report(ticker: str, form: str) -> tuple[Filing, object] | None:
    """Latest filing object + parsed document for one form, or None.

    Typed locally (instead of reusing edgar_client.get_latest_report, whose
    edgartools seam is unannotated and infers a getitem union): iterating
    pins the single filing, and works for both the real EntityFilings and
    list-returning test doubles. The module-global name is kept so tests
    can still substitute fakes.
    """
    filings = edgar_client.get_company(ticker).get_filings(form=[form])
    if not filings:
        return None
    filing = next(iter(filings))
    return filing, filing.obj()


def _xbrl_store_facts(ticker: str) -> list[FinancialFactRow]:
    """Normalized-store XBRL facts for obligation concepts (PIT provenance).

    Returns raw ``financial_facts`` rows (concept/value/period/filed_at/
    accession/known_at); empty when the store has nothing (ingestion gap).
    Separated for testability; raises only on unexpected store errors.
    """
    from datetime import date

    from .services import sec_facts
    from .storage import duckdb

    today = date.today()  # noqa: DTZ011 - trading-calendar local date has no tz meaning
    entity_id = sec_facts._resolve_entity(ticker, today, sec_facts.DEFAULT_DATA_ROOT)
    if not entity_id:
        return []
    needles = tuple(_XBRL_OBLIGATION_CONCEPTS)
    like = " OR ".join(["concept LIKE ?"] * len(needles))
    clause, param = duckdb.as_of_clause(today.isoformat())
    rows = duckdb.query(
        "SELECT concept, value, period_start, period_end, fiscal_year, "
        "fiscal_period, filed_at, accession, known_at, source_url "
        "FROM financial_facts "
        f"WHERE entity_id = ? AND ({like}) AND {clause} "
        "ORDER BY period_end, filed_at, accession",
        params=[entity_id, *[f"%{n}%" for n in needles], param],
        data_root=sec_facts.DEFAULT_DATA_ROOT,
    )
    return [fact for row in rows if (fact := sec_facts._validated_fact_row(row)) is not None]


def _store_fact_key(f: FinancialFactRow) -> StoredFactKey:
    """Newest restatement wins: period end, filed at, accession."""
    return (
        (f.get("period_end") or ""),
        (f.get("filed_at") or ""),
        (f.get("accession") or ""),
    )


_XBRL_ON_BALANCE_KINDS = frozenset({
    "debt", "deferred_revenue", "operating_leases",
    "finance_leases", "unrecognized_tax_benefits",
})


def _xbrl_status(kind: str) -> str:
    """Balance-sheet status for one XBRL kind (accrued vs future cash)."""
    if kind in _XBRL_ON_BALANCE_KINDS:
        return "on_balance_sheet"
    return "future_cash_obligation"


def _xbrl_pick_newer(pick: FinancialFactRow, prev: FinancialFactRow | None) -> bool:
    """True when the picked fact supersedes the previous kind winner."""
    if prev is None:
        return True
    return _store_fact_key(pick) > _store_fact_key(prev)


def _xbrl_kind_pick(store_facts: list[FinancialFactRow], needle: str) -> FinancialFactRow | None:
    """Newest non-zero fact for one concept needle (None when absent/zero)."""
    cands = [f for f in store_facts if needle in (f.get("concept") or "")]
    if not cands:
        return None
    pick = max(cands, key=_store_fact_key)
    if float(pick.get("value") or 0) == 0:
        return None
    return pick


def _xbrl_best_by_kind(store_facts: list[FinancialFactRow]) -> dict[str, FinancialFactRow]:
    """Newest non-zero fact per obligation kind (restatement-resolved)."""
    store_by_kind: dict[str, FinancialFactRow] = {}
    for needle, kind in _XBRL_OBLIGATION_CONCEPTS.items():
        pick = _xbrl_kind_pick(store_facts, needle)
        if pick is None:
            continue
        if _xbrl_pick_newer(pick, store_by_kind.get(kind)):
            store_by_kind[kind] = pick
    return store_by_kind


def _xbrl_load_store_facts(ticker: str) -> list[FinancialFactRow]:
    """Store facts with store-read failures logged as warnings, never raised."""
    try:
        return _xbrl_store_facts(ticker)
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("xbrl store read failed for %s: %s", ticker, e)
        return []

def _xbrl_live_facts(ticker: str) -> tuple[pd.DataFrame | None, str | None]:
    """Live Company Facts frame plus error text (None, None only on success path split)."""
    try:
        facts_obj = edgar_client.get_company(ticker).get_facts()
        if facts_obj is None:
            raise ValueError("company facts unavailable")
        return facts_obj.to_dataframe(), None
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("xbrl obligations failed for %s: %s", ticker, e)
        return None, str(e)


def _xbrl_store_row(kind: str, fact: FinancialFactRow) -> dict[str, object] | None:
    """One store-backed obligation row (None when value missing)."""
    concept = fact.get("concept")
    val_raw = fact.get("value")
    if val_raw is None:
        return None
    value = val_raw
    period_end = fact.get("period_end")
    return {
        "type": kind,
        "amount_billions": round(value / 1e9, 3),
        "certainty": "contractual",
        "status": _xbrl_status(kind),
        "revenue_matched": False,
        "default_triggered": False,
        "source": f"SEC EDGAR XBRL {concept}",
        "filed": fact.get("filed_at"),
        "known_at": fact.get("known_at"),
        "as_of": period_end,
        "excerpt": f"XBRL fact {concept} = {value:,.0f} as of {period_end}",
        "concept": concept,
        "_accession": (fact.get("accession") or ""),
    }


def _xbrl_store_rows(store_by_kind: dict[str, FinancialFactRow], seen: set[tuple[str, str]]) -> list[dict[str, object]]:
    """Store-backed rows with (kind, period) dedup via the shared seen set."""
    rows: list[dict[str, object]] = []
    for kind, fact in store_by_kind.items():
        row = _xbrl_store_row(kind, fact)
        if row is None:
            continue
        key = (kind, str(fact.get("period_end")))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _xbrl_proxy_filing(ticker: str) -> tuple[str | None, str, Filing | None]:
    """Latest 10-K/10-Q filing date triple for proxied live-fact provenance."""
    for form in ("10-K", "10-Q"):
        found = _latest_report(ticker, form)
        if found is not None:
            proxy_filing = found[0]
            return str(proxy_filing.filing_date), form, proxy_filing
    return None, "XBRL", None


def _xbrl_live_row(kind: str, concept: str, value: float, period_end: str, filing_date: str | None, proxy_form: str) -> dict[str, object]:
    """One live-fallback row with proxied-provenance coverage warning."""
    return {
        "type": kind,
        "amount_billions": round(value / 1e9, 3),
        "certainty": "contractual",
        "status": _xbrl_status(kind),
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


def _xbrl_live_frame_concepts(facts: pd.DataFrame) -> list[str]:
    """Distinct concept tags from the live facts frame."""
    unique = facts["concept"].unique()
    return [str(c) for c in list(unique)]


def _xbrl_live_concept_rows(facts: pd.DataFrame, store_by_kind: dict[str, FinancialFactRow], seen: set[tuple[str, str]], filing_date: str | None, proxy_form: str) -> list[dict[str, object]]:
    """Live-fallback rows for concepts the store lacks (deduped, non-zero)."""
    rows: list[dict[str, object]] = []
    for concept in _xbrl_live_frame_concepts(facts):
        row = _xbrl_live_concept_row(facts, concept, store_by_kind, seen, filing_date, proxy_form)
        if row is not None:
            rows.append(row)
    return rows


def _xbrl_live_concept_kind(concept: str, store_by_kind: dict[str, FinancialFactRow]) -> str | None:
    """Obligation kind for one live concept (None when unmatched/stored)."""
    for needle, kind in _XBRL_OBLIGATION_CONCEPTS.items():
        if needle not in concept:
            continue
        if kind in store_by_kind:
            return None
        return kind
    return None


def _xbrl_live_latest(facts: pd.DataFrame, concept: str) -> tuple[float, str]:
    """Latest (value, period_end) for one live concept tag."""
    sub = facts[facts["concept"] == concept]
    latest = sub.sort_values("period_end").iloc[-1]
    return float(latest["value"]), str(latest["period_end"])


def _xbrl_live_concept_row(facts: pd.DataFrame, concept: str, store_by_kind: dict[str, FinancialFactRow], seen: set[tuple[str, str]], filing_date: str | None, proxy_form: str) -> dict[str, object] | None:
    """One live-fallback concept row (None when stored/duplicate/zero)."""
    kind = _xbrl_live_concept_kind(concept, store_by_kind)
    if kind is None:
        return None
    value, period_end = _xbrl_live_latest(facts, concept)
    key = (kind, period_end)
    if key in seen or value == 0:
        return None
    seen.add(key)
    return _xbrl_live_row(kind, concept, value, period_end, filing_date, proxy_form)


def _xbrl_record_manifest(manifest: list[dict[str, object]] | None, proxy_form: str, proxy_filing: Filing | None, count: int, live: bool, store_by_kind: dict[str, FinancialFactRow], error: str | None) -> None:
    """Manifest entry for the XBRL scan (failed/scanned per source state)."""
    if manifest is None:
        return
    if not live and not store_by_kind:
        manifest.append(_manifest_entry("XBRL", None, ["xbrl_facts"], 0, 0, "failed", error))
    elif live:
        manifest.append(_manifest_entry(proxy_form, proxy_filing, ["xbrl_facts"], count, 0, "scanned"))
    elif store_by_kind:
        manifest.append(_manifest_entry("XBRL", None, ["xbrl_facts"], count, 0, "scanned"))


def _xbrl_obligations(ticker: str, *, manifest: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    """Layer 1: standardized us-gaap obligation concepts.

    Store-first: each concept resolves through the normalized
    ``financial_facts`` store so rows carry their own fact provenance
    (``filed=filed_at``, ``known_at``, ``accession``, ``as_of=period_end``).
    Restatements resolve by latest ``(filed_at, accession)``. The live
    Company Facts read is fallback only for concepts the store lacks, and
    those rows stash a proxied-provenance warning for coverage.
    """
    store_by_kind = _xbrl_best_by_kind(_xbrl_load_store_facts(ticker))
    facts, live_error = _xbrl_live_facts(ticker)
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    rows.extend(_xbrl_store_rows(store_by_kind, seen))
    proxy_form, proxy_filing = "XBRL", None
    if facts is not None:
        # Company facts aggregate every filing; the facts frame exposes no
        # per-fact filing date, so the latest 10-K/10-Q filing date stands in
        # as the row's filed date (never period_end — see persist known_at).
        filing_date, proxy_form, proxy_filing = _xbrl_proxy_filing(ticker)
        rows.extend(_xbrl_live_concept_rows(facts, store_by_kind, seen, filing_date, proxy_form))
    _xbrl_record_manifest(manifest, proxy_form, proxy_filing, len(rows), facts is not None, store_by_kind, live_error)
    return rows


def _targeted_balance_rows(title: str, md: str, filing: _Filing, present_kinds: set[str]) -> list[dict[str, object]]:
    """One-line balances (unearned SBC, unrecognized tax benefits) that XBRL
    may not tag; first match only, never multi-year comparison lists."""
    lower = title.lower()
    rows: list[dict[str, object]] = []
    if ("stock" in lower or "share" in lower) and "unearned_sbc" not in present_kinds:
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
    if "tax" in lower and "unrecognized_tax_benefits" not in present_kinds:
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


def _note_markdown_index(doc: object) -> dict[str, str]:
    """Title-indexed note markdown (first two hits per keyword, deduped)."""
    notes = getattr(doc, "notes", None)
    notes_md: dict[str, str] = {}
    if notes is None:
        return notes_md
    for kw in _NOTE_KEYWORDS:
        for note in notes.search(kw)[:2]:
            title = getattr(note, "title", "?")
            if title not in notes_md:
                notes_md[title] = note.to_markdown()
    return notes_md


def _note_scan_markdown(notes_md: dict[str, str], filing: _Filing, rows: list[dict[str, object]], unquantified: list[dict[str, object]], capital: list[dict[str, object]]) -> None:
    """Collect quantified rows plus unquantified/capital splits per note."""
    for title, md in notes_md.items():
        _collect_note_rows(rows, title, md, filing)
        exps, caps = _scan_unquantified_exposures(title, md, filing)
        unquantified.extend(exps)
        capital.extend(caps)


def _note_form_present_kinds(rows: list[dict[str, object]], filing: _Filing) -> set[str]:
    """Obligation kinds already quantified for this filing date."""
    return {str(r["type"]) for r in rows if r.get("filed") == str(filing.filing_date)}


def _note_annotate_form(rows: list[dict[str, object]], unquantified: list[dict[str, object]], capital: list[dict[str, object]], start: int, u_start: int, c_start: int, ticker: str, filing: _Filing, joined: str, has_notes: bool, *, archive: bool) -> None:
    """Archive-annotate rows sliced from this form's window."""
    if has_notes:
        _annotate_archive(rows[start:], ticker, filing, joined, archive=archive)
    _annotate_archive(unquantified[u_start:], ticker, filing, joined, archive=archive)
    _annotate_archive(capital[c_start:], ticker, filing, joined, archive=archive)


def _note_scan_form(ticker: str, form: str, filing: _Filing, doc: object, rows: list[dict[str, object]], unquantified: list[dict[str, object]], capital: list[dict[str, object]], *, archive: bool, manifest: list[dict[str, object]] | None) -> None:
    """Scan one form's notes: rows, targeted balances, archive, manifest."""
    start, u_start, c_start = len(rows), len(unquantified), len(capital)
    notes_md = _note_markdown_index(doc)
    _note_scan_markdown(notes_md, filing, rows, unquantified, capital)
    present_kinds = _note_form_present_kinds(rows, filing)
    for title, md in notes_md.items():
        rows.extend(_targeted_balance_rows(title, md, filing, present_kinds))
    joined = "\n\n".join(notes_md.values())
    _note_annotate_form(rows, unquantified, capital, start, u_start, c_start, ticker, filing, joined, bool(notes_md), archive=archive)
    if manifest is not None:
        manifest.append(_manifest_entry(
            form, filing, sorted(notes_md),
            len(rows) - start, len(unquantified) - u_start, "scanned",
        ))


def _note_obligations(ticker: str, *, archive: bool = False, manifest: list[dict[str, object]] | None = None) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Layer 2: note-text extraction from latest 10-Q and 10-K."""
    rows: list[dict[str, object]] = []
    unquantified: list[dict[str, object]] = []
    capital: list[dict[str, object]] = []
    for form in ("10-Q", "10-K"):
        found = _latest_report(ticker, form)
        if found is None:
            continue
        filing, doc = found
        if getattr(doc, "notes", None) is None:
            if manifest is not None:
                manifest.append(_manifest_entry(form, filing, [], 0, 0, "parser_warning", "filing notes unavailable"))
            continue
        _note_scan_form(ticker, form, filing, doc, rows, unquantified, capital, archive=archive, manifest=manifest)
    return rows, unquantified, capital

class _NoteTitleFlags:
    """Note-title classifiers routing rows to debt/lease/commitment paths."""

    def __init__(self, title: str) -> None:
        lower_title = title.lower()
        self.is_lease = "lease" in lower_title
        self.is_debt = "debt" in lower_title
        self.is_commitment = "commitment" in lower_title or "contingenc" in lower_title
        self.is_tax = "tax" in lower_title
        self.is_sbc = "stock" in lower_title or "share" in lower_title
        self.is_intangible = "intangible" in lower_title

    def sentence_note(self) -> bool:
        """True when sentence amounts belong to this note (not tax/SBC noise)."""
        return self.is_lease or self.is_debt or self.is_commitment

    def authoritative_balance_note(self) -> bool:
        """True when XBRL owns the balance (tax/SBC/intangible prose is noise)."""
        return self.is_tax or self.is_sbc or self.is_intangible


def _debt_issue_row(match: re.Match[str], title: str, md: str, filing: _Filing) -> dict[str, object] | None:
    """One debt per-issue row (None when the carrying amount is zero)."""
    amount_m = float(match.group(2).replace(",", ""))
    if amount_m <= 0:
        return None
    return {
        "type": "debt",
        "amount_billions": round(amount_m / 1000.0, 3),
        "fiscal_year": match.group(1),
        "certainty": "contractual",
        "status": "on_balance_sheet",
        "revenue_matched": False,
        "default_triggered": False,
        "source": f"SEC EDGAR {filing.filing_date} {title} note table",
        "filed": str(filing.filing_date),
        "as_of": str(filing.filing_date),
        "excerpt": _excerpt(md, match.start(), match.end()),
    }


def _collect_debt_issue_rows(rows: list[dict[str, object]], md: str, title: str, filing: _Filing, flags: _NoteTitleFlags) -> None:
    """Debt per-issue table rows (no-op outside debt notes)."""
    if not flags.is_debt:
        return
    for m in _DEBT_ISSUE_RE.finditer(md):
        row = _debt_issue_row(m, title, md, filing)
        if row is not None:
            rows.append(row)


def _table_row_kind(flags: _NoteTitleFlags) -> tuple[str, str] | None:
    """Table-row (kind, status) for this note title (None when unrouted)."""
    if flags.is_lease:
        return "operating_leases", "on_balance_sheet"
    if flags.is_debt:
        return "debt", "on_balance_sheet"
    if flags.is_commitment:
        return "purchase_commitments", "future_cash_obligation"
    if flags.is_intangible:
        return "intangible_amortization", "on_balance_sheet"
    return None


def _table_amount_row(row: _FiscalTableRow, kind: str, status: str, md: str, title: str, filing: _Filing) -> dict[str, object]:
    """One fiscal-year table amount row with filing provenance."""
    return {
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


def _collect_table_rows(rows: list[dict[str, object]], md: str, title: str, filing: _Filing, flags: _NoteTitleFlags) -> None:
    """Fiscal-year table amount rows (skipped when the title is unrouted)."""
    routed = _table_row_kind(flags)
    if routed is None:
        return
    kind, status = routed
    for row in _parse_fiscal_year_table(md):
        if row["amount_millions"] <= 0:
            continue
        rows.append(_table_amount_row(row, kind, status, md, title, filing))


def _sentence_kind(kind: str, flags: _NoteTitleFlags) -> str:
    """Sentence kind defaulted per note title (other/facility fall back)."""
    if kind == "other" or (flags.is_lease and kind == "facility"):
        if flags.is_lease:
            return "operating_leases"
        if flags.is_debt:
            return "debt"
        if flags.is_commitment:
            return "purchase_commitments"
    return kind


def _sentence_status_certainty(s: _SentenceAmount, flags: _NoteTitleFlags) -> tuple[str, str]:
    """Status/certainty for one sentence amount (off-balance-sheet aware)."""
    status = "off_balance_sheet" if s["off_balance_sheet"] else "future_cash_obligation"
    if flags.is_lease and not s["off_balance_sheet"]:
        status = "on_balance_sheet"
    # Not-yet-commenced leases are conditional (subject to
    # conditions being met / commencement), never "contractual".
    if s["off_balance_sheet"] and flags.is_lease:
        return status, "contingent"
    return status, s["certainty"]


def _sentence_schedule(kind: str, md: str, amount: float) -> tuple[dict[str, object] | None, list[ObligationScheduleYear] | None]:
    """Payment-horizon/schedule pair for one sentence kind (reconciled only)."""
    if kind == "cloud":
        prose = _parse_prose_schedule(md)
        if prose is not None:
            return {"schedule": prose}, None
        return None, None
    if kind in ("supply", "investment"):
        # ponytail: prose/table schedule match is heuristic; attach only on
        # 10%-total reconciliation, else keep the front-loaded horizon.
        sched = _parse_prose_schedule(md, amount)
        if sched is None:
            sched = _reconciled_table_schedule(md, amount)
        if sched is not None:
            return None, sched
        return _parse_front_horizon(md, amount), None
    if kind == "operating_leases":
        return None, _reconciled_table_schedule(md, amount)
    return None, None


def _reconciled_table_schedule(md: str, amount: float) -> list[ObligationScheduleYear] | None:
    """Table schedule kept only on 10%-total reconciliation with the amount."""
    table_sched = _parse_table_schedule(md)
    if table_sched is None:
        return None
    total = sum(y["amount_billions"] for y in table_sched)
    if total and abs(total - amount) / total < 0.1:
        return table_sched
    return None


def _sentence_row(s: _SentenceAmount, kind: str, status: str, certainty: str, md: str, title: str, filing: _Filing) -> dict[str, object]:
    """One sentence-amount row with schedule pair and filing provenance."""
    payment_horizon, schedule = _sentence_schedule(kind, md, s["amount_billions"])
    return {
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


def _collect_sentence_rows(rows: list[dict[str, object]], md: str, title: str, filing: _Filing, flags: _NoteTitleFlags) -> None:
    """Sentence-amount rows (XBRL-owned notes and unrouted titles skipped)."""
    if flags.authoritative_balance_note():
        # Tax/SBC/intangible balances come from Layer 1 XBRL facts,
        # which are authoritative single values. Note prose carries
        # multi-year comparison tables that create duplicate noise.
        return
    if not flags.sentence_note():
        return
    for s in _parse_sentence_amounts(md):
        if s["amount_billions"] < 0.001:
            continue
        kind = _sentence_kind(s["kind"], flags)
        status, certainty = _sentence_status_certainty(s, flags)
        rows.append(_sentence_row(s, kind, status, certainty, md, title, filing))


def _collect_note_rows(rows: list[dict[str, object]], title: str, md: str, filing: _Filing) -> None:
    start = len(rows)
    flags = _NoteTitleFlags(title)
    _collect_debt_issue_rows(rows, md, title, filing, flags)
    _collect_table_rows(rows, md, title, filing, flags)
    _collect_sentence_rows(rows, md, title, filing, flags)
    _reconcile_schedule_components(rows, start)


def _is_fiscal_component_row(row: Mapping[str, object]) -> bool:
    """Fiscal-year table rows (never debt per-issue rows like 'Notes Due 2026')."""
    if not str(row.get("source") or "").endswith("note table"):
        return False
    fy = str(row.get("fiscal_year") or "")
    return "Due" not in fy and ("20" in fy or "hereafter" in fy.lower())


def _filed_key(m: Mapping[str, object]) -> str:
    """Filing-date order key for lifecycle marks."""
    return str(m.get("filed") or "")


def _amount_gap(total: float, h: Mapping[str, object]) -> float:
    """Absolute gap between a headline amount and the table total."""
    return abs(total - (_positive_amount(h) or 0.0))

def _reconcile_split(new: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Split one filing-note window into table rows plus headline rows."""
    table_rows = [r for r in new if _is_fiscal_component_row(r)]
    headlines = [
        r for r in new
        if str(r.get("source") or "").endswith(" note") and _positive_amount(r) is not None
    ]
    return table_rows, headlines


def _reconcile_total(table_rows: list[dict[str, object]]) -> float:
    """Summed table total (0.0 when empty, caller treats as no-match)."""
    return sum((_positive_amount(r) or 0.0) for r in table_rows)


def _reconcile_close(total: float, headlines: list[dict[str, object]]) -> list[dict[str, object]]:
    """Headlines within 10% tolerance of the table total."""
    return [h for h in headlines if abs(total - (_positive_amount(h) or 0.0)) / total < 0.1]


def _reconcile_match(table_rows: list[dict[str, object]], headlines: list[dict[str, object]]) -> tuple[float, list[dict[str, object]]] | None:
    """Table total plus 10%-tolerance headline matches (None when no match)."""
    if not table_rows or not headlines:
        return None
    total = _reconcile_total(table_rows)
    if not total:
        return None
    matches = _reconcile_close(total, headlines)
    if not matches:
        return None
    return total, matches


def _reconcile_attach(best: dict[str, object], table_rows: list[dict[str, object]], total: float, matches: list[dict[str, object]]) -> None:
    """Attach the table breakdown to the closest headline; flag components."""
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


def _reconcile_window(new: list[dict[str, object]]) -> None:
    """Reconcile one filing-note window (no-op when nothing matches)."""
    table_rows, headlines = _reconcile_split(new)
    matched = _reconcile_match(table_rows, headlines)
    if matched is None:
        return
    total, matches = matched
    _reconcile_attach(min(matches, key=partial(_amount_gap, total)), table_rows, total, matches)


def _reconcile_schedule_components(rows: list[dict[str, object]], start: int) -> None:
    """Flag fiscal-year table rows that break down a headline sentence amount.

    Per filing-note window: when the table total (incl. Thereafter) is within
    the 10% reconciliation tolerance of a headline amount, the headline keeps
    the amount (plus the schedule) and each table row becomes a
    ``schedule_component`` of that headline kind. Closest headline wins; a
    multi-headline match stashes an ambiguity warning for coverage.
    """
    _reconcile_window(rows[start:])


def _legacy_windows(rows: list[dict[str, object]]) -> dict[tuple[str, str], list[dict[str, object]]]:
    """Group unflagged note/note-table rows by (filed, note source)."""
    by_window: dict[tuple[str, str], list[dict[str, object]]] = {}
    for r in rows:
        if r.get("schedule_component") is not None:
            continue
        src = str(r.get("source") or "")
        if not src.endswith(" note") and not src.endswith("note table"):
            continue
        filed = str(r.get("filed") or "")
        note_key = src.removesuffix(" table")
        by_window.setdefault((filed, note_key), []).append(r)
    return by_window


def _legacy_flag_group(group: list[dict[str, object]]) -> None:
    """Backfill component flags for one legacy (filed, note) window."""
    table_rows, headlines = _reconcile_split(group)
    matched = _reconcile_match(table_rows, headlines)
    if matched is None:
        return
    _total, matches = matched
    best = min(matches, key=partial(_amount_gap, _total))
    for r in table_rows:
        r["schedule_component"] = True
        r["headline_type"] = best.get("type")


def _apply_legacy_component_flags(rows: list[dict[str, object]]) -> None:
    """Backfill component flags for events stored before flags existed."""
    for group in _legacy_windows(rows).values():
        _legacy_flag_group(group)


def _balance_sheet_latest(ticker: str) -> tuple[str | None, tuple[Filing, object] | None]:
    """Latest 10-Q (else 10-K) report triple for balance-sheet parsing."""
    found = _latest_report(ticker, "10-Q")
    if found is not None:
        return "10-Q", found
    found = _latest_report(ticker, "10-K")
    if found is not None:
        return "10-K", found
    return None, None


def _balance_sheet_markdown(doc: object) -> str:
    """Balance-sheet markdown from the filing's financials seam."""
    bs = getattr(doc, "financials").balance_sheet()  # noqa: B009 - dynamic boundary, no stubs; getattr keeps checker green
    return bs.to_markdown() if hasattr(bs, "to_markdown") else str(bs)


def _balance_sheet_line(field: str, label: str, md: str, filing: Filing) -> dict[str, object] | None:
    """One balance-sheet liability row (None when the label is absent)."""
    match = re.search(rf"{re.escape(label)}[^|]*\|\s*\$?([\d,]+)", md)
    if not match:
        return None
    return {
        "type": f"bs_{field}",
        "amount_billions": round(float(match.group(1).replace(",", "")) / 1000.0, 3),
        "certainty": "contractual",
        "status": "on_balance_sheet",
        "revenue_matched": False,
        "default_triggered": False,
        "source": f"SEC EDGAR {filing.filing_date} balance sheet",
        "filed": str(filing.filing_date),
        "as_of": str(filing.filing_date),
        "excerpt": f"Balance sheet line item: {label}",
    }


def _balance_sheet_collect(rows: list[dict[str, object]], md: str, filing: Filing) -> None:
    """Append one row per matched balance-sheet line item."""
    for field, label in _BS_LINE_ITEMS:
        row = _balance_sheet_line(field, label, md, filing)
        if row is not None:
            rows.append(row)


def _balance_sheet_liabilities(ticker: str, *, archive: bool = False, manifest: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    """Layer 3: balance-sheet liabilities with on-balance-sheet status."""
    rows: list[dict[str, object]] = []
    form: str | None = None
    filing: Filing | None = None
    try:
        form, found = _balance_sheet_latest(ticker)
        if found is None:
            return rows
        filing, doc = found
        md = _balance_sheet_markdown(doc)
        start = len(rows)
        _balance_sheet_collect(rows, md, filing)
        _annotate_archive(rows[start:], ticker, filing, md, archive=archive)
        if manifest is not None:
            manifest.append(_manifest_entry(form, filing, ["balance_sheet"], len(rows) - start, 0, "scanned"))
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
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

def _8k_company_filings(ticker: str, manifest: list[dict[str, object]] | None) -> list[_Filing] | None:
    """8-K filing list, None with a failure manifest when the company read fails."""
    try:
        filings: object = edgar_client.get_company(ticker).get_filings(form=["8-K"])
        rows: list[_Filing] = []
        if isinstance(filings, (list, tuple)):
            for f in filings:
                if isinstance(f, _Filing):
                    rows.append(f)
        return rows
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("8-K scan failed for %s: %s", ticker, e)
        if manifest is not None:
            manifest.append(_manifest_entry("8-K", None, [], 0, 0, "failed", str(e)))
        return None


def _8k_filing_relevant(items: list[object], text: str) -> bool:
    """True when the 8-K carries a material-agreement item plus keywords."""
    if not any(i in items for i in ("Item 1.01", "Item 1.02", "Item 2.03", "Item 7.01")):
        return False
    return any(kw in text.lower() for kw in _8K_OBLIGATION_KEYWORDS)


def _8k_window_lifecycle(window: str) -> str | None:
    """Lifecycle mark for one guarantee window (termination wins ties)."""
    if _8K_TERMINATION_RE.search(window) and _8K_AGREEMENT_RE.search(window):
        return "termination"
    if _8K_AMENDMENT_RE.search(window) and _8K_AGREEMENT_RE.search(window):
        return "amendment"
    return None


def _8k_guarantee_row(filing: _Filing, text: str, match: re.Match[str], amount_b: float) -> dict[str, object]:
    """One quantified 8-K guarantee row with lifecycle/agreement identity."""
    window = text[max(0, match.start() - 500):match.end() + 500]
    return {
        "type": "8k_guarantees",
        "amount_billions": round(amount_b, 3),
        "certainty": "contingent",
        "status": "contingent",
        "revenue_matched": False,
        "default_triggered": True,
        "source": f"SEC EDGAR 8-K {filing.filing_date} material agreement",
        "filed": str(filing.filing_date),
        "as_of": str(filing.filing_date),
        "excerpt": _excerpt(text, match.start(), match.end()),
        "_lifecycle_event": _8k_window_lifecycle(window),
        "agreement_key": _agreement_key(window),
    }


def _8k_collect_quantified(rows: list[dict[str, object]], filing: _Filing, text: str) -> list[tuple[int, int]]:
    """Quantified guarantee rows plus their match windows (>=0.1B only)."""
    quantified_windows: list[tuple[int, int]] = []
    for m in _8K_GUARANTEE_RE.finditer(text):
        amount_b = _billion(m.group(1), m.group(2))
        if amount_b < 0.1:
            continue
        quantified_windows.append((m.start() - 500, m.end() + 500))
        rows.append(_8k_guarantee_row(filing, text, m, amount_b))
    return quantified_windows


def _8k_lifecycle_triggers(text: str) -> list[tuple[re.Match[str], str]]:
    """Termination/amendment trigger sites in document order."""
    return [(t, "termination") for t in _8K_TERMINATION_RE.finditer(text)] + [(a, "amendment") for a in _8K_AMENDMENT_RE.finditer(text)]


def _8k_lifecycle_row(filing: _Filing, text: str, trig: re.Match[str], event: str) -> dict[str, object] | None:
    """One amount-less lifecycle row (None when no agreement language nearby)."""
    window = text[max(0, trig.start() - 500):trig.end() + 500]
    if not _8K_AGREEMENT_RE.search(window):
        return None
    return {
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


def _8k_collect_lifecycle(rows: list[dict[str, object]], filing: _Filing, text: str, quantified_windows: list[tuple[int, int]]) -> None:
    """Lifecycle-only rows, one per trigger site outside quantified windows."""
    lifecycle_windows: list[tuple[int, int]] = []
    for trig, event in _8k_lifecycle_triggers(text):
        pos = trig.start()
        if any(lo <= pos <= hi for lo, hi in quantified_windows):
            continue
        if any(lo <= pos <= hi for lo, hi in lifecycle_windows):
            continue
        row = _8k_lifecycle_row(filing, text, trig, event)
        if row is None:
            continue
        lifecycle_windows.append((trig.start() - 500, trig.end() + 500))
        rows.append(row)


def _8k_parsed_text(filing: _Filing) -> tuple[list[object], list[str], str]:
    """Items plus sections plus document text for one 8-K filing."""
    obj: object = filing.obj()
    items: object = getattr(obj, "items", [])
    rows: list[object] = list(items) if isinstance(items, list) else []
    return rows, [str(i) for i in rows], str(getattr(obj, "document", ""))


def _8k_collect_filing(ticker: str, filing: _Filing, rows: list[dict[str, object]], start: int, text: str, sections: list[str], *, archive: bool, manifest: list[dict[str, object]] | None) -> None:
    """Collect quantified/lifecycle rows for one relevant filing, then manifest."""
    quantified_windows = _8k_collect_quantified(rows, filing, text)
    # Lifecycle-only mentions (e.g. amount-less Item 1.02 termination):
    # one row per trigger site outside any quantified window.
    _8k_collect_lifecycle(rows, filing, text, quantified_windows)
    if len(rows) > start:
        _annotate_archive(rows[start:], ticker, filing, text, archive=archive)
    if manifest is not None:
        manifest.append(_manifest_entry("8-K", filing, sections, len(rows) - start, 0, "scanned"))


def _8k_scan_error(filing: _Filing, rows: list[dict[str, object]], start: int, sections: list[str], e: Exception, manifest: list[dict[str, object]] | None) -> None:
    """Log one 8-K scan failure with a failed manifest entry."""
    logger.warning("8-K %s scan error: %s", getattr(filing, "accession_no", "?"), e)
    if manifest is not None:
        manifest.append(_manifest_entry("8-K", filing, sections, len(rows) - start, 0, "failed", str(e)))


def _8k_scan_one(ticker: str, filing: _Filing, rows: list[dict[str, object]], *, archive: bool, manifest: list[dict[str, object]] | None) -> None:
    """Scan one 8-K filing: quantified plus lifecycle rows, then manifest."""
    start = len(rows)
    sections: list[str] = []
    try:
        items, sections, text = _8k_parsed_text(filing)
        if not _8k_filing_relevant(items, text):
            if manifest is not None:
                manifest.append(_manifest_entry("8-K", filing, sections, 0, 0, "scanned"))
            return
        _8k_collect_filing(ticker, filing, rows, start, text, sections, archive=archive, manifest=manifest)
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        _8k_scan_error(filing, rows, start, sections, e, manifest)


def _scan_8k_obligations(ticker: str, *, archive: bool = False, manifest: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    """Recent 8-K material agreements with quantified guarantees."""
    rows: list[dict[str, object]] = []
    filings = _8k_company_filings(ticker, manifest)
    if filings is None:
        return rows
    for index, filing in enumerate(filings):
        if index >= 6:
            break
        _8k_scan_one(ticker, filing, rows, archive=archive, manifest=manifest)
    return rows


def _known_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _archive_filing_text(ticker: str, filing: _Filing, text: str, *, archive: bool = False) -> tuple[str, str] | None:
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


def _annotate_archive(rows: list[dict[str, object]], ticker: str, filing: _Filing, text: str, *, archive: bool = False) -> None:
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


def _normalize_excerpt(text: object) -> str:
    """Whitespace-collapsed, case-folded excerpt for identity (never display)."""
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _hash_years(entries: list[object]) -> list[tuple[str, float]]:
    """Sorted (fiscal_year, amount) pairs from schedule entries."""
    norm: list[tuple[str, float]] = []
    for y in entries:
        if not isinstance(y, Mapping):
            continue
        y_amount = y.get("amount_billions")
        norm.append((str(y.get("fiscal_year")), float(y_amount) if isinstance(y_amount, (int, float)) else 0.0))
    return sorted(norm)


def _hash_schedule(row: Mapping[str, object], horizon_map: Mapping[str, object]) -> list[tuple[str, float]]:
    """Normalized schedule from the row or its payment horizon."""
    sched_raw = row.get("schedule") or horizon_map.get("schedule")
    sched: list[object] = sched_raw if isinstance(sched_raw, list) else []
    return _hash_years(sched)


def _hash_timing(horizon: object) -> dict[str, object] | None:
    """Timing payload for rows with horizon detail (None when absent)."""
    if not isinstance(horizon, dict):
        return None
    if not any(horizon.get(k) is not None for k in ("schedule", "paid_in_remainder_of_fy", "paid_in_remainder_billions", "paid_after_remainder_billions")):
        return None
    # ponytail: conditional key — rows without timing keep byte-identical
    # payloads (no quantified id churn); a 95/24 correction retunes identity.
    horizon_sched = horizon.get("schedule") or []
    entries: list[object] = horizon_sched if isinstance(horizon_sched, list) else []
    return {
        "schedule": _hash_years(entries),
        "remainder_fy": horizon.get("paid_in_remainder_of_fy"),
        "remainder_b": horizon.get("paid_in_remainder_billions"),
        "after_b": horizon.get("paid_after_remainder_billions"),
    }


def _hash_evidence_identity(row: Mapping[str, object], payload: dict[str, object]) -> None:
    """Evidence identity for amount-less rows (accession/trigger/excerpt)."""
    if row.get("amount_billions") is not None:
        return
    # Unquantified identity is evidence identity: distinct excerpts in one
    # filing yield distinct rows; byte-identical repeats still collapse.
    payload["accession"] = str(row.get("accession") or row.get("_accession") or "")
    payload["trigger"] = row.get("trigger")
    payload["excerpt"] = _normalize_excerpt(row.get("excerpt"))


def _content_hash(row: Mapping[str, object]) -> str:
    horizon = row.get("payment_horizon")
    horizon_map: Mapping[str, object] = horizon if isinstance(horizon, Mapping) else {}
    payload: dict[str, object] = {
        **{k: row.get(k) for k in (
            "type", "amount_billions", "filed", "certainty", "status",
            "revenue_matched", "default_triggered", "fiscal_year",
        )},
        "schedule": _hash_schedule(row, horizon_map),
    }
    timing = _hash_timing(horizon)
    if timing is not None:
        payload["timing"] = timing
    _hash_evidence_identity(row, payload)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _snapshot_layer(row: Mapping[str, object]) -> str:
    if "concept" in row:
        return "xbrl"
    kind = str(row.get("type") or "")
    source = str(row.get("source") or "")
    if kind.startswith("8k_") or "8-K" in source:
        return "8k"
    if "balance sheet" in source.lower():
        return "balance"
    return "note"


def _lifecycle_guarantees(rows: Sequence[MutableMapping[str, object]]) -> list[MutableMapping[str, object]]:
    """8-K guarantee rows eligible for lifecycle stamping (quantified/marks)."""
    return [
        r for r in rows
        if _snapshot_layer(r) == "8k" and (
            _positive_amount(r) is not None
            or (r.get("amount_billions") is None and r.get("_lifecycle_event") in ("amendment", "termination"))
        )
    ]


def _lifecycle_groups(guarantees: list[MutableMapping[str, object]]) -> dict[str | None, list[MutableMapping[str, object]]]:
    """Guarantee rows grouped by agreement key (None = unlinkable)."""
    groups: dict[str | None, list[MutableMapping[str, object]]] = {}
    for r in guarantees:
        groups.setdefault(_opt_str(r.get("agreement_key")), []).append(r)
    return groups


def _lifecycle_stamp_unlinked(members: list[MutableMapping[str, object]]) -> None:
    """Stamp unlinkable rows: terminations terminate, rest stay additive."""
    for r in members:
        if r.get("_lifecycle_event") == "termination":
            r["lifecycle_status"] = "terminated"
        else:
            r.pop("lifecycle_status", None)


def _lifecycle_mark_dates(members: list[MutableMapping[str, object]], event: str, quantified: bool | None) -> list[str]:
    """Sorted filed dates for one lifecycle mark kind in a group."""
    dates = []
    for r in members:
        if r.get("_lifecycle_event") != event:
            continue
        if quantified is True and _positive_amount(r) is None:
            continue
        if quantified is False and r.get("amount_billions") is not None:
            continue
        dates.append(str(r.get("filed") or ""))
    return sorted(dates)


def _lifecycle_stamp_quantified(row: MutableMapping[str, object], term_marks: list[str], quant_amend_marks: list[str], amountless_amend_marks: list[str]) -> None:
    """Stamp one quantified row from later termination/amendment marks."""
    filed = str(row.get("filed") or "")
    if any(m > filed for m in term_marks) or any(m > filed for m in quant_amend_marks):
        row["lifecycle_status"] = "unknown"
    elif any(m > filed for m in amountless_amend_marks):
        row["lifecycle_status"] = "amended"
    else:
        row.pop("lifecycle_status", None)


def _lifecycle_stamp_member(row: MutableMapping[str, object], term_marks: list[str], quant_amend_marks: list[str], amountless_amend_marks: list[str]) -> None:
    """Stamp one linked row: termination marks self, amount-less stays bare."""
    if row.get("_lifecycle_event") == "termination":
        row["lifecycle_status"] = "terminated"
    elif row.get("amount_billions") is None:
        row.pop("lifecycle_status", None)
    else:
        _lifecycle_stamp_quantified(row, term_marks, quant_amend_marks, amountless_amend_marks)


def _lifecycle_stamp_group(members: list[MutableMapping[str, object]]) -> None:
    """Stamp one linked agreement group from its later marks."""
    term_marks = _lifecycle_mark_dates(members, "termination", None)
    quant_amend_marks = _lifecycle_mark_dates(members, "amendment", True)
    amountless_amend_marks = _lifecycle_mark_dates(members, "amendment", False)
    for r in members:
        _lifecycle_stamp_member(r, term_marks, quant_amend_marks, amountless_amend_marks)


def _lifecycle_stamp_groups(groups: dict[str | None, list[MutableMapping[str, object]]]) -> None:
    """Stamp every agreement group (unlinked groups stay additive)."""
    for key, members in groups.items():
        if key is None:
            _lifecycle_stamp_unlinked(members)
        else:
            _lifecycle_stamp_group(members)


def _lifecycle_unresolved_warning(guarantees: list[MutableMapping[str, object]]) -> str:
    """Summation warning when multiple guarantees stay unresolved."""
    unresolved = [r for r in guarantees if "lifecycle_status" not in r and _positive_amount(r) is not None]
    if len(unresolved) > 1:
        return (
            f"{len(unresolved)} unresolved 8-K guarantees are summed without "
            "lifecycle resolution"
        )
    return ""


def _lifecycle_retained_group_warnings(members: list[MutableMapping[str, object]], seen_retained: set[SnapshotBestKey]) -> list[str]:
    """Retention warnings for one linked group (deduped per key/filed)."""
    warnings: list[str] = []
    for m in members:
        if m.get("amount_billions") is None and m.get("_lifecycle_event") == "amendment":
            rk: SnapshotBestKey = (_opt_str(m.get("agreement_key")), str(m.get("filed") or ""))
            if rk in seen_retained:
                continue
            seen_retained.add(rk)
            warnings.append(
                "A later amendment was found but did not disclose a replacement amount. "
                "The last quantified exposure is retained for downside analysis until "
                "superseded by a new amount or termination."
            )
    return warnings


def _lifecycle_retained_warnings(groups: dict[str | None, list[MutableMapping[str, object]]]) -> list[str]:
    """Retention warnings for amount-less amendments (one per key/filed)."""
    warnings: list[str] = []
    seen_retained: set[SnapshotBestKey] = set()
    for key, members in groups.items():
        if key is None:
            continue
        if not any(_positive_amount(m) is not None for m in members):
            continue
        warnings.extend(_lifecycle_retained_group_warnings(members, seen_retained))
    return warnings


def _lifecycle_prior_quant(members: list[MutableMapping[str, object]], t_filed: str) -> str | None:
    """Latest quantified filed date before one termination (None when none)."""
    prior_quant = [str(m.get("filed") or "") for m in members if _positive_amount(m) is not None and str(m.get("filed") or "") < t_filed]
    if not prior_quant:
        return None
    return max(prior_quant)


def _lifecycle_latest_intervening(members: list[MutableMapping[str, object]], last_quant: str, t_filed: str) -> MutableMapping[str, object] | None:
    """Latest amendment/termination mark between last quant and termination."""
    intervening = [m for m in members if last_quant < str(m.get("filed") or "") < t_filed and m.get("_lifecycle_event") in ("amendment", "termination")]
    if not intervening:
        return None
    return max(intervening, key=_filed_key)


def _lifecycle_is_stale_cancel(latest: MutableMapping[str, object] | None) -> bool:
    """True when the latest intervening mark is an amount-less amendment."""
    return latest is not None and latest.get("_lifecycle_event") == "amendment" and latest.get("amount_billions") is None


def _lifecycle_stale_termination_warning(key: str | None, members: list[MutableMapping[str, object]], seen: set[SnapshotBestKey]) -> list[str]:
    """Stale-cancellation warnings for terminations after amount-less amendments."""
    warnings: list[str] = []
    assert key is not None
    terms = [m for m in members if m.get("_lifecycle_event") == "termination"]
    for t in terms:
        t_filed = str(t.get("filed") or "")
        last_quant = _lifecycle_prior_quant(members, t_filed)
        if last_quant is None:
            continue
        latest = _lifecycle_latest_intervening(members, last_quant, t_filed)
        if not _lifecycle_is_stale_cancel(latest):
            continue
        sk: SnapshotBestKey = (key, t_filed)
        if sk in seen:
            continue
        seen.add(sk)
        warnings.append(
            "Agreement terminated after an amendment that disclosed no replacement amount; "
            "canceled amount unknown, defaulting to zero until further news."
        )
    return warnings


def _lifecycle_stale_warnings(groups: dict[str | None, list[MutableMapping[str, object]]]) -> list[str]:
    """Stale-cancellation warnings across linked agreement groups."""
    warnings: list[str] = []
    seen_stale_term: set[SnapshotBestKey] = set()
    for key, members in groups.items():
        if key is None:
            continue
        warnings.extend(_lifecycle_stale_termination_warning(key, members, seen_stale_term))
    return warnings


def _lifecycle_dangling_mark(m: MutableMapping[str, object]) -> tuple[str | None, str | None, str] | None:
    """Dedup key for one amount-less mark (None when quantified/unmarked)."""
    if m.get("amount_billions") is None and m.get("_lifecycle_event") in ("amendment", "termination"):
        return (
            _opt_str(m.get("agreement_key")),
            _opt_str(m.get("_lifecycle_event")),
            str(m.get("filed") or ""),
        )
    return None


def _lifecycle_dangling_group_warnings(members: list[MutableMapping[str, object]], seen_dangling: set[tuple[str | None, str | None, str]]) -> list[str]:
    """No-effect warnings for one group without any quantified agreement."""
    warnings: list[str] = []
    for m in members:
        dk = _lifecycle_dangling_mark(m)
        if dk is None or dk in seen_dangling:
            continue
        seen_dangling.add(dk)
        warnings.append(
            f"8-K {m.get('_lifecycle_event')} on {m.get('filed')} matches no known "
            "agreement and was recorded without effect"
        )
    return warnings


def _lifecycle_dangling_warnings(groups: dict[str | None, list[MutableMapping[str, object]]]) -> list[str]:
    """No-effect warnings for amount-less marks with no quantified agreement."""
    warnings: list[str] = []
    seen_dangling: set[tuple[str | None, str | None, str]] = set()
    for members in groups.values():
        if any(_positive_amount(m) is not None for m in members):
            continue
        warnings.extend(_lifecycle_dangling_group_warnings(members, seen_dangling))
    return warnings


def _resolve_8k_lifecycle(rows: Sequence[MutableMapping[str, object]]) -> list[str]:
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
    guarantees = _lifecycle_guarantees(rows)
    if not guarantees:
        return []
    groups = _lifecycle_groups(guarantees)
    _lifecycle_stamp_groups(groups)
    warnings: list[str] = []
    unresolved = _lifecycle_unresolved_warning(guarantees)
    if unresolved:
        warnings.append(unresolved)
    warnings.extend(_lifecycle_retained_warnings(groups))
    warnings.extend(_lifecycle_stale_warnings(groups))
    warnings.extend(_lifecycle_dangling_warnings(groups))
    return warnings


def _snapshot_best_filed(rows: list[dict[str, object]]) -> dict[SnapshotBestKey, str]:
    """Latest filed date per (type, layer) for non-8-K rows."""
    best: dict[SnapshotBestKey, str] = {}
    for row in rows:
        if _snapshot_layer(row) == "8k":
            continue
        filed = str(row.get("filed") or "").strip()
        if not filed:
            continue
        row_type = row.get("type")
        key: SnapshotBestKey = (row_type if isinstance(row_type, str) else None, _snapshot_layer(row))
        if key not in best or filed > best[key]:
            best[key] = filed
    return best


def _snapshot_keep_8k(row: dict[str, object]) -> bool:
    """True when an 8-K row enters the snapshot (quantified, live)."""
    if row.get("amount_billions") is None:
        return False
    return row.get("lifecycle_status") not in ("terminated", "unknown")


def _snapshot_warn_no_filed(row: dict[str, object], warned: set[str], warnings: list[str]) -> None:
    """Record one no-filing-date exclusion warning per obligation type."""
    wtype = row.get("type")
    wkey = wtype if isinstance(wtype, str) else ""
    if wkey in warned:
        return
    warned.add(wkey)
    warnings.append(
        f"excluded from current snapshot (no filing date): "
        f"{row.get('type')} {row.get('amount_billions')}B"
    )


def _snapshot_keep_layered(row: dict[str, object], best: dict[SnapshotBestKey, str], warned: set[str], warnings: list[str]) -> bool:
    """True when a non-8-K row is the latest filing for its (type, layer)."""
    filed = str(row.get("filed") or "").strip()
    if not filed:
        _snapshot_warn_no_filed(row, warned, warnings)
        return False
    snap_type = row.get("type")
    return filed == best.get((snap_type if isinstance(snap_type, str) else None, _snapshot_layer(row)))


def _current_snapshot(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[str]]:
    """Latest filing per (type, layer); 8-K events are additive, never supersede.

    Rows sharing (type, layer, filed) are all kept (same-filing schedule
    siblings); only strictly older filings are superseded. Rows without a
    filing date stay in the ledger but are excluded here, with a warning.
    Reconciled ``schedule_component`` rows and lifecycle-excluded
    (terminated/unknown) 8-K rows never enter the snapshot.
    Snapshot rows are references to the already-stamped ledger rows.
    Returns (snapshot, warnings).
    """
    best = _snapshot_best_filed(rows)
    snapshot: list[dict[str, object]] = []
    warnings: list[str] = _resolve_8k_lifecycle(rows)
    warned: set[str] = set()
    for row in rows:
        if row.get("schedule_component"):
            continue
        if _snapshot_layer(row) == "8k":
            if _snapshot_keep_8k(row):
                snapshot.append(row)
            continue
        if _snapshot_keep_layered(row, best, warned, warnings):
            snapshot.append(row)
    return snapshot, warnings


def _obligations_cached(ticker: str, persist: bool) -> dict[str, object] | None:
    """Cached picture when present (never served on a persist refresh)."""
    key = f"obligations:{ticker}"
    hit = cache.get(key, ttl=CACHE_TTL_SECONDS)
    if isinstance(hit, dict) and not persist:
        return hit
    return None


def _obligations_fetch(ticker: str, persist: bool, manifest: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]] | None:
    """Fetch all four layers into quantified rows plus exposure splits."""
    try:
        rows: list[dict[str, object]] = []
        rows.extend(_xbrl_obligations(ticker, manifest=manifest))
        note_rows, unquantified, capital_raw = _note_obligations(ticker, archive=persist, manifest=manifest)
        rows.extend(note_rows)
        rows.extend(_balance_sheet_liabilities(ticker, archive=persist, manifest=manifest))
        rows.extend(_scan_8k_obligations(ticker, archive=persist, manifest=manifest))
        return rows, unquantified, capital_raw
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("obligations failed for %s: %s", ticker, e)
        return None


def _obligations_lifecycle_key(row: dict[str, object]) -> tuple[str | None, str | None, str | None, str | None]:
    """Dedup key for amount-less lifecycle marks (type/filed/key/event)."""
    return (
        _opt_str(row.get("type")),
        _opt_str(row.get("filed")),
        _opt_str(row.get("agreement_key")),
        _opt_str(row.get("_lifecycle_event")),
    )


def _obligations_dedup_key(row: dict[str, object], amount: float) -> ObligationDedupKey:
    """Dedup key for quantified rows (type/amount/filed/fiscal year)."""
    return (
        str(row.get("type") or ""),
        round(float(amount), 2),
        str(row.get("filed") or ""),
        str(row.get("fiscal_year") or ""),
    )


class _ObligationsDedup:
    """Dedup sink for quantified rows plus lifecycle marks."""

    def __init__(self) -> None:
        self.seen: set[ObligationDedupKey] = set()
        self.seen_lifecycle: set[tuple[str | None, str | None, str | None, str | None]] = set()
        self.cleaned: list[dict[str, object]] = []

    def add_lifecycle(self, row: dict[str, object]) -> None:
        """Add one amount-less lifecycle mark unless already seen."""
        key = _obligations_lifecycle_key(row)
        if key in self.seen_lifecycle:
            return
        self.seen_lifecycle.add(key)
        self.cleaned.append(row)

    def add_quantified(self, row: dict[str, object], amount: float) -> None:
        """Add one quantified row unless its dedup key already seen."""
        dedup_key = _obligations_dedup_key(row, amount)
        if dedup_key in self.seen:
            return
        self.seen.add(dedup_key)
        self.cleaned.append(row)


def _obligations_dedup_row(row: dict[str, object], sink: _ObligationsDedup) -> None:
    """Route one row to lifecycle/quantified dedup (non-positive dropped)."""
    amount = row.get("amount_billions")
    if amount is None and row.get("_lifecycle_event") in ("amendment", "termination"):
        sink.add_lifecycle(row)
        return
    if amount is None or not isinstance(amount, (int, float)) or amount <= 0:
        return
    sink.add_quantified(row, float(amount))


def _obligations_dedup(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Dedup quantified rows plus lifecycle marks; drop non-positive amounts."""
    sink = _ObligationsDedup()
    for row in rows:
        _obligations_dedup_row(row, sink)
    return sink.cleaned


def _obligations_stamp_rows(rows: list[dict[str, object]], ticker: str, known_at: str) -> None:
    """Stamp quantified rows with ticker/hash/provenance/trigger identity."""
    for row in rows:
        row["ticker"] = ticker
        row["content_hash"] = _content_hash(row)
        row["known_at"] = row.get("known_at") or known_at
        row["parser_version"] = PARSER_VERSION
        row["accession"] = str(row.get("_accession") or "") or None
        row["trigger"] = _row_trigger(row)


def _obligations_dedup_bucket(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """Content-hash dedup for exposure/capital entries (evidence identity)."""
    seen: set[str] = set()
    bucket: list[dict[str, object]] = []
    for exp in entries:
        digest = _content_hash(exp)
        if digest in seen:
            continue
        seen.add(digest)
        exp["content_hash"] = digest
        bucket.append(exp)
    return bucket


def _obligations_stamp_bucket(bucket: list[dict[str, object]], capital: list[dict[str, object]], ticker: str, known_at: str) -> None:
    """Stamp exposure/capital entries with ticker/hash/parser provenance."""
    for exp in bucket + capital:
        exp["ticker"] = ticker
        exp["content_hash"] = _content_hash(exp)
        exp["known_at"] = known_at
        exp["parser_version"] = PARSER_VERSION
        exp["accession"] = str(exp.get("_accession") or "") or None


def _obligations_coverage(manifest: list[dict[str, object]], rows: list[dict[str, object]], bucket: list[dict[str, object]], snap_warnings: list[str], stashed: list[str]) -> dict[str, object]:
    """Coverage mapping from the scan manifest plus row counts/warnings."""
    return {
        "scan_manifest": manifest,
        "quantified_count": len(rows),
        "unquantified_count": len(bucket),
        "warnings": list(snap_warnings) + stashed,
    }


def _obligations_stash_warnings(rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]]) -> list[str]:
    """Pop persist-internal warning stashes into coverage warnings."""
    stashed: list[str] = []
    for row in rows + bucket + capital:
        for stash_key in ("_reconciliation_warning", "_coverage_warning"):
            warning = row.pop(stash_key, None)
            if warning:
                stashed.append(str(warning))
    return stashed
def _obligations_filings_examined(manifest: list[dict[str, object]], rows: list[dict[str, object]]) -> list[str]:
    """Filing dates examined (manifest first, row fallback when empty)."""
    return sorted({str(m.get("filing_date")) for m in manifest if m.get("filing_date")}) or sorted({str(r.get("filed")) for r in rows if r.get("filed")})


def _obligations_sections_examined(manifest: list[dict[str, object]], rows: list[dict[str, object]]) -> list[str]:
    """Sections examined (manifest first, row-source fallback when empty)."""
    sections = sorted({str(s) for m in manifest for s in (m.get("sections_examined") if isinstance(m.get("sections_examined"), list) else []) if s})
    return sections or sorted({str(r.get("source")) for r in rows if r.get("source")})


def _obligations_persist_summary(ticker: str, rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]]) -> None:
    """Persist events with per-skip logging (failures logged, never raised)."""
    try:
        summary = persist_obligation_events(rows, unquantified=bucket, capital=capital)
        if summary["events_written"]:
            logger.info("persisted %d obligation events for %s", summary["events_written"], ticker)
        if summary["skipped_no_filing_date"]:
            logger.warning("skipped %d obligation rows without a filing date for %s", summary["skipped_no_filing_date"], ticker)
        if summary["skipped_proxied"]:
            logger.warning("skipped %d proxied XBRL rows (live-only) for %s", summary["skipped_proxied"], ticker)
    except Exception as e:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        logger.warning("obligation persistence failed for %s: %s", ticker, e)


def _obligations_stamp_all(fetched_rows: list[dict[str, object]], unquantified: list[dict[str, object]], capital_raw: list[dict[str, object]], ticker: str, known_at: str) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Deduped plus stamped rows/bucket/capital for one ticker snapshot."""
    # Dedup: identical (type, amount, filed) rows appear from both the
    # 10-Q and 10-K or from table + sentence paths; drop negatives.
    rows = _obligations_dedup(fetched_rows)
    _obligations_stamp_rows(rows, ticker, known_at)
    # Unquantified identity is evidence identity: dedup on the content hash
    # (accession/trigger/normalized excerpt), never on (type, filed).
    bucket = _obligations_dedup_bucket(unquantified)
    capital = _obligations_dedup_bucket(capital_raw)
    _obligations_stamp_bucket(bucket, capital, ticker, known_at)
    return rows, bucket, capital


def _obligations_finalize(ticker: str, known_at: str, manifest: list[dict[str, object]], rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]], persist: bool) -> dict[str, object]:
    """Coverage-checked picture assembly plus cache/persist/publish."""
    snapshot, snap_warnings = _current_snapshot(rows)
    stashed = _obligations_stash_warnings(rows, bucket, capital)
    if not rows and not bucket and not capital:
        return _no_data(ticker, "no quantified obligations found in filings")
    value: dict[str, object] = {
        "ticker": ticker,
        "as_of": known_at,
        "source": _PICTURE_SOURCE,
        "obligations": rows,
        "current_snapshot": snapshot,
        "unquantified_exposures": bucket,
        "capital_allocation": capital,
        "coverage": _obligations_coverage(manifest, rows, bucket, snap_warnings, stashed),
        "filings_examined": _obligations_filings_examined(manifest, rows),
        "sections_examined": _obligations_sections_examined(manifest, rows),
        "note": _PICTURE_NOTE,
    }
    cache.set(f"obligations:{ticker}", value)
    if persist:
        _obligations_persist_summary(ticker, rows, bucket, capital)
    _publish_lifecycle(rows, bucket, capital)
    return value


def get_obligations(ticker: str, *, persist: bool = False) -> dict[str, object]:
    """Return the full obligations picture for ANY ticker (cached 24h)."""
    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    cached = _obligations_cached(ticker, persist)
    if cached is not None:
        return cached
    manifest: list[dict[str, object]] = []
    fetched = _obligations_fetch(ticker, persist, manifest)
    if fetched is None:
        return {"error": f"Obligations unavailable for {ticker}: unknown fetch failure"}
    fetched_rows, unquantified, capital_raw = fetched
    known_at = _known_at()
    rows, bucket, capital = _obligations_stamp_all(fetched_rows, unquantified, capital_raw, ticker, known_at)
    return _obligations_finalize(ticker, known_at, manifest, rows, bucket, capital, persist)


class _PersistBuild:
    """Mutable persist sink: event/capital/evidence rows plus skip counts."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.event_rows: list[dict[str, object]] = []
        self.capital_rows: list[dict[str, object]] = []
        self.evidence_rows: list[dict[str, object]] = []
        self.skipped = 0
        self.skipped_proxied = 0


def _persist_normalize_exposure(exp: Mapping[str, object]) -> dict[str, object]:
    """Exposure as an amount-None contingent event row (default flag kept)."""
    filed = str(exp.get("filed") or "").strip() or None
    norm: dict[str, object] = {
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
    return norm


def _persist_work_lists(rows: Sequence[Mapping[str, object]], unquantified: Sequence[Mapping[str, object]] | None, capital: Sequence[Mapping[str, object]] | None) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    """Event work list plus hashed capital work list for one persist call."""
    work: list[Mapping[str, object]] = list(rows or [])
    for exp in unquantified or []:
        work.append(_persist_normalize_exposure(exp))
    capital_work: list[Mapping[str, object]] = []
    for entry in capital or []:
        if not entry.get("content_hash"):
            entry = {**entry, "content_hash": _content_hash(entry)}
        capital_work.append(entry)
    return work, capital_work


def _persist_timing_jsons(row: Mapping[str, object]) -> tuple[str | None, str | None]:
    """Schedule/timing JSON pair for one row (horizon fallback when scheduled)."""
    horizon_raw = row.get("payment_horizon")
    horizon: Mapping[str, object] = horizon_raw if isinstance(horizon_raw, Mapping) else {}
    sched_raw = row.get("schedule") or horizon.get("schedule")
    sched: list[object] = sched_raw if isinstance(sched_raw, list) else []
    schedule_json = json.dumps(sched) if sched else None
    if sched:
        return schedule_json, schedule_json
    if any(horizon.get(k) is not None for k in ("paid_in_remainder_of_fy", "paid_in_remainder_billions", "paid_after_remainder_billions")):
        return schedule_json, json.dumps({
            "paid_in_remainder_of_fy": horizon.get("paid_in_remainder_of_fy"),
            "paid_in_remainder_billions": horizon.get("paid_in_remainder_billions"),
            "paid_after_remainder_billions": horizon.get("paid_after_remainder_billions"),
        })
    return schedule_json, None


def _persist_event_parts(row: Mapping[str, object], filed: str) -> tuple[str, str, str]:
    """Event id plus ticker plus content hash for one persist row."""
    ticker = str(row.get("ticker") or "").strip().upper()
    content_hash = str(row.get("content_hash") or "")
    return sec_event_id(ticker, content_hash), ticker, content_hash


def _persist_entity_id(row: Mapping[str, object], ticker: str, filed: str, data_root: Path) -> str | None:
    """Entity id from the accession CIK, else resolved for the filed date."""
    from datetime import date

    from .domain.market.ids import sec_entity_id
    from .services.sec_facts import _resolve_entity

    accession = str(row.get("_accession") or "")
    m = re.match(r"^(\d{10})-", accession)
    if m:
        return sec_entity_id(m.group(1))
    if not ticker:
        return None
    return _resolve_entity(ticker, date.fromisoformat(filed[:10]), data_root)


def _persist_build_event(row: Mapping[str, object], filed: str, target: list[dict[str, object]], sink: _PersistBuild) -> None:
    """Append one CorporateEvent dict for a filed, non-proxied row."""
    from dataclasses import asdict

    from .domain.events import CorporateEvent

    event_id, ticker, content_hash = _persist_event_parts(row, filed)
    schedule_json, payment_timing_json = _persist_timing_jsons(row)
    row_amount = row.get("amount_billions")
    event = CorporateEvent(
        event_id=event_id,
        entity_id=_persist_entity_id(row, ticker, filed, sink.data_root),
        security_id=None,
        ticker=ticker,
        event_type=str(row.get("type") or "other"),
        amount_billions=float(row_amount) if isinstance(row_amount, (int, float)) else None,
        certainty=_opt_str(row.get("certainty")),
        status=_opt_str(row.get("status")),
        revenue_matched=bool(row.get("revenue_matched")),
        default_triggered=bool(row.get("default_triggered")),
        fiscal_year=str(row.get("fiscal_year")) if row.get("fiscal_year") is not None else None,
        schedule_json=schedule_json,
        payment_timing_json=payment_timing_json,
        filed_at=filed,
        known_at=filed,
        retrieved_at=str(row.get("known_at") or ""),
        accession=str(row.get("_accession") or "") or None,
        source=_opt_str(row.get("source")),
        source_url=None,
        content_hash=content_hash,
        parser_version=_opt_str(row.get("parser_version")),
        agreement_key=_opt_str(row.get("agreement_key")),
        lifecycle_event=_opt_str(row.get("_lifecycle_event", row.get("lifecycle_event"))),
        schedule_component=_opt_bool(row.get("schedule_component")),
        headline_type=_opt_str(row.get("headline_type")),
    )
    event_dict: dict[str, object] = {}
    event_dict.update(asdict(event))
    target.append(event_dict)


def _persist_evidence_span(row: Mapping[str, object], archive_key: str, sink: _PersistBuild) -> tuple[str | None, int | None, int | None]:
    """Archived SHA plus excerpt span for one filing-text evidence row."""
    from .storage import raw_archive

    record = raw_archive.find("sec", _ARCHIVE_KIND, archive_key, root=sink.data_root / "raw")
    if record is None:
        return None, None, None
    text = record.payload_path.read_text(encoding="utf-8", errors="replace")
    excerpt = str(row.get("excerpt") or "")
    if not excerpt:
        return record.sha256, None, None
    start = text.find(excerpt)
    if start < 0:
        return record.sha256, None, None
    return record.sha256, start, start + len(excerpt)


def _persist_build_evidence(row: Mapping[str, object], event_id: str, content_hash: str, sink: _PersistBuild) -> None:
    """Append one Evidence dict (filing-text archive span or XBRL fact)."""
    from dataclasses import asdict

    from .domain.events import Evidence, sec_evidence_id

    is_xbrl_fact = "concept" in row
    archive_key = str(row.get("_archive_key") or "") or None
    archived_sha: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    if archive_key is not None and not is_xbrl_fact:
        archived_sha, span_start, span_end = _persist_evidence_span(row, archive_key, sink)
    evidence = Evidence(
        evidence_id=sec_evidence_id(event_id, content_hash),
        event_id=event_id,
        source_type="xbrl_fact" if is_xbrl_fact else "filing_text",
        archive_key=archive_key if not is_xbrl_fact else None,
        content_hash=archived_sha,
        excerpt=_opt_str(row.get("excerpt")),
        span_start=span_start,
        span_end=span_end,
        retrieved_at=str(row.get("known_at") or ""),
        parser_version=_opt_str(row.get("parser_version")),
    )
    evidence_dict: dict[str, object] = {}
    evidence_dict.update(asdict(evidence))
    sink.evidence_rows.append(evidence_dict)


def _persist_build_row(row: Mapping[str, object], sink: _PersistBuild, target: list[dict[str, object]]) -> str | None:
    """Build event into target plus evidence into the sink (None if skipped)."""
    if row.get("provenance") == "proxied":
        sink.skipped_proxied += 1
        return None
    filed = str(row.get("filed") or "").strip() or None
    if not filed:
        sink.skipped += 1
        return None
    event_id, _ticker, content_hash = _persist_event_parts(row, filed)
    _persist_build_event(row, filed, target, sink)
    _persist_build_evidence(row, event_id, content_hash, sink)
    return event_id

def _persist_write_sink(sink: _PersistBuild) -> dict[str, object]:
    """Flush the sink to parquet tables with skip counts."""
    from .storage import parquet

    return {
        "events_written": parquet.write_rows("events", sink.event_rows, root=sink.data_root / "parquet"),
        "capital_events_written": parquet.write_rows("capital_events", sink.capital_rows, root=sink.data_root / "parquet"),
        "evidence_written": parquet.write_rows("evidence", sink.evidence_rows, root=sink.data_root / "parquet"),
        "skipped_no_filing_date": sink.skipped,
        "skipped_proxied": sink.skipped_proxied,
    }


def persist_obligation_events(rows: Sequence[Mapping[str, object]], data_root: str | Path | None = None, *, unquantified: Sequence[Mapping[str, object]] | None = None, capital: Sequence[Mapping[str, object]] | None = None) -> dict[str, object]:
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
    Reconciled fiscal-year components persist with their flags and are excluded from snapshots at read time.
    """
    from .storage import duckdb

    root = Path(data_root) if data_root is not None else Path(duckdb.DEFAULT_DATA_ROOT)
    sink = _PersistBuild(root)
    work, capital_work = _persist_work_lists(rows, unquantified, capital)
    for row in work:
        _persist_build_row(row, sink, sink.event_rows)
    for row in capital_work:
        _persist_build_row(row, sink, sink.capital_rows)
    return _persist_write_sink(sink)


def _asof_read_tables(data_root: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Stored events/capital/evidence tables (capital empty when absent)."""
    from .storage import parquet

    parquet_root = data_root / "parquet"
    stored: list[dict[str, object]] = parquet.read_table("events", root=parquet_root).to_pylist()
    try:
        stored_capital: list[dict[str, object]] = parquet.read_table("capital_events", root=parquet_root).to_pylist()
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        stored_capital = []
    stored_evidence: list[dict[str, object]] = parquet.read_table("evidence", root=parquet_root).to_pylist()
    return stored, stored_capital, stored_evidence


def _asof_evidence_index(stored_evidence: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """First evidence row per event id (replay joins on either id form)."""
    evidence_by_event: dict[str, dict[str, object]] = {}
    for ev in stored_evidence:
        eid = str(ev.get("event_id") or "")
        if eid and eid not in evidence_by_event:
            evidence_by_event[eid] = ev
    return evidence_by_event


def _asof_keep_ticker(events: list[dict[str, object]], ticker: str, as_of: str) -> list[dict[str, object]]:
    """Events for one ticker filed on or before the as-of date."""
    kept: list[dict[str, object]] = []
    for e in events:
        if str(e.get("ticker") or "").strip().upper() != ticker:
            continue
        if str(e.get("filed_at") or "")[:10] > as_of:
            continue
        kept.append(e)
    return kept


def _rebuild_schedule(event: Mapping[str, object]) -> object:
    """Schedule payload from stored JSON (None when absent/unparseable)."""
    schedule_raw = event.get("schedule_json")
    try:
        return json.loads(schedule_raw) if isinstance(schedule_raw, str) else None
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None


def _rebuild_horizon(event: Mapping[str, object]) -> object:
    """Payment-horizon payload from stored JSON (None when absent/unparseable)."""
    timing_raw = event.get("payment_timing_json")
    if not timing_raw:
        return None
    try:
        parsed = json.loads(timing_raw) if isinstance(timing_raw, str) else None
    except Exception:  # noqa: BLE001 - intentional best-effort boundary, never aborts
        return None
    if isinstance(parsed, list):
        return {"schedule": parsed}
    if isinstance(parsed, dict):
        return parsed
    return None


def _rebuild_evidence(event: Mapping[str, object], ticker: str, evidence_by_event: dict[str, dict[str, object]]) -> dict[str, object] | None:
    """Evidence row for one event (either event-id form, None when absent)."""
    content_hash = str(event.get("content_hash") or "")
    eid = str(event.get("event_id") or "") or sec_event_id(ticker, content_hash)
    return evidence_by_event.get(eid) or evidence_by_event.get(sec_event_id(ticker, content_hash))


def _rebuild_event_row(event: Mapping[str, object], ticker: str, evidence_by_event: dict[str, dict[str, object]]) -> dict[str, object]:
    """One ledger row rebuilt from a stored event plus its evidence excerpt."""
    content_hash = str(event.get("content_hash") or "")
    ev = _rebuild_evidence(event, ticker, evidence_by_event)
    row: dict[str, object] = {
        "type": event.get("event_type"),
        "amount_billions": event.get("amount_billions"),
        "filed": event.get("filed_at"),
        "known_at": event.get("known_at"),
        "certainty": event.get("certainty"),
        "status": event.get("status"),
        "revenue_matched": event.get("revenue_matched"),
        "default_triggered": event.get("default_triggered"),
        "fiscal_year": event.get("fiscal_year"),
        "schedule": _rebuild_schedule(event),
        "payment_horizon": _rebuild_horizon(event),
        "agreement_key": event.get("agreement_key"),
        "_lifecycle_event": event.get("lifecycle_event"),
        "schedule_component": event.get("schedule_component"),
        "headline_type": event.get("headline_type"),
        "source": event.get("source"),
        "accession": event.get("accession"),
        "_accession": event.get("accession"),
        "excerpt": (ev or {}).get("excerpt"),
        "ticker": ticker,
        "content_hash": content_hash,
        "parser_version": event.get("parser_version"),
    }
    if (ev or {}).get("source_type") == "xbrl_fact":
        row["concept"] = True
    return row


def _asof_is_unquantified(row: dict[str, object]) -> bool:
    """True when a rebuilt row belongs in the bucket (amount-less, unmarked)."""
    return row.get("amount_billions") is None and row.get("_lifecycle_event") not in ("amendment", "termination")


def _asof_split_rows(kept: list[dict[str, object]], ticker: str, evidence_by_event: dict[str, dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Rebuilt rows split into quantified rows plus unquantified bucket."""
    rows: list[dict[str, object]] = []
    bucket: list[dict[str, object]] = []
    for e in kept:
        row = _rebuild_event_row(e, ticker, evidence_by_event)
        row["trigger"] = _row_trigger(row)
        if _asof_is_unquantified(row):
            bucket.append(row)
        else:
            rows.append(row)
    return rows, bucket


def _asof_rebuild_capital(kept_capital: list[dict[str, object]], ticker: str, evidence_by_event: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    """Rebuilt capital rows (board-discretion trigger, never obligations)."""
    capital: list[dict[str, object]] = []
    for e in kept_capital:
        row = _rebuild_event_row(e, ticker, evidence_by_event)
        row["trigger"] = "board_discretion"
        capital.append(row)
    return capital


def _asof_event_ids(kept: list[dict[str, object]], kept_capital: list[dict[str, object]], ticker: str) -> set[str]:
    """Known event ids in both stored and recomputed id forms."""
    event_ids = {str(e.get("event_id") or "") for e in kept + kept_capital}
    event_ids |= {sec_event_id(ticker, str(e.get("content_hash") or "")) for e in kept + kept_capital}
    return event_ids


def _asof_provenance(rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    """Filings/sections examined from rebuilt rows (sorted, present only)."""
    filings = sorted({str(r.get("filed")) for r in rows + bucket + capital if r.get("filed")})
    sections = sorted({str(r.get("source")) for r in rows + bucket + capital if r.get("source")})
    return filings, sections


def _asof_assemble(ticker: str, as_of: str, rows: list[dict[str, object]], bucket: list[dict[str, object]], capital: list[dict[str, object]], snapshot: list[dict[str, object]], snap_warnings: list[str]) -> dict[str, object]:
    """Assembled replay picture from rebuilt rows plus snapshot/coverage."""
    filings, sections = _asof_provenance(rows, bucket, capital)
    return {
        "ticker": ticker,
        "as_of": as_of,
        "source": f"{_PICTURE_SOURCE} (replayed from stored events as of {as_of})",
        "obligations": rows,
        "current_snapshot": snapshot,
        "unquantified_exposures": bucket,
        "capital_allocation": capital,
        "coverage": {
            "scan_manifest": [],
            "quantified_count": len(rows),
            "unquantified_count": len(bucket),
            "warnings": list(snap_warnings),
        },
        "filings_examined": filings,
        "sections_examined": sections,
        "note": _PICTURE_NOTE,
    }


def get_obligations_as_of(ticker: str, as_of: str, data_root: str | Path | None = None) -> dict[str, object]:
    """Replay the full obligations picture from stored events as of a date."""
    from .storage import duckdb

    ticker = ticker.strip().upper()
    if not ticker:
        return _no_data("", "empty ticker")
    data_root = Path(data_root) if data_root is not None else Path(duckdb.DEFAULT_DATA_ROOT)
    stored, stored_capital, stored_evidence = _asof_read_tables(data_root)
    evidence_by_event = _asof_evidence_index(stored_evidence)
    kept = _asof_keep_ticker(stored, ticker, as_of)
    kept_capital = _asof_keep_ticker(stored_capital, ticker, as_of)
    event_ids = _asof_event_ids(kept, kept_capital, ticker)
    rows, bucket = _asof_split_rows(kept, ticker, evidence_by_event)
    capital = _asof_rebuild_capital(kept_capital, ticker, evidence_by_event)
    # Ignore evidence without a matching event (never joined above).
    _ = {ev.get("event_id") for ev in stored_evidence if str(ev.get("event_id") or "") not in event_ids}
    if not rows and not bucket and not capital:
        return _no_data(ticker, f"no stored obligation events as of {as_of}")
    _apply_legacy_component_flags(rows)
    snapshot, snap_warnings = _current_snapshot(rows)
    _publish_lifecycle(rows, bucket, capital)
    return _asof_assemble(ticker, as_of, rows, bucket, capital, snapshot, snap_warnings)


__all__ = ["DEFAULT_TRIGGERED_TYPES", "REVENUE_MATCHED_KINDS", "get_obligations", "get_obligations_as_of", "persist_obligation_events"]